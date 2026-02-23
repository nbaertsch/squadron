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
    CANCELLED = "cancelled"


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
    PR_LABELED = "pr.labeled"  # pull_request.labeled — label applied to PR
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

    # Action command fields (status, cancel, retry)
    action_name: str | None = None
    """Built-in action name (e.g. 'status', 'cancel', 'retry')."""
    action_args: list[str] = Field(default_factory=list)
    """Positional arguments to the action (e.g. role name for cancel/retry)."""

    @property
    def is_action(self) -> bool:
        """True when this parsed command is a built-in action, not an agent route."""
        return self.action_name is not None


# ── Default known agents (used as fallback when no config is available) ─────
# This set mirrors the agent roles defined in .squadron/config.yaml and is
# used only by the backward-compatible ``parse_command`` shim below.
# Prefer ``CommandParser`` with config-driven agent list for new code.
_DEFAULT_KNOWN_AGENTS: frozenset[str] = frozenset(
    {
        "pm",
        "bug-fix",
        "feat-dev",
        "docs-dev",
        "infra-dev",
        "security-review",
        "test-coverage",
        "pr-review",
    }
)

# Built-in action commands that are dispatched to the framework (not to agents)
_BUILT_IN_ACTIONS: frozenset[str] = frozenset({"status", "cancel", "retry"})


class CommandParser:
    """Config-driven parser for ``@<prefix>`` commands in GitHub comments.

    Replaces the module-level ``parse_command`` function with a parser that
    knows about the active agent roster (from ``config.agent_roles``) and
    the configured command prefix (from ``config.command_prefix``).

    Usage::

        parser = CommandParser(
            command_prefix=config.command_prefix,
            known_agents=set(config.agent_roles.keys()),
            commands=config.commands,
        )
        result = parser.parse(comment_body)

    The parser handles three command classes:
    - ``<prefix> help`` — returns ``ParsedCommand(is_help=True)``
    - ``<prefix> status|cancel|retry [args]`` — action commands
    - ``<prefix> <agent>: <message>`` — agent routing commands
    """

    def __init__(
        self,
        command_prefix: str = "@squadron-dev",
        known_agents: set[str] | None = None,
        commands: dict | None = None,
    ) -> None:
        self.command_prefix = command_prefix
        self.known_agents: frozenset[str] = frozenset(known_agents or _DEFAULT_KNOWN_AGENTS)
        self.commands: dict = commands or {}

        # Derive the set of action names from both built-in defaults and config
        config_actions: set[str] = set()
        for cmd_name, cmd_def in self.commands.items():
            cmd_type = (
                getattr(cmd_def, "type", None)
                if not isinstance(cmd_def, dict)
                else cmd_def.get("type")
            )
            if cmd_type == "action":
                config_actions.add(cmd_name)
        self.action_names: frozenset[str] = _BUILT_IN_ACTIONS | frozenset(config_actions)

        # Build regex patterns from command_prefix
        escaped = re.escape(command_prefix)
        self._command_re = re.compile(
            rf"{escaped}\s+([\w][\w-]*):?\s*(.*)",
            re.IGNORECASE | re.DOTALL,
        )
        self._help_re = re.compile(
            rf"{escaped}\s+help\b",
            re.IGNORECASE,
        )

    def parse(self, text: str) -> ParsedCommand | None:
        """Parse ``text`` for a squadron command.

        Returns a ``ParsedCommand`` if the text contains a valid command, or
        ``None`` if no command was found.  Mentions inside backtick-wrapped
        code spans or fenced code blocks are intentionally ignored.

        Args:
            text: Raw comment body from GitHub.

        Returns:
            Parsed command or None.
        """
        if not text:
            return None

        # Strip code spans so that backtick-wrapped mentions are not matched
        searchable = _strip_code_spans(text)

        # Check for help command first (higher priority than action/agent checks)
        if self._help_re.search(searchable):
            return ParsedCommand(is_help=True)

        # Match the general @prefix <word> pattern
        match = self._command_re.search(searchable)
        if not match:
            return None

        token = match.group(1).lower()
        rest = match.group(2).strip()
        match_text = searchable[match.start() : match.end()]
        has_colon = ":" in match_text

        # Action commands: status, cancel <role>, retry <role>
        if token in self.action_names:
            args = rest.split() if rest else []
            return ParsedCommand(action_name=token, action_args=args)

        # Agent routing commands
        if has_colon or token in self.known_agents:
            return ParsedCommand(agent_name=token, message=rest)

        return None


def parse_command(text: str) -> ParsedCommand | None:
    """Parse a comment for squadron command syntax.

    .. deprecated::
        Use ``CommandParser`` with config-driven agent list for new code.
        This function uses the default (hardcoded) known-agents list as a
        backward-compatible fallback.

    Supports:
    - ``@squadron-dev help`` — returns ParsedCommand(is_help=True)
    - ``@squadron-dev status`` — returns ParsedCommand(action_name="status")
    - ``@squadron-dev cancel <role>`` — returns ParsedCommand with action/args
    - ``@squadron-dev <agent>: <message>`` — returns ParsedCommand with agent_name

    Mentions that appear inside backtick-wrapped inline code or fenced code
    blocks are **ignored** — they are treated as literal text, not commands.
    This mirrors GitHub's own behaviour (backtick @mentions don't notify).

    Returns None if the comment doesn't match any command syntax.
    """
    return _DEFAULT_PARSER.parse(text)


# Module-level default parser (backward compat — uses hardcoded known_agents)
_DEFAULT_PARSER = CommandParser(known_agents=set(_DEFAULT_KNOWN_AGENTS))
