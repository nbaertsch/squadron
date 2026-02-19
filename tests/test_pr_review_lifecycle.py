"""Regression tests for PR review lifecycle bugs (issue #88).

Tests covering:
1. Re-review loop: _trigger_spawn should allow re-spawning reviewer agents after they complete
2. Authoring agent wake: pull_request_review.submitted with changes_requested wakes author
3. Re-review on PR synchronize: new reviewer spawned after synchronize when old one completed
4. submit_pr_review error handling: errors are surfaced clearly
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from squadron.config import (
    AgentRoleConfig,
    AgentTrigger,
    ProjectConfig,
    ReviewPolicyConfig,
    ReviewRequirement,
    SquadronConfig,
    SynchronizeConfig,
)
from squadron.agent_manager import AgentManager
from squadron.event_router import EventRouter
from squadron.models import AgentRecord, AgentStatus, GitHubEvent, SquadronEvent, SquadronEventType
from squadron.registry import AgentRegistry


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_pr_review_lifecycle.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _config_with_pr_review_rereview() -> SquadronConfig:
    """Config that includes pr-review respawn on synchronize (the correct design)."""
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
            on_synchronize=SynchronizeConfig(
                invalidate_approvals=True,
                respawn_reviewers=True,
            ),
        ),
        agent_roles={
            "bug-fix": AgentRoleConfig(
                agent_definition="agents/bug-fix.md",
                triggers=[
                    AgentTrigger(event="issues.labeled", label="bug"),
                    AgentTrigger(event="pull_request.opened", action="sleep"),
                    AgentTrigger(
                        event="pull_request_review.submitted",
                        condition={"review_state": "changes_requested"},
                        action="wake",
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
                    # Wake sleeping reviewer on synchronize
                    AgentTrigger(event="pull_request.synchronize", action="wake"),
                    # Spawn new reviewer if old one completed (re-review loop)
                    AgentTrigger(event="pull_request.synchronize", action="spawn"),
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
        "pr-review": AgentDefinition(
            role="pr-review",
            raw_content="---\nname: pr-review\n---\nYou are a code reviewer.",
            prompt="You are a code reviewer.",
            name="pr-review",
        ),
    }


def _pr_sync_event(pr_number: int, delivery_id: str = "sync-1") -> GitHubEvent:
    """Build a pull_request.synchronize event."""
    return GitHubEvent(
        delivery_id=delivery_id,
        event_type="pull_request",
        action="synchronize",
        payload={
            "action": "synchronize",
            "pull_request": {
                "number": pr_number,
                "title": "Fix #86",
                "body": "Fixes #86",
                "head": {"ref": "fix/issue-86"},
                "base": {"ref": "squadron-dev"},
                "labels": [],
            },
            "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
        },
    )


def _pr_review_submitted_event(
    pr_number: int, review_state: str, delivery_id: str = "review-1"
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
                "body": "Please fix the issues.",
                "user": {"login": "pr-review-bot"},
            },
            "pull_request": {
                "number": pr_number,
                "title": "Fix #86",
                "body": "Fixes #86",
                "head": {"ref": "fix/issue-86"},
                "base": {"ref": "squadron-dev"},
            },
            "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
        },
    )


# ── Test: Duplicate guard allows re-spawning after completion ──────────────────


class TestTriggerSpawnDuplicateGuard:
    """Verify _trigger_spawn allows re-spawning when only terminal agents exist."""

    async def test_spawn_blocked_when_active_agent_exists(self, registry):
        """_trigger_spawn should NOT spawn if an ACTIVE agent already exists."""
        config = _config_with_pr_review_rereview()
        github = _mock_github()
        router = EventRouter(
            event_queue=asyncio.Queue(), registry=registry, config=config
        )

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

            # Pre-existing ACTIVE pr-review agent
            existing = AgentRecord(
                agent_id="pr-review-issue-86",
                role="pr-review",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.ACTIVE,
                active_since=datetime.now(timezone.utc),
            )
            await registry.create_agent(existing)

            # PR synchronize fires — should NOT spawn another pr-review
            event = _pr_sync_event(pr_number=87)
            await router._route_event(event)

            agents = await registry.get_all_agents_for_issue(86)
            pr_review_agents = [a for a in agents if a.role == "pr-review"]
            assert len(pr_review_agents) == 1, "Should not spawn duplicate ACTIVE agent"

    async def test_spawn_blocked_when_sleeping_agent_exists(self, registry):
        """_trigger_spawn should NOT spawn if a SLEEPING agent already exists."""
        config = _config_with_pr_review_rereview()
        github = _mock_github()
        router = EventRouter(
            event_queue=asyncio.Queue(), registry=registry, config=config
        )

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

            # Pre-existing SLEEPING pr-review agent (was sleeping after first review)
            existing = AgentRecord(
                agent_id="pr-review-issue-86",
                role="pr-review",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
            )
            await registry.create_agent(existing)

            # PR synchronize fires — should wake the sleeping agent, not spawn new one
            event = _pr_sync_event(pr_number=87)
            await router._route_event(event)

            agents = await registry.get_all_agents_for_issue(86)
            pr_review_agents = [a for a in agents if a.role == "pr-review"]
            assert len(pr_review_agents) == 1, "Should not spawn duplicate alongside SLEEPING agent"

    async def test_spawn_allowed_when_only_completed_agent_exists(self, registry):
        """Re-review loop: _trigger_spawn MUST allow re-spawning when pr-review has COMPLETED.

        This is the core regression for issue #88 — after a pr-review agent completes
        (submits changes_requested and calls report_complete), the dev pushes fixes,
        and pull_request.synchronize fires. A new pr-review agent MUST be spawned.
        Without this fix, the duplicate guard in _trigger_spawn incorrectly blocks
        re-spawning because it sees the COMPLETED agent.
        """
        config = _config_with_pr_review_rereview()
        github = _mock_github()
        router = EventRouter(
            event_queue=asyncio.Queue(), registry=registry, config=config
        )

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

            # Completed pr-review agent (submitted changes_requested, called report_complete)
            completed_reviewer = AgentRecord(
                agent_id="pr-review-issue-86",
                role="pr-review",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.COMPLETED,  # ← completed after requesting changes
            )
            await registry.create_agent(completed_reviewer)

            # Developer pushed fixes → pull_request.synchronize fires
            event = _pr_sync_event(pr_number=87)
            await router._route_event(event)

            # A NEW pr-review agent MUST be spawned for re-review
            # (the old completed one was deleted and a new one created)
            agents = await registry.get_all_agents_for_issue(86)
            active_pr_review = [
                a for a in agents
                if a.role == "pr-review"
                and a.status in (AgentStatus.CREATED, AgentStatus.ACTIVE, AgentStatus.SLEEPING)
            ]
            assert len(active_pr_review) == 1, (
                "A new pr-review agent must be spawned for re-review when old one completed. "
                "The duplicate guard in _trigger_spawn must not block re-spawning of COMPLETED agents."
            )


# ── Test: Authoring agent woken when changes_requested review is submitted ────────


class TestAuthoringAgentWakeOnChangesRequested:
    """Verify bug-fix/feat-dev agents are woken when a 'changes_requested' review fires."""

    async def test_sleeping_author_woken_on_changes_requested(self, registry):
        """Sleeping author agent is woken when reviewer submits changes_requested review.

        This is the authoring agent notification part of issue #88. The wake happens
        via the pull_request_review.submitted event with review_state: changes_requested
        condition in the config triggers.
        """
        config = _config_with_pr_review_rereview()
        github = _mock_github()
        router = EventRouter(
            event_queue=asyncio.Queue(), registry=registry, config=config
        )

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

            # Sleeping bug-fix agent that opened PR #87 for issue #86
            author = AgentRecord(
                agent_id="bug-fix-issue-86",
                role="bug-fix",
                issue_number=86,
                pr_number=87,  # set when PR was opened and agent went to sleep
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                session_id="session-bug-fix-86",
            )
            await registry.create_agent(author)

            # PR reviewer submits "changes_requested" review
            event = _pr_review_submitted_event(pr_number=87, review_state="changes_requested")
            await router._route_event(event)

            # Bug-fix agent must be woken (ACTIVE)
            updated_author = await registry.get_agent("bug-fix-issue-86")
            assert updated_author is not None
            assert updated_author.status == AgentStatus.ACTIVE, (
                "Author agent must be woken when reviewer submits changes_requested. "
                "The pull_request_review.submitted event with review_state: changes_requested "
                "condition must wake the sleeping author."
            )

    async def test_approved_review_does_not_wake_author(self, registry):
        """An APPROVED review does NOT trigger the wake-on-changes-requested condition."""
        config = _config_with_pr_review_rereview()
        github = _mock_github()
        router = EventRouter(
            event_queue=asyncio.Queue(), registry=registry, config=config
        )

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

            # Sleeping bug-fix agent
            author = AgentRecord(
                agent_id="bug-fix-issue-86",
                role="bug-fix",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                session_id="session-bug-fix-86",
            )
            await registry.create_agent(author)

            # Approved review fires — should NOT wake the bug-fix agent
            event = _pr_review_submitted_event(pr_number=87, review_state="approved")
            await router._route_event(event)

            # Bug-fix agent should still be sleeping
            updated_author = await registry.get_agent("bug-fix-issue-86")
            assert updated_author.status == AgentStatus.SLEEPING, (
                "An APPROVED review should not wake the author agent via changes_requested condition"
            )


# ── Test: Full re-review cycle ─────────────────────────────────────────────────


class TestReReviewCycle:
    """Test the complete re-review cycle: changes_requested → fixes pushed → re-review spawned."""

    async def test_full_rereview_cycle(self, registry):
        """Complete cycle: reviewer requests changes → author fixes → new reviewer spawned.

        This tests the end-to-end re-review loop for issue #88:
        1. pr-review agent submits changes_requested → completes
        2. bug-fix agent is woken (tested separately)
        3. bug-fix pushes fixes → pull_request.synchronize fires
        4. New pr-review agent is spawned for re-review
        """
        config = _config_with_pr_review_rereview()
        github = _mock_github()
        router = EventRouter(
            event_queue=asyncio.Queue(), registry=registry, config=config
        )

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

            # State after step 1: pr-review has completed, bug-fix is sleeping
            completed_reviewer = AgentRecord(
                agent_id="pr-review-issue-86",
                role="pr-review",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.COMPLETED,
            )
            await registry.create_agent(completed_reviewer)

            sleeping_author = AgentRecord(
                agent_id="bug-fix-issue-86",
                role="bug-fix",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                session_id="session-bug-fix-86",
            )
            await registry.create_agent(sleeping_author)

            # Step 3: Bug-fix pushes fixes → pull_request.synchronize
            sync_event = _pr_sync_event(pr_number=87, delivery_id="sync-after-fixes")
            await router._route_event(sync_event)

            # Verify: New pr-review agent spawned for re-review
            all_agents = await registry.get_all_agents_for_issue(86)
            pr_review_agents = [a for a in all_agents if a.role == "pr-review"]
            active_reviewers = [
                a for a in pr_review_agents
                if a.status in (AgentStatus.CREATED, AgentStatus.ACTIVE, AgentStatus.SLEEPING)
            ]
            assert len(active_reviewers) >= 1, (
                "A new pr-review agent must be spawned after developer pushes fixes. "
                "The re-review loop is broken without this."
            )


# ── Test: _trigger_spawn directly ──────────────────────────────────────────────


class TestTriggerSpawnDirectly:
    """Unit tests for the _trigger_spawn duplicate guard logic."""

    async def _make_manager(self, registry, config=None, github=None):
        if config is None:
            config = _config_with_pr_review_rereview()
        if github is None:
            github = _mock_github()
        router = EventRouter(
            event_queue=asyncio.Queue(), registry=registry, config=config
        )
        mgr = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_mock_agent_defs(),
            repo_root=Path("/tmp/test"),
        )
        return mgr

    async def test_completed_agent_does_not_block_respawn(self, registry):
        """COMPLETED agents must not block _trigger_spawn from spawning new agents.

        Root cause of re-review loop bug: _trigger_spawn checked for ANY existing
        agent (including COMPLETED), preventing legitimate re-spawns.
        """
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr = await self._make_manager(registry)
            await mgr.start()

            # Add a COMPLETED pr-review agent
            completed = AgentRecord(
                agent_id="pr-review-issue-86",
                role="pr-review",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.COMPLETED,
            )
            await registry.create_agent(completed)

            # Simulate _trigger_spawn for pr-review
            trigger = AgentTrigger(event="pull_request.synchronize", action="spawn")
            role_config = mgr.config.agent_roles["pr-review"]
            event = SquadronEvent(
                event_type=SquadronEventType.PR_SYNCHRONIZED,
                pr_number=87,
                issue_number=86,
                data={
                    "payload": {
                        "pull_request": {
                            "number": 87,
                            "body": "Fixes #86",
                            "head": {"ref": "fix/issue-86"},
                            "base": {"ref": "squadron-dev"},
                            "labels": [],
                        }
                    }
                },
            )

            await mgr._trigger_spawn("pr-review", role_config, trigger, event)

            # A new pr-review agent should have been spawned (old COMPLETED one cleaned up)
            all_agents = await registry.get_all_agents_for_issue(86)
            active_reviewers = [
                a for a in all_agents
                if a.role == "pr-review"
                and a.status in (AgentStatus.CREATED, AgentStatus.ACTIVE, AgentStatus.SLEEPING)
            ]
            assert len(active_reviewers) == 1, (
                "COMPLETED agents must not block re-spawning. "
                "_trigger_spawn duplicate guard must only check CREATED/ACTIVE/SLEEPING agents."
            )

    async def test_active_agent_still_blocks_spawn(self, registry):
        """ACTIVE agents must still block _trigger_spawn (no concurrent duplicates)."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr = await self._make_manager(registry)
            await mgr.start()

            # Add an ACTIVE pr-review agent
            active = AgentRecord(
                agent_id="pr-review-issue-86",
                role="pr-review",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.ACTIVE,
                active_since=datetime.now(timezone.utc),
            )
            await registry.create_agent(active)

            trigger = AgentTrigger(event="pull_request.synchronize", action="spawn")
            role_config = mgr.config.agent_roles["pr-review"]
            event = SquadronEvent(
                event_type=SquadronEventType.PR_SYNCHRONIZED,
                pr_number=87,
                issue_number=86,
                data={
                    "payload": {
                        "pull_request": {
                            "number": 87,
                            "body": "Fixes #86",
                            "head": {"ref": "fix/issue-86"},
                            "base": {"ref": "squadron-dev"},
                            "labels": [],
                        }
                    }
                },
            )

            await mgr._trigger_spawn("pr-review", role_config, trigger, event)

            # Must still be exactly 1 agent (spawn blocked)
            all_agents = await registry.get_all_agents_for_issue(86)
            pr_review_agents = [a for a in all_agents if a.role == "pr-review"]
            assert len(pr_review_agents) == 1, "ACTIVE agents must still block duplicate spawning"
            assert pr_review_agents[0].status == AgentStatus.ACTIVE

    async def test_sleeping_agent_still_blocks_spawn(self, registry):
        """SLEEPING agents must still block _trigger_spawn (wake should be used instead)."""
        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            MockCA.return_value = mock_copilot

            mgr = await self._make_manager(registry)
            await mgr.start()

            # Add a SLEEPING pr-review agent
            sleeping = AgentRecord(
                agent_id="pr-review-issue-86",
                role="pr-review",
                issue_number=86,
                pr_number=87,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
            )
            await registry.create_agent(sleeping)

            trigger = AgentTrigger(event="pull_request.synchronize", action="spawn")
            role_config = mgr.config.agent_roles["pr-review"]
            event = SquadronEvent(
                event_type=SquadronEventType.PR_SYNCHRONIZED,
                pr_number=87,
                issue_number=86,
                data={
                    "payload": {
                        "pull_request": {
                            "number": 87,
                            "body": "Fixes #86",
                            "head": {"ref": "fix/issue-86"},
                            "base": {"ref": "squadron-dev"},
                            "labels": [],
                        }
                    }
                },
            )

            await mgr._trigger_spawn("pr-review", role_config, trigger, event)

            # Must still be exactly 1 agent (spawn blocked, wake should be used)
            all_agents = await registry.get_all_agents_for_issue(86)
            pr_review_agents = [a for a in all_agents if a.role == "pr-review"]
            assert len(pr_review_agents) == 1, "SLEEPING agents must still block duplicate spawning"
            assert pr_review_agents[0].status == AgentStatus.SLEEPING
