"""Diff and output content inspection for supply chain and exfiltration protection.

DiffInspector:  Reviews git diffs before push for changes to sensitive
                infrastructure files (Makefile, CI configs, git hooks, etc.)

OutputInspector: Scans outbound tool call parameters for known-sensitive
                 patterns (tokens, secrets, internal paths, etc.)
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from squadron.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)

# ── Output inspection patterns ────────────────────────────────────────────────

# Built-in patterns that are always inspected regardless of config.
# These cover common secret / credential shapes.
_BUILTIN_SENSITIVE_PATTERNS: list[str] = [
    # GitHub / OAuth tokens
    r"gh[pousr]_[A-Za-z0-9_]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    # Generic bearer tokens
    r"Bearer\s+[A-Za-z0-9\-_\.]{20,}",
    # Generic API key shapes
    r"api[_\-]?key[_\-]?[=:]\s*[A-Za-z0-9\-_\.]{16,}",
    # AWS credentials
    r"AKIA[0-9A-Z]{16}",
    r"aws[_\-]?secret[_\-]?access[_\-]?key[_\-]?[=:]\s*[A-Za-z0-9+/]{32,}",
    # Private keys (PEM header)
    r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
    # .netrc with credentials
    r"machine\s+\S+\s+login\s+\S+\s+password\s+\S+",
    # Internal paths (host filesystem paths that should not leak)
    r"/proc/[0-9]+/environ",
    r"/etc/shadow",
    r"/root/\.ssh",
]

_COMPILED_BUILTIN: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in _BUILTIN_SENSITIVE_PATTERNS
]


@dataclass
class InspectionResult:
    """Result from an inspection operation."""

    passed: bool
    reason: str = ""
    flagged_patterns: list[str] = field(default_factory=list)
    flagged_paths: list[str] = field(default_factory=list)


class OutputInspector:
    """Scans outbound tool call parameters for sensitive data.

    Called by the ToolProxy before forwarding any call to the AuthBroker.
    If a match is found the call is blocked and logged.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self._extra: list[re.Pattern] = [
            re.compile(p, re.IGNORECASE) for p in config.extra_sensitive_patterns
        ]
        self._enabled = config.output_inspection_enabled

    def inspect(self, tool: str, params: dict) -> InspectionResult:
        """Check all string values in ``params`` for sensitive patterns.

        Returns:
            InspectionResult with passed=True if safe.
        """
        if not self._enabled:
            return InspectionResult(passed=True, reason="inspection disabled")

        all_patterns = _COMPILED_BUILTIN + self._extra
        flagged: list[str] = []

        for key, value in self._flatten(params):
            if not isinstance(value, str):
                continue
            for pattern in all_patterns:
                if pattern.search(value):
                    flagged.append(f"{key}: matched /{pattern.pattern}/")
                    logger.warning(
                        "Output inspection: sensitive pattern in tool=%s param=%s", tool, key
                    )

        if flagged:
            return InspectionResult(
                passed=False,
                reason=f"sensitive data detected in {len(flagged)} parameter(s)",
                flagged_patterns=flagged,
            )
        return InspectionResult(passed=True)

    @staticmethod
    def _flatten(obj: object, prefix: str = "") -> list[tuple[str, object]]:
        """Recursively yield (dotted.path, value) pairs from a dict/list."""
        items: list[tuple[str, object]] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                full_key = f"{prefix}.{k}" if prefix else k
                items.extend(OutputInspector._flatten(v, full_key))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                items.extend(OutputInspector._flatten(v, f"{prefix}[{i}]"))
        else:
            items.append((prefix, obj))
        return items


class DiffInspector:
    """Reviews git diffs before push for supply-chain risks.

    Checks for:
    1. Changes to sensitive infrastructure paths (configurable glob list).
    2. Git hooks in the diff (always blocked — hooks can execute arbitrary code
       on the host when the repo is used).
    3. Large binary blobs (heuristic for encoded data exfiltration).
    """

    # Patterns in diff that indicate git hook modifications.
    _GIT_HOOK_PATTERN = re.compile(r"^\+\+\+ b/\.git/hooks/", re.MULTILINE)
    # Heuristic: lines longer than 10k chars in a diff suggest base64/binary blobs.
    _LONG_LINE_THRESHOLD = 10_000

    def __init__(self, config: SandboxConfig) -> None:
        self._sensitive_globs: list[str] = list(config.sensitive_paths)
        self._block: bool = config.block_sensitive_path_changes
        self._enabled: bool = config.diff_inspection_enabled

    def inspect_diff(self, diff_text: str) -> InspectionResult:
        """Analyse a unified diff string.

        Args:
            diff_text: Output of ``git diff`` or ``git show``.

        Returns:
            InspectionResult.  If passed=False, push should be blocked.
        """
        if not self._enabled:
            return InspectionResult(passed=True, reason="diff inspection disabled")

        flagged_paths: list[str] = []
        reasons: list[str] = []

        # Check for git hooks (always block regardless of config)
        if self._GIT_HOOK_PATTERN.search(diff_text):
            return InspectionResult(
                passed=False,
                reason="git hook modification detected in diff — always blocked",
            )

        # Extract changed file paths from diff headers
        changed_files = self._extract_changed_files(diff_text)
        for path in changed_files:
            for glob in self._sensitive_globs:
                if fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(path.lstrip("/"), glob):
                    flagged_paths.append(path)
                    logger.warning("Diff inspection: sensitive path modified: %s", path)
                    break

        # Heuristic: look for suspiciously long lines (possible binary exfiltration)
        for line in diff_text.splitlines():
            if line.startswith("+") and len(line) > self._LONG_LINE_THRESHOLD:
                reasons.append(f"suspiciously long added line ({len(line)} chars)")
                logger.warning(
                    "Diff inspection: possible binary blob in diff (line len=%d)", len(line)
                )
                break

        if flagged_paths:
            msg = f"sensitive path(s) modified: {', '.join(flagged_paths)}"
            if self._block:
                return InspectionResult(
                    passed=False,
                    reason=msg,
                    flagged_paths=flagged_paths,
                )
            else:
                logger.warning("Diff inspection warning (not blocking): %s", msg)

        if reasons:
            return InspectionResult(
                passed=False,
                reason="; ".join(reasons),
            )

        return InspectionResult(passed=True, flagged_paths=flagged_paths)

    @staticmethod
    def _extract_changed_files(diff_text: str) -> list[str]:
        """Extract file paths from diff +++ headers."""
        paths: list[str] = []
        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                paths.append(line[6:])
            elif line.startswith("diff --git "):
                # "diff --git a/foo b/foo" — extract b-side path
                parts = line.split(" b/", 1)
                if len(parts) == 2:
                    paths.append(parts[1])
        return list(dict.fromkeys(paths))  # deduplicate, preserve order
