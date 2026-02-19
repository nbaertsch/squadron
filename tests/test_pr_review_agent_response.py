"""Tests for PR review agent response system (issue #112).

Covers:
1. Framework-level _handle_pr_review_submitted delivers review to agent inbox
2. Framework-level _handle_pr_review_comment delivers inline comments to agent inbox
3. check_for_events returns rich PR review context (state, body, reviewer)
4. _build_wake_prompt includes issue_number from agent record when event has none
5. _build_wake_prompt includes explicit get_pr_feedback directive for review wakes
6. "commented" review state wakes PR owner (via config trigger)
7. Non-PR-owning agents are not notified of reviews
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest_asyncio

from squadron.agent_manager import AgentManager
from squadron.config import (
    AgentRoleConfig,
    AgentTrigger,
    ProjectConfig,
    ReviewPolicyConfig,
    ReviewRequirement,
    SquadronConfig,
)
from squadron.event_router import EventRouter
from squadron.models import (
    AgentRecord,
    AgentStatus,
    GitHubEvent,
    SquadronEvent,
    SquadronEventType,
)
from squadron.registry import AgentRegistry
from squadron.tools.squadron_tools import SquadronTools


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_pr_review_response.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _config_with_commented_trigger() -> SquadronConfig:
    """Config that includes both changes_requested AND commented triggers."""
    return SquadronConfig(
        project=ProjectConfig(
            name="test-project",
            owner="testowner",
            repo="testrepo",
            default_branch="main",
        ),
        review_policy=ReviewPolicyConfig(
            enabled=True,
            default_requirements=[ReviewRequirement(role="pr-review", count=1)],
        ),
        agent_roles={
            "feat-dev": AgentRoleConfig(
                agent_definition="agents/feat-dev.md",
                triggers=[
                    AgentTrigger(event="issues.labeled", label="feature"),
                    AgentTrigger(event="pull_request.opened", action="sleep"),
                    AgentTrigger(
                        event="pull_request_review.submitted",
                        condition={"review_state": "changes_requested"},
                        action="wake",
                    ),
                    AgentTrigger(
                        event="pull_request_review.submitted",
                        condition={"review_state": "commented"},
                        action="wake",
                    ),
                    AgentTrigger(
                        event="pull_request.closed",
                        condition={"merged": True},
                        action="complete",
                    ),
                ],
            ),
            "pr-review": AgentRoleConfig(
                agent_definition="agents/pr-review.md",
                triggers=[
                    AgentTrigger(
                        event="pull_request.opened",
                        condition={"approval_flow": True},
                    ),
                    AgentTrigger(event="pull_request.closed", action="complete"),
                ],
            ),
        },
    )


def _mock_github():
    github = AsyncMock()
    github.comment_on_issue = AsyncMock(return_value={"id": 1})
    github.comment_on_pr = AsyncMock(return_value={"id": 1})
    github.create_issue = AsyncMock(return_value={"number": 200})
    github.get_issue = AsyncMock(return_value={"state": "open", "title": "Test", "body": ""})
    github.add_labels = AsyncMock()
    github.assign_issue = AsyncMock()
    github.ensure_labels_exist = AsyncMock()
    github.get_pr_reviews = AsyncMock(return_value=[])
    github.get_pr_review_comments = AsyncMock(return_value=[])
    github.list_pull_request_files = AsyncMock(return_value=[])
    github.list_issues = AsyncMock(return_value=[])
    github.list_pull_requests = AsyncMock(return_value=[])
    return github


def _mock_agent_defs():
    from squadron.config import AgentDefinition

    return {
        "feat-dev": AgentDefinition(
            role="feat-dev",
            raw_content="---\nname: feat-dev\n---\nYou are a feature developer.",
            prompt="You are a feature developer.",
            name="feat-dev",
        ),
        "pr-review": AgentDefinition(
            role="pr-review",
            raw_content="---\nname: pr-review\n---\nYou are a code reviewer.",
            prompt="You are a code reviewer.",
            name="pr-review",
        ),
    }


def _pr_review_submitted_event(
    pr_number: int,
    review_state: str,
    reviewer: str = "pr-review-bot",
    review_body: str = "Please fix the issues.",
    delivery_id: str = "review-1",
) -> GitHubEvent:
    """Build a pull_request_review.submitted event."""
    return GitHubEvent(
        delivery_id=delivery_id,
        event_type="pull_request_review",
        action="submitted",
        payload={
            "action": "submitted",
            "review": {
                "id": 101,
                "state": review_state,
                "body": review_body,
                "user": {"login": reviewer, "type": "Bot"},
            },
            "pull_request": {
                "number": pr_number,
                "title": "Fix #42",
                "body": "Fixes #42",
                "head": {"ref": "feat/issue-42"},
                "base": {"ref": "main"},
            },
            "sender": {"login": reviewer, "type": "Bot"},
        },
    )


def _pr_review_comment_event(
    pr_number: int,
    reviewer: str = "pr-review-bot",
    comment_body: str = "This looks wrong.",
    path: str = "src/foo.py",
    line: int = 42,
    delivery_id: str = "comment-1",
) -> GitHubEvent:
    """Build a pull_request_review_comment.created event."""
    return GitHubEvent(
        delivery_id=delivery_id,
        event_type="pull_request_review_comment",
        action="created",
        payload={
            "action": "created",
            "comment": {
                "id": 201,
                "body": comment_body,
                "path": path,
                "line": line,
                "user": {"login": reviewer, "type": "Bot"},
            },
            "pull_request": {
                "number": pr_number,
                "title": "Fix #42",
                "body": "Fixes #42",
                "head": {"ref": "feat/issue-42"},
                "base": {"ref": "main"},
            },
            "sender": {"login": reviewer, "type": "Bot"},
        },
    )


async def _make_manager(registry, config=None, github=None):
    if config is None:
        config = _config_with_commented_trigger()
    if github is None:
        github = _mock_github()
    router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
    mgr = AgentManager(
        config=config,
        registry=registry,
        github=github,
        router=router,
        agent_definitions=_mock_agent_defs(),
        repo_root=Path("/tmp/test"),
    )
    return mgr, router


async def _sleeping_feat_dev(registry, issue_number: int, pr_number: int) -> AgentRecord:
    agent = AgentRecord(
        agent_id=f"feat-dev-issue-{issue_number}",
        role="feat-dev",
        issue_number=issue_number,
        pr_number=pr_number,
        status=AgentStatus.SLEEPING,
        sleeping_since=datetime.now(timezone.utc),
        session_id=f"session-feat-dev-{issue_number}",
        branch=f"feat/issue-{issue_number}",
    )
    await registry.create_agent(agent)
    return agent


# ── Tests: _handle_pr_review_submitted (framework-level handler) ──────────────


class TestHandlePRReviewSubmitted:
    """Test the framework-level _handle_pr_review_submitted handler."""

    async def test_review_queued_to_sleeping_pr_owner_inbox(self, registry):
        """PR review is queued into the sleeping PR-owner's inbox."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr, router = await _make_manager(registry)
            await mgr.start()

            # Create a sleeping feat-dev agent that opened PR #87
            await _sleeping_feat_dev(registry, issue_number=42, pr_number=87)

            # Ensure inbox for this agent doesn't exist yet (pre-condition)
            assert "feat-dev-issue-42" not in mgr.agent_inboxes

            # PR review submitted
            event = _pr_review_submitted_event(pr_number=87, review_state="changes_requested")
            await mgr._handle_pr_review_submitted(
                await _make_squadron_event_from_github(event, SquadronEventType.PR_REVIEW_SUBMITTED)
            )

            # Inbox should have been created and contain the review event
            inbox = mgr.agent_inboxes.get("feat-dev-issue-42")
            assert inbox is not None, "Inbox should be created for sleeping agent"
            assert not inbox.empty(), "Review event should be queued in agent inbox"

            queued_event = inbox.get_nowait()
            assert queued_event.event_type == SquadronEventType.PR_REVIEW_SUBMITTED
            assert queued_event.pr_number == 87
            assert queued_event.issue_number == 42  # populated from agent record

    async def test_review_not_queued_to_wrong_pr_agent(self, registry):
        """PR review for PR #87 is not queued to agent that owns PR #88."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr, _ = await _make_manager(registry)
            await mgr.start()

            # Agent for a DIFFERENT PR
            wrong_agent = AgentRecord(
                agent_id="feat-dev-issue-99",
                role="feat-dev",
                issue_number=99,
                pr_number=88,  # different PR
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
            )
            await registry.create_agent(wrong_agent)

            event = _pr_review_submitted_event(pr_number=87, review_state="changes_requested")
            await mgr._handle_pr_review_submitted(
                await _make_squadron_event_from_github(event, SquadronEventType.PR_REVIEW_SUBMITTED)
            )

            # Inbox should NOT be created for wrong agent
            inbox = mgr.agent_inboxes.get("feat-dev-issue-99")
            assert inbox is None or inbox.empty(), "Wrong agent should not receive review"

    async def test_review_queued_with_issue_number_from_agent_record(self, registry):
        """Issue number in queued event comes from agent record, not from the webhook payload."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr, _ = await _make_manager(registry)
            await mgr.start()

            await _sleeping_feat_dev(registry, issue_number=42, pr_number=87)

            event = _pr_review_submitted_event(pr_number=87, review_state="changes_requested")
            squadron_event = await _make_squadron_event_from_github(
                event, SquadronEventType.PR_REVIEW_SUBMITTED
            )
            # Webhook payload has no issue_number (PR review events don't include it)
            assert squadron_event.issue_number is None

            await mgr._handle_pr_review_submitted(squadron_event)

            inbox = mgr.agent_inboxes.get("feat-dev-issue-42")
            assert inbox is not None
            queued = inbox.get_nowait()
            # The queued event should have issue_number populated from agent record
            assert queued.issue_number == 42, (
                "issue_number should be populated from agent record "
                "since PR review webhook payloads don't include it"
            )


# ── Tests: _handle_pr_review_comment (inline comment inbox delivery) ──────────


class TestHandlePRReviewComment:
    """Test that inline PR review comments are queued to the PR owner's inbox."""

    async def test_inline_comment_queued_to_active_pr_owner(self, registry):
        """Inline review comment is queued to an ACTIVE (running) PR owner."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr, _ = await _make_manager(registry)
            await mgr.start()

            # Active feat-dev agent
            active_agent = AgentRecord(
                agent_id="feat-dev-issue-42",
                role="feat-dev",
                issue_number=42,
                pr_number=87,
                status=AgentStatus.ACTIVE,
                active_since=datetime.now(timezone.utc),
            )
            await registry.create_agent(active_agent)
            mgr.agent_inboxes["feat-dev-issue-42"] = asyncio.Queue()

            event = _pr_review_comment_event(pr_number=87)
            await mgr._handle_pr_review_comment(
                await _make_squadron_event_from_github(
                    event, SquadronEventType.PR_REVIEW_COMMENT
                )
            )

            inbox = mgr.agent_inboxes["feat-dev-issue-42"]
            assert not inbox.empty(), "Inline comment should be queued to active agent"
            queued = inbox.get_nowait()
            assert queued.event_type == SquadronEventType.PR_REVIEW_COMMENT
            assert queued.pr_number == 87

    async def test_inline_comment_queued_to_sleeping_pr_owner(self, registry):
        """Inline review comment is queued to a SLEEPING PR owner (consumed on wake)."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr, _ = await _make_manager(registry)
            await mgr.start()

            await _sleeping_feat_dev(registry, issue_number=42, pr_number=87)

            event = _pr_review_comment_event(pr_number=87)
            await mgr._handle_pr_review_comment(
                await _make_squadron_event_from_github(
                    event, SquadronEventType.PR_REVIEW_COMMENT
                )
            )

            inbox = mgr.agent_inboxes.get("feat-dev-issue-42")
            assert inbox is not None
            assert not inbox.empty(), "Inline comment should be queued to sleeping agent"

    async def test_multiple_inline_comments_accumulate_in_inbox(self, registry):
        """Multiple inline comments accumulate in inbox; agent sees all on wake."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr, _ = await _make_manager(registry)
            await mgr.start()

            await _sleeping_feat_dev(registry, issue_number=42, pr_number=87)

            for i in range(3):
                event = _pr_review_comment_event(
                    pr_number=87,
                    comment_body=f"Comment {i}",
                    delivery_id=f"comment-{i}",
                )
                await mgr._handle_pr_review_comment(
                    await _make_squadron_event_from_github(
                        event, SquadronEventType.PR_REVIEW_COMMENT
                    )
                )

            inbox = mgr.agent_inboxes.get("feat-dev-issue-42")
            assert inbox is not None
            assert inbox.qsize() == 3, "All 3 inline comments should be in inbox"


# ── Tests: check_for_events rich output ──────────────────────────────────────


class TestCheckForEventsRichOutput:
    """Test that check_for_events returns rich PR review context."""

    async def _make_tools(self, registry):
        """Create a SquadronTools instance with a test agent inbox."""
        agent_inboxes: dict = {}
        tools = SquadronTools(
            registry=registry,
            github=_mock_github(),
            agent_inboxes=agent_inboxes,
            owner="testowner",
            repo="testrepo",
            config=_config_with_commented_trigger(),
            agent_definitions=_mock_agent_defs(),
        )
        return tools, agent_inboxes

    async def test_empty_inbox_returns_no_events(self, registry):
        tools, _ = await self._make_tools(registry)
        result = await tools.check_for_events("feat-dev-issue-42", _empty_params())
        assert result == "No pending events."

    async def test_pr_review_submitted_returns_rich_context(self, registry):
        """check_for_events returns review state, body, and reviewer for PR review events."""
        tools, agent_inboxes = await self._make_tools(registry)

        # Manually queue a review event
        agent_inboxes["feat-dev-issue-42"] = asyncio.Queue()
        review_event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=87,
            issue_number=42,
            data={
                "payload": {
                    "review": {
                        "id": 101,
                        "state": "CHANGES_REQUESTED",
                        "body": "Please add error handling.",
                        "user": {"login": "pr-review-bot"},
                    }
                }
            },
        )
        await agent_inboxes["feat-dev-issue-42"].put(review_event)

        result = await tools.check_for_events("feat-dev-issue-42", _empty_params())

        assert "PR_REVIEW_SUBMITTED" in result
        assert "CHANGES_REQUESTED" in result
        assert "pr-review-bot" in result
        assert "Please add error handling." in result
        assert "get_pr_feedback" in result, "Should prompt agent to call get_pr_feedback"

    async def test_pr_review_comment_returns_file_and_line(self, registry):
        """check_for_events returns file path and line for inline review comments."""
        tools, agent_inboxes = await self._make_tools(registry)

        agent_inboxes["feat-dev-issue-42"] = asyncio.Queue()
        comment_event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_COMMENT,
            pr_number=87,
            issue_number=42,
            data={
                "payload": {
                    "comment": {
                        "id": 201,
                        "body": "This variable name is confusing.",
                        "path": "src/foo.py",
                        "line": 42,
                        "user": {"login": "pr-reviewer"},
                    }
                }
            },
        )
        await agent_inboxes["feat-dev-issue-42"].put(comment_event)

        result = await tools.check_for_events("feat-dev-issue-42", _empty_params())

        assert "PR_REVIEW_COMMENT" in result
        assert "src/foo.py" in result
        assert "42" in result  # line number
        assert "pr-reviewer" in result
        assert "This variable name is confusing." in result

    async def test_multiple_events_all_returned(self, registry):
        """All queued events are returned and drained from inbox."""
        tools, agent_inboxes = await self._make_tools(registry)

        agent_inboxes["feat-dev-issue-42"] = asyncio.Queue()

        # Queue a review and one inline comment
        await agent_inboxes["feat-dev-issue-42"].put(SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=87,
            data={"payload": {"review": {"state": "COMMENTED", "body": "", "user": {"login": "r1"}}}},
        ))
        await agent_inboxes["feat-dev-issue-42"].put(SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_COMMENT,
            pr_number=87,
            data={"payload": {"comment": {"body": "Comment 1", "path": "foo.py", "line": 1, "user": {"login": "r1"}}}},
        ))

        result = await tools.check_for_events("feat-dev-issue-42", _empty_params())

        assert "PR_REVIEW_SUBMITTED" in result
        assert "PR_REVIEW_COMMENT" in result

        # Inbox should be drained
        assert agent_inboxes["feat-dev-issue-42"].empty(), "check_for_events should drain inbox"


# ── Tests: _build_wake_prompt improvements ───────────────────────────────────


class TestBuildWakePrompt:
    """Test that _build_wake_prompt includes issue_number and PR review directive."""

    async def _make_manager_and_agent(self, registry):
        with patch("squadron.agent_manager.CopilotAgent"):
            mgr, _ = await _make_manager(registry)
            await mgr.start()
        agent = await _sleeping_feat_dev(registry, issue_number=42, pr_number=87)
        return mgr, agent

    async def test_issue_number_from_agent_record_when_event_has_none(self, registry):
        """Wake prompt includes issue number from agent record when trigger event lacks it."""
        mgr, agent = await self._make_manager_and_agent(registry)

        # Create a wake event WITHOUT issue_number (as happens with PR review webhooks)
        wake_event = SquadronEvent(
            event_type=SquadronEventType.WAKE_AGENT,
            pr_number=87,
            issue_number=None,  # PR review webhooks don't include issue number
            data={
                "payload": {
                    "review": {
                        "id": 101,
                        "state": "CHANGES_REQUESTED",
                        "body": "Please fix the tests.",
                        "user": {"login": "pr-review-bot"},
                    }
                }
            },
        )

        prompt = await mgr._build_wake_prompt(agent, wake_event)

        assert "#42" in prompt, (
            "Wake prompt must include issue number from agent record "
            "even when trigger event has no issue_number"
        )
        assert "Issue" in prompt, "Wake prompt should label it as Issue"

    async def test_pr_review_directive_in_wake_prompt(self, registry):
        """Wake prompt includes explicit get_pr_feedback directive for review wakes."""
        mgr, agent = await self._make_manager_and_agent(registry)

        wake_event = SquadronEvent(
            event_type=SquadronEventType.WAKE_AGENT,
            pr_number=87,
            issue_number=None,
            data={
                "payload": {
                    "review": {
                        "id": 101,
                        "state": "CHANGES_REQUESTED",
                        "body": "Please fix the tests.",
                        "user": {"login": "pr-review-bot"},
                    }
                }
            },
        )

        prompt = await mgr._build_wake_prompt(agent, wake_event)

        assert "get_pr_feedback" in prompt, (
            "Wake prompt must include explicit instruction to call get_pr_feedback"
        )
        assert "CHANGES_REQUESTED" in prompt.upper(), "Wake prompt must include review state"

    async def test_inbox_hint_when_messages_queued(self, registry):
        """Wake prompt mentions pending inbox messages when inbox is non-empty."""
        mgr, agent = await self._make_manager_and_agent(registry)

        # Pre-populate agent inbox (simulating inline comments queued before wake)
        mgr.agent_inboxes["feat-dev-issue-42"] = asyncio.Queue()
        await mgr.agent_inboxes["feat-dev-issue-42"].put(
            SquadronEvent(
                event_type=SquadronEventType.PR_REVIEW_COMMENT,
                pr_number=87,
            )
        )

        wake_event = SquadronEvent(
            event_type=SquadronEventType.WAKE_AGENT,
            pr_number=87,
            issue_number=None,
            data={"payload": {"review": {"state": "CHANGES_REQUESTED", "body": "", "user": {"login": "r"}}}},
        )

        prompt = await mgr._build_wake_prompt(agent, wake_event)

        assert "check_for_events" in prompt, (
            "Wake prompt should hint that inbox has pending events and agent should call check_for_events"
        )
        assert "1 pending event" in prompt or "Inbox:" in prompt

    async def test_wake_prompt_without_review_no_directive(self, registry):
        """Non-review wakes don't include the get_pr_feedback directive."""
        mgr, agent = await self._make_manager_and_agent(registry)

        # Blocker-resolved wake (no review in payload)
        wake_event = SquadronEvent(
            event_type=SquadronEventType.BLOCKER_RESOLVED,
            issue_number=42,
            data={"resolved_issue": 99},
        )

        prompt = await mgr._build_wake_prompt(agent, wake_event)

        # Should NOT have the review directive (no review in this wake)
        assert "Action required:" not in prompt or "get_pr_feedback" not in prompt.split("Action required:")[1] if "Action required:" in prompt else True
        assert "Resolved blocker" in prompt
        assert "#99" in prompt
        assert "closed" in prompt


# ── Tests: "commented" review state wakes PR owner ───────────────────────────


class TestCommentedReviewWakesAuthor:
    """Test that COMMENT-state reviews also wake the PR owner via config trigger."""

    async def test_commented_review_wakes_sleeping_author(self, registry):
        """A COMMENT-state review wakes the sleeping feat-dev agent."""
        config = _config_with_commented_trigger()
        github = _mock_github()
        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr = AgentManager(
                config=config,
                registry=registry,
                github=github,
                router=router,
                agent_definitions=_mock_agent_defs(),
                repo_root=Path("/tmp/test"),
            )
            await mgr.start()

            # Sleeping feat-dev agent with PR associated
            author = AgentRecord(
                agent_id="feat-dev-issue-42",
                role="feat-dev",
                issue_number=42,
                pr_number=87,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                session_id="session-42",
            )
            await registry.create_agent(author)

            # Reviewer submits "commented" review (asking questions)
            event = _pr_review_submitted_event(
                pr_number=87,
                review_state="commented",
                review_body="Can you explain why you chose this approach?",
                delivery_id="comment-review-1",
            )
            await router._route_event(event)

            # feat-dev agent must be woken
            updated = await registry.get_agent("feat-dev-issue-42")
            assert updated is not None
            assert updated.status == AgentStatus.ACTIVE, (
                "COMMENT-state review should wake the feat-dev agent so it can respond "
                "to reviewer questions. Without this, reviewer questions go unanswered."
            )

    async def test_changes_requested_review_still_wakes_author(self, registry):
        """CHANGES_REQUESTED reviews still wake the author (regression guard)."""
        config = _config_with_commented_trigger()
        github = _mock_github()
        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr = AgentManager(
                config=config,
                registry=registry,
                github=github,
                router=router,
                agent_definitions=_mock_agent_defs(),
                repo_root=Path("/tmp/test"),
            )
            await mgr.start()

            author = AgentRecord(
                agent_id="feat-dev-issue-42",
                role="feat-dev",
                issue_number=42,
                pr_number=87,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                session_id="session-42",
            )
            await registry.create_agent(author)

            event = _pr_review_submitted_event(
                pr_number=87,
                review_state="changes_requested",
                delivery_id="changes-req-1",
            )
            await router._route_event(event)

            updated = await registry.get_agent("feat-dev-issue-42")
            assert updated.status == AgentStatus.ACTIVE


# ── Tests: end-to-end framework handler integration ───────────────────────────


class TestFrameworkHandlerRegistration:
    """Verify that the framework-level handlers are registered on start()."""

    async def test_pr_review_submitted_handler_registered(self, registry):
        """_handle_pr_review_submitted is registered as a framework handler."""
        with patch("squadron.agent_manager.CopilotAgent"):
            mgr, router = await _make_manager(registry)
            await mgr.start()

            # The handler should be registered
            handlers = router._handlers.get(SquadronEventType.PR_REVIEW_SUBMITTED, [])
            handler_names = [h.__name__ for h in handlers]
            assert "_handle_pr_review_submitted" in handler_names, (
                "_handle_pr_review_submitted must be registered as a framework handler "
                "for PR_REVIEW_SUBMITTED events (issue #112)"
            )

    async def test_pr_review_comment_handler_registered(self, registry):
        """_handle_pr_review_comment is registered as a framework handler."""
        with patch("squadron.agent_manager.CopilotAgent"):
            mgr, router = await _make_manager(registry)
            await mgr.start()

            handlers = router._handlers.get(SquadronEventType.PR_REVIEW_COMMENT, [])
            handler_names = [h.__name__ for h in handlers]
            assert "_handle_pr_review_comment" in handler_names, (
                "_handle_pr_review_comment must be registered as a framework handler "
                "for PR_REVIEW_COMMENT events (issue #112)"
            )

    async def test_end_to_end_review_queued_via_event_router(self, registry):
        """End-to-end: PR review submitted via event router queues to agent inbox."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr, router = await _make_manager(registry)
            await mgr.start()

            await _sleeping_feat_dev(registry, issue_number=42, pr_number=87)

            # Route a PR review comment event through the full router
            event = _pr_review_comment_event(pr_number=87, delivery_id="e2e-comment-1")
            await router._route_event(event)

            inbox = mgr.agent_inboxes.get("feat-dev-issue-42")
            assert inbox is not None, "Inbox should be created after routing PR review comment"
            assert not inbox.empty(), "Review comment should be in inbox after routing"


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _make_squadron_event_from_github(
    github_event: GitHubEvent, event_type: SquadronEventType
) -> SquadronEvent:
    """Convert a GitHubEvent to a SquadronEvent (mirrors EventRouter logic)."""
    pr_number = github_event.payload.get("pull_request", {}).get("number")
    issue_number = github_event.payload.get("issue", {}).get("number")

    data: dict = {
        "action": github_event.action,
        "sender": github_event.sender,
        "payload": github_event.payload,
        "issue_creator": None,
    }

    if event_type == SquadronEventType.PR_REVIEW_SUBMITTED:
        review = github_event.payload.get("review", {})
        data["review"] = {
            "id": review.get("id"),
            "state": review.get("state"),
            "body": review.get("body"),
            "user": review.get("user", {}).get("login"),
        }

    if event_type == SquadronEventType.PR_REVIEW_COMMENT:
        comment = github_event.payload.get("comment", {})
        data["review_comment"] = {
            "id": comment.get("id"),
            "body": comment.get("body"),
            "path": comment.get("path"),
            "line": comment.get("line"),
            "user": comment.get("user", {}).get("login"),
        }

    return SquadronEvent(
        event_type=event_type,
        source_delivery_id=github_event.delivery_id,
        pr_number=pr_number,
        issue_number=issue_number,
        data=data,
    )


class _empty_params:
    """Minimal params object for check_for_events."""
    pass
