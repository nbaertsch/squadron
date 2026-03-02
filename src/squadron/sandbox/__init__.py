"""Sandboxed agent execution module.

Implements the security architecture from issue #85 + #146:
- Linux namespace isolation (pid, net, mount, ipc, uts)
- seccomp-bpf syscall allowlist filter
- Ephemeral overlayfs/tmpfs worktrees
- Host-side auth broker (single async service)
- Per-agent tool proxy with Unix socket and session token
- Hash-chained audit logging
- Diff + output content inspection
- Network namespace with veth bridge (Issue #146)
- MitM HTTPS inference proxy with credential injection (Issue #146)
- Ephemeral CA for TLS interception (Issue #146)
- Environment scrubbing — zero secrets in agent namespace (Issue #146)

All components are optional — sandbox is only active when
``sandbox.enabled = true`` in config.yaml.
"""

from .audit import SandboxAuditLogger
from .broker import AuthBroker
from .ca import SandboxCA
from .config import SandboxConfig
from .env_scrub import build_sanitized_env, get_dynamic_byok_vars
from .inference_proxy import InferenceProxy, build_credentials_from_env
from .inspector import DiffInspector, OutputInspector
from .manager import SandboxManager
from .namespace import SandboxNamespace
from .net_bridge import NetworkBridge, VethPair
from .proxy import ToolProxy
from .worktree import EphemeralWorktree

__all__ = [
    "AuthBroker",
    "DiffInspector",
    "EphemeralWorktree",
    "InferenceProxy",
    "NetworkBridge",
    "OutputInspector",
    "SandboxAuditLogger",
    "SandboxCA",
    "SandboxConfig",
    "SandboxManager",
    "SandboxNamespace",
    "ToolProxy",
    "VethPair",
    "build_credentials_from_env",
    "build_sanitized_env",
    "get_dynamic_byok_vars",
]
