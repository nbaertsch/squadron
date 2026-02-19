"""Regression tests for issue #91: bug-fix agent opens new PR instead of using existing one.

Tests that:
1. When an existing open PR is found for an issue (via closing keywords in body),
   create_agent uses that PR's branch and PR number.
2. When an existing open PR is found via branch name pattern,
   create_agent uses that PR's branch and PR number.
3. When no existing PR is found, the normal branch name is generated.
4. The agent start prompt includes a note about the existing PR when pr_number is set.
5. _find_existing_pr_for_issue returns None gracefully when GitHub API fails.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio

from squadron.agent_manager import AgentManager
from squadron.config import (
    AgentRoleConfig,
    BranchNamingConfig,
    CircuitBreakerConfig,
    LabelsConfig,
    ProjectConfig,
    RuntimeConfig,
    SquadronConfig,
)
from squadron.models import AgentRecord, AgentStatus
from squadron.registry import AgentRegistry


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_existing_pr.db")
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
    config.agent_roles = {
        "bug-fix": AgentRoleConfig(
            agent_definition="agents/bug-fix.md",
        )
    }
    config.branch_naming = BranchNamingConfig()
    return config


def _make_github_mock() -> AsyncMock:
    github = AsyncMock()
    github.comment_on_issue = AsyncMock(return_value={"id": 1})
    github.list_pull_requests = AsyncMock(return_value=[])
    return github


def _make_manager(config, registry, github) -> AgentManager:
    router = MagicMock()
    router.subscribe = MagicMock()
    return AgentManager(
        config=config,
        registry=registry,
        github=github,
        router=router,
        agent_definitions={},
        repo_root=Path("/tmp/test"),
    )


# ── Tests for _find_existing_pr_for_issue ────────────────────────────────────


class TestFindExistingPrForIssue:
    """Unit tests for the _find_existing_pr_for_issue helper."""

    def _make_mgr(self) -> AgentManager:
        config = _make_config()
        registry = MagicMock(spec=AgentRegistry)
        github = _make_github_mock()
        mgr = _make_manager(config, registry, github)
        mgr.owner = "testowner"
        mgr.repo = "testrepo"
        return mgr

    async def test_finds_pr_by_closing_keyword_in_body(self):
        """PR with 'Fixes #86' in body should be detected."""
        mgr = self._make_mgr()
        mgr.github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 99,
                    "body": "This PR fixes #86 by patching the auth module.",
                    "head": {"ref": "some-branch"},
                }
            ]
        )

        result = await mgr._find_existing_pr_for_issue(86)

        assert result is not None
        assert result["number"] == 99

    async def test_finds_pr_by_closes_keyword(self):
        """PR with 'Closes #86' in body should be detected."""
        mgr = self._make_mgr()
        mgr.github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 100,
                    "body": "Closes #86",
                    "head": {"ref": "fix/issue-86"},
                }
            ]
        )

        result = await mgr._find_existing_pr_for_issue(86)

        assert result is not None
        assert result["number"] == 100

    async def test_finds_pr_by_branch_name_pattern(self):
        """PR with branch 'fix/issue-86' should be detected even without closing keyword."""
        mgr = self._make_mgr()
        mgr.github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 101,
                    "body": "Some description without closing keywords",
                    "head": {"ref": "fix/issue-86"},
                }
            ]
        )

        result = await mgr._find_existing_pr_for_issue(86)

        assert result is not None
        assert result["number"] == 101

    async def test_finds_pr_by_feat_branch_pattern(self):
        """PR with branch 'feat/issue-86' should be detected."""
        mgr = self._make_mgr()
        mgr.github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 102,
                    "body": "",
                    "head": {"ref": "feat/issue-86"},
                }
            ]
        )

        result = await mgr._find_existing_pr_for_issue(86)

        assert result is not None
        assert result["number"] == 102

    async def test_returns_none_when_no_matching_pr(self):
        """No PR linked to issue → returns None."""
        mgr = self._make_mgr()
        mgr.github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 103,
                    "body": "Fixes #99",
                    "head": {"ref": "fix/issue-99"},
                }
            ]
        )

        result = await mgr._find_existing_pr_for_issue(86)

        assert result is None

    async def test_returns_none_on_api_error(self):
        """GitHub API failure → returns None gracefully (no exception)."""
        mgr = self._make_mgr()
        mgr.github.list_pull_requests = AsyncMock(side_effect=Exception("API error"))

        result = await mgr._find_existing_pr_for_issue(86)

        assert result is None

    async def test_returns_none_for_empty_pr_list(self):
        """Empty PR list → returns None."""
        mgr = self._make_mgr()
        mgr.github.list_pull_requests = AsyncMock(return_value=[])

        result = await mgr._find_existing_pr_for_issue(86)

        assert result is None

    async def test_does_not_false_match_similar_issue_number(self):
        """PR for issue #860 should not match issue #86."""
        mgr = self._make_mgr()
        mgr.github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 104,
                    "body": "Fixes #860",
                    "head": {"ref": "fix/issue-860"},
                }
            ]
        )

        result = await mgr._find_existing_pr_for_issue(86)

        assert result is None


# ── Tests for create_agent using existing PR branch ───────────────────────────


class TestCreateAgentExistingPrBranch:
    """Tests that create_agent reuses the existing PR branch when one is found."""

    async def test_uses_existing_pr_branch_when_found(self, tmp_path, registry):
        """create_agent uses the existing PR's head branch, not a generated name."""
        config = _make_config()
        github = _make_github_mock()
        github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 99,
                    "body": "Fixes #86",
                    "head": {"ref": "existing-pr-branch"},
                }
            ]
        )

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                mgr = _make_manager(config, registry, github)
                mgr.owner = "testowner"
                mgr.repo = "testrepo"
                # Mock sandbox to avoid unix socket creation in WSL
                mgr._sandbox = MagicMock()
                mgr._sandbox.create_session = AsyncMock()

                record = await mgr.create_agent("bug-fix", 86)

        # Branch should be from the existing PR, not the generated name
        assert record.branch == "existing-pr-branch", (
            f"Expected 'existing-pr-branch' but got '{record.branch}'. "
            "Agent should reuse the existing PR's head branch."
        )
        # PR number should be recorded
        assert record.pr_number == 99, (
            f"Expected pr_number=99 but got {record.pr_number}. "
            "Agent should record the existing PR number."
        )

    async def test_uses_generated_branch_when_no_existing_pr(self, tmp_path, registry):
        """create_agent generates a fresh branch when no existing PR is found."""
        config = _make_config()
        github = _make_github_mock()
        github.list_pull_requests = AsyncMock(return_value=[])

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                mgr = _make_manager(config, registry, github)
                mgr.owner = "testowner"
                mgr.repo = "testrepo"

                record = await mgr.create_agent("bug-fix", 86)

        # Should use the generated branch name
        assert record.branch == "fix/issue-86", (
            f"Expected 'fix/issue-86' but got '{record.branch}'."
        )
        # No existing PR number
        assert record.pr_number is None

    async def test_pr_number_set_when_existing_pr_found(self, tmp_path, registry):
        """create_agent sets pr_number on the record when reusing an existing PR."""
        config = _make_config()
        github = _make_github_mock()
        github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "body": "Resolves #86",
                    "head": {"ref": "fix/issue-86"},
                }
            ]
        )

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            with patch.object(
                AgentManager, "_create_worktree", new_callable=AsyncMock, return_value=tmp_path
            ):
                mgr = _make_manager(config, registry, github)
                mgr.owner = "testowner"
                mgr.repo = "testrepo"

                record = await mgr.create_agent("bug-fix", 86)

        assert record.pr_number == 42


# ── Tests for _build_agent_prompt with existing PR ────────────────────────────


class TestBuildAgentPromptWithExistingPr:
    """Tests that the agent prompt includes existing PR info when pr_number is set."""

    def _make_mgr(self) -> AgentManager:
        config = _make_config()
        registry = MagicMock(spec=AgentRegistry)
        github = _make_github_mock()
        return _make_manager(config, registry, github)

    def _make_record(self, pr_number: int | None = None) -> AgentRecord:
        return AgentRecord(
            agent_id="bug-fix-issue-86",
            role="bug-fix",
            issue_number=86,
            status=AgentStatus.ACTIVE,
            branch="fix/issue-86",
            pr_number=pr_number,
        )

    def test_prompt_includes_existing_pr_warning_when_pr_number_set(self):
        """When pr_number is set, the prompt warns not to open a new PR."""
        mgr = self._make_mgr()
        record = self._make_record(pr_number=99)

        prompt = mgr._build_agent_prompt(record, trigger_event=None)

        assert "99" in prompt, "Prompt should include the existing PR number"
        # Should warn about the existing PR
        assert any(
            phrase in prompt for phrase in ["Existing PR", "existing PR", "existing pull request"]
        ), "Prompt should mention the existing PR"
        # Should instruct not to open a new PR
        assert any(
            phrase in prompt
            for phrase in [
                "do NOT open a new PR",
                "not open a new PR",
                "do not open a new PR",
            ]
        ), "Prompt should instruct agent not to open a new PR"

    def test_prompt_does_not_include_pr_warning_when_no_pr_number(self):
        """When pr_number is not set, the prompt should not include PR warning."""
        mgr = self._make_mgr()
        record = self._make_record(pr_number=None)

        prompt = mgr._build_agent_prompt(record, trigger_event=None)

        assert "Existing PR" not in prompt
        assert "do NOT open a new PR" not in prompt

    def test_prompt_includes_branch_name(self):
        """Prompt should always include the branch name."""
        mgr = self._make_mgr()
        record = self._make_record(pr_number=None)
        record.branch = "fix/issue-86"

        prompt = mgr._build_agent_prompt(record, trigger_event=None)

        assert "fix/issue-86" in prompt
