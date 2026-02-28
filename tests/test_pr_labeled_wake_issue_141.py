"""Regression test for issue #141 — needs-changes label fallback wakes PR-owning agent.

When a pr-review agent cannot submit a formal REQUEST_CHANGES review (HTTP 403 —
same bot identity), it applies the 'needs-changes' label as a fallback.  This fires
a pull_request.labeled webhook event.  The framework must route this event and wake
the sleeping PR-owning agent so it can address the review feedback.

Previously, pull_request.labeled was absent from EVENT_MAP, so the event was silently
dropped and the PR-owning agent was never woken.

Fix:
- Added pull_request.labeled → PR_LABELED to EVENT_MAP (event_router.py)
- Added PR_LABELED to SquadronEventType (models.py)
- Added wake trigger for pull_request.labeled + label: needs-changes to all dev
  agent roles in config.yaml
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest_asyncio

from squadron.config import (
    AgentRoleConfig,
    AgentTrigger,
    ProjectConfig,
    SquadronConfig,
)
from squadron.agent_manager import AgentManager
from squadron.event_router import EVENT_MAP, EventRouter
from squadron.models import (
    AgentRecord,
    AgentStatus,
    GitHubEvent,
    SquadronEventType,
)
from squadron.registry import AgentRegistry


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_issue_141.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _config_with_needs_changes_trigger() -> SquadronConfig:
    """Config that includes pull_request.labeled + needs-changes wake trigger."""
    return SquadronConfig(
        project=ProjectConfig(
            name="test-project",
            owner="testowner",
            repo="testrepo",
            default_branch="main",
        ),
        agent_roles={
            "bug-fix": AgentRoleConfig(
                agent_definition="agents/bug-fix.md",
                triggers=[
                    AgentTrigger(event="issues.labeled", label="bug"),
                    AgentTrigger(event="pull_request.opened", action="sleep"),
                    # Belt-and-suspenders: formal review path
                    AgentTrigger(
                        event="pull_request_review.submitted",
                        condition={"review_state": "changes_requested"},
                        action="wake",
                    ),
                    # NEW: label-based fallback path (fix for issue #141)
                    AgentTrigger(
                        event="pull_request.labeled",
                        label="needs-changes",
                        action="wake",
                    ),
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
    github.invalidate_pr_approvals = AsyncMock(return_value=0)
    return github


def _mock_agent_defs():
    from squadron.config import AgentDefinition

    return {
        "bug-fix": AgentDefinition(
            role="bug-fix",
            raw_content="---\nname: bug-fix\n---\nYou are a bug fixer.",
            prompt="You are a bug fixer.",
            name="bug-fix",
        ),
    }


def _pr_labeled_event(pr_number: int, label: str, delivery_id: str = "labeled-1") -> GitHubEvent:
    """Build a pull_request.labeled event (as GitHub would send it)."""
    return GitHubEvent(
        delivery_id=delivery_id,
        event_type="pull_request",
        action="labeled",
        payload={
            "action": "labeled",
            "label": {"name": label, "color": "e11d48"},
            "pull_request": {
                "number": pr_number,
                "title": "Fix #10",
                "body": "Fixes #10",
                "head": {"ref": "fix/issue-10"},
                "base": {"ref": "squadron-dev"},
                "labels": [{"name": label}],
            },
            "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
        },
    )


# ── Unit test: EVENT_MAP contains pull_request.labeled ────────────────────────


class TestEventMapPRLabeled:
    """Verify pull_request.labeled is registered in the event map."""

    def test_pr_labeled_in_event_map(self):
        """pull_request.labeled must be in EVENT_MAP (regression for issue #141)."""
        assert "pull_request.labeled" in EVENT_MAP, (
            "pull_request.labeled is missing from EVENT_MAP. "
            "Without this mapping the needs-changes label fallback cannot wake "
            "the PR-owning agent (issue #141)."
        )

    def test_pr_labeled_maps_to_correct_type(self):
        """pull_request.labeled must map to PR_LABELED event type."""
        assert EVENT_MAP["pull_request.labeled"] == SquadronEventType.PR_LABELED

    def test_pr_labeled_event_type_exists(self):
        """SquadronEventType must have a PR_LABELED member."""
        assert hasattr(SquadronEventType, "PR_LABELED"), (
            "SquadronEventType.PR_LABELED is missing — add PR_LABELED to the enum."
        )


# ── Integration test: needs-changes label wakes PR-owning agent ───────────────


class TestNeedsChangesLabelWakesAgent:
    """Verify the full pipeline: label applied → event routed → agent woken."""

    async def test_sleeping_author_woken_when_needs_changes_label_applied(self, registry):
        """Sleeping PR-owning agent must be woken when needs-changes label is applied.

        This is the regression test for issue #141.  The scenario:
        1. bug-fix agent opened a PR and went to sleep.
        2. pr-review agent tried to submit REQUEST_CHANGES but got HTTP 403
           (same bot identity — all agents share the bot account).
        3. pr-review agent applied 'needs-changes' label as fallback.
        4. GitHub fires pull_request.labeled webhook.
        5. Framework must route this event and wake the sleeping bug-fix agent.
        """
        config = _config_with_needs_changes_trigger()
        github = _mock_github()
        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            mock_copilot.stop = AsyncMock()
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

            # bug-fix agent that opened PR #11 for issue #10, now sleeping
            author = AgentRecord(
                agent_id="bug-fix-issue-10",
                role="bug-fix",
                issue_number=10,
                pr_number=11,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                session_id="session-bug-fix-10",
            )
            await registry.create_agent(author)

            # pr-review agent applied 'needs-changes' label (fallback for 403)
            event = _pr_labeled_event(pr_number=11, label="needs-changes")
            await router._route_event(event)

            # bug-fix agent must now be ACTIVE (woken)
            updated_author = await registry.get_agent("bug-fix-issue-10")
            assert updated_author is not None
            assert updated_author.status == AgentStatus.ACTIVE, (
                "bug-fix agent must be woken when needs-changes label is applied to its PR. "
                "The pull_request.labeled event with label: needs-changes must trigger the "
                "wake action defined in config triggers. "
                "This is the regression test for issue #141."
            )

    async def test_other_label_does_not_wake_author(self, registry):
        """Applying a label OTHER than needs-changes must NOT wake the author agent."""
        config = _config_with_needs_changes_trigger()
        github = _mock_github()
        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            mock_copilot.stop = AsyncMock()
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

            # bug-fix agent that opened PR #13 for issue #12, now sleeping
            author = AgentRecord(
                agent_id="bug-fix-issue-12",
                role="bug-fix",
                issue_number=12,
                pr_number=13,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                session_id="session-bug-fix-12",
            )
            await registry.create_agent(author)

            # An unrelated label is applied — should NOT wake the agent
            event = _pr_labeled_event(pr_number=13, label="in-progress", delivery_id="labeled-2")
            await router._route_event(event)

            # bug-fix agent should still be sleeping
            updated_author = await registry.get_agent("bug-fix-issue-12")
            assert updated_author.status == AgentStatus.SLEEPING, (
                "Applying an unrelated label should not wake the agent. "
                "Only 'needs-changes' should trigger the wake."
            )

    async def test_needs_changes_on_different_pr_does_not_wake_agent(self, registry):
        """Applying needs-changes to a DIFFERENT PR must not wake the sleeping agent."""
        config = _config_with_needs_changes_trigger()
        github = _mock_github()
        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            mock_copilot.stop = AsyncMock()
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

            # bug-fix agent that opened PR #20, now sleeping
            author = AgentRecord(
                agent_id="bug-fix-issue-19",
                role="bug-fix",
                issue_number=19,
                pr_number=20,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                session_id="session-bug-fix-19",
            )
            await registry.create_agent(author)

            # needs-changes label applied to a DIFFERENT PR (#99) — not PR #20
            event = _pr_labeled_event(pr_number=99, label="needs-changes", delivery_id="labeled-3")
            await router._route_event(event)

            # bug-fix agent for PR #20 should still be sleeping
            updated_author = await registry.get_agent("bug-fix-issue-19")
            assert updated_author.status == AgentStatus.SLEEPING, (
                "Applying needs-changes to a different PR should not wake this agent."
            )
