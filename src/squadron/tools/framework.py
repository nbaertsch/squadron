"""Framework tools — functions that agents call via custom tool definitions.

These implement the agent ↔ framework bridge described in AD-017.
Each function is registered as a custom tool with the Copilot SDK
via the @define_tool decorator and Pydantic parameter models.

Tools:
- check_for_events: Agent checks its inbox for pending events
- report_blocked: Agent declares it's blocked on another issue
- report_complete: Agent declares its task is done
- create_blocker_issue: Agent creates a new blocking issue
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from copilot import define_tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    import asyncio

    from squadron.github_client import GitHubClient
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)


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


# ── Tool Implementations ─────────────────────────────────────────────────────


class FrameworkTools:
    """Container for framework tool implementations.

    Initialized with references to the registry, GitHub client, and
    agent inbox map. Each method corresponds to a tool the agent can call.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        github: GitHubClient,
        agent_inboxes: dict[str, asyncio.Queue],
        owner: str,
        repo: str,
    ):
        self.registry = registry
        self.github = github
        self.agent_inboxes = agent_inboxes
        self.owner = owner
        self.repo = repo

    async def check_for_events(self, agent_id: str, params: CheckEventsParams) -> str:
        """Check for pending framework events in the agent's inbox.

        Agents should call this between major work phases to receive:
        - PR feedback notifications
        - Blocker resolution notifications
        - Human messages
        - Reassignment notifications
        """
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
        """Report that the agent is blocked on another issue.

        The framework will:
        1. Register the blocker in the agent registry
        2. Transition agent to SLEEPING
        3. Post a comment on the agent's issue
        4. The post-turn state machine in _run_agent handles session cleanup
        """
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
                f"**[squadron:{agent.role}]** Blocked by #{params.blocker_issue}: {params.reason}\n\nGoing to sleep until the blocker is resolved.",
            )

        return (
            f"Blocker #{params.blocker_issue} registered. "
            "Your session will be saved. You will be resumed when the blocker is resolved. "
            "Stop working now — your session is being suspended."
        )

    async def report_complete(self, agent_id: str, params: ReportCompleteParams) -> str:
        """Report that the agent's task is complete.

        Sets status to COMPLETED. The post-turn state machine in _run_agent
        handles session destruction and resource cleanup.
        """
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
                f"**[squadron:{agent.role}]** Task complete: {params.summary}",
            )

        return (
            "Task marked complete. Session will be cleaned up. "
            "Stop working now — your session is being terminated."
        )

    async def create_blocker_issue(self, agent_id: str, params: CreateBlockerIssueParams) -> str:
        """Create a new GitHub issue for a blocker the agent discovered.

        The PM will triage the new issue. The current agent will be
        blocked until it's resolved.
        """
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

        # Transition to SLEEPING (same as report_blocked)
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
                f"**[squadron:{agent.role}]** Discovered a blocker — created #{new_issue_number}: {params.title}\n\nGoing to sleep until it's resolved.",
            )

        return (
            f"Created issue #{new_issue_number}. You are now blocked on it. "
            "Your session will be saved. Stop working now — your session is being suspended."
        )

    async def escalate_to_human(self, agent_id: str, params: EscalateToHumanParams) -> str:
        """Escalate the current task to a human maintainer.

        Marks the agent as ESCALATED, labels the issue 'needs-human',
        and posts a comment explaining why.
        """
        agent = await self.registry.get_agent(agent_id)
        if agent is None:
            return f"Error: agent {agent_id} not found"

        from squadron.models import AgentStatus

        agent.status = AgentStatus.ESCALATED
        agent.active_since = None
        await self.registry.update_agent(agent)

        if agent.issue_number:
            # Label the issue for human attention
            try:
                await self.github.add_labels(
                    self.owner,
                    self.repo,
                    agent.issue_number,
                    ["needs-human", f"escalation:{params.category}"],
                )
            except Exception:
                logger.warning("Failed to add escalation labels to #%d", agent.issue_number)

            # Post escalation comment
            await self.github.comment_on_issue(
                self.owner,
                self.repo,
                agent.issue_number,
                (
                    f"**[squadron:{agent.role}]** \u26a0\ufe0f **Escalation — needs human attention**\n\n"
                    f"**Category:** {params.category}\n"
                    f"**Reason:** {params.reason}\n\n"
                    "This task has been escalated and the agent has stopped. "
                    "A human maintainer should review and take action."
                ),
            )

        return (
            "Task escalated to human maintainers. The issue has been labeled 'needs-human'. "
            "Stop working now \u2014 your session is being terminated."
        )

    async def comment_on_issue(self, agent_id: str, params: CommentOnIssueParams) -> str:
        """Post a comment on a GitHub issue.

        Dev and review agents use this to communicate progress,
        ask clarifying questions, or post status updates.
        """
        agent = await self.registry.get_agent(agent_id)
        prefix = f"**[squadron:{agent.role}]** " if agent else ""

        await self.github.comment_on_issue(
            self.owner,
            self.repo,
            params.issue_number,
            f"{prefix}{params.body}",
        )
        return f"Posted comment on #{params.issue_number}"

    async def submit_pr_review(self, agent_id: str, params: SubmitPRReviewParams) -> str:
        """Submit a review on a pull request.

        Review agents use this to approve, request changes, or comment on PRs.
        """
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
        """Open a new pull request.

        Dev agents use this after completing their implementation to
        submit their work for review.
        """
        result = await self.github.create_pull_request(
            self.owner,
            self.repo,
            title=params.title,
            body=params.body,
            head=params.head,
            base=params.base,
        )
        pr_number = result.get("number", "unknown")
        return f"Opened PR #{pr_number}: {params.title}"

    def get_tools_for_agent(self, agent_id: str) -> list:
        """Return a list of SDK-compatible Tool objects bound to this agent.

        These are passed via the `tools` key in SessionConfig so the
        Copilot CLI exposes them to the agent as callable tools.
        """

        framework = self  # capture for closures

        @define_tool(
            description="Check for pending framework events (PR feedback, blocker resolutions, human messages). Call between major work phases."
        )
        async def check_for_events(params: CheckEventsParams) -> str:
            return await framework.check_for_events(agent_id, params)

        @define_tool(
            description="Report that you are blocked on another GitHub issue. Your session will be saved and you will be resumed when the blocker is resolved."
        )
        async def report_blocked(params: ReportBlockedParams) -> str:
            return await framework.report_blocked(agent_id, params)

        @define_tool(
            description="Report that your assigned task is complete. Provide a summary of what was accomplished."
        )
        async def report_complete(params: ReportCompleteParams) -> str:
            return await framework.report_complete(agent_id, params)

        @define_tool(
            description="Create a new GitHub issue for a blocker you discovered. You will be blocked on the new issue until it is resolved."
        )
        async def create_blocker_issue(params: CreateBlockerIssueParams) -> str:
            return await framework.create_blocker_issue(agent_id, params)

        @define_tool(
            description="Escalate the current task to a human maintainer. Use when you encounter architectural decisions, policy questions, ambiguous requirements, or security concerns that need human judgment."
        )
        async def escalate_to_human(params: EscalateToHumanParams) -> str:
            return await framework.escalate_to_human(agent_id, params)

        @define_tool(
            description="Post a comment on a GitHub issue. Use to communicate progress, ask clarifying questions, or post status updates."
        )
        async def comment_on_issue(params: CommentOnIssueParams) -> str:
            return await framework.comment_on_issue(agent_id, params)

        @define_tool(
            description="Submit a review on a pull request. Use 'APPROVE' to approve, 'REQUEST_CHANGES' to request changes, or 'COMMENT' for general feedback."
        )
        async def submit_pr_review(params: SubmitPRReviewParams) -> str:
            return await framework.submit_pr_review(agent_id, params)

        @define_tool(
            description="Open a new pull request from your working branch. Include a descriptive title, body referencing the issue (e.g. 'Fixes #42'), source branch, and target branch."
        )
        async def open_pr(params: OpenPRParams) -> str:
            return await framework.open_pr(agent_id, params)

        return [
            check_for_events,
            report_blocked,
            report_complete,
            create_blocker_issue,
            escalate_to_human,
            comment_on_issue,
            submit_pr_review,
            open_pr,
        ]
