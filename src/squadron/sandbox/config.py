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
