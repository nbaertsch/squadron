"""Hash-chained append-only audit logger for sandbox operations.

Every log entry is SHA-256 hashed and chained to the previous entry,
making tampering detectable (any modification breaks the chain).

Log format (one JSON object per line, newline-delimited):
    {
        "seq": <int>,          // monotonic sequence number
        "ts": "<iso8601>",     // UTC timestamp
        "agent_id": "<str>",
        "session_token_hash": "<hex>",  // SHA-256 of session token (never raw)
        "tool": "<str>",
        "params_summary": "<str>",      // truncated / redacted params
        "response_summary": "<str>",    // truncated response
        "status": "ok" | "blocked" | "error",
        "prev_hash": "<hex>",  // SHA-256 of previous raw JSON line
        "hash": "<hex>"        // SHA-256 of this line (sans "hash" field itself)
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Maximum parameter length stored in audit log to prevent log bloat.
_PARAM_SUMMARY_MAX = 512
# Maximum response summary length.
_RESP_SUMMARY_MAX = 512


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def _truncate(value: object, max_len: int = _PARAM_SUMMARY_MAX) -> str:
    text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "…[truncated]"
    return text


class SandboxAuditLogger:
    """Append-only, hash-chained audit log for sandboxed tool calls.

    Thread/task safe: uses an asyncio.Lock to serialise writes.
    Each log file is opened in append mode so partial writes are not
    overwritten.  The log is rotated by date automatically.
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._lock = asyncio.Lock()
        self._seq = 0
        self._prev_hash = "0" * 64  # genesis sentinel

    def _log_file(self) -> Path:
        """Return the current log file path (one file per UTC day)."""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._log_dir / f"audit-{date_str}.ndjson"

    async def start(self) -> None:
        """Initialise the log directory and resume sequence from existing log."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_file = self._log_file()
        if log_file.exists():
            await self._resume_from(log_file)

    async def _resume_from(self, log_file: Path) -> None:
        """Read the last log line to resume sequence number and prev_hash."""
        try:
            last_line: str | None = None
            with open(log_file) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last_line = line
            if last_line:
                entry = json.loads(last_line)
                self._seq = entry.get("seq", 0)
                self._prev_hash = entry.get("hash", "0" * 64)
        except Exception:
            logger.warning("Could not resume audit log from %s — starting fresh", log_file)

    async def log_tool_call(
        self,
        agent_id: str,
        session_token: bytes,
        tool: str,
        params: dict,
        response: object,
        status: Literal["ok", "blocked", "error"],
    ) -> None:
        """Append one audit record for a tool call."""
        async with self._lock:
            self._seq += 1
            token_hash = _sha256_hex(session_token)
            params_summary = _truncate(params)
            response_summary = _truncate(response, _RESP_SUMMARY_MAX)

            # Build entry without hash field first (hash computed over this)
            entry: dict = {
                "seq": self._seq,
                "ts": _now_iso(),
                "agent_id": agent_id,
                "session_token_hash": token_hash,
                "tool": tool,
                "params_summary": params_summary,
                "response_summary": response_summary,
                "status": status,
                "prev_hash": self._prev_hash,
            }
            # Deterministic JSON for hashing
            raw = json.dumps(entry, sort_keys=True)
            entry_hash = _sha256_hex(raw)
            entry["hash"] = entry_hash

            line = json.dumps(entry, sort_keys=True)
            self._prev_hash = entry_hash

            try:
                log_file = self._log_file()
                with open(log_file, "a") as fh:
                    fh.write(line + "\n")
            except OSError:
                logger.error("Failed to write audit log entry for %s/%s", agent_id, tool)

    async def log_worktree_hash(
        self,
        agent_id: str,
        session_token: bytes,
        diff_hash: str,
    ) -> None:
        """Record the cryptographic hash of an agent's final diff before wipe."""
        await self.log_tool_call(
            agent_id=agent_id,
            session_token=session_token,
            tool="__worktree_diff_hash__",
            params={"diff_hash": diff_hash},
            response={"action": "worktree_wiped"},
            status="ok",
        )

    async def log_session_event(
        self,
        agent_id: str,
        session_token: bytes,
        event: str,
        details: dict | None = None,
    ) -> None:
        """Log a session lifecycle event (spawn, teardown, anomalous exit, etc.)."""
        await self.log_tool_call(
            agent_id=agent_id,
            session_token=session_token,
            tool=f"__session_{event}__",
            params=details or {},
            response={},
            status="ok",
        )

    def verify_chain(self, log_file: Path | None = None) -> tuple[bool, str]:
        """Verify the hash-chain integrity of a log file.

        Returns:
            (ok, message) where ok=True means the chain is intact.
        """
        if log_file is None:
            log_file = self._log_file()
        if not log_file.exists():
            return True, "no log file"

        prev_hash = "0" * 64
        prev_seq = 0
        try:
            with open(log_file) as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    stored_hash = entry.pop("hash", "")
                    raw = json.dumps(entry, sort_keys=True)
                    computed = _sha256_hex(raw)
                    if computed != stored_hash:
                        return (
                            False,
                            f"line {lineno}: hash mismatch (stored={stored_hash[:16]}…, "
                            f"computed={computed[:16]}…)",
                        )
                    if entry.get("prev_hash") != prev_hash:
                        return (
                            False,
                            f"line {lineno}: chain broken (expected prev_hash={prev_hash[:16]}…)",
                        )
                    if entry.get("seq", 0) != prev_seq + 1:
                        return (
                            False,
                            f"line {lineno}: sequence gap (expected {prev_seq + 1}, "
                            f"got {entry.get('seq')})",
                        )
                    prev_hash = stored_hash
                    prev_seq = entry.get("seq", prev_seq)
        except (json.JSONDecodeError, OSError) as exc:
            return False, f"read error: {exc}"

        return True, f"chain intact ({prev_seq} entries)"
