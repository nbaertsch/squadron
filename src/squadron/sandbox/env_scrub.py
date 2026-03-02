"""Environment scrubbing for sandbox agent processes (Issue #146).

Builds a sanitized environment dict for agent subprocesses by:
1. Stripping all known secret env vars (GitHub App creds, API keys, tokens).
2. Stripping any dynamic BYOK key env var from ProviderConfig.
3. Injecting sandbox-specific vars (CA cert path, proxy socket, etc.).

The scrubbed env is passed to CopilotClient / subprocess.Popen so that
secrets never enter the agent namespace.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from squadron.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)


def build_sanitized_env(
    config: SandboxConfig,
    *,
    ca_cert_path: Path | None = None,
    socket_path: Path | None = None,
    session_token_hex: str | None = None,
    extra_strip: list[str] | None = None,
) -> dict[str, str]:
    """Build a sanitized copy of os.environ for an agent subprocess.

    Args:
        config: SandboxConfig with ``secret_env_vars`` list.
        ca_cert_path: Path to the ephemeral CA cert (injected as
            SSL_CERT_FILE and NODE_EXTRA_CA_CERTS).
        socket_path: Path to the ToolProxy Unix socket.
        session_token_hex: Hex-encoded session token for the ToolProxy.
        extra_strip: Additional env var names to strip (e.g. dynamic BYOK keys).

    Returns:
        A new dict — never mutates ``os.environ``.
    """
    # Start from current env.
    env = dict(os.environ)

    # Build the full set of vars to strip.
    strip_set: set[str] = set(config.secret_env_vars)
    if extra_strip:
        strip_set.update(extra_strip)

    # Also strip any var whose name contains common secret patterns
    # (defense in depth — catches dynamically-named keys).
    _SECRET_PATTERNS = frozenset(
        {
            "API_KEY",
            "SECRET_KEY",
            "PRIVATE_KEY",
            "ACCESS_TOKEN",
            "AUTH_TOKEN",
        }
    )

    stripped: list[str] = []
    for key in list(env.keys()):
        if key in strip_set:
            del env[key]
            stripped.append(key)
        else:
            # Pattern-based stripping (defense in depth).
            key_upper = key.upper()
            for pattern in _SECRET_PATTERNS:
                if pattern in key_upper and key not in {
                    # Whitelist: keep these even though they match patterns.
                    "SSH_AUTH_SOCK",
                }:
                    del env[key]
                    stripped.append(key)
                    break

    if stripped:
        logger.info(
            "Env scrub: stripped %d secret vars: %s",
            len(stripped),
            ", ".join(sorted(stripped)),
        )

    # Inject sandbox-specific vars.
    if ca_cert_path and ca_cert_path.exists():
        cert_str = str(ca_cert_path)
        env["SSL_CERT_FILE"] = cert_str
        env["NODE_EXTRA_CA_CERTS"] = cert_str
        env["REQUESTS_CA_BUNDLE"] = cert_str

    if socket_path:
        env["SQUADRON_PROXY_SOCKET"] = str(socket_path)

    if session_token_hex:
        env["SQUADRON_SESSION_TOKEN"] = session_token_hex

    return env


def get_dynamic_byok_vars(provider_api_key_env: str) -> list[str]:
    """Return additional env var names to strip based on provider config.

    The BYOK key env var (e.g. ``ANTHROPIC_API_KEY``) is dynamic — it
    comes from ``ProviderConfig.api_key_env`` in the user's config.yaml.
    """
    extra: list[str] = []
    if provider_api_key_env:
        extra.append(provider_api_key_env)
    # Common BYOK env vars that users might set.
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        if var not in extra and os.environ.get(var):
            extra.append(var)
    return extra
