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
from pydantic import BaseModel, Field, model_validator

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
    """Defines when an agent should be spawned, woken, completed, or put to sleep.

    Actions:
        - spawn (default): Create a new agent for this role
        - wake: Wake a sleeping agent of this role (for the matched PR/issue)
        - complete: Complete an active/sleeping agent of this role
        - sleep: Transition an active agent to SLEEPING (e.g. after opening PR)

    Examples:
        - {event: "issues.labeled", label: "feature"}  → spawn on label
        - {event: "pull_request.opened", condition: {approval_flow: true}}  → spawn reviewer via approval flow
        - {event: "pull_request.opened", action: "sleep"}  → sleep dev after PR opened
        - {event: "pull_request.synchronize", action: "wake"}  → wake reviewer on PR update
        - {event: "pull_request.closed", action: "complete"}  → complete agent on PR close
        - {event: "pull_request.closed", condition: {merged: false}, action: "wake"}  → wake dev on PR rejection
        - {event: "pull_request_review.submitted", condition: {review_state: "changes_requested"}, action: "wake"}
    """

    event: str  # GitHub webhook event type, e.g. "issues.opened", "issues.labeled"
    label: str | None = None  # Only trigger when this specific label is applied
    action: Literal["spawn", "wake", "complete", "sleep"] = "spawn"
    condition: dict[str, Any] | None = None  # e.g. {approval_flow: true}, {merged: false}


class AgentRoleConfig(BaseModel):
    agent_definition: str  # Relative path to agent .md file
    singleton: bool = False
    lifecycle: Literal["ephemeral", "persistent"] = "persistent"
    triggers: list[AgentTrigger] = Field(default_factory=list)  # Event triggers for this agent
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


# ── Human Invocation Config ───────────────────────────────────────────────────


class HumanInvocationConfig(BaseModel):
    """Configuration for how to notify humans when agent intervention is needed."""

    method: Literal["github_comment", "issue_label", "both"] = "both"
    mention_format: str = "@{username}"  # or "@{group}" for team mentions
    include_context: bool = True  # include failure details in comment
    labels_to_add: list[str] = Field(default_factory=lambda: ["needs-human"])


# ── Review Policy Config (replaces approval_flows + workflows) ────────────────


class FailureAction(BaseModel):
    """Defines what happens when a failure occurs (merge conflict, CI fail, etc.)."""

    action: Literal["spawn", "notify", "escalate"] = "notify"
    target: str = "maintainers"  # agent role OR human group name
    fallback: "FailureAction | None" = None  # if primary action fails


class AutoMergeConfig(BaseModel):
    """Configuration for automatic PR merging."""

    enabled: bool = True
    method: Literal["squash", "merge", "rebase"] = "squash"
    delete_branch: bool = True
    require_ci_pass: bool = False  # wait for status checks before merge

    # Failure handlers
    on_merge_conflict: FailureAction = Field(
        default_factory=lambda: FailureAction(action="notify", target="maintainers")
    )
    on_ci_failed: FailureAction = Field(
        default_factory=lambda: FailureAction(action="notify", target="maintainers")
    )
    on_unknown_error: FailureAction = Field(
        default_factory=lambda: FailureAction(action="escalate", target="maintainers")
    )


class SynchronizeConfig(BaseModel):
    """What happens when a PR is updated after reviews."""

    invalidate_approvals: bool = True  # require full re-review (not just "still LGTM")
    respawn_reviewers: bool = True  # wake/spawn reviewer agents to re-check


class ReviewRequirement(BaseModel):
    """A single review requirement — which role must approve and how many."""

    role: str  # agent role name, e.g. "security-review"
    count: int = 1  # how many approvals from this role required


class MatchCondition(BaseModel):
    """Conditions for when a review rule applies."""

    labels: list[str] = Field(default_factory=list)  # any label matches
    paths: list[str] = Field(default_factory=list)  # glob patterns for changed files
    base_branch: str | None = None  # target branch must match

    def matches(
        self, labels: list[str], changed_files: list[str] | None = None, base_branch: str = ""
    ) -> bool:
        """Check if this condition matches the given PR context."""
        import fnmatch

        # Label match: any overlap (if labels specified)
        if self.labels:
            if not any(lbl in self.labels for lbl in labels):
                return False

        # Path match: any changed file matches any glob (if paths specified)
        if self.paths and changed_files is not None:
            if not any(
                fnmatch.fnmatch(f, pattern) for f in changed_files for pattern in self.paths
            ):
                return False

        # Base branch match (if specified)
        if self.base_branch and base_branch != self.base_branch:
            return False

        return True


class ReviewRule(BaseModel):
    """A conditional review rule — when matched, adds requirements."""

    name: str
    match: MatchCondition
    requirements: list[ReviewRequirement] = Field(default_factory=list)
    sequence: list[str] = Field(default_factory=list)  # optional: enforce review order


class ReviewPolicyConfig(BaseModel):
    """Unified PR review policy — replaces approval_flows + workflows.

    Defines:
    - Which roles must approve PRs (default + conditional rules)
    - Optional sequencing (role A must approve before role B starts)
    - Auto-merge behavior and failure handling
    - What happens when PRs are updated after review
    """

    enabled: bool = True

    # Auto-merge configuration
    auto_merge: AutoMergeConfig = Field(default_factory=AutoMergeConfig)

    # What happens when PR is updated after approvals
    on_synchronize: SynchronizeConfig = Field(default_factory=SynchronizeConfig)

    # Default requirements for all PRs
    default_requirements: list[ReviewRequirement] = Field(
        default_factory=lambda: [ReviewRequirement(role="pr-review", count=1)]
    )

    # Conditional rules (additive with defaults)
    rules: list[ReviewRule] = Field(default_factory=list)

    def get_requirements_for_pr(
        self,
        labels: list[str],
        changed_files: list[str] | None = None,
        base_branch: str = "",
    ) -> tuple[list[ReviewRequirement], list[str]]:
        """Get all review requirements for a PR.

        Returns:
            Tuple of (requirements_list, sequence_list).
            - requirements_list: All required roles with counts
            - sequence_list: Order in which roles must approve (empty = parallel)
        """
        # Start with defaults
        requirements: dict[str, int] = {}
        for req in self.default_requirements:
            requirements[req.role] = max(requirements.get(req.role, 0), req.count)

        # Add from matching rules
        sequence: list[str] = []
        for rule in self.rules:
            if rule.match.matches(labels, changed_files, base_branch):
                for req in rule.requirements:
                    requirements[req.role] = max(requirements.get(req.role, 0), req.count)
                # Use sequence from first matching rule that defines one
                if rule.sequence and not sequence:
                    sequence = rule.sequence

        return [ReviewRequirement(role=r, count=c) for r, c in requirements.items()], sequence

    def get_required_roles(
        self,
        labels: list[str],
        changed_files: list[str] | None = None,
        base_branch: str = "",
    ) -> list[str]:
        """Get list of required reviewer roles for a PR."""
        requirements, _ = self.get_requirements_for_pr(labels, changed_files, base_branch)
        return [req.role for req in requirements]


# ── Legacy: ApprovalFlowConfig (deprecated, kept for backward compatibility) ──


class ApprovalFlowRule(BaseModel):
    """DEPRECATED: Use review_policy instead. Kept for backward compatibility."""

    name: str
    match_labels: list[str] = Field(default_factory=list)
    match_paths: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)
    required_approvals: int = 1

    def matches(self, labels: list[str], changed_files: list[str] | None = None) -> bool:
        import fnmatch

        if self.match_labels:
            if not any(lbl in self.match_labels for lbl in labels):
                return False
        if self.match_paths and changed_files is not None:
            if not any(
                fnmatch.fnmatch(f, pattern) for f in changed_files for pattern in self.match_paths
            ):
                return False
        return True


class ApprovalFlowConfig(BaseModel):
    """DEPRECATED: Use review_policy instead. Kept for backward compatibility."""

    enabled: bool = False  # Disabled by default — use review_policy
    default_reviewers: list[str] = Field(default_factory=list)
    rules: list[ApprovalFlowRule] = Field(default_factory=list)

    def get_reviewers_for_pr(
        self, labels: list[str], changed_files: list[str] | None = None
    ) -> list[str]:
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

    # New unified review policy (replaces approval_flows + workflows)
    review_policy: ReviewPolicyConfig = Field(default_factory=ReviewPolicyConfig)
    human_invocation: HumanInvocationConfig = Field(default_factory=HumanInvocationConfig)

    # DEPRECATED: kept for backward compatibility, use review_policy instead
    approval_flows: ApprovalFlowConfig = Field(default_factory=ApprovalFlowConfig)
    commands: dict[str, "CommandDefinition"] = Field(default_factory=dict)
    workflows: list["WorkflowDefinition"] = Field(default_factory=list)


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
    emoji: str = "🤖"  # Default emoji for agent signatures
    infer: bool = True
    tools: list[str] | None = None  # Allowlist of tool names (built-in aliases + custom).
    #                                  None = all tools available; list = only listed tools.
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
        tools=fm.get("tools") or None,
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


# ── Command Configuration ─────────────────────────────────────────────────────


class CommandDefinition(BaseModel):
    """Configuration for a specific command."""

    enabled: bool = True
    invoke_agent: bool = True
    delegate_to: str | None = None  # Agent role to delegate to if invoke_agent is True
    response: str | None = None  # Static response for commands with invoke_agent=False
