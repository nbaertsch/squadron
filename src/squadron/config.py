"""Configuration loading for Squadron.

Reads .squadron/config.yaml and agent definitions from .squadron/agents/.
Pydantic models validate the config schema defined in research/config-schema.md.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Config Models (match config-schema.md) ───────────────────────────────────


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


class AgentTrigger(BaseModel):
    """Defines when an agent should be spawned.

    Examples:
        - {event: "issues.opened"}  → new issues
        - {event: "issues.labeled", label: "feature"}  → specific label applied
        - {event: "pull_request.opened"}  → PR opened
    """

    event: str  # GitHub webhook event type, e.g. "issues.opened", "issues.labeled"
    label: str | None = None  # Only trigger when this specific label is applied
    filter_bot: bool = True  # Skip events from the bot (default: yes)


class AgentRoleConfig(BaseModel):
    agent_definition: str  # Relative path to agent .md file
    singleton: bool = False
    stateless: bool = False  # Stateless agents: no worktree, session destroyed after each run
    triggers: list[AgentTrigger] = Field(default_factory=list)  # Event triggers for this agent
    assignable_labels: list[str] = Field(default_factory=list)  # DEPRECATED — use triggers
    trigger: str | None = None  # e.g. "approval_flow" — DEPRECATED — use triggers
    subagents: list[str] = Field(default_factory=list)  # Other agent roles available as subagents


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
    default_model: str = "claude-sonnet-4"
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


class ApprovalFlowRule(BaseModel):
    """A single approval flow rule — maps labels/paths to review agent roles."""

    name: str
    match_labels: list[str] = Field(default_factory=list)
    match_paths: list[str] = Field(default_factory=list)  # glob patterns
    reviewers: list[str] = Field(default_factory=list)  # role names
    required_approvals: int = 1

    def matches(self, labels: list[str], changed_files: list[str] | None = None) -> bool:
        """Check if this rule matches the given PR labels and changed files."""
        import fnmatch

        # Label match: any overlap
        if self.match_labels:
            if not any(lbl in self.match_labels for lbl in labels):
                return False

        # Path match: any changed file matches any glob
        if self.match_paths and changed_files is not None:
            if not any(
                fnmatch.fnmatch(f, pattern) for f in changed_files for pattern in self.match_paths
            ):
                return False

        return True


class ApprovalFlowConfig(BaseModel):
    """Approval flow configuration — defines which reviewers to spawn for PRs."""

    enabled: bool = True
    default_reviewers: list[str] = Field(
        default_factory=lambda: ["pr-review"]
    )  # roles always assigned
    rules: list[ApprovalFlowRule] = Field(default_factory=list)

    def get_reviewers_for_pr(
        self, labels: list[str], changed_files: list[str] | None = None
    ) -> list[str]:
        """Return the set of reviewer roles for a PR based on labels/files."""
        roles = set(self.default_reviewers)
        for rule in self.rules:
            if rule.matches(labels, changed_files):
                roles.update(rule.reviewers)
        return sorted(roles)


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
    approval_flows: ApprovalFlowConfig = Field(default_factory=ApprovalFlowConfig)
    workflows: list[WorkflowDefinition] = Field(default_factory=list)


# ── Workflow Definitions ─────────────────────────────────────────────────────


class WorkflowStage(BaseModel):
    """A single stage in a workflow pipeline.

    Stages execute sequentially. Each stage spawns an agent, and the
    pipeline advances when the agent completes its action (e.g. approves a PR).
    """

    name: str  # unique within workflow, e.g. "test-coverage"
    agent: str  # agent role from .squadron/agents/
    action: str = "review"  # review | review_and_merge | develop | custom
    on_approve: str = "next"  # next | complete | stage-name
    on_reject: str = "stop"  # stop | restart | stage-name
    on_timeout: str = "escalate"  # escalate | stop | stage-name


class WorkflowTrigger(BaseModel):
    """Defines when a workflow activates."""

    event: str  # e.g. "pull_request.opened", "issues.opened", "push"
    conditions: dict[str, Any] = Field(default_factory=dict)
    # Supported condition keys:
    #   base_branch: str — match PR target branch
    #   head_branch_pattern: str — glob match on PR source branch
    #   labels: list[str] — require any of these labels
    #   paths: list[str] — glob match on changed files

    def matches_event(self, event_type: str) -> bool:
        """Check if the trigger event type matches."""
        return self.event == event_type

    def matches_conditions(self, payload: dict) -> bool:
        """Evaluate conditions against a webhook payload."""
        import fnmatch

        # base_branch condition
        base = self.conditions.get("base_branch")
        if base:
            pr_base = payload.get("pull_request", {}).get("base", {}).get("ref", "")
            if pr_base != base:
                return False

        # head_branch_pattern condition
        head_pattern = self.conditions.get("head_branch_pattern")
        if head_pattern:
            pr_head = payload.get("pull_request", {}).get("head", {}).get("ref", "")
            if not fnmatch.fnmatch(pr_head, head_pattern):
                return False

        # labels condition (any match)
        required_labels = self.conditions.get("labels")
        if required_labels:
            event_labels = [
                lbl.get("name", "")
                for lbl in (
                    payload.get("pull_request", {}).get("labels", [])
                    or payload.get("issue", {}).get("labels", [])
                )
            ]
            if not any(lbl in required_labels for lbl in event_labels):
                return False

        return True


class WorkflowDefinition(BaseModel):
    """A complete workflow definition loaded from .squadron/workflows/*.yaml."""

    name: str
    description: str = ""
    trigger: WorkflowTrigger
    stages: list[WorkflowStage] = Field(min_length=1)

    def matches(self, event_type: str, payload: dict) -> bool:
        """Check if this workflow should activate for a given event."""
        return self.trigger.matches_event(event_type) and self.trigger.matches_conditions(payload)


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
    infer: bool = True
    tools: list[str] = Field(default_factory=list)
    mcp_servers: dict[str, MCPServerDefinition] = Field(default_factory=dict)

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
        tools=fm.get("tools", []) or [],
        mcp_servers=mcp_servers,
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


def load_workflow_definitions(squadron_dir: Path) -> list[WorkflowDefinition]:
    """Load workflow definitions from .squadron/workflows/*.yaml.

    Each YAML file defines one or more workflows with triggers,
    conditions, and sequential stage pipelines.

    Returns:
        List of validated WorkflowDefinition objects.
    """
    workflows_dir = squadron_dir / "workflows"
    definitions: list[WorkflowDefinition] = []

    if not workflows_dir.exists():
        logger.debug("No workflows directory found at %s", workflows_dir)
        return definitions

    for yaml_file in sorted(workflows_dir.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                raw = yaml.safe_load(f) or {}

            # A file can contain a single workflow or a list
            if isinstance(raw, list):
                for item in raw:
                    wf = WorkflowDefinition(**item)
                    definitions.append(wf)
                    logger.info("Loaded workflow: %s (from %s)", wf.name, yaml_file.name)
            else:
                wf = WorkflowDefinition(**raw)
                definitions.append(wf)
                logger.info("Loaded workflow: %s (from %s)", wf.name, yaml_file.name)
        except Exception:
            logger.exception("Failed to load workflow from %s", yaml_file)

    return definitions
