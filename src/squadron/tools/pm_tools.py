"""PM-specific tools — issue triage, assignment, and registry queries.

The PM agent needs different tools than dev/review agents. Instead of
lifecycle tools (report_blocked, report_complete), the PM gets issue
management tools that map to GitHubClient operations.

These are registered as custom tools with the Copilot SDK via @define_tool.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from copilot import define_tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from squadron.github_client import GitHubClient
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)


# ── Tool Parameter Models ────────────────────────────────────────────────────


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


class CommentOnIssueParams(BaseModel):
    issue_number: int = Field(description="The GitHub issue number to comment on")
    body: str = Field(description="Comment body (markdown supported)")


class CheckRegistryParams(BaseModel):
    """No parameters needed — returns all active agents."""

    pass


class ReadIssueParams(BaseModel):
    issue_number: int = Field(description="The GitHub issue number to read")


# ── PM Tools Implementation ─────────────────────────────────────────────────


class PMTools:
    """Container for PM-specific tool implementations.

    The PM agent uses these to triage, classify, and assign issues.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        github: GitHubClient,
        owner: str,
        repo: str,
    ):
        self.registry = registry
        self.github = github
        self.owner = owner
        self.repo = repo

    async def create_issue(self, params: CreateIssueParams) -> str:
        """Create a new GitHub issue (for blockers or sub-tasks)."""
        result = await self.github.create_issue(
            self.owner,
            self.repo,
            title=params.title,
            body=params.body,
            labels=params.labels,
        )
        return f"Created issue #{result['number']}: {params.title}"

    async def assign_issue(self, params: AssignIssueParams) -> str:
        """Assign a GitHub issue to one or more users."""
        await self.github.assign_issue(
            self.owner,
            self.repo,
            params.issue_number,
            params.assignees,
        )
        return f"Assigned #{params.issue_number} to {', '.join(params.assignees)}"

    async def label_issue(self, params: LabelIssueParams) -> str:
        """Apply labels to a GitHub issue."""
        await self.github.add_labels(
            self.owner,
            self.repo,
            params.issue_number,
            params.labels,
        )
        return f"Applied labels {params.labels} to #{params.issue_number}"

    async def comment_on_issue(self, params: CommentOnIssueParams) -> str:
        """Post a comment on a GitHub issue."""
        await self.github.comment_on_issue(
            self.owner,
            self.repo,
            params.issue_number,
            params.body,
        )
        return f"Posted comment on #{params.issue_number}"

    async def check_registry(self, params: CheckRegistryParams) -> str:
        """Query the agent registry for active agents and their status."""
        agents = await self.registry.get_all_active_agents()
        if not agents:
            return "No active agents in the registry."

        lines = [f"**Active agents:** {len(agents)}\n"]
        for agent in agents:
            blockers = f" (blocked by: {agent.blocked_by})" if agent.blocked_by else ""
            lines.append(
                f"- `{agent.agent_id}` [{agent.role.value}] "
                f"status={agent.status.value} issue=#{agent.issue_number}{blockers}"
            )
        return "\n".join(lines)

    async def read_issue(self, params: ReadIssueParams) -> str:
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

    def get_tools(self) -> list:
        """Return SDK-compatible Tool objects for the PM agent."""
        pm = self  # capture for closures

        @define_tool(
            description="Create a new GitHub issue for a blocker, sub-task, or new work item."
        )
        async def create_issue(params: CreateIssueParams) -> str:
            return await pm.create_issue(params)

        @define_tool(
            description="Assign a GitHub issue to squadron-dev[bot] for tracking visibility. Labels are the actual agent spawn trigger."
        )
        async def assign_issue(params: AssignIssueParams) -> str:
            return await pm.assign_issue(params)

        @define_tool(description="Apply labels to classify a GitHub issue (type, priority, etc.).")
        async def label_issue(params: LabelIssueParams) -> str:
            return await pm.label_issue(params)

        @define_tool(
            description="Post a triage comment on a GitHub issue with your analysis and decisions."
        )
        async def comment_on_issue(params: CommentOnIssueParams) -> str:
            return await pm.comment_on_issue(params)

        @define_tool(
            description="Query the agent registry to see all active agents and their current status."
        )
        async def check_registry(params: CheckRegistryParams) -> str:
            return await pm.check_registry(params)

        @define_tool(
            description="Read a GitHub issue's full details including title, body, labels, and assignees."
        )
        async def read_issue(params: ReadIssueParams) -> str:
            return await pm.read_issue(params)

        return [
            create_issue,
            assign_issue,
            label_issue,
            comment_on_issue,
            check_registry,
            read_issue,
        ]
