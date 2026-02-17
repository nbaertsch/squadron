"""Tests for Squadron agent registry."""

import pytest_asyncio

from squadron.models import AgentRecord, AgentStatus
from squadron.registry import AgentRegistry


@pytest_asyncio.fixture
async def registry(tmp_path):
    """Create a fresh registry for each test."""
    db_path = str(tmp_path / "test_registry.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _make_agent(
    agent_id: str = "feat-dev-issue-1",
    role: str = "feat-dev",
    issue_number: int = 1,
    status: AgentStatus = AgentStatus.CREATED,
    **kwargs,
) -> AgentRecord:
    return AgentRecord(
        agent_id=agent_id,
        role=role,
        issue_number=issue_number,
        status=status,
        **kwargs,
    )


class TestCRUD:
    async def test_create_and_get(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)

        fetched = await registry.get_agent("feat-dev-issue-1")
        assert fetched is not None
        assert fetched.agent_id == "feat-dev-issue-1"
        assert fetched.role == "feat-dev"
        assert fetched.issue_number == 1

    async def test_get_nonexistent(self, registry: AgentRegistry):
        result = await registry.get_agent("nonexistent")
        assert result is None

    async def test_get_by_issue(self, registry: AgentRegistry):
        agent = _make_agent(status=AgentStatus.ACTIVE)
        await registry.create_agent(agent)

        found = await registry.get_agent_by_issue(1)
        assert found is not None
        assert found.agent_id == "feat-dev-issue-1"

    async def test_get_by_issue_ignores_completed(self, registry: AgentRegistry):
        agent = _make_agent(status=AgentStatus.COMPLETED)
        await registry.create_agent(agent)

        found = await registry.get_agent_by_issue(1)
        assert found is None

    async def test_get_by_status(self, registry: AgentRegistry):
        await registry.create_agent(_make_agent("a1", issue_number=1, status=AgentStatus.ACTIVE))
        await registry.create_agent(_make_agent("a2", issue_number=2, status=AgentStatus.SLEEPING))
        await registry.create_agent(_make_agent("a3", issue_number=3, status=AgentStatus.ACTIVE))

        active = await registry.get_agents_by_status(AgentStatus.ACTIVE)
        assert len(active) == 2

        sleeping = await registry.get_agents_by_status(AgentStatus.SLEEPING)
        assert len(sleeping) == 1

    async def test_update_agent(self, registry: AgentRegistry):
        agent = _make_agent(status=AgentStatus.CREATED)
        await registry.create_agent(agent)

        agent.status = AgentStatus.ACTIVE
        agent.branch = "feat/issue-1"
        await registry.update_agent(agent)

        fetched = await registry.get_agent("feat-dev-issue-1")
        assert fetched.status == AgentStatus.ACTIVE
        assert fetched.branch == "feat/issue-1"

    async def test_get_all_active(self, registry: AgentRegistry):
        await registry.create_agent(_make_agent("a1", issue_number=1, status=AgentStatus.ACTIVE))
        await registry.create_agent(_make_agent("a2", issue_number=2, status=AgentStatus.SLEEPING))
        await registry.create_agent(_make_agent("a3", issue_number=3, status=AgentStatus.COMPLETED))
        await registry.create_agent(_make_agent("a4", issue_number=4, status=AgentStatus.CREATED))

        active = await registry.get_all_active_agents()
        assert len(active) == 3  # CREATED + ACTIVE + SLEEPING


class TestBlockers:
    async def test_add_blocker(self, registry: AgentRegistry):
        agent = _make_agent(status=AgentStatus.ACTIVE)
        await registry.create_agent(agent)

        success = await registry.add_blocker("feat-dev-issue-1", 99)
        assert success is True

        fetched = await registry.get_agent("feat-dev-issue-1")
        assert 99 in fetched.blocked_by

    async def test_remove_blocker(self, registry: AgentRegistry):
        agent = _make_agent(status=AgentStatus.ACTIVE, blocked_by=[99, 100])
        await registry.create_agent(agent)

        await registry.remove_blocker("feat-dev-issue-1", 99)
        fetched = await registry.get_agent("feat-dev-issue-1")
        assert 99 not in fetched.blocked_by
        assert 100 in fetched.blocked_by

    async def test_get_agents_blocked_by(self, registry: AgentRegistry):
        a1 = _make_agent("a1", issue_number=1, status=AgentStatus.SLEEPING, blocked_by=[10])
        a2 = _make_agent("a2", issue_number=2, status=AgentStatus.SLEEPING, blocked_by=[10, 20])
        a3 = _make_agent("a3", issue_number=3, status=AgentStatus.SLEEPING, blocked_by=[20])
        await registry.create_agent(a1)
        await registry.create_agent(a2)
        await registry.create_agent(a3)

        blocked = await registry.get_agents_blocked_by(10)
        assert len(blocked) == 2
        ids = {a.agent_id for a in blocked}
        assert ids == {"a1", "a2"}

    async def test_cycle_detection_simple(self, registry: AgentRegistry):
        """A blocks B, B tries to block A → cycle detected."""
        a = _make_agent("a", issue_number=1, status=AgentStatus.ACTIVE, blocked_by=[2])
        b = _make_agent("b", issue_number=2, status=AgentStatus.ACTIVE)
        await registry.create_agent(a)
        await registry.create_agent(b)

        # B tries to block on issue 1 (which A is working on)
        success = await registry.add_blocker("b", 1)
        assert success is False  # Cycle detected

    async def test_cycle_detection_transitive(self, registry: AgentRegistry):
        """A blocks B, B blocks C, C tries to block A → cycle."""
        a = _make_agent("a", issue_number=1, status=AgentStatus.ACTIVE, blocked_by=[2])
        b = _make_agent("b", issue_number=2, status=AgentStatus.ACTIVE, blocked_by=[3])
        c = _make_agent("c", issue_number=3, status=AgentStatus.ACTIVE)
        await registry.create_agent(a)
        await registry.create_agent(b)
        await registry.create_agent(c)

        # C tries to block on issue 1 → would create A→B→C→A cycle
        success = await registry.add_blocker("c", 1)
        assert success is False

    async def test_no_false_cycle(self, registry: AgentRegistry):
        """Unrelated blocker should not trigger cycle detection."""
        a = _make_agent("a", issue_number=1, status=AgentStatus.ACTIVE, blocked_by=[2])
        b = _make_agent("b", issue_number=2, status=AgentStatus.ACTIVE)
        c = _make_agent("c", issue_number=3, status=AgentStatus.ACTIVE)
        await registry.create_agent(a)
        await registry.create_agent(b)
        await registry.create_agent(c)

        # C blocks on issue 2 — no cycle (A→B, C→B is a DAG)
        success = await registry.add_blocker("c", 2)
        assert success is True


class TestWebhookDedup:
    async def test_mark_and_check(self, registry: AgentRegistry):
        assert await registry.has_seen_event("delivery-1") is False

        await registry.mark_event_seen("delivery-1", "issues.opened")
        assert await registry.has_seen_event("delivery-1") is True

    async def test_idempotent_mark(self, registry: AgentRegistry):
        await registry.mark_event_seen("delivery-1", "issues.opened")
        await registry.mark_event_seen("delivery-1", "issues.opened")  # No error
        assert await registry.has_seen_event("delivery-1") is True
