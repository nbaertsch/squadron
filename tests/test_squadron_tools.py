"""Tests for tools/squadron_tools.py — unified tool registry.

Covers:
  - Tool selection (get_tools with explicit names vs defaults)
  - Each tool implementation's happy path
  - Error handling (agent not found, cycle detection)
  - Tool registration and SDK compatibility
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest_asyncio

from squadron.models import AgentRecord, AgentStatus
from squadron.registry import AgentRegistry
from squadron.tools.squadron_tools import (
    ALL_TOOL_NAMES,
    AssignIssueParams,
    CheckEventsParams,
    CheckRegistryParams,
    CommentOnIssueParams,
    CreateBlockerIssueParams,
    CreateIssueParams,
    EscalateToHumanParams,
    LabelIssueParams,
    OpenPRParams,
    ReadIssueParams,
    ReportBlockedParams,
    ReportCompleteParams,
    SubmitPRReviewParams,
    SquadronTools,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_tools.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


@pytest_asyncio.fixture
async def tools(registry):
    github = AsyncMock()
    github.comment_on_issue = AsyncMock(return_value={"id": 1})
    github.create_issue = AsyncMock(return_value={"number": 200})
    github.assign_issue = AsyncMock()
    github.add_labels = AsyncMock()
    github.get_issue = AsyncMock(
        return_value={
            "number": 42,
            "title": "Test Issue",
            "state": "open",
            "body": "Test body",
            "labels": [{"name": "feature"}],
            "assignees": [{"login": "user1"}],
            "user": {"login": "issue-creator"},
            "created_at": "2024-01-15T10:30:00Z",
        }
    )
    github.list_issue_comments = AsyncMock(
        return_value=[
            {
                "user": {"login": "commenter1"},
                "created_at": "2024-01-15T11:00:00Z",
                "body": "This is a comment",
            },
            {
                "user": {"login": "commenter2"},
                "created_at": "2024-01-15T12:00:00Z",
                "body": "Another comment",
            },
        ]
    )
    github.submit_pr_review = AsyncMock(return_value={"id": 100})
    github.create_pull_request = AsyncMock(return_value={"number": 50})

    inboxes: dict[str, asyncio.Queue] = {}

    return SquadronTools(
        registry=registry,
        github=github,
        agent_inboxes=inboxes,
        owner="testowner",
        repo="testrepo",
    )


@pytest_asyncio.fixture
async def agent(registry):
    """Create a test agent in the registry."""
    record = AgentRecord(
        agent_id="test-agent-1",
        role="feat-dev",
        issue_number=42,
        status=AgentStatus.ACTIVE,
    )
    await registry.create_agent(record)
    return record


# ── Tool Selection ───────────────────────────────────────────────────────────


class TestGetTools:
    def test_explicit_tool_names(self, tools):
        result = tools.get_tools("agent-1", ["comment_on_issue", "open_pr"])
        assert len(result) == 2

    def test_none_returns_empty(self, tools):
        """No tools in frontmatter → no Squadron tools (must be explicit)."""
        result = tools.get_tools("agent-1", None)
        assert len(result) == 0

    def test_invalid_tool_names_filtered(self, tools):
        result = tools.get_tools("agent-1", ["comment_on_issue", "nonexistent_tool"])
        assert len(result) == 1

    def test_all_tools_selectable(self, tools):
        result = tools.get_tools("agent-1", ALL_TOOL_NAMES)
        assert len(result) == len(ALL_TOOL_NAMES)

    def test_empty_list_returns_empty(self, tools):
        result = tools.get_tools("agent-1", [])
        assert len(result) == 0


# ── Framework Tools ──────────────────────────────────────────────────────────


class TestCheckForEvents:
    async def test_no_events(self, tools):
        result = await tools.check_for_events("agent-1", CheckEventsParams())
        assert "No pending events" in result

    async def test_with_event(self, tools):
        from squadron.models import SquadronEvent, SquadronEventType

        inbox = asyncio.Queue()
        tools.agent_inboxes["agent-1"] = inbox
        await inbox.put(
            SquadronEvent(
                event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
                issue_number=42,
                pr_number=10,
            )
        )

        result = await tools.check_for_events("agent-1", CheckEventsParams())
        assert "Pending events" in result
        # The new rich format uses uppercase PR_REVIEW_SUBMITTED label (issue #112)
        assert "PR_REVIEW_SUBMITTED" in result or "review_submitted" in result


class TestReportBlocked:
    async def test_blocks_agent(self, tools, agent, registry):
        params = ReportBlockedParams(blocker_issue=99, reason="Need design doc first")
        result = await tools.report_blocked("test-agent-1", params)

        updated = await registry.get_agent("test-agent-1")
        assert updated.status == AgentStatus.SLEEPING
        assert updated.sleeping_since is not None
        assert "suspended" in result.lower() or "saved" in result.lower()

    async def test_posts_comment(self, tools, agent):
        params = ReportBlockedParams(blocker_issue=99, reason="Needs clarification")
        await tools.report_blocked("test-agent-1", params)

        tools.github.comment_on_issue.assert_called()
        call_args = tools.github.comment_on_issue.call_args[0]
        assert call_args[2] == 42  # issue_number
        assert "#99" in call_args[3]

    async def test_agent_not_found(self, tools):
        params = ReportBlockedParams(blocker_issue=99, reason="test")
        result = await tools.report_blocked("nonexistent", params)
        assert "not found" in result.lower()


class TestReportComplete:
    async def test_completes_agent(self, tools, agent, registry):
        params = ReportCompleteParams(summary="Feature implemented and tested")
        result = await tools.report_complete("test-agent-1", params)

        updated = await registry.get_agent("test-agent-1")
        assert updated.status == AgentStatus.COMPLETED
        assert "complete" in result.lower()

    async def test_agent_not_found(self, tools):
        params = ReportCompleteParams(summary="done")
        result = await tools.report_complete("nonexistent", params)
        assert "not found" in result.lower()


class TestCreateBlockerIssue:
    async def test_creates_issue_and_blocks(self, tools, agent, registry):
        params = CreateBlockerIssueParams(title="Missing API", body="Need API endpoint")
        result = await tools.create_blocker_issue("test-agent-1", params)

        tools.github.create_issue.assert_called_once()
        updated = await registry.get_agent("test-agent-1")
        assert updated.status == AgentStatus.SLEEPING
        assert "#200" in result

    async def test_agent_not_found(self, tools):
        params = CreateBlockerIssueParams(title="test", body="test")
        result = await tools.create_blocker_issue("nonexistent", params)
        assert "not found" in result.lower()


class TestEscalateToHuman:
    async def test_escalates_agent(self, tools, agent, registry):
        params = EscalateToHumanParams(
            reason="Need architecture decision", category="architectural"
        )
        result = await tools.escalate_to_human("test-agent-1", params)

        updated = await registry.get_agent("test-agent-1")
        assert updated.status == AgentStatus.ESCALATED
        assert "escalated" in result.lower()

    async def test_adds_labels(self, tools, agent):
        params = EscalateToHumanParams(reason="test", category="security")
        await tools.escalate_to_human("test-agent-1", params)

        tools.github.add_labels.assert_called()
        call_args = tools.github.add_labels.call_args[0]
        labels = call_args[3]
        assert "needs-human" in labels


class TestSubmitPRReview:
    async def test_submits_review(self, tools, agent):
        params = SubmitPRReviewParams(pr_number=10, body="Looks good!", event="APPROVE")
        result = await tools.submit_pr_review("test-agent-1", params)

        tools.github.submit_pr_review.assert_called_once()
        assert "APPROVE" in result

    async def test_with_inline_comments(self, tools, agent):
        params = SubmitPRReviewParams(
            pr_number=10,
            body="Some issues",
            event="REQUEST_CHANGES",
            comments=[{"path": "src/main.py", "position": 5, "body": "Fix this"}],
        )
        result = await tools.submit_pr_review("test-agent-1", params)
        assert "REQUEST_CHANGES" in result


class TestOpenPR:
    async def test_opens_pr(self, tools, agent, registry):
        params = OpenPRParams(
            title="Implement feature",
            body="Fixes #42",
            head="feat/issue-42",
            base="main",
        )
        result = await tools.open_pr("test-agent-1", params)

        tools.github.create_pull_request.assert_called_once()
        assert "#50" in result

        # Should record PR number on agent
        updated = await registry.get_agent("test-agent-1")
        assert updated.pr_number == 50


# ── PM Tools ─────────────────────────────────────────────────────────────────


class TestCreateIssue:
    async def test_creates_issue(self, tools, agent):
        params = CreateIssueParams(title="New sub-task", body="Details here", labels=["feature"])
        result = await tools.create_issue("test-agent-1", params)

        tools.github.create_issue.assert_called_once()
        assert "#200" in result


class TestAssignIssue:
    async def test_assigns_issue(self, tools, agent):
        params = AssignIssueParams(issue_number=42, assignees=["squadron-dev[bot]"])
        result = await tools.assign_issue("test-agent-1", params)

        tools.github.assign_issue.assert_called_once()
        assert "#42" in result


class TestLabelIssue:
    async def test_labels_issue(self, tools, agent):
        params = LabelIssueParams(issue_number=42, labels=["feature", "high"])
        result = await tools.label_issue("test-agent-1", params)

        tools.github.add_labels.assert_called_once()
        assert "#42" in result


class TestReadIssue:
    async def test_reads_issue_with_comments(self, tools, agent):
        params = ReadIssueParams(issue_number=42)
        result = await tools.read_issue("test-agent-1", params)

        # Verify both methods are called
        tools.github.get_issue.assert_called_once()
        tools.github.list_issue_comments.assert_called_once_with(
            "testowner", "testrepo", 42, per_page=100
        )

        # Check that issue details are present
        assert "Test Issue" in result
        assert "feature" in result
        assert "issue-creator" in result
        assert "2024-01-15T10:30" in result

        # Check that comments are included
        assert "Comments (2)" in result
        assert "commenter1" in result
        assert "commenter2" in result
        assert "This is a comment" in result
        assert "Another comment" in result

    async def test_reads_issue_with_no_comments(self, tools, agent):
        # Mock no comments scenario
        tools.github.list_issue_comments.return_value = []

        params = ReadIssueParams(issue_number=42)
        result = await tools.read_issue("test-agent-1", params)

        tools.github.get_issue.assert_called()
        tools.github.list_issue_comments.assert_called()

        assert "Test Issue" in result
        assert "issue-creator" in result
        assert "**Comments:** None" in result


class TestCheckRegistry:
    async def test_lists_active_agents(self, tools, agent):
        params = CheckRegistryParams()
        result = await tools.check_registry("test-agent-1", params)

        assert "test-agent-1" in result
        assert "feat-dev" in result

    async def test_no_active_agents(self, tools, registry):
        params = CheckRegistryParams()
        result = await tools.check_registry("test-agent-1", params)
        assert "No active agents" in result


# ── Shared Tools ─────────────────────────────────────────────────────────────


class TestCommentOnIssue:
    async def test_posts_comment_with_agent_signature(self, tools, agent):
        params = CommentOnIssueParams(issue_number=42, body="Working on this now")
        result = await tools.comment_on_issue("test-agent-1", params)

        tools.github.comment_on_issue.assert_called()
        call_args = tools.github.comment_on_issue.call_args[0]
        body = call_args[3]
        # Should include emoji + display_name signature (or default 🤖 **role**)
        assert "🤖 **" in body or "**" in body
        assert "feat-dev" in body
        assert "Working on this now" in body
        assert "#42" in result


# ── PR Review Tools ─────────────────────────────────────────────────────────


class TestListPRReviews:
    async def test_lists_reviews(self, tools, agent):
        from squadron.tools.squadron_tools import ListPRReviewsParams

        tools.github.get_pr_reviews = AsyncMock(
            return_value=[
                {
                    "id": 123,
                    "user": {"login": "reviewer1"},
                    "state": "APPROVED",
                    "body": "LGTM!",
                    "submitted_at": "2024-01-15T10:00:00Z",
                },
                {
                    "id": 124,
                    "user": {"login": "reviewer2"},
                    "state": "CHANGES_REQUESTED",
                    "body": "Please fix the tests",
                    "submitted_at": "2024-01-15T11:00:00Z",
                },
            ]
        )

        params = ListPRReviewsParams(pr_number=50)
        result = await tools.list_pr_reviews("test-agent-1", params)

        tools.github.get_pr_reviews.assert_called_once()
        assert "reviewer1" in result
        assert "APPROVED" in result
        assert "reviewer2" in result
        assert "CHANGES_REQUESTED" in result


class TestGetReviewDetails:
    async def test_gets_review_with_comments(self, tools, agent):
        from squadron.tools.squadron_tools import GetReviewDetailsParams

        tools.github.get_review_details = AsyncMock(
            return_value={
                "id": 123,
                "user": {"login": "reviewer1"},
                "state": "CHANGES_REQUESTED",
                "body": "Please address these issues",
                "submitted_at": "2024-01-15T10:00:00Z",
            }
        )
        tools.github.get_review_comments = AsyncMock(
            return_value=[
                {
                    "id": 456,
                    "path": "src/main.py",
                    "line": 42,
                    "body": "This should handle null",
                },
            ]
        )

        params = GetReviewDetailsParams(pr_number=50, review_id=123)
        result = await tools.get_review_details("test-agent-1", params)

        tools.github.get_review_details.assert_called_once()
        tools.github.get_review_comments.assert_called_once()
        assert "reviewer1" in result
        assert "CHANGES_REQUESTED" in result
        assert "src/main.py:42" in result
        assert "handle null" in result


class TestGetPRReviewStatus:
    async def test_gets_comprehensive_status(self, tools, agent):
        from squadron.tools.squadron_tools import GetPRReviewStatusParams

        tools.github.get_pr_reviews = AsyncMock(
            return_value=[
                {"user": {"login": "approver"}, "state": "APPROVED"},
                {"user": {"login": "requester"}, "state": "CHANGES_REQUESTED"},
            ]
        )
        tools.github.list_requested_reviewers = AsyncMock(
            return_value={
                "users": [{"login": "pending_user"}],
                "teams": [{"slug": "core-team"}],
            }
        )

        params = GetPRReviewStatusParams(pr_number=50)
        result = await tools.get_pr_review_status("test-agent-1", params)

        assert "approver" in result
        assert "requester" in result
        assert "pending_user" in result
        assert "core-team" in result
        assert "CHANGES_REQUESTED" in result  # Overall status


class TestAddPRLineComment:
    async def test_adds_inline_comment(self, tools, agent):
        from squadron.tools.squadron_tools import AddPRLineCommentParams

        tools.github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc123"}})
        tools.github.create_pr_review_comment = AsyncMock(return_value={"id": 789})

        params = AddPRLineCommentParams(
            pr_number=50, path="src/main.py", line=42, body="Consider using a guard clause"
        )
        result = await tools.add_pr_line_comment("test-agent-1", params)

        tools.github.create_pr_review_comment.assert_called_once()
        call_args = tools.github.create_pr_review_comment.call_args
        assert call_args[0][2] == 50  # pr_number
        assert "guard clause" in call_args[0][3]  # body includes original message
        assert call_args[0][5] == "src/main.py"  # path
        assert call_args[0][6] == 42  # line
        assert "src/main.py:42" in result


class TestReplyToReviewComment:
    async def test_replies_to_comment(self, tools, agent):
        from squadron.tools.squadron_tools import ReplyToReviewCommentParams

        tools.github.reply_to_pr_review_comment = AsyncMock(return_value={"id": 790})

        params = ReplyToReviewCommentParams(
            pr_number=50, comment_id=789, body="Fixed in the latest commit"
        )
        result = await tools.reply_to_review_comment("test-agent-1", params)

        tools.github.reply_to_pr_review_comment.assert_called_once()
        call_args = tools.github.reply_to_pr_review_comment.call_args
        assert call_args[0][2] == 50  # pr_number
        assert call_args[0][3] == 789  # comment_id
        assert "Fixed" in call_args[0][4]  # body
        assert "#789" in result


# ── Pre-sleep hook ───────────────────────────────────────────────────────────


class TestPreSleepHook:
    async def test_calls_pre_sleep_hook_on_block(self, registry):
        """report_blocked should call the WIP commit hook before sleep."""
        github = AsyncMock()
        github.comment_on_issue = AsyncMock()

        hook = AsyncMock()
        inboxes: dict[str, asyncio.Queue] = {}

        st = SquadronTools(
            registry=registry,
            github=github,
            agent_inboxes=inboxes,
            owner="o",
            repo="r",
            pre_sleep_hook=hook,
        )

        agent = AgentRecord(
            agent_id="hook-test-1",
            role="feat-dev",
            issue_number=1,
            status=AgentStatus.ACTIVE,
        )
        await registry.create_agent(agent)

        params = ReportBlockedParams(blocker_issue=99, reason="blocked")
        await st.report_blocked("hook-test-1", params)

        hook.assert_called_once()

    async def test_hook_failure_doesnt_prevent_sleep(self, registry):
        """Even if the hook fails, the agent should still transition to SLEEPING."""
        github = AsyncMock()
        github.comment_on_issue = AsyncMock()

        hook = AsyncMock(side_effect=Exception("git push failed"))
        inboxes: dict[str, asyncio.Queue] = {}

        st = SquadronTools(
            registry=registry,
            github=github,
            agent_inboxes=inboxes,
            owner="o",
            repo="r",
            pre_sleep_hook=hook,
        )

        agent = AgentRecord(
            agent_id="hook-test-2",
            role="feat-dev",
            issue_number=2,
            status=AgentStatus.ACTIVE,
        )
        await registry.create_agent(agent)

        params = ReportBlockedParams(blocker_issue=99, reason="blocked")
        await st.report_blocked("hook-test-2", params)

        updated = await registry.get_agent("hook-test-2")
        assert updated.status == AgentStatus.SLEEPING


# ── Constants ────────────────────────────────────────────────────────────────


class TestConstants:
    def test_all_tool_names_count(self):
        assert len(ALL_TOOL_NAMES) == 35  # Updated: added PR review tools (#67, #68)

    def test_git_push_in_all_tools(self):
        """git_push should be available for explicit selection."""
        assert "git_push" in ALL_TOOL_NAMES
