"""Integration test — full happy-path event flow with mocked externals.

Simulates the lifecycle from ACTION-PLAN 1.4:
1. Issue opened → PM spawns (ephemeral, triages)
2. Issue labeled "feature" → feat-dev spawns
3. feat-dev opens PR → feat-dev sleeps, pr-review spawns (via approval_flow)
4. Reviewer submits "changes_requested" → feat-dev wakes
5. PR merged → feat-dev completes, pr-review completes

All GitHub API calls and Copilot SDK sessions are mocked.
This validates the config-driven trigger system end-to-end.
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
    ApprovalFlowConfig,
    ProjectConfig,
    SquadronConfig,
)
from squadron.agent_manager import AgentManager
from squadron.event_router import EventRouter
from squadron.models import AgentRecord, AgentStatus, GitHubEvent
from squadron.registry import AgentRegistry


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_happy.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _example_config() -> SquadronConfig:
    """Config mirroring the example .squadron/config.yaml."""
    return SquadronConfig(
        project=ProjectConfig(
            name="test-project",
            owner="testowner",
            repo="testrepo",
            default_branch="main",
        ),
        approval_flows=ApprovalFlowConfig(
            enabled=True,
            default_reviewers=["pr-review"],
        ),
        agent_roles={
            "pm": AgentRoleConfig(
                agent_definition="agents/pm.md",
                singleton=True,
                lifecycle="ephemeral",
                triggers=[
                    AgentTrigger(event="issues.opened"),
                ],
            ),
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
                        event="pull_request.closed",
                        condition={"merged": True},
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
                    AgentTrigger(event="pull_request.closed", action="complete"),
                ],
            ),
        },
    )


def _mock_github():
    github = AsyncMock()
    github.comment_on_issue = AsyncMock(return_value={"id": 1})
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
    """Minimal agent definitions for each role."""
    from squadron.config import AgentDefinition

    return {
        "pm": AgentDefinition(
            role="pm",
            raw_content="---\nname: pm\n---\nYou are the PM.",
            prompt="You are the PM.",
            name="pm",
        ),
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


# ── Helper to simulate webhook events ───────────────────────────────────────


def _github_event(
    event_type: str,
    action: str | None = None,
    issue_number: int | None = None,
    pr_number: int | None = None,
    sender: str = "humanuser",
    label: str | None = None,
    merged: bool | None = None,
    review_state: str | None = None,
    delivery_id: str | None = None,
) -> GitHubEvent:
    """Build a GitHubEvent matching the EventRouter's expected format."""
    payload: dict = {"sender": {"login": sender}}

    if issue_number:
        payload["issue"] = {"number": issue_number, "title": "Test Issue", "body": "Body"}
    if pr_number:
        payload["pull_request"] = {
            "number": pr_number,
            "title": "PR Title",
            "body": "Closes #42",
            "head": {"ref": "feat/issue-42"},
            "base": {"ref": "main"},
        }
        if merged is not None:
            payload["pull_request"]["merged"] = merged
    if label:
        payload["label"] = {"name": label}
    if action:
        payload["action"] = action
    if review_state:
        payload["review"] = {"state": review_state}

    full_type = f"{event_type}.{action}" if action else event_type

    return GitHubEvent(
        delivery_id=delivery_id or f"delivery-{full_type}-{issue_number or pr_number}",
        event_type=event_type.split(".")[0] if "." in event_type else event_type,
        action=action,
        full_type=full_type,
        sender=sender,
        payload=payload,
        issue=payload.get("issue"),
        pull_request=payload.get("pull_request"),
    )


# ── Integration Test ─────────────────────────────────────────────────────────


class TestHappyPathFlow:
    """Test the complete trigger-driven event flow without running real agents."""

    async def test_issue_opened_spawns_pm(self, registry):
        """Step 1: Opening an issue triggers PM spawn."""
        config = _example_config()
        github = _mock_github()
        event_queue = asyncio.Queue()

        router = EventRouter(
            event_queue=event_queue,
            registry=registry,
            config=config,
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

            # Simulate issue opened event
            event = _github_event("issues", action="opened", issue_number=42)
            await event_queue.put(event)

            # Route the event
            await router._route_event(event)

            # Check PM agent was created
            agents = await registry.get_agents_for_issue(42)
            pm_agents = [a for a in agents if a.role == "pm"]
            assert len(pm_agents) == 1
            assert pm_agents[0].status == AgentStatus.ACTIVE

    async def test_issue_labeled_spawns_dev(self, registry):
        """Step 2: Labeling issue 'feature' triggers feat-dev spawn."""
        config = _example_config()
        github = _mock_github()
        event_queue = asyncio.Queue()

        router = EventRouter(
            event_queue=event_queue,
            registry=registry,
            config=config,
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

            event = _github_event("issues", action="labeled", issue_number=42, label="feature")
            await router._route_event(event)

            agents = await registry.get_agents_for_issue(42)
            dev_agents = [a for a in agents if a.role == "feat-dev"]
            assert len(dev_agents) == 1
            assert dev_agents[0].status == AgentStatus.ACTIVE

    async def test_pr_opened_sleeps_dev_and_spawns_reviewer(self, registry):
        """Step 3: PR opened → feat-dev sleeps, pr-review spawns."""
        config = _example_config()
        github = _mock_github()
        event_queue = asyncio.Queue()

        router = EventRouter(
            event_queue=event_queue,
            registry=registry,
            config=config,
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

            # First, create a feat-dev agent for issue #42
            dev_record = AgentRecord(
                agent_id="feat-dev-issue-42",
                role="feat-dev",
                issue_number=42,
                status=AgentStatus.ACTIVE,
                active_since=datetime.now(timezone.utc),
                pr_number=10,
            )
            await registry.create_agent(dev_record)

            # Simulate PR opened by bot
            event = _github_event(
                "pull_request",
                action="opened",
                pr_number=10,
                issue_number=42,
                sender="squadron[bot]",
            )
            await router._route_event(event)

            # Dev should transition to SLEEPING
            updated_dev = await registry.get_agent("feat-dev-issue-42")
            assert updated_dev.status == AgentStatus.SLEEPING

            # pr-review should be spawned (via approval_flow)
            all_agents = await registry.get_agents_for_issue(42)
            # The PR-spawned reviewer uses PR number as issue fallback
            review_agents = [a for a in all_agents if a.role == "pr-review"]
            # Or check by PR number
            if not review_agents:
                # Reviewer might be registered under the PR number as issue
                all_status = await registry.get_agents_by_status(AgentStatus.ACTIVE)
                review_agents = [a for a in all_status if a.role == "pr-review"]
            assert len(review_agents) >= 1

    async def test_changes_requested_wakes_dev(self, registry):
        """Step 4: Review with changes_requested → feat-dev wakes."""
        config = _example_config()
        github = _mock_github()
        event_queue = asyncio.Queue()

        router = EventRouter(
            event_queue=event_queue,
            registry=registry,
            config=config,
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

            # Create a sleeping feat-dev agent
            dev_record = AgentRecord(
                agent_id="feat-dev-issue-42",
                role="feat-dev",
                issue_number=42,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                pr_number=10,
                session_id="session-123",
            )
            await registry.create_agent(dev_record)

            event = _github_event(
                "pull_request_review",
                action="submitted",
                pr_number=10,
                review_state="changes_requested",
            )
            await router._route_event(event)

            # Dev should be woken (ACTIVE)
            updated_dev = await registry.get_agent("feat-dev-issue-42")
            assert updated_dev.status == AgentStatus.ACTIVE

    async def test_pr_merged_completes_agents(self, registry):
        """Step 5: PR merged → feat-dev woken for cleanup, pr-review completes."""
        config = _example_config()
        github = _mock_github()
        event_queue = asyncio.Queue()

        router = EventRouter(
            event_queue=event_queue,
            registry=registry,
            config=config,
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

            # Create sleeping dev and active reviewer for PR #10
            dev_record = AgentRecord(
                agent_id="feat-dev-issue-42",
                role="feat-dev",
                issue_number=42,
                status=AgentStatus.SLEEPING,
                sleeping_since=datetime.now(timezone.utc),
                pr_number=10,
            )
            review_record = AgentRecord(
                agent_id="pr-review-issue-10",
                role="pr-review",
                issue_number=10,
                status=AgentStatus.ACTIVE,
                active_since=datetime.now(timezone.utc),
                pr_number=10,
            )
            await registry.create_agent(dev_record)
            await registry.create_agent(review_record)

            # Simulate PR closed + merged
            event = _github_event(
                "pull_request",
                action="closed",
                pr_number=10,
                issue_number=42,
                merged=True,
            )
            await router._route_event(event)

            # Dev should be woken up for cleanup (trigger action: wake, condition: merged=true)
            updated_dev = await registry.get_agent("feat-dev-issue-42")
            assert updated_dev.status == AgentStatus.ACTIVE

            # Reviewer should be completed (trigger action: complete on PR closed)
            updated_review = await registry.get_agent("pr-review-issue-10")
            assert updated_review.status == AgentStatus.COMPLETED

    async def test_reassignment_aborts_agent(self, registry):
        """D-12: Reassigning issue away from bot stops the agent."""
        config = _example_config()
        github = _mock_github()
        event_queue = asyncio.Queue()

        router = EventRouter(
            event_queue=event_queue,
            registry=registry,
            config=config,
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

            dev_record = AgentRecord(
                agent_id="feat-dev-issue-42",
                role="feat-dev",
                issue_number=42,
                status=AgentStatus.ACTIVE,
                active_since=datetime.now(timezone.utc),
            )
            await registry.create_agent(dev_record)

            event = _github_event(
                "issues",
                action="assigned",
                issue_number=42,
                sender="humandev",
            )
            # Add assignee to payload
            event.payload["assignee"] = {"login": "humandev"}
            await router._route_event(event)

            updated = await registry.get_agent("feat-dev-issue-42")
            assert updated.status == AgentStatus.COMPLETED

            # Should have posted a comment
            github.comment_on_issue.assert_called_once()

    async def test_label_mismatch_does_not_spawn(self, registry):
        """Labeling with 'bug' should not trigger feat-dev (requires 'feature')."""
        config = _example_config()
        github = _mock_github()
        event_queue = asyncio.Queue()

        router = EventRouter(
            event_queue=event_queue,
            registry=registry,
            config=config,
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

            # No bug-fix role in our trimmed config
            event = _github_event("issues", action="labeled", issue_number=42, label="docs")
            await router._route_event(event)

            agents = await registry.get_agents_for_issue(42)
            assert len(agents) == 0

    async def test_duplicate_spawn_prevented(self, registry):
        """Second labeled event for same issue should not create duplicate."""
        config = _example_config()
        github = _mock_github()
        event_queue = asyncio.Queue()

        router = EventRouter(
            event_queue=event_queue,
            registry=registry,
            config=config,
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

            event1 = _github_event(
                "issues",
                action="labeled",
                issue_number=42,
                label="feature",
                delivery_id="d1",
            )
            event2 = _github_event(
                "issues",
                action="labeled",
                issue_number=42,
                label="feature",
                delivery_id="d2",
            )
            await router._route_event(event1)
            await router._route_event(event2)

            agents = await registry.get_agents_for_issue(42)
            dev_agents = [a for a in agents if a.role == "feat-dev"]
            assert len(dev_agents) == 1  # only one, not two
