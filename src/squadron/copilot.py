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

logger = logging.getLogger(__name__)


class CopilotAgent:
    """Manages a CopilotClient instance for a single agent.

    One CopilotAgent = one CLI subprocess (Pattern 1 from research).
    Handles session creation, resumption, and cleanup.
    """

    def __init__(self, runtime_config: RuntimeConfig, working_directory: str):
        self.runtime_config = runtime_config
        self.working_directory = working_directory
        self._client: CopilotClient | None = None
        self._session: SDKSession | None = None

    async def start(self) -> None:
        """Start the underlying CopilotClient (spawns CLI subprocess)."""
        self._client = CopilotClient({"cwd": self.working_directory})
        await self._client.start()
        logger.info("CopilotClient started (cwd=%s)", self.working_directory)

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
    reasoning = (
        model_override.reasoning_effort
        if model_override
        else runtime_config.default_reasoning_effort
    )

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
    # Only include reasoning_effort if explicitly configured for this role
    # (not all models support it — e.g. claude-sonnet-4 rejects it)
    if model_override and model_override.reasoning_effort:
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
        "reasoning_effort": reasoning,
        "infinite_sessions": {
            "enabled": True,
            "background_compaction_threshold": 0.80,
            "buffer_exhaustion_threshold": 0.95,
        },
    }
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
