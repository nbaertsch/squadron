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
    # Framework (agent lifecycle)
    "check_for_events",
    "report_blocked",
    "report_complete",
    "create_blocker_issue",
    "escalate_to_human",
    "submit_pr_review",
    "open_pr",
    "git_push",
    # Issue management
    "create_issue",
    "assign_issue",
    "label_issue",
    "read_issue",
    "close_issue",
    "update_issue",
    # PR context
    "list_pr_files",
    "get_pr_details",
    "get_pr_feedback",
    "merge_pr",
    # Repository context
    "get_ci_status",
    "get_repo_info",
    "delete_branch",
    # Introspection
    "check_registry",
    "get_recent_history",
    "list_agent_roles",
    # Listing
    "list_issues",
    "list_pull_requests",
    "list_issue_comments",
    # Communication
    "comment_on_issue",
]

# O(1) lookup set for splitting .md tool lists into custom vs SDK built-in
ALL_TOOL_NAMES_SET = frozenset(ALL_TOOL_NAMES)


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


class GitPushParams(BaseModel):
    """Push commits to the remote repository."""

    force: bool = Field(
        default=False,
        description="Use force push (--force-with-lease). Only use if explicitly needed.",
    )


class ListPRFilesParams(BaseModel):
    """List files changed in a pull request."""

    pr_number: int = Field(description="The pull request number")


class GetPRDetailsParams(BaseModel):
    """Get detailed information about a pull request."""

    pr_number: int = Field(description="The pull request number")


class GetCIStatusParams(BaseModel):
    """Get CI/check status for a commit or branch."""

    ref: str = Field(description="Git ref to check (commit SHA or branch name)")


class CloseIssueParams(BaseModel):
    """Close a GitHub issue."""

    issue_number: int = Field(description="The GitHub issue number to close")
    comment: str = Field(default="", description="Optional comment to post before closing")


class UpdateIssueParams(BaseModel):
    """Update a GitHub issue's fields."""

    issue_number: int = Field(description="The GitHub issue number to update")
    title: str | None = Field(default=None, description="New title (or None to keep current)")
    body: str | None = Field(default=None, description="New body (or None to keep current)")
    state: str | None = Field(
        default=None, description="'open' or 'closed' (or None to keep current)"
    )
    labels: list[str] | None = Field(
        default=None, description="New labels (or None to keep current)"
    )


class MergePRParams(BaseModel):
    """Merge a pull request."""

    pr_number: int = Field(description="The pull request number to merge")
    merge_method: str = Field(
        default="squash",
        description="Merge method: 'merge', 'squash', or 'rebase'",
    )
    commit_title: str | None = Field(default=None, description="Custom commit title (optional)")
    commit_message: str | None = Field(default=None, description="Custom commit message (optional)")


class DeleteBranchParams(BaseModel):
    """Delete a branch from the repository."""

    branch: str = Field(description="Branch name to delete (not the full ref, just the name)")


class GetRepoInfoParams(BaseModel):
    """Get repository information."""

    pass


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
        git_push_callback: Callable[[AgentRecord, bool], Awaitable[tuple[int, str, str]]]
        | None = None,
        auto_merge_callback: Callable[[int], Awaitable[None]] | None = None,
    ):
        self.registry = registry
        self.github = github
        self.agent_inboxes = agent_inboxes
        self.owner = owner
        self.repo = repo
        self.config = config
        self.agent_definitions = agent_definitions or {}
        self._pre_sleep_hook = pre_sleep_hook
        self._git_push_callback = git_push_callback
        self._auto_merge_callback = auto_merge_callback

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
        """Submit a review on a pull request.

        Records the review in the approval tracking system and triggers
        auto-merge if all requirements are satisfied.
        """
        # Submit review to GitHub
        result = await self.github.submit_pr_review(
            self.owner,
            self.repo,
            params.pr_number,
            body=params.body,
            event=params.event,
            comments=params.comments if params.comments else None,
        )
        review_id = result.get("id", "unknown")

        # Map GitHub event to our approval state
        state_map = {
            "APPROVE": "approved",
            "REQUEST_CHANGES": "changes_requested",
            "COMMENT": None,  # Comments don't affect approval state
        }
        approval_state = state_map.get(params.event.upper())

        # Record approval in database if it's an approval-relevant review
        merge_status = ""
        if approval_state:
            agent = await self.registry.get_agent(agent_id)
            if agent:
                await self.registry.record_pr_approval(
                    pr_number=params.pr_number,
                    agent_role=agent.role,
                    agent_id=agent_id,
                    state=approval_state,
                    review_body=params.body,
                )

                # Check if PR is now ready for auto-merge
                is_ready, missing = await self.registry.check_pr_merge_ready(params.pr_number)
                if is_ready and self._auto_merge_callback:
                    logger.info("PR #%d ready for auto-merge, triggering merge", params.pr_number)
                    try:
                        await self._auto_merge_callback(params.pr_number)
                        merge_status = " PR is ready for merge — auto-merge triggered."
                    except Exception as e:
                        logger.exception("Auto-merge failed for PR #%d", params.pr_number)
                        merge_status = f" Auto-merge failed: {e}"
                elif not is_ready:
                    merge_status = f" Merge blocked: {', '.join(missing)}"

        return f"Submitted {params.event} review (id={review_id}) on PR #{params.pr_number}.{merge_status}"

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

    async def git_push(self, agent_id: str, params: GitPushParams) -> str:
        """Push commits to the remote repository using GitHub App authentication.

        This tool provides authenticated git push without exposing credentials
        to the agent's bash environment. Only agents with this tool explicitly
        granted can push code.
        """
        if not self._git_push_callback:
            return "Error: git_push not configured — contact system administrator"

        agent = await self.registry.get_agent(agent_id)
        if agent is None:
            return f"Error: agent {agent_id} not found"

        if not agent.worktree_path:
            return "Error: no worktree configured for this agent"

        if not agent.branch:
            return "Error: no branch configured for this agent"

        try:
            returncode, stdout, stderr = await self._git_push_callback(agent, params.force)
            if returncode == 0:
                return f"Successfully pushed branch `{agent.branch}` to origin"
            else:
                return f"Push failed (exit {returncode}): {stderr or stdout}"
        except Exception as e:
            logger.exception("git_push failed for agent %s", agent_id)
            return f"Push failed: {e}"

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
        """Read a GitHub issue's full details including all comments with usernames."""
        # Fetch issue details
        issue = await self.github.get_issue(self.owner, self.repo, params.issue_number)
        
        # Fetch all comments
        comments = await self.github.list_issue_comments(
            self.owner, self.repo, params.issue_number, per_page=100
        )
        
        # Format basic issue details
        labels = ", ".join(lbl.get("name", "") for lbl in issue.get("labels", []))
        assignees = ", ".join(a.get("login", "") for a in issue.get("assignees", []))
        issue_creator = issue.get("user", {}).get("login", "unknown")
        created_at = issue.get("created_at", "")[:16] if issue.get("created_at") else "unknown"
        
        # Start with issue details
        result_parts = [
            f"**#{issue['number']}:** {issue.get('title', 'N/A')}",
            f"**State:** {issue.get('state', 'unknown')}",
            f"**Created by:** {issue_creator} ({created_at})",
            f"**Labels:** {labels or 'none'}",
            f"**Assignees:** {assignees or 'none'}",
            f"**Body:**\n{issue.get('body', '') or '(empty)'}"
        ]
        
        # Add comments section if any exist
        if comments:
            result_parts.append(f"\n**Comments ({len(comments)}):**")
            for comment in comments:
                comment_user = comment.get("user", {}).get("login", "unknown")
                comment_created = comment.get("created_at", "")[:16] if comment.get("created_at") else "unknown"
                comment_body = comment.get("body", "").strip()
                
                result_parts.append(f"\n**{comment_user}** ({comment_created}):")
                result_parts.append(comment_body if comment_body else "(empty comment)")
        else:
            result_parts.append("\n**Comments:** None")
        
        return "\n".join(result_parts)


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

    # ── GitHub Context Tools ──────────────────────────────────────────────

    async def list_pr_files(self, agent_id: str, params: ListPRFilesParams) -> str:
        """List files changed in a pull request with diff stats."""
        files = await self.github.list_pull_request_files(self.owner, self.repo, params.pr_number)
        if not files:
            return f"No files changed in PR #{params.pr_number}."

        lines = [f"**{len(files)} file(s) changed in PR #{params.pr_number}:**\n"]
        total_additions = 0
        total_deletions = 0

        for f in files:
            filename = f.get("filename", "unknown")
            status = f.get("status", "?")
            additions = f.get("additions", 0)
            deletions = f.get("deletions", 0)
            total_additions += additions
            total_deletions += deletions

            # Status indicators
            status_icon = {"added": "+", "removed": "-", "modified": "~", "renamed": "→"}.get(
                status, "?"
            )
            lines.append(f"  {status_icon} `{filename}` (+{additions}/-{deletions})")

            # Include patch preview for small changes (first 500 chars)
            patch = f.get("patch", "")
            if patch and len(patch) < 1000:
                # Show first few lines of patch
                patch_lines = patch.split("\n")[:10]
                if patch_lines:
                    lines.append("    ```diff")
                    for pl in patch_lines:
                        lines.append(f"    {pl}")
                    if len(patch.split("\n")) > 10:
                        lines.append("    ... (truncated)")
                    lines.append("    ```")

        lines.append(f"\n**Total:** +{total_additions}/-{total_deletions}")
        return "\n".join(lines)

    async def get_pr_details(self, agent_id: str, params: GetPRDetailsParams) -> str:
        """Get detailed information about a pull request."""
        pr = await self.github.get_pull_request(self.owner, self.repo, params.pr_number)

        # Extract key info
        title = pr.get("title", "N/A")
        state = pr.get("state", "unknown")
        merged = pr.get("merged", False)
        mergeable = pr.get("mergeable")
        mergeable_state = pr.get("mergeable_state", "unknown")
        draft = pr.get("draft", False)

        head_ref = pr.get("head", {}).get("ref", "?")
        head_sha = pr.get("head", {}).get("sha", "?")[:8]
        base_ref = pr.get("base", {}).get("ref", "?")

        user = pr.get("user", {}).get("login", "unknown")
        labels = ", ".join(lbl.get("name", "") for lbl in pr.get("labels", []))
        body = pr.get("body", "") or "(no description)"

        additions = pr.get("additions", 0)
        deletions = pr.get("deletions", 0)
        changed_files = pr.get("changed_files", 0)

        # Build response
        lines = [
            f"## PR #{params.pr_number}: {title}",
            f"**Author:** {user}",
            f"**State:** {state}" + (" (MERGED)" if merged else "") + (" (DRAFT)" if draft else ""),
            f"**Branch:** `{head_ref}` ({head_sha}) → `{base_ref}`",
            f"**Mergeable:** {mergeable} ({mergeable_state})",
            f"**Labels:** {labels or 'none'}",
            f"**Changes:** {changed_files} files, +{additions}/-{deletions}",
            "",
            "**Description:**",
            body[:2000] + ("..." if len(body) > 2000 else ""),
        ]

        return "\n".join(lines)

    async def get_ci_status(self, agent_id: str, params: GetCIStatusParams) -> str:
        """Get CI/check status for a commit or branch."""
        lines = [f"**CI Status for `{params.ref}`:**\n"]

        try:
            # Get combined commit status (legacy status API)
            status = await self.github.get_combined_status(self.owner, self.repo, params.ref)
            overall_state = status.get("state", "unknown")
            statuses = status.get("statuses", [])

            lines.append(f"**Overall Status:** {overall_state.upper()}\n")

            if statuses:
                lines.append("### Status Checks")
                for s in statuses:
                    context = s.get("context", "unknown")
                    state = s.get("state", "?")
                    desc = s.get("description", "")
                    icon = {"success": "✅", "failure": "❌", "pending": "⏳", "error": "⚠️"}.get(
                        state, "❓"
                    )
                    lines.append(f"  {icon} **{context}**: {state}")
                    if desc:
                        lines.append(f"     {desc}")

            # Get check runs (newer checks API)
            check_runs = await self.github.list_check_runs(self.owner, self.repo, params.ref)
            if check_runs:
                lines.append("\n### Check Runs")
                for c in check_runs:
                    name = c.get("name", "unknown")
                    status_val = c.get("status", "?")
                    conclusion = c.get("conclusion", "pending")
                    icon = {
                        "success": "✅",
                        "failure": "❌",
                        "neutral": "⚪",
                        "cancelled": "🚫",
                        "skipped": "⏭️",
                        "timed_out": "⏱️",
                        "action_required": "🔔",
                    }.get(conclusion or "pending", "⏳")
                    lines.append(f"  {icon} **{name}**: {status_val} → {conclusion or 'pending'}")

            if not statuses and not check_runs:
                lines.append("No status checks or check runs found.")

        except Exception as e:
            logger.debug("Failed to fetch CI status for %s", params.ref, exc_info=True)
            lines.append(f"Error fetching CI status: {e}")

        return "\n".join(lines)

    async def close_issue(self, agent_id: str, params: CloseIssueParams) -> str:
        """Close a GitHub issue."""
        agent = await self.registry.get_agent(agent_id)
        prefix = self._agent_signature(agent.role) if agent else ""

        # Post closing comment if provided
        if params.comment:
            await self.github.comment_on_issue(
                self.owner,
                self.repo,
                params.issue_number,
                f"{prefix}{params.comment}",
            )

        await self.github.close_issue(self.owner, self.repo, params.issue_number)
        return f"Closed issue #{params.issue_number}"

    async def update_issue(self, agent_id: str, params: UpdateIssueParams) -> str:
        """Update a GitHub issue's fields."""
        await self.github.update_issue(
            self.owner,
            self.repo,
            params.issue_number,
            title=params.title,
            body=params.body,
            state=params.state,
            labels=params.labels,
        )

        updates = []
        if params.title is not None:
            updates.append(f"title='{params.title[:30]}...'")
        if params.body is not None:
            updates.append("body updated")
        if params.state is not None:
            updates.append(f"state={params.state}")
        if params.labels is not None:
            updates.append(f"labels={params.labels}")

        return f"Updated issue #{params.issue_number}: {', '.join(updates)}"

    async def merge_pr(self, agent_id: str, params: MergePRParams) -> str:
        """Merge a pull request."""
        try:
            result = await self.github.merge_pull_request(
                self.owner,
                self.repo,
                params.pr_number,
                merge_method=params.merge_method,
                commit_title=params.commit_title,
                commit_message=params.commit_message,
            )
            sha = result.get("sha", "unknown")[:8]
            return f"Merged PR #{params.pr_number} via {params.merge_method} (commit: {sha})"
        except Exception as e:
            error_msg = str(e)
            if "409" in error_msg or "conflict" in error_msg.lower():
                return f"Merge failed: PR #{params.pr_number} has merge conflicts. Resolve conflicts and try again."
            elif "405" in error_msg:
                return f"Merge failed: PR #{params.pr_number} is not mergeable (may need reviews or CI to pass)."
            else:
                return f"Merge failed for PR #{params.pr_number}: {e}"

    async def delete_branch(self, agent_id: str, params: DeleteBranchParams) -> str:
        """Delete a branch from the repository."""
        success = await self.github.delete_branch(self.owner, self.repo, params.branch)
        if success:
            return f"Deleted branch `{params.branch}`"
        else:
            return f"Branch `{params.branch}` not found (may already be deleted)"

    async def get_repo_info(self, agent_id: str, params: GetRepoInfoParams) -> str:
        """Get repository information."""
        repo = await self.github.get_repo(self.owner, self.repo)

        name = repo.get("full_name", f"{self.owner}/{self.repo}")
        description = repo.get("description", "(no description)")
        default_branch = repo.get("default_branch", "main")
        visibility = repo.get("visibility", "unknown")
        language = repo.get("language", "unknown")

        open_issues = repo.get("open_issues_count", 0)
        forks = repo.get("forks_count", 0)
        stars = repo.get("stargazers_count", 0)

        topics = ", ".join(repo.get("topics", [])) or "none"

        lines = [
            f"## Repository: {name}",
            f"**Description:** {description}",
            f"**Default Branch:** `{default_branch}`",
            f"**Visibility:** {visibility}",
            f"**Language:** {language}",
            f"**Stats:** {stars} stars, {forks} forks, {open_issues} open issues",
            f"**Topics:** {topics}",
        ]

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
    ) -> list:
        """Return SDK-compatible Tool objects for the specified tool names.

        Args:
            agent_id: The agent these tools are bound to.
            names: Explicit list of tool names to include. If None or empty,
                   returns no Squadron tools (agent must declare tools in frontmatter).
        """
        if not names:
            return []

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
            "git_push",
            "Push your committed changes to the remote repository. Use this before opening a PR. Only available to agents with push permissions.",
            GitPushParams,
            tools.git_push,
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
            "Read a GitHub issue's full details including title, body, labels, assignees, and all comments with usernames.",
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
        _register(
            "list_pr_files",
            "List files changed in a pull request with diff stats and patch previews. Essential for code review.",
            ListPRFilesParams,
            tools.list_pr_files,
        )
        _register(
            "get_pr_details",
            "Get detailed PR information including mergeable state, head/base branches, and description.",
            GetPRDetailsParams,
            tools.get_pr_details,
        )
        _register(
            "get_ci_status",
            "Get CI/check status for a commit SHA or branch. Shows all status checks and check runs.",
            GetCIStatusParams,
            tools.get_ci_status,
        )
        _register(
            "close_issue",
            "Close a GitHub issue, optionally posting a closing comment.",
            CloseIssueParams,
            tools.close_issue,
        )
        _register(
            "update_issue",
            "Update a GitHub issue's title, body, state, or labels.",
            UpdateIssueParams,
            tools.update_issue,
        )
        _register(
            "merge_pr",
            "Merge a pull request using squash, merge, or rebase method.",
            MergePRParams,
            tools.merge_pr,
        )
        _register(
            "delete_branch",
            "Delete a branch from the repository (e.g., after PR merge).",
            DeleteBranchParams,
            tools.delete_branch,
        )
        _register(
            "get_repo_info",
            "Get repository information including default branch, visibility, and stats.",
            GetRepoInfoParams,
            tools.get_repo_info,
        )

        # Build and return only the requested tools
        result = []
        for name in names:
            builder = tool_builders.get(name)
            if builder:
                result.append(builder())

        return result
