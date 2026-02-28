"""Copilot SDK integration — wraps the github-copilot-sdk package.

Each agent gets its own CopilotClient instance (AD-017, one CLI server per agent).
PM agents get fresh sessions per event batch. Dev/review agents get persistent
sessions with sleep/wake via resume_session().

The SDK communicates with the Copilot CLI binary via JSON-RPC. The CLI manages
conversation context, tool execution, and planning state internally — context
is opaque (we cannot manipulate messages[] directly).

Requires: github-copilot-sdk package and a running Copilot CLI binary.

SDK types used:
  - SessionConfig (TypedDict) for create_session()
  - ResumeSessionConfig (TypedDict) for resume_session()
  - CopilotSession with send_and_wait(), get_messages(), destroy()
  - SessionEvent with type (enum), data, id, timestamp
  - ProviderConfig (TypedDict) for BYOK — {type, base_url, api_key}
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from copilot import CopilotClient
from copilot import CopilotSession as SDKSession
from copilot.types import (
    ProviderConfig as SDKProviderConfig,
    ResumeSessionConfig as SDKResumeConfig,
    SessionConfig as SDKSessionConfig,
)

from squadron.config import RuntimeConfig
from squadron.dashboard_security import DASHBOARD_API_KEY_ENV

logger = logging.getLogger(__name__)

# ── Copilot CLI authentication ────────────────────────────────────────────
# Env var that holds the GitHub token used by the Copilot CLI to
# authenticate with the model API.  Resolved once at CopilotAgent
# construction and passed to CopilotClient as ``github_token`` — the SDK
# then injects it into the subprocess env under a different name
# (COPILOT_SDK_AUTH_TOKEN) via ``--auth-token-env``.  This means the
# original env var is *still* stripped from the subprocess (good for
# security) while the CLI has valid auth (fixes the agent-hangs-at-
# send_and_wait bug).
COPILOT_GITHUB_TOKEN_ENV = "COPILOT_GITHUB_TOKEN"

# ── Secret isolation ──────────────────────────────────────────────────────
# Environment variables that contain application secrets and MUST NOT be
# inherited by agent CLI subprocesses.  The agent's bash tool runs inside
# that subprocess, so any env var present there is trivially exfiltrable
# via `env`, `printenv`, or `/proc/self/environ`.
#
# All GitHub API interactions are handled framework-side (Squadron tools
# use the framework's GitHubClient; git push uses _git_auth_env which
# builds its own env).  BYOK API keys are resolved once by the framework
# and passed as values in the SDK SessionConfig — the subprocess does not
# need the env var.
#
# COPILOT_GITHUB_TOKEN is stripped here too — the CopilotAgent passes the
# token *value* to CopilotClient({"github_token": ...}) instead.  The SDK
# re-injects it as COPILOT_SDK_AUTH_TOKEN (inaccessible to bash tool
# under the original name).

_SECRET_ENV_VARS: frozenset[str] = frozenset(
    {
        # GitHub App credentials — used only by the framework's GitHubClient
        "GITHUB_APP_ID",
        "GITHUB_PRIVATE_KEY",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_INSTALLATION_ID",
        # Copilot / GitHub tokens — used by server startup or Azure deploy
        "COPILOT_GITHUB_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        # Dashboard API key
        DASHBOARD_API_KEY_ENV,
    }
)


def build_agent_env(extra_blocked: set[str] | None = None) -> dict[str, str]:
    """Build a sanitized copy of os.environ for agent CLI subprocesses.

    Strips all known secret env vars (and optionally additional ones, e.g.
    BYOK ``api_key_env`` names) so the agent's built-in bash tool cannot
    read application credentials.

    The returned dict retains everything the Copilot CLI needs to function:
    PATH, HOME, TMPDIR, locale settings, etc.

    .. note:: **Design decision — blocklist vs allowlist**

       This function uses a *blocklist* approach (strip known secrets, pass
       everything else) rather than an *allowlist* (pass only explicitly
       approved vars).  An allowlist would be more secure in theory, but the
       Copilot CLI and the tools it spawns (git, node, language servers, etc.)
       depend on a wide and unpredictable set of environment variables (PATH,
       HOME, TMPDIR, locale, SSH_AUTH_SOCK, custom tool config, etc.).
       Maintaining an allowlist that doesn't break legitimate tool usage
       across different OSes and CI environments is impractical.  The
       blocklist is a pragmatic trade-off: we strip the secrets we *know*
       about, and accept that novel secrets added to the framework must also
       be added here.

    Args:
        extra_blocked: Additional env var names to strip (e.g. dynamic BYOK
            key names like ``ANTHROPIC_API_KEY``).

    Returns:
        A dict suitable for passing as ``CopilotClient({"env": ...})``.
    """
    blocked = _SECRET_ENV_VARS | (extra_blocked or set())
    return {k: v for k, v in os.environ.items() if k not in blocked}


class CopilotAgent:
    """Manages a CopilotClient instance for a single agent.

    One CopilotAgent = one CLI subprocess (Pattern 1 from research).
    Handles session creation, resumption, and cleanup.

    The ``env`` parameter controls the environment inherited by the CLI
    subprocess.  Callers MUST pass a sanitized env (via
    :func:`build_agent_env`) to prevent the agent's built-in bash tool
    from reading application secrets.

    Authentication: The Copilot CLI needs a GitHub token to call the
    model API.  Rather than leaking the token into the subprocess env
    (where the bash tool could read it), we pass it as the SDK's
    ``github_token`` option.  The SDK injects it under a different env
    var name (``COPILOT_SDK_AUTH_TOKEN``) via ``--auth-token-env``.
    """

    def __init__(
        self,
        runtime_config: RuntimeConfig,
        working_directory: str,
        env: dict[str, str] | None = None,
    ):
        self.runtime_config = runtime_config
        self.working_directory = working_directory
        self._env = env  # sanitized env for CLI subprocess
        self._client: CopilotClient | None = None
        self._session: SDKSession | None = None

        # Resolve Copilot auth token from the *framework* environment
        # (before env stripping).  This is the token the CLI needs to
        # authenticate with the model API.
        self._github_token: str | None = os.environ.get(COPILOT_GITHUB_TOKEN_ENV)

    async def start(self) -> None:
        """Start the underlying CopilotClient (spawns CLI subprocess).

        Includes retry logic for transient ping verification timeouts
        during startup (Issue #38).
        """
        max_retries = 3
        retry_delay = 2.0  # seconds

        # Build client options — always include cwd; include env when
        # a sanitized env dict was provided (which it always should be).
        client_opts: dict[str, Any] = {"cwd": self.working_directory}
        if self._env is not None:
            client_opts["env"] = self._env

        # Pass Copilot GitHub token via the SDK's dedicated auth mechanism.
        # The SDK sets --auth-token-env COPILOT_SDK_AUTH_TOKEN on the CLI
        # and injects the token into env["COPILOT_SDK_AUTH_TOKEN"].  This
        # keeps the original COPILOT_GITHUB_TOKEN stripped (bash tool can't
        # read it) while the CLI can authenticate with the model API.
        if self._github_token:
            client_opts["github_token"] = self._github_token
        else:
            logger.warning(
                "No %s found in environment — CLI may fail to authenticate "
                "with the model API (send_and_wait will hang)",
                COPILOT_GITHUB_TOKEN_ENV,
            )

        for attempt in range(max_retries + 1):
            try:
                self._client = CopilotClient(client_opts)
                await self._client.start()
                logger.info("CopilotClient started (cwd=%s)", self.working_directory)
                return
            except asyncio.TimeoutError:
                if attempt < max_retries:
                    wait_time = retry_delay * (2**attempt)  # Exponential backoff
                    logger.warning(
                        "CopilotClient startup attempt %d failed with timeout, "
                        "retrying in %.1fs (resource contention)",
                        attempt + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    # Clean up failed client
                    if self._client:
                        try:
                            await self._client.stop()
                        except Exception:
                            pass
                        self._client = None
                else:
                    logger.error(
                        "CopilotClient startup failed after %d attempts, "
                        "likely due to resource contention or CLI server issues",
                        max_retries + 1,
                    )
                    raise

    async def stop(self) -> None:
        """Stop the client and clean up resources."""
        if self._session:
            try:
                await self._session.destroy()
            except Exception:
                logger.exception("Error destroying session")
            self._session = None
        if self._client:
            try:
                await self._client.stop()
            except Exception:
                logger.exception("Error stopping CopilotClient")
            self._client = None

    @property
    def client(self) -> CopilotClient:
        if self._client is None:
            raise RuntimeError("CopilotAgent not started — call start() first")
        return self._client

    def get_cli_stderr(self) -> str:
        """Return captured stderr output from the CLI subprocess.

        The SDK's JsonRpcClient accumulates stderr in a thread-safe list.
        This is useful for post-mortem diagnostics when an agent hangs or
        fails — the CLI's stderr often contains auth errors, model API
        failures, or internal panics that are otherwise invisible.

        Returns an empty string if no client exists or no stderr was captured.
        """
        if self._client is None:
            return ""
        # Access the SDK's internal JsonRpcClient which captures stderr
        rpc_client = getattr(self._client, "_client", None)
        if rpc_client is not None and hasattr(rpc_client, "get_stderr_output"):
            try:
                return rpc_client.get_stderr_output()
            except Exception:
                return ""
        return ""

    async def create_session(self, config: SDKSessionConfig) -> SDKSession:
        """Create a new session for this agent."""
        session = await self.client.create_session(config)
        self._session = session
        sid = config.get("session_id", "unnamed")
        model = config.get("model", "default")
        logger.info("Created session: %s (model=%s)", sid, model)
        return session

    async def resume_session(
        self, session_id: str, config: SDKResumeConfig | None = None
    ) -> SDKSession:
        """Resume a previously persisted session (sleep → wake).

        BYOK credentials must be re-provided on every resume.
        """
        session = await self.client.resume_session(session_id, config)
        self._session = session
        logger.info("Resumed session: %s", session_id)
        return session

    async def delete_session(self, session_id: str) -> None:
        """Delete a session's persisted state permanently."""
        await self.client.delete_session(session_id)
        logger.info("Deleted session: %s", session_id)

    async def list_sessions(self) -> list:
        """List all sessions known to this CopilotClient."""
        return await self.client.list_sessions()


def _build_provider_dict(runtime_config: RuntimeConfig) -> SDKProviderConfig | None:
    """Build a BYOK ProviderConfig dict from RuntimeConfig.

    Returns None when no BYOK credentials are available — the SDK will
    use Copilot's built-in auth (requires ``copilot auth login``).
    """
    provider = runtime_config.provider

    # Resolve API key — direct value or env var lookup
    api_key = provider.api_key or (
        os.environ.get(provider.api_key_env) if provider.api_key_env else None
    )

    # If provider type is "copilot" or no API key available, let the SDK
    # use its built-in Copilot auth — no provider dict needed.
    if provider.type == "copilot" or not api_key:
        return None

    result: SDKProviderConfig = {
        "type": provider.type,
        "base_url": provider.base_url,
    }
    result["api_key"] = api_key
    return result


def build_session_config(
    *,
    role: str,
    issue_number: int | None,
    system_message: str,
    working_directory: str,
    runtime_config: RuntimeConfig,
    tools: list | None = None,
    hooks: dict[str, Callable] | None = None,
    session_id_override: str | None = None,
    custom_agents: list[dict[str, Any]] | None = None,
    mcp_servers: dict[str, Any] | None = None,
    skill_directories: list[str] | None = None,
    available_tools: list[str] | None = None,
    excluded_tools: list[str] | None = None,
) -> SDKSessionConfig:
    """Build an SDK SessionConfig dict from role + runtime config.

    Convention: session_id = "squadron-{role}-issue-{number}"
    PM sessions get a batch suffix since they're stateless.
    """
    if session_id_override:
        session_id = session_id_override
    elif issue_number is not None:
        session_id = f"squadron-{role}-issue-{issue_number}"
    else:
        session_id = f"squadron-{role}"

    # Resolve model for this role (override or default)
    model_override = runtime_config.models.get(role)
    model = model_override.model if model_override else runtime_config.default_model

    # Resolve reasoning_effort: role override > global default > omit entirely.
    # Not all models support it (e.g. claude-sonnet-4.6 rejects it), so only
    # include when explicitly set.
    reasoning = (
        model_override.reasoning_effort if model_override else None
    ) or runtime_config.default_reasoning_effort

    config: SDKSessionConfig = {
        "session_id": session_id,
        "model": model,
        "system_message": {"mode": "replace", "content": system_message},
        "working_directory": working_directory,
        "infinite_sessions": {
            "enabled": True,
            "background_compaction_threshold": 0.80,
            "buffer_exhaustion_threshold": 0.95,
        },
    }
    # Only include provider when BYOK credentials are configured
    # (omitting lets the SDK use Copilot's built-in auth)
    provider_dict = _build_provider_dict(runtime_config)
    if provider_dict:
        config["provider"] = provider_dict
    # Only include reasoning_effort when explicitly configured
    # (not all models support it — e.g. claude-sonnet-4.6 rejects it)
    if reasoning:
        config["reasoning_effort"] = reasoning
    if tools:
        config["tools"] = tools
    if hooks:
        config["hooks"] = hooks
    if custom_agents:
        config["custom_agents"] = custom_agents
    if mcp_servers:
        config["mcp_servers"] = mcp_servers
    if skill_directories:
        config["skill_directories"] = skill_directories
    if available_tools:
        config["available_tools"] = available_tools
    if excluded_tools:
        config["excluded_tools"] = excluded_tools
    return config


def build_resume_config(
    *,
    role: str,
    system_message: str,
    working_directory: str,
    runtime_config: RuntimeConfig,
    tools: list | None = None,
    hooks: dict[str, Callable] | None = None,
    custom_agents: list[dict[str, Any]] | None = None,
    mcp_servers: dict[str, Any] | None = None,
    skill_directories: list[str] | None = None,
    available_tools: list[str] | None = None,
    excluded_tools: list[str] | None = None,
) -> SDKResumeConfig:
    """Build an SDK ResumeSessionConfig for session resumption.

    Like build_session_config but without session_id (passed separately
    to client.resume_session()).
    """
    model_override = runtime_config.models.get(role)
    model = model_override.model if model_override else runtime_config.default_model
    reasoning = (
        model_override.reasoning_effort
        if model_override
        else runtime_config.default_reasoning_effort
    )

    config: SDKResumeConfig = {
        "model": model,
        "system_message": {"mode": "replace", "content": system_message},
        "working_directory": working_directory,
        "infinite_sessions": {
            "enabled": True,
            "background_compaction_threshold": 0.80,
            "buffer_exhaustion_threshold": 0.95,
        },
    }
    # Only include reasoning_effort when explicitly configured
    if reasoning is not None:
        config["reasoning_effort"] = reasoning
    # Only include provider when BYOK credentials are configured
    provider_dict = _build_provider_dict(runtime_config)
    if provider_dict:
        config["provider"] = provider_dict
    if tools:
        config["tools"] = tools
    if hooks:
        config["hooks"] = hooks
    if custom_agents:
        config["custom_agents"] = custom_agents
    if mcp_servers:
        config["mcp_servers"] = mcp_servers
    if skill_directories:
        config["skill_directories"] = skill_directories
    if available_tools:
        config["available_tools"] = available_tools
    if excluded_tools:
        config["excluded_tools"] = excluded_tools
    return config
