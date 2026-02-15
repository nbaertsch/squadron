"""Tests for ReconciliationLoop — blocker checks, stale agent detection, wake callbacks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from squadron.config import CircuitBreakerConfig, CircuitBreakerDefaults, ProjectConfig, SquadronConfig
from squadron.models import AgentRecord, AgentRole, AgentStatus
from squadron.reconciliation import ReconciliationLoop
from squadron.registry import AgentRegistry


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_reconciliation.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _config(**overrides) -> SquadronConfig:
    defaults = dict(project=ProjectConfig(name="test", owner="testowner", repo="testrepo"))
    defaults.update(overrides)
    return SquadronConfig(**defaults)


def _make_agent(
    agent_id: str = "feat-dev-issue-42",
    role: AgentRole = AgentRole.FEAT_DEV,
    issue_number: int = 42,
    status: AgentStatus = AgentStatus.SLEEPING,
    **kwargs,
) -> AgentRecord:
    return AgentRecord(
        agent_id=agent_id,
        role=role,
        issue_number=issue_number,
        status=status,
        **kwargs,
    )


def _make_github(**overrides) -> AsyncMock:
    github = AsyncMock()
    github.get_issue = AsyncMock(return_value={"state": "open"})
    github.create_issue = AsyncMock(return_value={"number": 999})
    for k, v in overrides.items():
        setattr(github, k, v)
    return github


def _make_loop(registry, github=None, on_wake=None, config=None) -> ReconciliationLoop:
    return ReconciliationLoop(
        config=config or _config(),
        registry=registry,
        github=github or _make_github(),
        owner="testowner",
        repo="testrepo",
        on_wake_agent=on_wake,
    )


# ── Sleeping Agent Checks ───────────────────────────────────────────────────


class TestCheckSleepingAgents:
    async def test_no_sleeping_agents_is_noop(self, registry):
        loop = _make_loop(registry)
        await loop._check_sleeping_agents()
        # Should not raise

    async def test_sleeping_with_no_blockers_skipped(self, registry):
        agent = _make_agent(status=AgentStatus.SLEEPING, blocked_by=[])
        await registry.create_agent(agent)

        on_wake = AsyncMock()
        loop = _make_loop(registry, on_wake=on_wake)
        await loop._check_sleeping_agents()

        on_wake.assert_not_called()

    async def test_blocker_resolved_wakes_agent(self, registry):
        agent = _make_agent(
            status=AgentStatus.SLEEPING,
            blocked_by=[99],
            sleeping_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        # GitHub says blocker issue is closed
        github = _make_github()
        github.get_issue = AsyncMock(return_value={"state": "closed"})

        on_wake = AsyncMock()
        loop = _make_loop(registry, github=github, on_wake=on_wake)
        await loop._check_sleeping_agents()

        on_wake.assert_called_once()
        assert on_wake.call_args[0][0] == agent.agent_id

    async def test_blocker_still_open_stays_sleeping(self, registry):
        agent = _make_agent(
            status=AgentStatus.SLEEPING,
            blocked_by=[99],
            sleeping_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        github = _make_github()
        github.get_issue = AsyncMock(return_value={"state": "open"})

        on_wake = AsyncMock()
        loop = _make_loop(registry, github=github, on_wake=on_wake)
        await loop._check_sleeping_agents()

        on_wake.assert_not_called()

    async def test_max_sleep_exceeded_escalates(self, registry):
        agent = _make_agent(
            status=AgentStatus.SLEEPING,
            blocked_by=[99],
            sleeping_since=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        await registry.create_agent(agent)

        loop = _make_loop(registry)
        await loop._check_sleeping_agents()

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.ESCALATED

    async def test_github_error_does_not_crash(self, registry):
        agent = _make_agent(
            status=AgentStatus.SLEEPING,
            blocked_by=[99],
            sleeping_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        github = _make_github()
        github.get_issue = AsyncMock(side_effect=Exception("API error"))

        loop = _make_loop(registry, github=github)
        await loop._check_sleeping_agents()

        # Agent should still be sleeping
        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.SLEEPING

    async def test_multiple_blockers_all_must_resolve(self, registry):
        agent = _make_agent(
            status=AgentStatus.SLEEPING,
            blocked_by=[99, 100],
            sleeping_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        # Only one blocker resolved
        github = _make_github()
        call_count = 0

        async def get_issue_side_effect(owner, repo, num):
            nonlocal call_count
            call_count += 1
            if num == 99:
                return {"state": "closed"}
            return {"state": "open"}

        github.get_issue = AsyncMock(side_effect=get_issue_side_effect)

        on_wake = AsyncMock()
        loop = _make_loop(registry, github=github, on_wake=on_wake)
        await loop._check_sleeping_agents()

        # Should NOT wake — still blocked by #100
        on_wake.assert_not_called()


# ── Stale Active Agent Checks ───────────────────────────────────────────────


class TestCheckStaleActiveAgents:
    async def test_no_active_agents_is_noop(self, registry):
        loop = _make_loop(registry)
        await loop._check_stale_active_agents()
        # Should not raise

    async def test_fresh_active_agent_not_escalated(self, registry):
        agent = _make_agent(
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)

        loop = _make_loop(registry)
        await loop._check_stale_active_agents()

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.ACTIVE

    async def test_stale_agent_escalated(self, registry):
        # Default max_active_duration is 7200s (2h)
        agent = _make_agent(
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        await registry.create_agent(agent)

        github = _make_github()
        loop = _make_loop(registry, github=github)
        await loop._check_stale_active_agents()

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.ESCALATED

    async def test_stale_agent_creates_github_issue(self, registry):
        agent = _make_agent(
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        await registry.create_agent(agent)

        github = _make_github()
        loop = _make_loop(registry, github=github)
        await loop._check_stale_active_agents()

        github.create_issue.assert_called_once()
        call_kwargs = github.create_issue.call_args
        assert "exceeded" in call_kwargs[1].get("title", call_kwargs[0][2] if len(call_kwargs[0]) > 2 else "")

    async def test_agent_without_active_since_skipped(self, registry):
        agent = _make_agent(status=AgentStatus.ACTIVE, active_since=None)
        await registry.create_agent(agent)

        loop = _make_loop(registry)
        await loop._check_stale_active_agents()

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.ACTIVE

    async def test_github_error_during_escalation_still_escalates(self, registry):
        agent = _make_agent(
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        await registry.create_agent(agent)

        github = _make_github()
        github.create_issue = AsyncMock(side_effect=Exception("Rate limited"))

        loop = _make_loop(registry, github=github)
        await loop._check_stale_active_agents()

        # Agent should still be escalated even if issue creation fails
        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.ESCALATED


# ── Reconcile (full pass) ───────────────────────────────────────────────────


class TestReconcile:
    async def test_full_pass_runs_both_checks(self, registry):
        loop = _make_loop(registry)

        # Patch internal methods to verify they're called
        loop._check_sleeping_agents = AsyncMock()
        loop._check_stale_active_agents = AsyncMock()

        await loop.reconcile()

        loop._check_sleeping_agents.assert_called_once()
        loop._check_stale_active_agents.assert_called_once()
