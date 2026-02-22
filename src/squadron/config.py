"""Configuration loading for Squadron.

Reads .squadron/config.yaml and agent definitions from .squadron/agents/.
Pydantic models validate the config schema defined in research/config-schema.md.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# ── Config Models (match config-schema.md) ───────────────────────────────────


class SkillDefinition(BaseModel):
    """A named skill (knowledge bundle) available to agents.

    Skills are directories of markdown/text files containing domain knowledge
    (architecture docs, coding standards, API schemas, workflow guides) that
    the Copilot SDK can index and inject as context.
    """

    path: str  # Relative to skills.base_path — must be a plain relative path
    description: str = ""

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        """Reject absolute paths and directory traversal components.

        Absolute paths (starting with /) cause pathlib to silently drop the
        base path: Path('/repo') / '/etc' → /etc. Traversal components (..)
        can escape the repository root. Both are rejected here as defence-in-depth.
        """
        from pathlib import PurePosixPath

        p = PurePosixPath(v)
        if p.is_absolute():
            raise ValueError(f"SkillDefinition.path must be a relative path, got absolute: {v!r}")
        if ".." in p.parts:
            raise ValueError(
                f"SkillDefinition.path must not contain directory traversal components (..): {v!r}"
            )
        return v


class SkillsConfig(BaseModel):
    """Top-level skills configuration for project-level skill definitions."""

    base_path: str = ".squadron/skills"
    definitions: dict[str, SkillDefinition] = Field(default_factory=dict)

    @field_validator("base_path")
    @classmethod
    def _validate_base_path(cls, v: str) -> str:
        """Reject absolute paths and directory traversal components.

        Same reasoning as SkillDefinition.path: absolute paths bypass the
        repo root entirely, and .. components can escape it.
        """
        from pathlib import PurePosixPath

        p = PurePosixPath(v)
        if p.is_absolute():
            raise ValueError(f"SkillsConfig.base_path must be a relative path, got absolute: {v!r}")
        if ".." in p.parts:
            raise ValueError(
                f"SkillsConfig.base_path must not contain directory traversal components (..): {v!r}"
            )
        return v


class ProjectConfig(BaseModel):
    name: str
    owner: str = ""  # GitHub org/user, e.g. "noahbaertsch"
    repo: str = ""  # GitHub repo name, e.g. "squadron"
    default_branch: str = "main"
    bot_username: str = "squadron-dev[bot]"  # GitHub App bot username for self-event filtering


class LabelsConfig(BaseModel):
    types: list[str] = Field(default_factory=lambda: ["feature", "bug", "security", "docs"])
    priorities: list[str] = Field(default_factory=lambda: ["critical", "high", "medium", "low"])
    states: list[str] = Field(
        default_factory=lambda: [
            "needs-triage",
            "in-progress",
            "blocked",
            "needs-human",
            "needs-clarification",
        ]
    )


class BranchNamingConfig(BaseModel):
    feature: str = "feat/issue-{issue_number}"
    bugfix: str = "fix/issue-{issue_number}"
    security: str = "security/issue-{issue_number}"
    docs: str = "docs/issue-{issue_number}"
    infra: str = "infra/issue-{issue_number}"
    hotfix: str = "hotfix/issue-{issue_number}"


class AgentRoleConfig(BaseModel):
    agent_definition: str  # Relative path to agent .md file
    singleton: bool = False
    lifecycle: Literal["ephemeral", "persistent", "stateful"] = "persistent"
    subagents: list[str] = Field(default_factory=list)  # Other agent roles available as subagents
    branch_template: str | None = (
        None  # e.g. "feat/issue-{issue_number}"; None → auto from BranchNamingConfig
    )

    # Backward-compat: accept `stateless: true` → lifecycle: ephemeral
    @model_validator(mode="before")
    @classmethod
    def _migrate_stateless(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.pop("stateless", None):
            data.setdefault("lifecycle", "ephemeral")
        return data

    @property
    def is_ephemeral(self) -> bool:
        """Check if this is an ephemeral (stateless) agent."""
        return self.lifecycle == "ephemeral"


class CircuitBreakerDefaults(BaseModel):
    max_iterations: int = 5
    max_tool_calls: int = 200
    max_turns: int = 50
    max_active_duration: int = 7200  # seconds
    max_sleep_duration: int = 86400  # seconds
    warning_threshold: float = 0.80


class CircuitBreakerConfig(BaseModel):
    defaults: CircuitBreakerDefaults = Field(default_factory=CircuitBreakerDefaults)
    roles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def for_role(self, role: str) -> CircuitBreakerDefaults:
        """Get merged circuit breaker config for a specific role."""
        base = self.defaults.model_dump()
        overrides = self.roles.get(role, {})
        base.update(overrides)
        return CircuitBreakerDefaults(**base)


class ProviderConfig(BaseModel):
    type: str = "copilot"
    base_url: str = ""
    api_key_env: str = ""

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


class ModelOverride(BaseModel):
    model: str
    reasoning_effort: str | None = None


class RuntimeConfig(BaseModel):
    default_model: str = "claude-sonnet-4.6"
    default_reasoning_effort: str | None = None
    models: dict[str, ModelOverride] = Field(default_factory=dict)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    reconciliation_interval: int = 300  # seconds
    max_concurrent_agents: int = 10  # max agents running simultaneously (0 = unlimited)
    sparse_checkout: bool = False  # use git sparse-checkout for worktrees
    worktree_dir: str | None = (
        None  # override worktree base path (default: .squadron-data/worktrees)
    )


class EscalationConfig(BaseModel):
    default_notify: str = "maintainers"
    escalation_labels: list[str] = Field(default_factory=lambda: ["needs-human", "escalation"])
    max_issue_depth: int = 3


# ── Human Invocation Config ───────────────────────────────────────────────────


class HumanInvocationConfig(BaseModel):
    """Configuration for how to notify humans when agent intervention is needed."""

    method: Literal["github_comment", "issue_label", "both"] = "both"
    mention_format: str = "@{username}"  # or "@{group}" for team mentions
    include_context: bool = True  # include failure details in comment
    labels_to_add: list[str] = Field(default_factory=lambda: ["needs-human"])


class SquadronConfig(BaseModel):
    """Top-level Squadron configuration (matches .squadron/config.yaml)."""

    project: ProjectConfig
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    branch_naming: BranchNamingConfig = Field(default_factory=BranchNamingConfig)
    human_groups: dict[str, list[str]] = Field(default_factory=dict)
    agent_roles: dict[str, AgentRoleConfig] = Field(default_factory=dict)
    circuit_breakers: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)

    human_invocation: HumanInvocationConfig = Field(default_factory=HumanInvocationConfig)

    # Sandbox configuration (issue #85: sandboxed worktree execution)
    sandbox: Any = Field(default_factory=dict)

    commands: dict[str, "CommandDefinition"] = Field(default_factory=dict)

    # Skills configuration — project-level skill definitions
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    # AD-019: Unified pipeline system (replaces triggers, review_policy, workflows)
    pipelines: dict[str, Any] = Field(default_factory=dict)

    def get_pipeline_definitions(self) -> dict[str, Any]:
        """Return pipeline definitions as validated PipelineDefinition models.

        Uses lazy import to avoid circular dependencies and to keep the
        pipeline package optional until Phase 3 migration is complete.
        """
        from squadron.pipeline.models import PipelineDefinition

        result: dict[str, Any] = {}
        for name, defn in self.pipelines.items():
            if isinstance(defn, PipelineDefinition):
                result[name] = defn
            elif isinstance(defn, dict):
                result[name] = PipelineDefinition(**defn)
            else:
                msg = (
                    f"Invalid pipeline definition for '{name}': expected dict or PipelineDefinition"
                )
                raise TypeError(msg)
        return result

    def get_sandbox_config(self):
        """Return a typed SandboxConfig, lazily importing to avoid circular deps."""
        from squadron.sandbox.config import SandboxConfig

        raw = self.sandbox
        if isinstance(raw, SandboxConfig):
            return raw
        if isinstance(raw, dict):
            return SandboxConfig(**raw)
        return SandboxConfig()


# ── Agent Definition Loading ─────────────────────────────────────────────────


class MCPServerDefinition(BaseModel):
    """MCP server config from agent frontmatter — maps to SDK MCPServerConfig."""

    type: str = "http"
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=lambda: ["*"])
    timeout: int = 30

    def to_sdk_dict(self) -> dict[str, Any]:
        """Convert to SDK MCPServerConfig (MCPLocalServerConfig or MCPRemoteServerConfig)."""
        if self.type == "http":
            result: dict[str, Any] = {
                "type": "http",
                "url": self.url,
                "timeout": self.timeout,
            }
            if self.tools != ["*"]:
                result["tools"] = self.tools
            if self.headers:
                result["headers"] = self.headers
            return result
        else:
            result = {
                "type": self.type,
                "command": self.command,
                "timeout": self.timeout,
            }
            if self.args:
                result["args"] = self.args
            if self.env:
                result["env"] = self.env
            if self.cwd:
                result["cwd"] = self.cwd
            if self.tools != ["*"]:
                result["tools"] = self.tools
            return result


class AgentDefinition(BaseModel):
    """Parsed agent definition from a .md file with YAML frontmatter.

    The frontmatter fields map 1:1 to SDK CustomAgentConfig:

        ---
        name: agent-name
        display_name: Human Name
        description: What this agent does
        infer: true
        tools:
          - read_file
          - write_file
        mcp_servers:
          server_name:
            type: http
            url: https://...
        ---
        Markdown body is the agent's prompt (used as system message).

    Orchestration config (subagents, circuit breakers, etc.) belongs
    in config.yaml under agent_roles and circuit_breakers, NOT here.
    """

    role: str  # From filename (e.g. "pm", "feat-dev")
    raw_content: str  # Full file content (frontmatter + body)
    prompt: str = ""  # Markdown body after frontmatter (the system message)

    # Fields mapping to SDK CustomAgentConfig
    name: str = ""  # Defaults to role if not set
    display_name: str = ""
    description: str = ""
    emoji: str = "🤖"  # Default emoji for agent signatures
    infer: bool = True
    tools: list[str] | None = None  # Allowlist of tool names (built-in aliases + custom).
    #                                  None = all tools available; list = only listed tools.
    mcp_servers: dict[str, MCPServerDefinition] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)  # Skill names from frontmatter

    def to_custom_agent_config(self) -> dict[str, Any]:
        """Convert to SDK CustomAgentConfig dict.

        Returns a dict compatible with copilot.types.CustomAgentConfig TypedDict:
        {name, display_name, description, tools, prompt, mcp_servers, infer}
        """
        config: dict[str, Any] = {
            "name": self.name or self.role,
            "prompt": self.prompt,
            "infer": self.infer,
        }
        if self.display_name:
            config["display_name"] = self.display_name
        if self.description:
            config["description"] = self.description
        if self.tools:
            config["tools"] = self.tools
        if self.mcp_servers:
            config["mcp_servers"] = {
                name: srv.to_sdk_dict() for name, srv in self.mcp_servers.items()
            }
        return config


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from markdown body.

    Returns (frontmatter_dict, body_markdown).
    If no frontmatter found, returns ({}, full_content).
    """
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return {}, content

    # Find the closing ---
    # Skip the first line (opening ---)
    lines = content.split("\n")
    start_idx = None
    end_idx = None

    for i, line in enumerate(lines):
        if line.strip() == "---":
            if start_idx is None:
                start_idx = i
            else:
                end_idx = i
                break

    if start_idx is None or end_idx is None:
        return {}, content

    frontmatter_text = "\n".join(lines[start_idx + 1 : end_idx])
    body = "\n".join(lines[end_idx + 1 :]).strip()

    try:
        fm = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        logger.warning("Failed to parse YAML frontmatter — treating as plain markdown")
        return {}, content

    return fm, body


def parse_agent_definition(role: str, content: str) -> AgentDefinition:
    """Parse an agent markdown definition file with YAML frontmatter.

    The YAML frontmatter maps 1:1 to SDK CustomAgentConfig fields:
    name, display_name, description, tools, mcp_servers, infer.

    Orchestration config (subagents, circuit breakers, etc.) belongs
    in config.yaml, NOT in agent .md frontmatter.

    The markdown body after frontmatter becomes the agent's prompt
    (used as the system message for the LLM session).
    """
    fm, body = _split_frontmatter(content)

    # Parse MCP server definitions
    mcp_servers: dict[str, MCPServerDefinition] = {}
    raw_mcp = fm.get("mcp_servers", {})
    if isinstance(raw_mcp, dict):
        for name, srv_data in raw_mcp.items():
            if isinstance(srv_data, dict):
                mcp_servers[name] = MCPServerDefinition(**srv_data)

    return AgentDefinition(
        role=role,
        raw_content=content,
        prompt=body,
        name=fm.get("name", role),
        display_name=fm.get("display_name", ""),
        description=fm.get("description", ""),
        infer=fm.get("infer", True),
        tools=fm.get("tools") or None,
        mcp_servers=mcp_servers,
        skills=fm.get("skills") or [],
    )


# ── Config Loader ────────────────────────────────────────────────────────────


def load_config(squadron_dir: Path) -> SquadronConfig:
    """Load Squadron configuration from a .squadron/ directory.

    Args:
        squadron_dir: Path to the .squadron/ directory.

    Returns:
        Validated SquadronConfig.

    Raises:
        FileNotFoundError: If config.yaml doesn't exist.
        ValueError: If config validation fails.
    """
    config_path = squadron_dir / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Squadron config not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    config = SquadronConfig(**raw)

    # Environment variable overrides for deployment
    worktree_dir = os.environ.get("SQUADRON_WORKTREE_DIR")
    if worktree_dir:
        config.runtime.worktree_dir = worktree_dir

    # Sandbox environment overrides
    sandbox_enabled = os.environ.get("SQUADRON_SANDBOX_ENABLED")
    if sandbox_enabled is not None:
        sandbox_raw = config.sandbox if isinstance(config.sandbox, dict) else {}
        sandbox_raw["enabled"] = sandbox_enabled.lower() in ("1", "true", "yes")
        config.sandbox = sandbox_raw

    sandbox_retention = os.environ.get("SQUADRON_SANDBOX_RETENTION_PATH")
    if sandbox_retention:
        sandbox_raw = config.sandbox if isinstance(config.sandbox, dict) else {}
        sandbox_raw["retention_path"] = sandbox_retention
        config.sandbox = sandbox_raw

    logger.info("Loaded Squadron config: project=%s", config.project.name)
    return config


def load_agent_definitions(squadron_dir: Path) -> dict[str, AgentDefinition]:
    """Load all agent definition files from .squadron/agents/.

    Returns:
        Dict mapping role name → AgentDefinition.
    """
    agents_dir = squadron_dir / "agents"
    definitions: dict[str, AgentDefinition] = {}

    if not agents_dir.exists():
        logger.warning("No agents directory found at %s", agents_dir)
        return definitions

    for md_file in sorted(agents_dir.glob("*.md")):
        role = md_file.stem  # e.g. "pm", "feat-dev"
        content = md_file.read_text()
        definitions[role] = parse_agent_definition(role, content)
        logger.info("Loaded agent definition: %s", role)

    return definitions


# ── Command Configuration ─────────────────────────────────────────────────────


class CommandDefinition(BaseModel):
    """Configuration for a specific command."""

    enabled: bool = True
    invoke_agent: bool = True
    delegate_to: str | None = None  # Agent role to delegate to if invoke_agent is True
    response: str | None = None  # Static response for commands with invoke_agent=False
