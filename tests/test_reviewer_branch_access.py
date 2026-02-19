"""Regression tests for issue #101: PR review agents cannot access feature branch code.

The bug:
- When a pr-review or security-review agent is spawned via a pull_request.opened trigger,
  create_agent() generates the reviewer's own branch name (e.g. "security/issue-85") and
  creates a worktree from squadron-dev — NOT the feature branch being reviewed.
- _trigger_spawn() then updates record.branch to the PR's head branch, but this is a
  metadata-only change: the worktree is already created with the wrong branch.
- Result: reviewers see only squadron-dev code, not the feature code in the PR.

The fix:
- _trigger_spawn() extracts the PR's head branch BEFORE calling create_agent() and passes
  it as override_branch.  create_agent() uses override_branch directly, so the worktree
  is created with the PR's head branch from the start.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio

from squadron.agent_manager import AgentManager
from squadron.config import (
    AgentRoleConfig,
    AgentTrigger,
    BranchNamingConfig,
    CircuitBreakerConfig,
    LabelsConfig,
    ProjectConfig,
    ReviewPolicyConfig,
    RuntimeConfig,
    SquadronConfig,
)
from squadron.models import GitHubEvent
from squadron.registry import AgentRegistry


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_reviewer_branch.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _make_config() -> SquadronConfig:
    config = MagicMock(spec=SquadronConfig)
    config.project = ProjectConfig(
        name="test-project",
        owner="testowner",
        repo="testrepo",
        default_branch="main",
    )
    config.runtime = RuntimeConfig()
    config.circuit_breakers = CircuitBreakerConfig()
    config.labels = LabelsConfig()
    config.branch_naming = BranchNamingConfig()
    config.agent_roles = {
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
        "security-review": AgentRoleConfig(
            agent_definition="agents/security-review.md",
            triggers=[
                AgentTrigger(
                    event="pull_request.opened",
                    condition={"approval_flow": True},
                ),
                AgentTrigger(event="pull_request.closed", action="complete"),
            ],
        ),
    }
    config.review_policy = ReviewPolicyConfig(enabled=False)
    config.escalation = MagicMock()
    config.escalation.default_notify = "maintainers"
    config.human_groups = {"maintainers": ["@testuser"]}
    return config


def _make_github_mock() -> AsyncMock:
    github = AsyncMock()
    github.comment_on_issue = AsyncMock(return_value={"id": 1})
    github.list_pull_requests = AsyncMock(return_value=[])
    github.list_pull_request_files = AsyncMock(return_value=[])
    github.get_pr_reviews = AsyncMock(return_value=[])
    return github


def _make_manager(config, registry, github, tmp_path) -> AgentManager:
    router = MagicMock()
    router.subscribe = MagicMock()
    mgr = AgentManager(
        config=config,
        registry=registry,
        github=github,
        router=router,
        agent_definitions={},
        repo_root=tmp_path,
    )
    mgr.owner = "testowner"
    mgr.repo = "testrepo"
    return mgr


def _pr_opened_event(pr_number: int, head_branch: str, issue_number: int) -> GitHubEvent:
    """Build a pull_request.opened event for a PR on a feature branch."""
    return GitHubEvent(
        delivery_id=f"pr-opened-{pr_number}",
        event_type="pull_request",
        action="opened",
        payload={
            "action": "opened",
            "pull_request": {
                "number": pr_number,
                "title": f"Fix #{issue_number}",
                "body": f"Fixes #{issue_number}",
                "head": {"ref": head_branch},
                "base": {"ref": "squadron-dev"},
                "labels": [],
            },
            "sender": {"login": "feat-dev[bot]", "type": "Bot"},
        },
    )


# ── Unit test: create_agent respects override_branch ──────────────────────────


class TestCreateAgentOverrideBranch:
    """create_agent() must use override_branch when provided, bypassing branch generation."""

    async def test_uses_override_branch_for_worktree(self, tmp_path, registry):
        """When override_branch is given, the agent record uses that branch."""
        config = _make_config()
        github = _make_github_mock()

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                mgr = _make_manager(config, registry, github, tmp_path)

                record = await mgr.create_agent(
                    "pr-review",
                    85,
                    override_branch="feat/issue-85",
                )

        # Branch must be the feature branch, not a generated reviewer branch
        assert record.branch == "feat/issue-85", (
            f"Expected 'feat/issue-85' but got '{record.branch}'. "
            "Reviewer agent must use the PR head branch for its worktree."
        )

    async def test_override_branch_bypasses_find_existing_pr(self, tmp_path, registry):
        """When override_branch is set, _find_existing_pr_for_issue must NOT be called."""
        config = _make_config()
        github = _make_github_mock()

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                with patch.object(
                    AgentManager,
                    "_find_existing_pr_for_issue",
                    new_callable=AsyncMock,
                ) as mock_find:
                    mgr = _make_manager(config, registry, github, tmp_path)

                    await mgr.create_agent(
                        "security-review",
                        85,
                        override_branch="feat/issue-85",
                    )

        # _find_existing_pr_for_issue should NOT be called when override_branch is set
        mock_find.assert_not_called()

    async def test_override_branch_not_set_still_uses_branch_generation(self, tmp_path, registry):
        """When override_branch is None, normal branch generation still works."""
        config = _make_config()
        github = _make_github_mock()
        # No existing PRs
        github.list_pull_requests = AsyncMock(return_value=[])

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                mgr = _make_manager(config, registry, github, tmp_path)

                record = await mgr.create_agent("pr-review", 85)

        # Should fall back to generated branch name
        assert record.branch == "pr-review/issue-85", (
            f"Expected 'pr-review/issue-85' but got '{record.branch}'. "
            "Without override_branch, normal branch generation must still work."
        )


# ── Integration test: _trigger_spawn uses PR head branch ─────────────────────


class TestTriggerSpawnPassesPrHeadBranch:
    """_trigger_spawn must pass the PR's head branch to create_agent as override_branch."""

    async def test_reviewer_worktree_uses_pr_head_branch(self, tmp_path, registry):
        """When a PR opened event fires, the reviewer's worktree branch = PR head branch."""
        config = _make_config()
        github = _make_github_mock()

        captured_override = {}

        original_create = AgentManager.create_agent

        async def capturing_create(
            self_mgr, role, issue_number, trigger_event=None, override_branch=None
        ):
            captured_override["branch"] = override_branch
            # Prevent actual worktree creation
            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                    mock_copilot = AsyncMock()
                    mock_copilot.start = AsyncMock()
                    MockCA.return_value = mock_copilot
                    return await original_create(
                        self_mgr, role, issue_number, trigger_event, override_branch
                    )

        with patch.object(AgentManager, "create_agent", capturing_create):
            mgr = _make_manager(config, registry, github, tmp_path)
            await mgr.start()

            # Simulate a config trigger with spawn action
            from squadron.models import SquadronEvent, SquadronEventType

            squadron_event = SquadronEvent(
                event_type=SquadronEventType.PR_OPENED,
                pr_number=97,
                issue_number=85,
                data={
                    "payload": {
                        "pull_request": {
                            "number": 97,
                            "title": "Fix #85",
                            "body": "Fixes #85",
                            "head": {"ref": "feat/issue-85"},
                            "base": {"ref": "squadron-dev"},
                            "labels": [],
                        }
                    }
                },
            )

            role_config = config.agent_roles["pr-review"]
            trigger = role_config.triggers[0]

            await mgr._trigger_spawn(
                role_name="pr-review",
                role_config=role_config,
                trigger=trigger,
                event=squadron_event,
            )

        # The override_branch passed to create_agent must be the PR's head branch
        assert captured_override.get("branch") == "feat/issue-85", (
            f"Expected override_branch='feat/issue-85' but got {captured_override.get('branch')!r}. "
            "_trigger_spawn must pass the PR head branch so the reviewer's worktree "
            "contains the feature code, not squadron-dev code."
        )

    async def test_reviewer_record_branch_is_feature_branch(self, tmp_path, registry):
        """After spawning, the reviewer agent record must have the PR head branch."""
        config = _make_config()
        github = _make_github_mock()

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                mgr = _make_manager(config, registry, github, tmp_path)
                await mgr.start()

                from squadron.models import SquadronEvent, SquadronEventType

                squadron_event = SquadronEvent(
                    event_type=SquadronEventType.PR_OPENED,
                    pr_number=97,
                    issue_number=85,
                    data={
                        "payload": {
                            "pull_request": {
                                "number": 97,
                                "title": "Fix #85",
                                "body": "Fixes #85",
                                "head": {"ref": "feat/issue-85"},
                                "base": {"ref": "squadron-dev"},
                                "labels": [],
                            }
                        }
                    },
                )

                role_config = config.agent_roles["pr-review"]
                trigger = role_config.triggers[0]

                await mgr._trigger_spawn(
                    role_name="pr-review",
                    role_config=role_config,
                    trigger=trigger,
                    event=squadron_event,
                )

        # The created agent's branch must be the feature branch
        agents = await registry.get_all_agents_for_issue(85)
        reviewer_agents = [a for a in agents if a.role == "pr-review"]
        assert len(reviewer_agents) == 1

        reviewer = reviewer_agents[0]
        assert reviewer.branch == "feat/issue-85", (
            f"Expected reviewer branch='feat/issue-85' but got '{reviewer.branch}'. "
            "The reviewer's branch (and therefore worktree checkout) must be the "
            "PR's feature branch so the agent can read the code under review."
        )

    async def test_no_override_branch_when_no_pr_in_event(self, tmp_path, registry):
        """When the trigger event has no PR data, no override_branch is passed."""
        config = _make_config()
        github = _make_github_mock()

        captured_override = {}

        original_create = AgentManager.create_agent

        async def capturing_create(
            self_mgr, role, issue_number, trigger_event=None, override_branch=None
        ):
            captured_override["branch"] = override_branch
            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                    mock_copilot = AsyncMock()
                    mock_copilot.start = AsyncMock()
                    MockCA.return_value = mock_copilot
                    return await original_create(
                        self_mgr, role, issue_number, trigger_event, override_branch
                    )

        with patch.object(AgentManager, "create_agent", capturing_create):
            mgr = _make_manager(config, registry, github, tmp_path)
            await mgr.start()

            from squadron.models import SquadronEvent, SquadronEventType

            # Event with no PR data
            squadron_event = SquadronEvent(
                event_type=SquadronEventType.ISSUE_LABELED,
                issue_number=85,
                pr_number=None,
                data={"payload": {"issue": {"title": "Test", "body": "", "labels": []}}},
            )

            role_config = config.agent_roles["pr-review"]
            trigger = role_config.triggers[0]

            await mgr._trigger_spawn(
                role_name="pr-review",
                role_config=role_config,
                trigger=trigger,
                event=squadron_event,
            )

        # No PR data → no override branch
        assert captured_override.get("branch") is None, (
            f"Expected override_branch=None but got {captured_override.get('branch')!r}. "
            "override_branch should only be set when there's a PR with a known head branch."
        )
