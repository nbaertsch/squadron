"""Sandbox configuration model."""

from __future__ import annotations
from pydantic import BaseModel, Field


class SandboxConfig(BaseModel):
    """Configuration for sandboxed agent execution."""

    enabled: bool = False
    namespace_mount: bool = True
    namespace_pid: bool = True
    namespace_net: bool = True
    namespace_ipc: bool = True
    namespace_uts: bool = True
    seccomp_enabled: bool = True
    use_overlayfs: bool = True
    retention_path: str = "/mnt/squadron-data/forensics"
    retention_days: int = 1
    memory_limit_mb: int = 2048
    cpu_quota_percent: int = 200
    disk_limit_mb: int = 5120
    session_timeout: int = 7200
    socket_dir: str = "/tmp/squadron-sockets"
    session_token_bytes: int = 32
    max_tool_calls_per_session: int = 200
    timing_floor_ms: int = 50
    output_inspection_enabled: bool = True
    extra_sensitive_patterns: list[str] = Field(default_factory=list)
    diff_inspection_enabled: bool = True
    sensitive_paths: list[str] = Field(
        default_factory=lambda: [
            ".github/**",
            "Makefile",
            "*.sh",
            "pyproject.toml",
            "Dockerfile",
            "docker-compose*.yml",
            "infra/**",
            "deploy/**",
            ".pre-commit-config.yaml",
        ]
    )
    block_sensitive_path_changes: bool = True

    # ── Network bridge configuration (Issue #146) ────────────────────────────
    # Host bridge that connects all agent veth pairs.
    bridge_name: str = "sq-br0"
    bridge_subnet: str = "10.146.0.0/16"
    bridge_ip: str = "10.146.0.1"
    # Agent IPs are assigned as 10.146.<agent_index>.2; gateway = bridge_ip.

    # ── MitM HTTPS proxy (Issue #146) ────────────────────────────────────────
    # The host-side proxy listens on the bridge IP, intercepts HTTPS from
    # agent namespaces, injects API credentials, and forwards to upstream.
    proxy_port: int = 8443
    # Directory for ephemeral CA cert + key (generated at startup).
    ca_dir: str = "/tmp/squadron-ca"
    # CA certificate validity in days.
    ca_validity_days: int = 1

    # ── Environment scrubbing (Issue #146) ────────────────────────────────────
    # Static env vars that must NEVER enter the agent namespace.
    # Dynamic BYOK vars (from ProviderConfig.api_key_env) are added at runtime.
    secret_env_vars: list[str] = Field(
        default_factory=lambda: [
            "GITHUB_APP_ID",
            "GITHUB_PRIVATE_KEY",
            "GITHUB_INSTALLATION_ID",
            "GITHUB_WEBHOOK_SECRET",
            "COPILOT_GITHUB_TOKEN",
            "SQUADRON_DASHBOARD_API_KEY",
        ]
    )
