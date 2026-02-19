"""Sandboxed agent execution module.

Implements the security architecture from issue #85:
- Linux namespace isolation (pid, net, mount, ipc, uts)
- seccomp-bpf syscall allowlist filter
- Ephemeral overlayfs/tmpfs worktrees
- Host-side auth broker (single async service)
- Per-agent tool proxy with Unix socket and session token
- Hash-chained audit logging
- Diff + output content inspection

All components are optional — sandbox is only active when
``sandbox.enabled = true`` in config.yaml.
"""

from .audit import SandboxAuditLogger
from .broker import AuthBroker
from .config import SandboxConfig
from .inspector import DiffInspector, OutputInspector
from .manager import SandboxManager
from .namespace import SandboxNamespace
from .proxy import ToolProxy
from .worktree import EphemeralWorktree

__all__ = [
    "AuthBroker",
    "DiffInspector",
    "EphemeralWorktree",
    "OutputInspector",
    "SandboxAuditLogger",
    "SandboxConfig",
    "SandboxManager",
    "SandboxNamespace",
    "ToolProxy",
]
