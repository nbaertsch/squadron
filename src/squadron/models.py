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


# ── Mail Messages (Push Delivery) ───────────────────────────────────────────


class MessageProvenanceType(str, enum.Enum):
    """Type identifier for the source of a mail message.

    Only ``issue_comment`` and ``pr_comment`` are implemented now, but the
    enum is the single place to add new source types (e.g. slack_message,
    email, direct_invocation) without changing the rest of the schema.
    """

    ISSUE_COMMENT = "issue_comment"
    PR_COMMENT = "pr_comment"


class MessageProvenance(BaseModel):
    """Structured origin of a mail message — type + source reference.

    The ``type`` field identifies the source kind; the remaining fields
    carry the reference coordinates for that type.  Fields irrelevant to
    the current type are left as ``None``.

    Extensibility: adding a new provenance type requires only:
      1. A new ``MessageProvenanceType`` variant.
      2. New optional fields for its reference coordinates (if any).
      No existing schema changes are required.

    Examples::

        # issue_comment: a comment on a GitHub issue
        MessageProvenance(
            type=MessageProvenanceType.ISSUE_COMMENT,
            issue_number=42,
            comment_id=987,
        )

        # pr_comment: a comment on a GitHub pull request
        MessageProvenance(
            type=MessageProvenanceType.PR_COMMENT,
            pr_number=10,
            comment_id=456,
        )
    """

    type: MessageProvenanceType = Field(description="Message source type")
    issue_number: int | None = Field(
        default=None, description="GitHub issue number (issue_comment provenance)"
    )
    pr_number: int | None = Field(
        default=None, description="GitHub PR number (pr_comment provenance)"
    )
    comment_id: int | None = Field(
        default=None, description="GitHub comment ID (issue_comment and pr_comment)"
    )


class MailMessage(BaseModel):
    """An inbound @ mention message to be pushed into an agent's context.

    Created when another user (or agent) @ mentions an active agent in a
    GitHub comment.  Stored per-agent in ``AgentManager.agent_mail_queues``
    and injected into the next ``send_and_wait`` prompt before the LLM call.
    After injection the message is removed — no double-delivery.
    """

    sender: str = Field(description="GitHub username of the message sender")
    body: str = Field(description="Full comment body (raw message content)")
    provenance: MessageProvenance = Field(description="Structured message origin")
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the message was enqueued",
    )


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

    @property
    def review(self) -> dict | None:
        """The review object for pull_request_review events."""
        return self.payload.get("review")

    @property
    def issue_creator(self) -> str | None:
        """GitHub username of the user who created the issue.

        Returns the username from payload.issue.user.login if available.
        This is different from sender which is the user who triggered the event.
        """
        issue = self.payload.get("issue", {})
        user = issue.get("user", {})
        return user.get("login")


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
    PR_REVIEW_COMMENT = "pr.review_comment"  # Inline comment on PR diff
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
    command: ParsedCommand | None = Field(
        default=None,
        description="Parsed command from @squadron-dev syntax (help or agent routing)",
    )
    data: dict = Field(default_factory=dict, description="Event-specific data")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Command Parsing ──────────────────────────────────────────────────────────

# The bot mention prefix. Hardcoded to avoid pinging random GitHub users.
# The GitHub org 'squadron-dev' is owned by the project.
BOT_MENTION = "squadron-dev"

# Matches @squadron-dev <agent>: <message> or @squadron-dev <agent> <message> syntax
# Groups: (1) agent name, (2) message
_COMMAND_RE = re.compile(
    rf"@{BOT_MENTION}\s+([\w][\w-]*):?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)

# Matches @squadron-dev help (case-insensitive)
_HELP_RE = re.compile(
    rf"@{BOT_MENTION}\s+help\b",
    re.IGNORECASE,
)

# Matches fenced code blocks (``` or ~~~), including optional language specifier
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")

# Matches inline code spans (single backtick, no newlines)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code_spans(text: str) -> str:
    """Remove fenced code blocks and inline code spans from text.

    Mentions inside backtick-wrapped code are treated as literal text —
    they should not trigger agent invocation.  This mirrors GitHub's own
    behaviour where backtick-wrapped ``@mentions`` render as plain text
    rather than sending notifications.

    Fenced blocks are stripped before inline spans so that a fenced block
    containing a backtick inside it is handled correctly.
    """
    text = _FENCED_CODE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


class ParsedCommand(BaseModel):
    """Result of parsing a comment for squadron commands."""

    is_help: bool = False
    agent_name: str | None = None
    message: str | None = None


def parse_command(text: str) -> ParsedCommand | None:
    """Parse a comment for squadron command syntax.

    Supports:
    - ``@squadron-dev help`` — returns ParsedCommand(is_help=True)
    - ``@squadron-dev <agent>: <message>`` — returns ParsedCommand with agent_name and message

    Mentions that appear inside backtick-wrapped inline code or fenced code
    blocks are **ignored** — they are treated as literal text, not commands.
    This mirrors GitHub's own behaviour (backtick @mentions don't notify).

    Returns None if the comment doesn't match any command syntax.
    """
    if not text:
        return None

    # Strip code spans so that backtick-wrapped mentions are not matched.
    # We operate on the stripped copy for all pattern searches.
    searchable = _strip_code_spans(text)

    # Check for help command first
    if _HELP_RE.search(searchable):
        return ParsedCommand(is_help=True)

    # Check for agent command
    match = _COMMAND_RE.search(searchable)
    if match:
        agent_name = match.group(1).lower()
        message = match.group(2).strip()

        # Define known agent names (from .squadron/config.yaml)
        known_agents = {
            "pm",
            "bug-fix",
            "feat-dev",
            "docs-dev",
            "infra-dev",
            "security-review",
            "test-coverage",
            "pr-review",
        }

        # If there's a colon in the match, it's definitely a command
        # If no colon, validate that it's a known agent name
        match_text = searchable[match.start() : match.end()]
        has_colon = ":" in match_text

        if has_colon or agent_name in known_agents:
            return ParsedCommand(agent_name=agent_name, message=message)

    return None
