"""Unified Squadron tools — all custom tools agents can call.

Merges the former FrameworkTools and PMTools into a single registry
with per-tool selection (D-7). Each tool is a standalone function,
and `get_tools(agent_id, names)` returns only the requested subset.

Tools:
  Framework (agent lifecycle):
    - check_for_events: Agent checks its inbox for pending events
    - report_blocked: Agent declares it's blocked on another issue
    - report_complete: Agent declares its task is done
    - create_blocker_issue: Agent creates a new blocking issue
    - escalate_to_human: Agent escalates to human maintainer
    - submit_pr_review: Agent submits a PR review
    - open_pr: Agent opens a pull request

  PM (issue management):
    - create_issue: Create a new GitHub issue
    - assign_issue: Assign an issue to users
    - label_issue: Apply labels to an issue
    - read_issue: Read an issue's details
    - check_registry: Query agent registry status

  Shared:
    - comment_on_issue: Post a comment on a GitHub issue
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from copilot import define_tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    import asyncio

    from squadron.config import AgentDefinition, SquadronConfig
    from squadron.github_client import GitHubClient
    from squadron.models import AgentRecord
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)

# All available tool names for validation and documentation
ALL_TOOL_NAMES = [
    "check_for_events",
    "report_blocked",
    "report_complete",
    "create_blocker_issue",
    "escalate_to_human",
    "comment_on_issue",
    "submit_pr_review",
    "open_pr",
    "create_issue",
    "assign_issue",
    "label_issue",
    "read_issue",
    "check_registry",
    "get_recent_history",
    "list_agent_roles",
    "get_pr_feedback",
    "list_issues",
    "list_pull_requests",
    "list_issue_comments",
]

# O(1) lookup set for splitting .md tool lists into custom vs SDK built-in
ALL_TOOL_NAMES_SET = frozenset(ALL_TOOL_NAMES)

# Default tool sets by lifecycle type (used when no explicit tools list is configured)
DEFAULT_TOOLS_PERSISTENT = [
    "check_for_events",
    "report_blocked",
    "report_complete",
    "create_blocker_issue",
    "escalate_to_human",
    "comment_on_issue",
    "submit_pr_review",
    "open_pr",
]

DEFAULT_TOOLS_EPHEMERAL = [
    "create_issue",
    "assign_issue",
    "label_issue",
    "comment_on_issue",
    "read_issue",
    "check_registry",
    "escalate_to_human",
    "report_complete",
]


# ── Tool Parameter Models ────────────────────────────────────────────────────


class CheckEventsParams(BaseModel):
    """No parameters needed."""

    pass


class ReportBlockedParams(BaseModel):
    blocker_issue: int = Field(description="The GitHub issue number that blocks this work")
    reason: str = Field(description="Why this issue blocks your current task")


class ReportCompleteParams(BaseModel):
    summary: str = Field(description="Summary of what was accomplished")


class CreateBlockerIssueParams(BaseModel):
    title: str = Field(description="Issue title")
    body: str = Field(description="Issue body describing the blocker")
    labels: list[str] = Field(default_factory=list, description="Labels to apply")


class EscalateToHumanParams(BaseModel):
    reason: str = Field(description="Why this needs human attention")
    category: str = Field(
        default="general",
        description="Escalation category: 'architectural', 'policy', 'ambiguous', 'security', 'general'",
    )


class CommentOnIssueParams(BaseModel):
    issue_number: int = Field(description="The GitHub issue number to comment on")
    body: str = Field(description="Comment body (markdown supported)")


class SubmitPRReviewParams(BaseModel):
    pr_number: int = Field(description="The pull request number to review")
    body: str = Field(description="Overall review comment")
    event: str = Field(
        default="COMMENT",
        description="Review decision: 'APPROVE', 'REQUEST_CHANGES', or 'COMMENT'",
    )
    comments: list[dict] = Field(
        default_factory=list,
        description="Inline review comments. Each entry: {'path': str, 'position': int, 'body': str}",
    )


class OpenPRParams(BaseModel):
    title: str = Field(description="Pull request title")
    body: str = Field(
        description="Pull request body (markdown). Reference the issue with 'Fixes #N'."
    )
    head: str = Field(description="Source branch name (the branch with your changes)")
    base: str = Field(description="Target branch name (usually 'main')")


class CreateIssueParams(BaseModel):
    title: str = Field(description="Issue title")
    body: str = Field(description="Issue body describing the task or problem")
    labels: list[str] = Field(default_factory=list, description="Labels to apply")


class AssignIssueParams(BaseModel):
    issue_number: int = Field(description="The GitHub issue number to assign")
    assignees: list[str] = Field(
        default_factory=lambda: ["squadron-dev[bot]"],
        description="GitHub usernames to assign. Default: squadron-dev[bot]",
    )


class LabelIssueParams(BaseModel):
    issue_number: int = Field(description="The GitHub issue number to label")
    labels: list[str] = Field(description="Labels to apply to the issue")


class ReadIssueParams(BaseModel):
    issue_number: int = Field(description="The GitHub issue number to read")


class CheckRegistryParams(BaseModel):
    """No parameters needed — returns all active agents."""

    pass


class GetRecentHistoryParams(BaseModel):
    limit: int = Field(default=10, description="Number of recent agents to return (max 50)")


class ListAgentRolesParams(BaseModel):
    """No parameters needed — returns configured agent roles."""

    pass


class GetPRFeedbackParams(BaseModel):
    pr_number: int = Field(description="The pull request number to get feedback for")


class ListIssuesParams(BaseModel):
    state: str = Field(default="open", description="Filter: 'open', 'closed', or 'all'")
    labels: str = Field(default="", description="Comma-separated label filter, e.g. 'bug,critical'")


class ListPullRequestsParams(BaseModel):
    state: str = Field(default="open", description="Filter: 'open', 'closed', or 'all'")


class ListIssueCommentsParams(BaseModel):
    issue_number: int = Field(description="The GitHub issue number to read comments from")
    limit: int = Field(default=20, description="Number of comments to return (most recent last)")


# ── Unified Tool Implementations ─────────────────────────────────────────────


class SquadronTools:
    """Unified container for all Squadron tool implementations.

    Replaces the former FrameworkTools + PMTools split. All 13 tools
    live here, and `get_tools(agent_id, names)` returns only the
    requested subset based on the role's config.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        github: GitHubClient,
        agent_inboxes: dict[str, asyncio.Queue],
        owner: str,
        repo: str,
        *,
        config: SquadronConfig | None = None,
        agent_definitions: dict[str, AgentDefinition] | None = None,
        pre_sleep_hook: Callable[[AgentRecord], Awaitable[None]] | None = None,
    ):
        self.registry = registry
        self.github = github
        self.agent_inboxes = agent_inboxes
        self.owner = owner
        self.repo = repo
        self.config = config
        self.agent_definitions = agent_definitions or {}
        self._pre_sleep_hook = pre_sleep_hook

    def _agent_signature(self, role: str) -> str:
        """Build the agent signature prefix: emoji + display_name on its own line.

        Format: "🎯 **Project Manager**\n\n"
        Falls back to "🤖 **role**\n\n" if agent definition not found.
        """
        agent_def = self.agent_definitions.get(role)
        if agent_def:
            emoji = agent_def.emoji
            display_name = agent_def.display_name or role
        else:
            emoji = "🤖"
            display_name = role
        return f"{emoji} **{display_name}**\n\n"

    # ── Framework Tools (agent lifecycle) ────────────────────────────────

    async def check_for_events(self, agent_id: str, params: CheckEventsParams) -> str:
        """Check for pending framework events in the agent's inbox."""
        inbox = self.agent_inboxes.get(agent_id)
        if not inbox or inbox.empty():
            return "No pending events."

        events = []
        while not inbox.empty():
            event = inbox.get_nowait()
            events.append(
                f"- [{event.event_type.value}] issue=#{event.issue_number} pr=#{event.pr_number}"
            )

        return "Pending events:\n" + "\n".join(events)

    async def report_blocked(self, agent_id: str, params: ReportBlockedParams) -> str:
        """Report that the agent is blocked on another issue."""
        agent = await self.registry.get_agent(agent_id)
        if agent is None:
            return f"Error: agent {agent_id} not found"

        # Check for cycles before adding blocker
        success = await self.registry.add_blocker(agent_id, params.blocker_issue)
        if not success:
            return (
                f"Error: adding blocker #{params.blocker_issue} would create a circular dependency. "
                "Please find an alternative approach or escalate to a human."
            )

        # Re-read agent after add_blocker modified blocked_by in DB
        agent = await self.registry.get_agent(agent_id)

        # WIP commit before sleep (3.1)
        if self._pre_sleep_hook and agent:
            try:
                await self._pre_sleep_hook(agent)
            except Exception:
                logger.warning("Pre-sleep hook failed for %s", agent_id)

        # Transition to SLEEPING
        from datetime import datetime, timezone

        from squadron.models import AgentStatus

        agent.status = AgentStatus.SLEEPING
        agent.sleeping_since = datetime.now(timezone.utc)
        agent.active_since = None
        await self.registry.update_agent(agent)

        # Post comment on the agent's issue
        if agent.issue_number:
            await self.github.comment_on_issue(
                self.owner,
                self.repo,
                agent.issue_number,
                f"{self._agent_signature(agent.role)}Blocked by #{params.blocker_issue}: {params.reason}\n\nGoing to sleep until the blocker is resolved.",
            )

        return (
            f"Blocker #{params.blocker_issue} registered. "
            "Your session will be saved. You will be resumed when the blocker is resolved. "
            "Stop working now — your session is being suspended."
        )

    async def report_complete(self, agent_id: str, params: ReportCompleteParams) -> str:
        """Report that the agent's task is complete."""
        agent = await self.registry.get_agent(agent_id)
        if agent is None:
            return f"Error: agent {agent_id} not found"

        from squadron.models import AgentStatus

        agent.status = AgentStatus.COMPLETED
        agent.active_since = None
        await self.registry.update_agent(agent)

        # Post completion comment
        if agent.issue_number:
            await self.github.comment_on_issue(
                self.owner,
                self.repo,
                agent.issue_number,
                f"{self._agent_signature(agent.role)}Task complete: {params.summary}",
            )

        return (
            "Task marked complete. Session will be cleaned up. "
            "Stop working now — your session is being terminated."
        )

    async def create_blocker_issue(self, agent_id: str, params: CreateBlockerIssueParams) -> str:
        """Create a new GitHub issue for a blocker the agent discovered."""
        agent = await self.registry.get_agent(agent_id)
        if agent is None:
            return f"Error: agent {agent_id} not found"

        # Create the issue
        body = f"{params.body}\n\n---\n_Blocking #{agent.issue_number} ({agent.agent_id})_"
        new_issue = await self.github.create_issue(
            self.owner,
            self.repo,
            title=params.title,
            body=body,
            labels=params.labels,
        )
        new_issue_number = new_issue["number"]

        # Register blocker
        success = await self.registry.add_blocker(agent_id, new_issue_number)
        if not success:
            return f"Created issue #{new_issue_number} but cannot block on it (would create cycle)."

        # Re-read agent after add_blocker modified blocked_by in DB
        agent = await self.registry.get_agent(agent_id)

        # WIP commit before sleep (3.1)
        if self._pre_sleep_hook and agent:
            try:
                await self._pre_sleep_hook(agent)
            except Exception:
                logger.warning("Pre-sleep hook failed for %s", agent_id)

        # Transition to SLEEPING
        from datetime import datetime, timezone

        from squadron.models import AgentStatus

        agent.status = AgentStatus.SLEEPING
        agent.sleeping_since = datetime.now(timezone.utc)
        agent.active_since = None
        await self.registry.update_agent(agent)

        # Comment on original issue
        if agent.issue_number:
            await self.github.comment_on_issue(
                self.owner,
                self.repo,
                agent.issue_number,
                f"{self._agent_signature(agent.role)}Discovered a blocker — created #{new_issue_number}: {params.title}\n\nGoing to sleep until it's resolved.",
            )

        return (
            f"Created issue #{new_issue_number}. You are now blocked on it. "
            "Your session will be saved. Stop working now — your session is being suspended."
        )

    async def escalate_to_human(self, agent_id: str, params: EscalateToHumanParams) -> str:
        """Escalate the current task to a human maintainer."""
        agent = await self.registry.get_agent(agent_id)
        if agent is None:
            return f"Error: agent {agent_id} not found"

        from squadron.models import AgentStatus

        agent.status = AgentStatus.ESCALATED
        agent.active_since = None
        await self.registry.update_agent(agent)

        if agent.issue_number:
            try:
                await self.github.add_labels(
                    self.owner,
                    self.repo,
                    agent.issue_number,
                    ["needs-human", f"escalation:{params.category}"],
                )
            except Exception:
                logger.warning("Failed to add escalation labels to #%d", agent.issue_number)

            await self.github.comment_on_issue(
                self.owner,
                self.repo,
                agent.issue_number,
                (
                    f"{self._agent_signature(agent.role)}⚠️ **Escalation — needs human attention**\n\n"
                    f"**Category:** {params.category}\n"
                    f"**Reason:** {params.reason}\n\n"
                    "This task has been escalated and the agent has stopped. "
                    "A human maintainer should review and take action."
                ),
            )

        return (
            "Task escalated to human maintainers. The issue has been labeled 'needs-human'. "
            "Stop working now — your session is being terminated."
        )

    async def submit_pr_review(self, agent_id: str, params: SubmitPRReviewParams) -> str:
        """Submit a review on a pull request."""
        result = await self.github.submit_pr_review(
            self.owner,
            self.repo,
            params.pr_number,
            body=params.body,
            event=params.event,
            comments=params.comments if params.comments else None,
        )
        review_id = result.get("id", "unknown")
        return f"Submitted {params.event} review (id={review_id}) on PR #{params.pr_number}"

    async def open_pr(self, agent_id: str, params: OpenPRParams) -> str:
        """Open a new pull request."""
        result = await self.github.create_pull_request(
            self.owner,
            self.repo,
            title=params.title,
            body=params.body,
            head=params.head,
            base=params.base,
        )
        pr_number = result.get("number", "unknown")

        # Record the PR number on the agent so sleep/wake triggers can match
        agent = await self.registry.get_agent(agent_id)
        if agent and isinstance(pr_number, int):
            agent.pr_number = pr_number
            await self.registry.update_agent(agent)

        return f"Opened PR #{pr_number}: {params.title}"

    # ── PM Tools (issue management) ──────────────────────────────────────

    async def create_issue(self, agent_id: str, params: CreateIssueParams) -> str:
        """Create a new GitHub issue."""
        result = await self.github.create_issue(
            self.owner,
            self.repo,
            title=params.title,
            body=params.body,
            labels=params.labels,
        )
        return f"Created issue #{result['number']}: {params.title}"

    async def assign_issue(self, agent_id: str, params: AssignIssueParams) -> str:
        """Assign a GitHub issue to one or more users."""
        await self.github.assign_issue(
            self.owner,
            self.repo,
            params.issue_number,
            params.assignees,
        )
        return f"Assigned #{params.issue_number} to {', '.join(params.assignees)}"

    async def label_issue(self, agent_id: str, params: LabelIssueParams) -> str:
        """Apply labels to a GitHub issue."""
        await self.github.add_labels(
            self.owner,
            self.repo,
            params.issue_number,
            params.labels,
        )
        return f"Applied labels {params.labels} to #{params.issue_number}"

    async def read_issue(self, agent_id: str, params: ReadIssueParams) -> str:
        """Read a GitHub issue's full details."""
        issue = await self.github.get_issue(self.owner, self.repo, params.issue_number)
        labels = ", ".join(lbl.get("name", "") for lbl in issue.get("labels", []))
        assignees = ", ".join(a.get("login", "") for a in issue.get("assignees", []))
        return (
            f"**#{issue['number']}:** {issue.get('title', 'N/A')}\n"
            f"**State:** {issue.get('state', 'unknown')}\n"
            f"**Labels:** {labels or 'none'}\n"
            f"**Assignees:** {assignees or 'none'}\n"
            f"**Body:**\n{issue.get('body', '') or '(empty)'}"
        )

    async def check_registry(self, agent_id: str, params: CheckRegistryParams) -> str:
        """Query the agent registry for active agents and their status."""
        agents = await self.registry.get_all_active_agents()
        if not agents:
            return "No active agents in the registry."

        lines = [f"**Active agents:** {len(agents)}\n"]
        for agent in agents:
            blockers = f" (blocked by: {agent.blocked_by})" if agent.blocked_by else ""
            lines.append(
                f"- `{agent.agent_id}` [{agent.role}] "
                f"status={agent.status.value} issue=#{agent.issue_number}{blockers}"
            )
        return "\n".join(lines)

    # ── Introspection Tools ────────────────────────────────────────────────

    async def get_recent_history(self, agent_id: str, params: GetRecentHistoryParams) -> str:
        """Get recently completed/failed/escalated agents."""
        limit = min(params.limit, 50)
        agents = await self.registry.get_recent_agents(limit=limit)
        if not agents:
            return "No recently completed agents."

        lines = [f"**Recent agent history** (last {len(agents)}):\n"]
        lines.append("| Agent | Role | Issue | Outcome | Finished |")
        lines.append("|-------|------|-------|---------|----------|")
        for a in agents:
            finished = a.updated_at.strftime("%Y-%m-%d %H:%M") if a.updated_at else "?"
            lines.append(
                f"| {a.agent_id} | {a.role} | #{a.issue_number} | {a.status.value} | {finished} |"
            )
        return "\n".join(lines)

    async def list_agent_roles(self, agent_id: str, params: ListAgentRolesParams) -> str:
        """List all configured agent roles, their triggers, and lifecycle type."""
        if not self.config:
            return "Error: config not available"

        lines = ["**Configured agent roles:**\n"]
        for role_name, role_config in self.config.agent_roles.items():
            lifecycle = role_config.lifecycle if hasattr(role_config, "lifecycle") else "persistent"
            singleton = "yes" if role_config.singleton else "no"
            trigger_info = ", ".join(
                f"{t.event}" + (f"[{t.label}]" if t.label else "") for t in role_config.triggers
            )
            lines.append(f"- **{role_name}** (lifecycle={lifecycle}, singleton={singleton})")
            lines.append(f"  Mention: `@{role_name}` or `/{role_name}`")
            if trigger_info:
                lines.append(f"  Triggers: {trigger_info}")
            if role_config.subagents:
                lines.append(f"  Subagents: {', '.join(role_config.subagents)}")
        return "\n".join(lines)

    async def get_pr_feedback(self, agent_id: str, params: GetPRFeedbackParams) -> str:
        """Get review comments, review status, and changed files for a PR."""
        lines = [f"**PR #{params.pr_number} feedback:**\n"]

        try:
            # Reviews summary
            reviews = await self.github.get_pr_reviews(self.owner, self.repo, params.pr_number)
            if reviews:
                lines.append("### Reviews\n")
                for r in reviews:
                    user = r.get("user", {}).get("login", "unknown")
                    state = r.get("state", "?")
                    body = r.get("body", "") or ""
                    lines.append(f"- **{user}**: {state}")
                    if body:
                        lines.append(f"  {body[:500]}")

            # Inline review comments
            review_comments = await self.github.get_pr_review_comments(
                self.owner, self.repo, params.pr_number
            )
            if review_comments:
                lines.append("\n### Inline Comments\n")
                for c in review_comments:
                    path = c.get("path", "unknown")
                    line_num = c.get("line") or c.get("original_line", "?")
                    body = c.get("body", "")
                    user = c.get("user", {}).get("login", "unknown")
                    lines.append(f"- **{path}:{line_num}** ({user}): {body}")

            # Changed files
            changed = await self.github.list_pull_request_files(
                self.owner, self.repo, params.pr_number
            )
            if changed:
                lines.append("\n### Changed Files\n")
                for f in changed:
                    fname = f.get("filename", "unknown")
                    status = f.get("status", "?")
                    adds = f.get("additions", 0)
                    dels = f.get("deletions", 0)
                    lines.append(f"- {fname} ({status}, +{adds}/-{dels})")

        except Exception:
            logger.debug("Failed to fetch PR feedback for #%d", params.pr_number, exc_info=True)
            lines.append("Error fetching PR feedback.")

        return "\n".join(lines)

    async def list_issues(self, agent_id: str, params: ListIssuesParams) -> str:
        """List issues in the repository."""
        issues = await self.github.list_issues(
            self.owner,
            self.repo,
            state=params.state,
            labels=params.labels or None,
        )
        if not issues:
            return f"No {params.state} issues found."

        lines = [f"**{len(issues)} {params.state} issue(s):**\n"]
        for issue in issues[:50]:  # cap output
            number = issue.get("number", "?")
            title = issue.get("title", "N/A")
            labels = ", ".join(lbl.get("name", "") for lbl in issue.get("labels", []))
            assignees = ", ".join(a.get("login", "") for a in issue.get("assignees", []))
            lines.append(f"- **#{number}** {title}")
            if labels:
                lines.append(f"  Labels: {labels}")
            if assignees:
                lines.append(f"  Assignees: {assignees}")
        return "\n".join(lines)

    async def list_pull_requests(self, agent_id: str, params: ListPullRequestsParams) -> str:
        """List pull requests in the repository."""
        prs = await self.github.list_pull_requests(
            self.owner,
            self.repo,
            state=params.state,
        )
        if not prs:
            return f"No {params.state} pull requests found."

        lines = [f"**{len(prs)} {params.state} PR(s):**\n"]
        for pr in prs[:50]:
            number = pr.get("number", "?")
            title = pr.get("title", "N/A")
            user = pr.get("user", {}).get("login", "?")
            head = pr.get("head", {}).get("ref", "?")
            base = pr.get("base", {}).get("ref", "?")
            lines.append(f"- **#{number}** {title} ({user}, {head} → {base})")
        return "\n".join(lines)

    async def list_issue_comments(self, agent_id: str, params: ListIssueCommentsParams) -> str:
        """List comments on a GitHub issue."""
        comments = await self.github.list_issue_comments(
            self.owner, self.repo, params.issue_number, per_page=params.limit
        )
        if not comments:
            return f"No comments on issue #{params.issue_number}."

        lines = [f"**{len(comments)} comment(s) on #{params.issue_number}:**\n"]
        for c in comments:
            user = c.get("user", {}).get("login", "unknown")
            created = c.get("created_at", "?")[:16]
            body = c.get("body", "")[:500]
            lines.append(f"**{user}** ({created}):\n{body}\n")
        return "\n".join(lines)

    # ── Shared Tools ─────────────────────────────────────────────────────

    async def comment_on_issue(self, agent_id: str, params: CommentOnIssueParams) -> str:
        """Post a comment on a GitHub issue with agent signature."""
        agent = await self.registry.get_agent(agent_id)
        prefix = self._agent_signature(agent.role) if agent else ""

        await self.github.comment_on_issue(
            self.owner,
            self.repo,
            params.issue_number,
            f"{prefix}{params.body}",
        )
        return f"Posted comment on #{params.issue_number}"

    # ── Tool Selection ───────────────────────────────────────────────────

    def get_tools(
        self,
        agent_id: str,
        names: list[str] | None = None,
        *,
        is_stateless: bool = False,
    ) -> list:
        """Return SDK-compatible Tool objects for the specified tool names.

        Args:
            agent_id: The agent these tools are bound to.
            names: Explicit list of tool names to include. If None, falls
                   back to lifecycle-based defaults.
            is_stateless: Used for default selection when names is None.
                          True → ephemeral/PM defaults, False → persistent/dev defaults.
        """
        if names is None:
            names = DEFAULT_TOOLS_EPHEMERAL if is_stateless else DEFAULT_TOOLS_PERSISTENT

        # Validate requested tool names
        invalid = set(names) - set(ALL_TOOL_NAMES)
        if invalid:
            logger.warning(
                "Unknown tool names requested for agent %s: %s (available: %s)",
                agent_id,
                invalid,
                ALL_TOOL_NAMES,
            )
            names = [n for n in names if n in ALL_TOOL_NAMES]

        tools = self  # capture for closures

        # Build the tool registry — each tool is created only if requested
        tool_builders: dict[str, Any] = {}

        def _register(name: str, description: str, param_cls, impl):
            """Register a tool builder. The tool is only created if its name is in `names`."""

            def builder(pc=param_cls, desc=description, method=impl):
                async def tool_fn(params) -> str:
                    return await method(agent_id, params)

                # Set annotations with actual type objects to avoid
                # __future__.annotations stringification issues
                tool_fn.__annotations__ = {"params": pc, "return": str}
                tool_fn.__name__ = name
                tool_fn.__qualname__ = name
                return define_tool(description=desc)(tool_fn)

            tool_builders[name] = builder

        _register(
            "check_for_events",
            "Check for pending framework events (PR feedback, blocker resolutions, human messages). Call between major work phases.",
            CheckEventsParams,
            tools.check_for_events,
        )
        _register(
            "report_blocked",
            "Report that you are blocked on another GitHub issue. Your session will be saved and you will be resumed when the blocker is resolved.",
            ReportBlockedParams,
            tools.report_blocked,
        )
        _register(
            "report_complete",
            "Report that your assigned task is complete. Provide a summary of what was accomplished.",
            ReportCompleteParams,
            tools.report_complete,
        )
        _register(
            "create_blocker_issue",
            "Create a new GitHub issue for a blocker you discovered. You will be blocked on the new issue until it is resolved.",
            CreateBlockerIssueParams,
            tools.create_blocker_issue,
        )
        _register(
            "escalate_to_human",
            "Escalate the current task to a human maintainer. Use when you encounter architectural decisions, policy questions, ambiguous requirements, or security concerns that need human judgment.",
            EscalateToHumanParams,
            tools.escalate_to_human,
        )
        _register(
            "comment_on_issue",
            "Post a comment on a GitHub issue. Use to communicate progress, ask clarifying questions, or post status updates.",
            CommentOnIssueParams,
            tools.comment_on_issue,
        )
        _register(
            "submit_pr_review",
            "Submit a review on a pull request. Use 'APPROVE' to approve, 'REQUEST_CHANGES' to request changes, or 'COMMENT' for general feedback.",
            SubmitPRReviewParams,
            tools.submit_pr_review,
        )
        _register(
            "open_pr",
            "Open a new pull request from your working branch. Include a descriptive title, body referencing the issue (e.g. 'Fixes #42'), source branch, and target branch.",
            OpenPRParams,
            tools.open_pr,
        )
        _register(
            "create_issue",
            "Create a new GitHub issue for a blocker, sub-task, or new work item.",
            CreateIssueParams,
            tools.create_issue,
        )
        _register(
            "assign_issue",
            "Assign a GitHub issue to squadron-dev[bot] for tracking visibility. Labels are the actual agent spawn trigger.",
            AssignIssueParams,
            tools.assign_issue,
        )
        _register(
            "label_issue",
            "Apply labels to classify a GitHub issue (type, priority, etc.).",
            LabelIssueParams,
            tools.label_issue,
        )
        _register(
            "read_issue",
            "Read a GitHub issue's full details including title, body, labels, and assignees.",
            ReadIssueParams,
            tools.read_issue,
        )
        _register(
            "check_registry",
            "Query the agent registry to see all active agents and their current status.",
            CheckRegistryParams,
            tools.check_registry,
        )
        _register(
            "get_recent_history",
            "Get recently completed, failed, or escalated agents. Useful for understanding what work has been done and avoiding duplicates.",
            GetRecentHistoryParams,
            tools.get_recent_history,
        )
        _register(
            "list_agent_roles",
            "List all configured agent roles, their triggers, lifecycle type, and mention syntax. Use to understand which agents are available and how to invoke them.",
            ListAgentRolesParams,
            tools.list_agent_roles,
        )
        _register(
            "get_pr_feedback",
            "Get review comments, review status, and changed files for a pull request. Use when woken for PR review feedback.",
            GetPRFeedbackParams,
            tools.get_pr_feedback,
        )
        _register(
            "list_issues",
            "List issues in the repository, optionally filtered by state and labels.",
            ListIssuesParams,
            tools.list_issues,
        )
        _register(
            "list_pull_requests",
            "List pull requests in the repository, optionally filtered by state.",
            ListPullRequestsParams,
            tools.list_pull_requests,
        )
        _register(
            "list_issue_comments",
            "List comments on a GitHub issue. Use to read conversation history and context.",
            ListIssueCommentsParams,
            tools.list_issue_comments,
        )

        # Build and return only the requested tools
        result = []
        for name in names:
            builder = tool_builders.get(name)
            if builder:
                result.append(builder())

        return result
