"""Core data models for Squadron."""

from __future__ import annotations

import enum
import re
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
    FAILED = "failed"


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
    ISSUE_REOPENED = "issue.reopened"
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
    mentioned_roles: list[str] = Field(
        default_factory=list,
        description="Agent roles mentioned in the comment body (e.g. ['pm', 'feat-dev'])",
    )
    data: dict = Field(default_factory=dict, description="Event-specific data")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Mention Parsing ──────────────────────────────────────────────────────────

# Matches @role or /role at word boundaries.  Role names may contain
# letters, digits, and hyphens (e.g. feat-dev, pr-review, docs-dev).
_MENTION_RE = re.compile(r"(?:^|(?<=\s))[@/]([\w][\w-]*)", re.MULTILINE)


def parse_mentions(text: str, known_roles: set[str]) -> list[str]:
    """Extract agent role mentions from a comment body.

    Supports two mention styles:
    - ``@role`` — e.g. ``@pm``, ``@feat-dev``
    - ``/role`` — e.g. ``/pm``, ``/feat-dev``

    Only returns roles that exist in *known_roles* to avoid false positives
    (e.g. ``@someone-random`` is ignored).

    Returns a deduplicated list in the order first seen.
    """
    if not text:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for match in _MENTION_RE.finditer(text):
        role = match.group(1).lower()
        if role in known_roles and role not in seen:
            seen.add(role)
            result.append(role)
    return result
