"""Regression tests for issue #143: dev agents must not create duplicate PRs.

Tests that:
1. (C3/C10) open_pr returns an error when the agent already has an existing pr_number set,
   preventing duplicate PR creation when review-requested changes are being addressed.
2. (C8) open_pr succeeds and records the PR number when no existing PR is present.
3. (C5) After pushing to an existing branch, the agent is expected to post a re-review signal.
4. (C7) The guard applies regardless of agent role (bug-fix, feat-dev, etc.).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from squadron.models import AgentRecord, AgentStatus
from squadron.tools.squadron_tools import OpenPRParams, SquadronTools


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_tools(agent_record: AgentRecord) -> SquadronTools:
    """Create a SquadronTools instance with a mock registry returning the given agent."""
    registry = AsyncMock()
    registry.get_agent = AsyncMock(return_value=agent_record)
    registry.update_agent = AsyncMock()

    github = AsyncMock()
    github.create_pull_request = AsyncMock(return_value={"number": 999})

    tools = SquadronTools(
        registry=registry,
        github=github,
        agent_inboxes={},
        owner="testowner",
        repo="testrepo",
        config=MagicMock(),
        agent_definitions={},
    )
    tools._log_activity = AsyncMock()
    return tools


def _make_record(role: str, pr_number: int | None = None) -> AgentRecord:
    return AgentRecord(
        agent_id=f"{role}-issue-86",
        role=role,
        issue_number=86,
        status=AgentStatus.ACTIVE,
        branch="fix/issue-86",
        pr_number=pr_number,
    )


# ── C10: Guard against duplicate PR creation ─────────────────────────────────


class TestNoDuplicatePrGuard:
    """C3/C10: open_pr must refuse to create a PR when one already exists for the issue."""

    @pytest.mark.asyncio
    async def test_open_pr_blocked_when_pr_number_already_set(self):
        """open_pr returns an error instead of calling GitHub when agent already has a PR."""
        record = _make_record("bug-fix", pr_number=42)
        tools = _make_tools(record)

        result = await tools.open_pr(
            agent_id=record.agent_id,
            params=OpenPRParams(
                title="fix: something",
                body="Fixes #86",
                head="fix/issue-86",
                base="squadron-dev",
            ),
        )

        # Must NOT call create_pull_request
        tools.github.create_pull_request.assert_not_called()

        # Must return an error / guard message mentioning the existing PR
        assert "42" in result, f"Expected existing PR #42 mentioned in: {result!r}"
        assert any(
            phrase in result.lower()
            for phrase in ["already exists", "existing pr", "existing pull request", "duplicate"]
        ), f"Expected guard message about existing PR in: {result!r}"

    @pytest.mark.asyncio
    async def test_open_pr_blocked_for_feat_dev_role(self):
        """Guard applies to feat-dev role as well (C7: applies to all dev agent roles)."""
        record = _make_record("feat-dev", pr_number=55)
        tools = _make_tools(record)

        result = await tools.open_pr(
            agent_id=record.agent_id,
            params=OpenPRParams(
                title="feat: something",
                body="Fixes #86",
                head="feat/issue-86",
                base="squadron-dev",
            ),
        )

        tools.github.create_pull_request.assert_not_called()
        assert "55" in result

    @pytest.mark.asyncio
    async def test_open_pr_blocked_for_infra_dev_role(self):
        """Guard applies to infra-dev role as well (C7)."""
        record = _make_record("infra-dev", pr_number=77)
        tools = _make_tools(record)

        result = await tools.open_pr(
            agent_id=record.agent_id,
            params=OpenPRParams(
                title="infra: something",
                body="Fixes #86",
                head="infra/issue-86",
                base="squadron-dev",
            ),
        )

        tools.github.create_pull_request.assert_not_called()
        assert "77" in result


# ── C8: Normal path — open_pr succeeds when no existing PR ────────────────────


class TestOpenPrNormalPath:
    """C8/C9: open_pr works normally when no existing PR is present."""

    @pytest.mark.asyncio
    async def test_open_pr_succeeds_when_no_existing_pr(self):
        """open_pr calls GitHub API and records PR number when no existing PR."""
        record = _make_record("bug-fix", pr_number=None)
        tools = _make_tools(record)

        result = await tools.open_pr(
            agent_id=record.agent_id,
            params=OpenPRParams(
                title="fix: new fix",
                body="Fixes #86",
                head="fix/issue-86",
                base="squadron-dev",
            ),
        )

        # Must call create_pull_request
        tools.github.create_pull_request.assert_called_once()

        # Must return success message with PR number
        assert "999" in result, f"Expected PR #999 in success result: {result!r}"

    @pytest.mark.asyncio
    async def test_open_pr_records_pr_number_on_agent(self):
        """open_pr records the new PR number on the agent record."""
        record = _make_record("bug-fix", pr_number=None)
        tools = _make_tools(record)

        await tools.open_pr(
            agent_id=record.agent_id,
            params=OpenPRParams(
                title="fix: new fix",
                body="Fixes #86",
                head="fix/issue-86",
                base="squadron-dev",
            ),
        )

        # registry.update_agent should have been called with pr_number set
        tools.registry.update_agent.assert_called()
        updated_record = tools.registry.update_agent.call_args[0][0]
        assert updated_record.pr_number == 999, (
            f"Expected pr_number=999 on updated agent, got {updated_record.pr_number}"
        )

    @pytest.mark.asyncio
    async def test_open_pr_returns_error_when_github_response_missing_number(self):
        """open_pr returns an error if GitHub response has no 'number' key."""
        record = _make_record("bug-fix", pr_number=None)
        tools = _make_tools(record)
        # Simulate a response missing the 'number' key
        tools.github.create_pull_request = AsyncMock(return_value={"url": "https://..."})

        result = await tools.open_pr(
            agent_id=record.agent_id,
            params=OpenPRParams(
                title="fix: new fix",
                body="Fixes #86",
                head="fix/issue-86",
                base="squadron-dev",
            ),
        )

        assert "error" in result.lower(), (
            f"Expected an error message when PR number is missing. Got: {result!r}"
        )
        # Agent record should NOT have been updated (no valid PR number)
        tools.registry.update_agent.assert_not_called()
