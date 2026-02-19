"""Per-agent tool proxy — Unix socket server with session token validation.

Each agent gets its own Unix domain socket at:
    <socket_dir>/<agent_id>.sock

The proxy:
1. Accepts connections from the agent process (running inside the sandbox).
2. Validates the cryptographic session token on every request.
3. Checks the tool name against the agent's frontmatter allowlist.
4. Validates parameter scope (issue_number must match agent's assigned issue).
5. Runs OutputInspector to detect potential exfiltration.
6. Enforces per-session rate limiting.
7. Adds timing normalisation for sensitive operations.
8. Forwards valid requests to the AuthBroker.
9. Returns the API response (data only — no credentials).
10. Appends every call to the SandboxAuditLogger.

Wire protocol: newline-delimited JSON over the Unix socket.

Request:
    {"token": "<hex>", "tool": "<name>", "params": {...}}

Response:
    {"ok": true, "data": {...}}
    {"ok": false, "error": "<message>"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from squadron.sandbox.audit import SandboxAuditLogger
    from squadron.sandbox.broker import AuthBroker, BrokerRequest
    from squadron.sandbox.config import SandboxConfig
    from squadron.sandbox.inspector import OutputInspector

logger = logging.getLogger(__name__)


class ToolProxy:
    """Per-agent Unix socket proxy.

    Lifecycle:
        proxy = ToolProxy(agent_id, session_token, allowed_tools, ...)
        await proxy.start()   # binds socket, starts accept loop
        ...
        await proxy.stop()    # closes socket, cancels accept loop
    """

    def __init__(
        self,
        agent_id: str,
        issue_number: int,
        session_token: bytes,
        allowed_tools: list[str],
        broker: AuthBroker,
        audit: SandboxAuditLogger,
        output_inspector: OutputInspector,
        config: SandboxConfig,
        owner: str,
        repo: str,
    ) -> None:
        self._agent_id = agent_id
        self._issue_number = issue_number
        self._session_token = session_token
        self._session_token_hex = session_token.hex()
        self._allowed_tools: frozenset[str] = frozenset(allowed_tools)
        self._broker = broker
        self._audit = audit
        self._output_inspector = output_inspector
        self._config = config
        self._owner = owner
        self._repo = repo

        # Socket path
        socket_dir = Path(config.socket_dir)
        socket_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._socket_path = socket_dir / f"{agent_id}.sock"

        # Rate limiting
        self._call_count = 0
        self._max_calls = config.max_tool_calls_per_session
        self._timing_floor = config.timing_floor_ms / 1000.0  # convert to seconds

        self._server: asyncio.AbstractServer | None = None
        self._task: asyncio.Task | None = None

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    async def start(self) -> None:
        """Bind the Unix socket and start the accept loop."""
        # Remove stale socket if exists
        if self._socket_path.exists():
            self._socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._socket_path),
        )
        # Restrict socket permissions: owner-only
        os.chmod(str(self._socket_path), 0o600)

        self._task = asyncio.create_task(
            self._server.serve_forever(),
            name=f"proxy-{self._agent_id}",
        )
        logger.info("ToolProxy started for %s at %s", self._agent_id, self._socket_path)

    async def stop(self) -> None:
        """Shut down the proxy and clean up the socket file."""
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._socket_path.exists():
            self._socket_path.unlink(missing_ok=True)
        logger.info("ToolProxy stopped for %s", self._agent_id)

    # ── Connection Handler ────────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one client connection — reads one JSON request, writes one JSON response."""
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not line:
                return
            request = json.loads(line.decode())
            response = await self._process_request(request)
        except asyncio.TimeoutError:
            response = {"ok": False, "error": "request timeout"}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            response = {"ok": False, "error": f"malformed request: {exc}"}
        except Exception as exc:
            logger.exception("ToolProxy: unexpected error for %s", self._agent_id)
            response = {"ok": False, "error": f"proxy error: {exc}"}
        finally:
            try:
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _process_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Validate and forward one tool call request."""
        call_start = time.monotonic()

        tool = request.get("tool", "")
        params = request.get("params", {})
        token_hex = request.get("token", "")

        # 1. Validate session token (constant-time comparison)
        if not _constant_time_eq(token_hex, self._session_token_hex):
            logger.warning("ToolProxy: invalid session token for %s", self._agent_id)
            await self._audit_blocked(tool, params, "invalid session token")
            return {"ok": False, "error": "tool-not-permitted: invalid session token"}

        # 2. Validate tool allowlist
        if tool not in self._allowed_tools:
            logger.warning(
                "ToolProxy: tool '%s' not in allowlist for %s", tool, self._agent_id
            )
            await self._audit_blocked(tool, params, f"tool '{tool}' not in allowlist")
            return {"ok": False, "error": f"tool-not-permitted: '{tool}' not in agent allowlist"}

        # 3. Validate parameter scope (prevent cross-issue writes)
        scope_ok, scope_err = self._validate_scope(tool, params)
        if not scope_ok:
            logger.warning(
                "ToolProxy: scope violation for %s/%s: %s", self._agent_id, tool, scope_err
            )
            await self._audit_blocked(tool, params, f"scope violation: {scope_err}")
            return {"ok": False, "error": f"tool-not-permitted: {scope_err}"}

        # 4. Rate limiting
        self._call_count += 1
        if self._call_count > self._max_calls:
            logger.warning(
                "ToolProxy: rate limit exceeded for %s (limit=%d)", self._agent_id, self._max_calls
            )
            await self._audit_blocked(tool, params, "rate limit exceeded")
            return {"ok": False, "error": "tool-not-permitted: rate limit exceeded"}

        # 5. Output content inspection
        inspection = self._output_inspector.inspect(tool, params)
        if not inspection.passed:
            logger.warning(
                "ToolProxy: output inspection blocked for %s/%s: %s",
                self._agent_id,
                tool,
                inspection.reason,
            )
            await self._audit_blocked(tool, params, f"output inspection: {inspection.reason}")
            return {"ok": False, "error": f"tool-not-permitted: {inspection.reason}"}

        # 6. Add routing metadata for broker
        enriched_params = dict(params)
        enriched_params["_owner"] = self._owner
        enriched_params["_repo"] = self._repo

        # 7. Forward to auth broker
        from squadron.sandbox.broker import BrokerRequest

        response_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        broker_req = BrokerRequest(
            agent_id=self._agent_id,
            session_token=self._session_token,
            tool=tool,
            params=enriched_params,
            response_queue=response_q,
        )
        broker_resp = await self._broker.submit(broker_req)

        status = "ok" if broker_resp.ok else "error"
        await self._audit.log_tool_call(
            agent_id=self._agent_id,
            session_token=self._session_token,
            tool=tool,
            params=params,
            response=broker_resp.data if broker_resp.ok else broker_resp.error,
            status=status,
        )

        # 8. Timing normalisation
        elapsed = time.monotonic() - call_start
        if elapsed < self._timing_floor:
            await asyncio.sleep(self._timing_floor - elapsed)

        if broker_resp.ok:
            return {"ok": True, "data": broker_resp.data}
        return {"ok": False, "error": broker_resp.error}

    def _validate_scope(self, tool: str, params: dict[str, Any]) -> tuple[bool, str]:
        """Ensure write tool parameters are scoped to this agent's issue.

        Prevents cross-issue writes: an agent assigned to issue #42 must
        not be able to comment on issue #99.
        """
        # Tools that carry an issue_number we must validate
        write_tools_with_issue = {
            "comment_on_issue",
            "create_issue",
            "label_issue",
            "assign_issue",
            "update_issue",
            "close_issue",
        }
        if tool in write_tools_with_issue:
            issue_number = params.get("issue_number")
            if issue_number is not None and int(issue_number) != self._issue_number:
                return (
                    False,
                    f"issue_number {issue_number} does not match agent's assigned "
                    f"issue #{self._issue_number}",
                )

        return True, ""

    async def _audit_blocked(self, tool: str, params: dict, reason: str) -> None:
        await self._audit.log_tool_call(
            agent_id=self._agent_id,
            session_token=self._session_token,
            tool=tool,
            params=params,
            response={"blocked_reason": reason},
            status="blocked",
        )


def _constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())
