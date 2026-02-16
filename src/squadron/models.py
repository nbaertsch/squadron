"""Core data models for Squadron."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, Field


# ── Agent Status & Role ──────────────────────────────────────────────────────


class AgentStatus(str, enum.Enum):
    """Agent lifecycle states (AD-013, agent-design.md state machine)."""

    CREATED = "created"
    ACTIVE = "active"
    SLEEPING = "sleeping"
    COMPLETED = "completed"
    ESCALATED = "escalated"


# Agent roles are plain strings — defined in .squadron/config.yaml, not in code.
# The config's agent_roles dict is the single source of truth for available roles.
AgentRole = str


# ── Agent Record ─────────────────────────────────────────────────────────────


class AgentRecord(BaseModel):
    """A tracked agent instance in the registry (AD-013)."""

    agent_id: str = Field(description="Unique agent identifier, e.g. 'feat-dev-issue-42'")
    role: AgentRole
    issue_number: int | None = Field(
        default=None, description="GitHub issue this agent is assigned to"
    )
    pr_number: int | None = Field(default=None, description="PR opened by this agent")
    session_id: str | None = Field(default=None, description="Copilot SDK session ID")
    status: AgentStatus = AgentStatus.CREATED
    branch: str | None = Field(default=None, description="Git branch this agent works on")
    worktree_path: str | None = Field(default=None, description="Path to agent's git worktree")
    blocked_by: list[int] = Field(
        default_factory=list, description="Issue numbers blocking this agent"
    )
    iteration_count: int = Field(default=0, description="Number of test-fix iterations")
    tool_call_count: int = Field(default=0, description="Total tool invocations")
    turn_count: int = Field(default=0, description="LLM conversation turns")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    active_since: datetime | None = Field(
        default=None, description="When agent last entered ACTIVE"
    )
    sleeping_since: datetime | None = Field(default=None, description="When agent entered SLEEPING")


# ── GitHub Events ────────────────────────────────────────────────────────────


class GitHubEvent(BaseModel):
    """Raw GitHub webhook event."""

    delivery_id: str = Field(description="X-GitHub-Delivery UUID")
    event_type: str = Field(description="X-GitHub-Event header value")
    action: str | None = Field(default=None, description="Event action (e.g. 'opened', 'closed')")
    payload: dict = Field(default_factory=dict, description="Full webhook payload")

    @property
    def full_type(self) -> str:
        """e.g. 'issues.opened', 'pull_request.closed'."""
        if self.action:
            return f"{self.event_type}.{self.action}"
        return self.event_type

    @property
    def sender(self) -> str | None:
        """GitHub username of the event sender."""
        sender = self.payload.get("sender", {})
        return sender.get("login")

    @property
    def is_bot(self) -> bool:
        """Whether the event was triggered by a bot."""
        sender = self.payload.get("sender", {})
        return sender.get("type") == "Bot"

    @property
    def repo_full_name(self) -> str | None:
        """owner/repo from the event payload."""
        repo = self.payload.get("repository", {})
        return repo.get("full_name")

    @property
    def issue(self) -> dict | None:
        return self.payload.get("issue")

    @property
    def pull_request(self) -> dict | None:
        return self.payload.get("pull_request")

    @property
    def comment(self) -> dict | None:
        return self.payload.get("comment")


# ── Internal Events ──────────────────────────────────────────────────────────


class SquadronEventType(str, enum.Enum):
    """Internal event types for inter-component communication."""

    # Webhook-originated
    ISSUE_OPENED = "issue.opened"
    ISSUE_CLOSED = "issue.closed"
    ISSUE_ASSIGNED = "issue.assigned"
    ISSUE_LABELED = "issue.labeled"
    ISSUE_COMMENT = "issue.comment"
    PR_OPENED = "pr.opened"
    PR_CLOSED = "pr.closed"
    PR_REVIEW_SUBMITTED = "pr.review_submitted"
    PR_SYNCHRONIZED = "pr.synchronized"
    PUSH = "push"

    # Framework-internal
    AGENT_BLOCKED = "agent.blocked"
    AGENT_COMPLETED = "agent.completed"
    AGENT_ESCALATED = "agent.escalated"
    BLOCKER_RESOLVED = "blocker.resolved"
    WAKE_AGENT = "wake.agent"


class SquadronEvent(BaseModel):
    """Internal event that flows through the event router."""

    event_type: SquadronEventType
    source_delivery_id: str | None = Field(default=None, description="Original webhook delivery ID")
    agent_id: str | None = Field(default=None, description="Related agent, if any")
    issue_number: int | None = None
    pr_number: int | None = None
    data: dict = Field(default_factory=dict, description="Event-specific data")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
