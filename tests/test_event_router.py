"""Tests for the event router."""

import asyncio

import pytest
import pytest_asyncio

from squadron.config import SquadronConfig
from squadron.event_router import EVENT_MAP, EventRouter
from squadron.models import GitHubEvent, SquadronEvent, SquadronEventType
from squadron.registry import AgentRegistry


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


@pytest.fixture
def config():
    return SquadronConfig(project={"name": "test"})


@pytest_asyncio.fixture
async def router(registry, config):
    queue = asyncio.Queue()
    r = EventRouter(event_queue=queue, registry=registry, config=config)
    yield r, queue


class TestEventMapping:
    def test_all_expected_mappings_exist(self):
        expected = {
            "issues.opened",
            "issues.closed",
            "issues.assigned",
            "issues.labeled",
            "issue_comment.created",
            "pull_request.opened",
            "pull_request.closed",
            "pull_request.synchronize",
            "pull_request_review.submitted",
            "push",
        }
        assert set(EVENT_MAP.keys()) == expected

    def test_mapping_values_are_valid(self):
        for value in EVENT_MAP.values():
            assert isinstance(value, SquadronEventType)


class TestBotFilter:
    async def test_filters_bot_events(self, router):
        r, _ = router
        event = GitHubEvent(
            delivery_id="d1",
            event_type="issues",
            action="opened",
            payload={"sender": {"login": "squadron[bot]", "type": "Bot"}},
        )
        # Should silently return (filtered)
        await r._route_event(event)
        assert r.pm_queue.empty()

    async def test_passes_human_events(self, router, registry):
        r, _ = router
        event = GitHubEvent(
            delivery_id="d2",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "alice", "type": "User"},
                "issue": {"number": 1, "labels": []},
            },
        )
        await r._route_event(event)
        assert not r.pm_queue.empty()


class TestDeduplication:
    async def test_duplicate_event_filtered(self, router, registry):
        r, _ = router
        event = GitHubEvent(
            delivery_id="dup-1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "alice", "type": "User"},
                "issue": {"number": 1, "labels": []},
            },
        )
        # First time — should be processed
        await r._route_event(event)
        assert not r.pm_queue.empty()
        r.pm_queue.get_nowait()

        # Second time with same delivery_id — should be filtered
        await r._route_event(event)
        assert r.pm_queue.empty()


class TestEventConversion:
    async def test_issue_event_conversion(self, router):
        r, _ = router
        event = GitHubEvent(
            delivery_id="c1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "alice"},
                "issue": {"number": 42, "labels": []},
            },
        )
        squadron_event = r._to_squadron_event(event, SquadronEventType.ISSUE_OPENED)
        assert squadron_event.event_type == SquadronEventType.ISSUE_OPENED
        assert squadron_event.issue_number == 42
        assert squadron_event.pr_number is None
        assert squadron_event.source_delivery_id == "c1"

    async def test_pr_event_conversion(self, router):
        r, _ = router
        event = GitHubEvent(
            delivery_id="c2",
            event_type="pull_request",
            action="opened",
            payload={
                "sender": {"login": "alice"},
                "pull_request": {"number": 7},
            },
        )
        squadron_event = r._to_squadron_event(event, SquadronEventType.PR_OPENED)
        assert squadron_event.event_type == SquadronEventType.PR_OPENED
        assert squadron_event.pr_number == 7


class TestDispatch:
    async def test_pm_receives_issue_events(self, router, registry):
        r, _ = router
        event = GitHubEvent(
            delivery_id="pm-1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "alice"},
                "issue": {"number": 1, "labels": []},
            },
        )
        await r._route_event(event)
        assert not r.pm_queue.empty()
        pm_event = r.pm_queue.get_nowait()
        assert pm_event.event_type == SquadronEventType.ISSUE_OPENED

    async def test_pm_receives_pr_events(self, router, registry):
        r, _ = router
        event = GitHubEvent(
            delivery_id="pm-2",
            event_type="pull_request",
            action="opened",
            payload={
                "sender": {"login": "alice"},
                "pull_request": {"number": 3},
            },
        )
        await r._route_event(event)
        # PR events go to PM too
        assert not r.pm_queue.empty()

    async def test_handler_called(self, router, registry):
        r, _ = router
        received = []

        async def handler(event: SquadronEvent):
            received.append(event)

        r.on(SquadronEventType.ISSUE_OPENED, handler)

        event = GitHubEvent(
            delivery_id="h1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "alice"},
                "issue": {"number": 5, "labels": []},
            },
        )
        await r._route_event(event)
        assert len(received) == 1
        assert received[0].event_type == SquadronEventType.ISSUE_OPENED

    async def test_unknown_event_ignored(self, router, registry):
        r, _ = router
        event = GitHubEvent(
            delivery_id="u1",
            event_type="unknown",
            action="whatever",
            payload={"sender": {"login": "alice"}},
        )
        await r._route_event(event)
        assert r.pm_queue.empty()
