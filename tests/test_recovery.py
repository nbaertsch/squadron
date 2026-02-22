"""Tests for recovery.py — GitHub-based state reconstruction on restart.

Validates the full recovery flow:
  Phase 1: Stale ACTIVE/CREATED agents → FAILED
  Phase 2: Reconstruct from GitHub issues (labels → role inference)
  Phase 3: Reconstruct from GitHub PRs (branch → role inference)

Also tests helper functions:
  - _infer_role_from_labels
  - _infer_role_from_branch
  - _infer_branch
  - _extract_blocker_refs
  - _extract_issue_ref
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest_asyncio

from squadron.config import (
    AgentRoleConfig,
    BranchNamingConfig,
    ProjectConfig,
    SquadronConfig,
)
from squadron.models import AgentRecord, AgentStatus
from squadron.recovery import (
    BRANCH_RE,
    _extract_blocker_refs,
    _extract_issue_ref,
    _infer_branch,
    _infer_role_from_branch,
    _infer_role_from_labels,
    recover_on_startup,
)
from squadron.registry import AgentRegistry


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_recovery.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _config() -> SquadronConfig:
    return SquadronConfig(
        project=ProjectConfig(
            name="test", owner="testowner", repo="testrepo", default_branch="main"
        ),
        agent_roles={
            "feat-dev": AgentRoleConfig(
                agent_definition="agents/feat-dev.md",
            ),
            "bug-fix": AgentRoleConfig(
                agent_definition="agents/bug-fix.md",
            ),
            "pr-review": AgentRoleConfig(
                agent_definition="agents/pr-review.md",
            ),
        },
    )


def _github():
    gh = AsyncMock()
    gh.comment_on_issue = AsyncMock()
    gh.list_issues = AsyncMock(return_value=[])
    gh.list_pull_requests = AsyncMock(return_value=[])
    return gh


# ── Phase 1: Fail stale agents ──────────────────────────────────────────────


class TestFailStaleAgents:
    async def test_active_agents_marked_failed(self, registry):
        """ACTIVE agents from a previous run should be moved to FAILED."""
        agent = AgentRecord(
            agent_id="feat-dev-issue-1",
            role="feat-dev",
            issue_number=1,
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        github = _github()
        summary = await recover_on_startup(_config(), registry, github)

        updated = await registry.get_agent("feat-dev-issue-1")
        assert updated.status == AgentStatus.FAILED
        assert updated.active_since is None
        assert summary["failed"] == 1

    async def test_created_agents_marked_failed(self, registry):
        """CREATED agents from a previous run should also be FAILED."""
        agent = AgentRecord(
            agent_id="feat-dev-issue-2",
            role="feat-dev",
            issue_number=2,
            status=AgentStatus.CREATED,
        )
        await registry.create_agent(agent)

        github = _github()
        summary = await recover_on_startup(_config(), registry, github)

        updated = await registry.get_agent("feat-dev-issue-2")
        assert updated.status == AgentStatus.FAILED
        assert summary["failed"] == 1

    async def test_sleeping_agents_not_touched(self, registry):
        """SLEEPING agents should NOT be marked FAILED — let reconciliation handle."""
        agent = AgentRecord(
            agent_id="feat-dev-issue-3",
            role="feat-dev",
            issue_number=3,
            status=AgentStatus.SLEEPING,
            sleeping_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        github = _github()
        summary = await recover_on_startup(_config(), registry, github)

        updated = await registry.get_agent("feat-dev-issue-3")
        assert updated.status == AgentStatus.SLEEPING
        assert summary["failed"] == 0

    async def test_posts_comment_on_failed(self, registry):
        """Should post a comment on the issue when marking FAILED."""
        agent = AgentRecord(
            agent_id="feat-dev-issue-4",
            role="feat-dev",
            issue_number=4,
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        github = _github()
        await recover_on_startup(_config(), registry, github)

        github.comment_on_issue.assert_called()
        call_args = github.comment_on_issue.call_args
        assert call_args[0][2] == 4  # issue_number
        assert "FAILED" in call_args[0][3] or "failed" in call_args[0][3]


# ── Phase 2: Reconstruct from issues ────────────────────────────────────────


class TestReconstructFromIssues:
    async def test_reconstructs_blocked_issue_as_sleeping(self, registry):
        """Issue with 'blocked' label → SLEEPING agent record."""
        github = _github()

        async def _list_issues(*args, **kw):
            if kw.get("labels") == "blocked":
                return [
                    {"number": 10, "labels": [{"name": "blocked"}, {"name": "feature"}], "body": ""}
                ]
            return []

        github.list_issues = AsyncMock(side_effect=_list_issues)

        summary = await recover_on_startup(_config(), registry, github)

        agents = await registry.get_agents_for_issue(10)
        assert len(agents) == 1
        assert agents[0].role == "feat-dev"
        assert agents[0].status == AgentStatus.SLEEPING
        assert summary["sleeping"] >= 1

    async def test_reconstructs_in_progress_as_failed(self, registry):
        """Issue with 'in-progress' label → FAILED (can't run, no session)."""
        github = _github()

        async def _list_issues(*args, **kw):
            if kw.get("labels") == "in-progress":
                return [
                    {
                        "number": 11,
                        "labels": [{"name": "in-progress"}, {"name": "feature"}],
                        "body": "",
                    }
                ]
            return []

        github.list_issues = _list_issues

        summary = await recover_on_startup(_config(), registry, github)

        agent = await registry.get_agent("feat-dev-issue-11")
        assert agent is not None
        assert agent.status == AgentStatus.FAILED
        assert summary["reconstructed"] >= 1

    async def test_reconstructs_needs_human_as_escalated(self, registry):
        """Issue with 'needs-human' label → ESCALATED."""
        github = _github()

        async def _list_issues(*args, **kw):
            if kw.get("labels") == "needs-human":
                return [
                    {"number": 12, "labels": [{"name": "needs-human"}, {"name": "bug"}], "body": ""}
                ]
            return []

        github.list_issues = AsyncMock(side_effect=_list_issues)

        await recover_on_startup(_config(), registry, github)

        agent = await registry.get_agent("bug-fix-issue-12")
        assert agent is not None
        assert agent.status == AgentStatus.ESCALATED

    async def test_skips_existing_agents(self, registry):
        """Don't reconstruct if we already have a record for this role + issue."""
        existing = AgentRecord(
            agent_id="feat-dev-issue-20",
            role="feat-dev",
            issue_number=20,
            status=AgentStatus.SLEEPING,
        )
        await registry.create_agent(existing)

        github = _github()

        async def _list_issues(*args, **kw):
            if kw.get("labels") == "in-progress":
                return [
                    {
                        "number": 20,
                        "labels": [{"name": "in-progress"}, {"name": "feature"}],
                        "body": "",
                    }
                ]
            return []

        github.list_issues = AsyncMock(side_effect=_list_issues)

        summary = await recover_on_startup(_config(), registry, github)
        assert summary["skipped"] >= 1
        # Should NOT have changed the existing agent
        agent = await registry.get_agent("feat-dev-issue-20")
        assert agent.status == AgentStatus.SLEEPING

    async def test_skips_unknown_role(self, registry):
        """Issues that can't be mapped to a configured role are skipped."""
        github = _github()

        async def _list_issues(*args, **kw):
            if kw.get("labels") == "in-progress":
                return [
                    {
                        "number": 30,
                        "labels": [{"name": "in-progress"}, {"name": "unknown-label"}],
                        "body": "",
                    }
                ]
            return []

        github.list_issues = AsyncMock(side_effect=_list_issues)

        summary = await recover_on_startup(_config(), registry, github)
        agents = await registry.get_agents_for_issue(30)
        assert len(agents) == 0
        assert summary["skipped"] >= 1


# ── Phase 3: Reconstruct from PRs ───────────────────────────────────────────


class TestReconstructFromPRs:
    async def test_reconstructs_from_open_pr(self, registry):
        """Open PR on squadron branch → SLEEPING agent record."""
        github = _github()
        github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 50,
                    "head": {"ref": "feat/issue-42"},
                    "body": "Fixes #42",
                }
            ]
        )

        summary = await recover_on_startup(_config(), registry, github)

        agents = await registry.get_agents_for_issue(42)
        assert len(agents) == 1
        assert agents[0].role == "feat-dev"
        assert agents[0].status == AgentStatus.SLEEPING
        assert agents[0].pr_number == 50
        assert summary["sleeping"] >= 1

    async def test_skips_non_squadron_branches(self, registry):
        """PRs on non-squadron branches are ignored."""
        github = _github()
        github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 51,
                    "head": {"ref": "dependabot/npm_and_yarn/lodash-4.17.21"},
                    "body": "Bump lodash",
                }
            ]
        )

        await recover_on_startup(_config(), registry, github)
        all_agents = await registry.get_agents_by_status(AgentStatus.SLEEPING)
        assert len(all_agents) == 0

    async def test_updates_pr_number_on_existing(self, registry):
        """If agent record exists without PR, update the PR number."""
        existing = AgentRecord(
            agent_id="feat-dev-issue-42",
            role="feat-dev",
            issue_number=42,
            status=AgentStatus.SLEEPING,
        )
        await registry.create_agent(existing)

        github = _github()
        github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 55,
                    "head": {"ref": "feat/issue-42"},
                    "body": "Fixes #42",
                }
            ]
        )

        summary = await recover_on_startup(_config(), registry, github)

        agent = await registry.get_agent("feat-dev-issue-42")
        assert agent.pr_number == 55
        assert summary["skipped"] >= 1  # didn't create new record

    async def test_extracts_issue_from_pr_body(self, registry):
        """Uses 'Fixes #N' from PR body when branch number differs."""
        github = _github()
        github.list_pull_requests = AsyncMock(
            return_value=[
                {
                    "number": 60,
                    "head": {"ref": "feat/issue-99"},
                    "body": "Fixes #99\n\nImplements the feature.",
                }
            ]
        )

        await recover_on_startup(_config(), registry, github)
        agents = await registry.get_agents_for_issue(99)
        assert len(agents) == 1


# ── No owner/repo configured ────────────────────────────────────────────────


class TestRecoveryEdgeCases:
    async def test_no_owner_repo_skips_reconstruction(self, registry):
        """If no owner/repo, only fail stale agents (phase 1), skip GitHub queries."""
        config = SquadronConfig(
            project=ProjectConfig(name="test", owner="", repo=""),
        )
        agent = AgentRecord(
            agent_id="test-1",
            role="feat-dev",
            issue_number=1,
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        github = _github()
        summary = await recover_on_startup(config, registry, github)

        assert summary["failed"] == 1
        assert summary["reconstructed"] == 0
        # GitHub API should NOT have been called for listing
        github.list_issues.assert_not_called()

    async def test_github_api_failure_doesnt_crash(self, registry):
        """If GitHub API throws, recovery should still complete."""
        github = _github()
        github.list_issues = AsyncMock(side_effect=Exception("API down"))
        github.list_pull_requests = AsyncMock(side_effect=Exception("API down"))

        summary = await recover_on_startup(_config(), registry, github)
        # Should not raise — errors are logged but not propagated
        assert summary["failed"] == 0
        assert summary["reconstructed"] == 0


# ── Helper function tests ───────────────────────────────────────────────────


class TestInferRoleFromLabels:
    def test_matches_trigger_label(self):
        config = _config()
        role = _infer_role_from_labels({"feature", "in-progress"}, config)
        assert role == "feat-dev"

    def test_matches_bug_label(self):
        config = _config()
        role = _infer_role_from_labels({"bug", "critical"}, config)
        assert role == "bug-fix"

    def test_fallback_heuristic(self):
        """Falls back to LABEL_ROLE_MAP for labels not in triggers."""
        config = _config()
        role = _infer_role_from_labels({"security"}, config)
        # security isn't a trigger label, but fallback has security → security-review
        # (only if the role exists in config)
        assert role is None  # security-review is in config but doesn't have a labeled trigger

    def test_unknown_labels(self):
        config = _config()
        role = _infer_role_from_labels({"random-label"}, config)
        assert role is None


class TestInferRoleFromBranch:
    def test_feat_branch(self):
        config = _config()
        assert _infer_role_from_branch("feat/issue-42", config) == "feat-dev"

    def test_fix_branch(self):
        config = _config()
        assert _infer_role_from_branch("fix/issue-10", config) == "bug-fix"

    def test_unknown_branch(self):
        config = _config()
        assert _infer_role_from_branch("chore/cleanup", config) is None

    def test_branch_prefix_not_in_config(self):
        """If the inferred role isn't in config.agent_roles, returns None."""
        config = SquadronConfig(
            project=ProjectConfig(name="t", owner="o", repo="r"),
            agent_roles={},
        )
        assert _infer_role_from_branch("feat/issue-1", config) is None


class TestInferBranch:
    def test_feat_dev_branch(self):
        bc = BranchNamingConfig()
        assert _infer_branch("feat-dev", 42, bc) == "feat/issue-42"

    def test_bug_fix_branch(self):
        bc = BranchNamingConfig()
        assert _infer_branch("bug-fix", 7, bc) == "fix/issue-7"

    def test_unknown_role_generic_template(self):
        bc = BranchNamingConfig()
        result = _infer_branch("custom-role", 99, bc)
        assert "99" in result


class TestBranchRegex:
    def test_matches_feat(self):
        m = BRANCH_RE.match("feat/issue-42")
        assert m and m.group(1) == "42"

    def test_matches_fix(self):
        m = BRANCH_RE.match("fix/issue-10")
        assert m and m.group(1) == "10"

    def test_no_match_random(self):
        assert BRANCH_RE.match("main") is None

    def test_no_match_dependabot(self):
        assert BRANCH_RE.match("dependabot/npm/lodash-4.17") is None


class TestExtractBlockerRefs:
    def test_blocking_ref(self):
        assert _extract_blocker_refs("Blocking #42 for now") == [42]

    def test_blocked_by_ref(self):
        assert _extract_blocker_refs("Blocked by #10") == [10]

    def test_multiple_refs(self):
        refs = _extract_blocker_refs("Blocking #5 and blocked by #10")
        assert 5 in refs
        assert 10 in refs

    def test_no_refs(self):
        assert _extract_blocker_refs("No blockers here") == []


class TestExtractIssueRef:
    def test_fixes_ref(self):
        assert _extract_issue_ref("Fixes #42") == 42

    def test_closes_ref(self):
        assert _extract_issue_ref("Closes #99") == 99

    def test_resolves_ref(self):
        assert _extract_issue_ref("Resolves #7") == 7

    def test_case_insensitive(self):
        assert _extract_issue_ref("fixes #10") == 10

    def test_no_ref(self):
        assert _extract_issue_ref("Just some description") is None
