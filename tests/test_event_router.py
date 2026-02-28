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
    """Config with 'alice' in the maintainers group for tests that use human senders."""
    return SquadronConfig(
        project={"name": "test"},
        human_groups={"maintainers": ["alice"]},
    )


@pytest.fixture
def config_no_maintainers():
    """Config with no maintainers group — only bot events are permitted."""
    return SquadronConfig(project={"name": "test"})


@pytest_asyncio.fixture
async def router(registry, config):
    queue = asyncio.Queue()
    r = EventRouter(event_queue=queue, registry=registry, config=config)
    yield r, queue


@pytest_asyncio.fixture
async def router_no_maintainers(registry, config_no_maintainers):
    queue = asyncio.Queue()
    r = EventRouter(event_queue=queue, registry=registry, config=config_no_maintainers)
    yield r, queue


class TestEventMapping:
    def test_all_expected_mappings_exist(self):
        expected = {
            "issues.opened",
            "issues.reopened",
            "issues.closed",
            "issues.assigned",
            "issues.labeled",
            "issue_comment.created",
            "pull_request.opened",
            "pull_request.closed",
            "pull_request.synchronize",
            "pull_request_review.submitted",
            "pull_request_review_comment.created",
            "push",
        }
        assert set(EVENT_MAP.keys()) == expected

    def test_mapping_values_are_valid(self):
        for value in EVENT_MAP.values():
            assert isinstance(value, SquadronEventType)


class TestBotEvents:
    """Bot-originated events (squadron-dev[bot]) are always permitted regardless
    of the maintainers list, to avoid self-blocking on bot-generated events."""

    async def test_routes_bot_events(self, router, registry):
        """Bot-originated events are always permitted — maintainer gate does not block them."""
        r, _ = router
        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

        event = GitHubEvent(
            delivery_id="d1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
                "issue": {"number": 1, "labels": []},
            },
        )
        await r._route_event(event)
        assert handler_called.is_set(), "Bot events must be routed (bot identity always permitted)"

    async def test_passes_human_events(self, router, registry):
        """Events from listed maintainers are permitted."""
        r, _ = router
        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

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
        assert handler_called.is_set()

    async def test_allows_bot_label_events(self, router, registry):
        """Bot-originated issues.labeled must pass through to handlers
        (this is how PM label → agent spawn works)."""
        r, _ = router
        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_LABELED, mock_handler)

        event = GitHubEvent(
            delivery_id="d-label-bot",
            event_type="issues",
            action="labeled",
            payload={
                "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
                "issue": {"number": 42, "labels": [{"name": "feature"}]},
                "label": {"name": "feature"},
            },
        )
        await r._route_event(event)

        # Handler MUST be called (agent spawn depends on this)
        assert handler_called.is_set(), "Bot label event must reach handlers"

    async def test_allows_bot_pr_opened_events(self, router, registry):
        """Bot-originated pull_request.opened must pass through to handlers
        (dev agents open PRs which trigger review agents)."""
        r, _ = router
        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.PR_OPENED, mock_handler)

        event = GitHubEvent(
            delivery_id="d-pr-bot",
            event_type="pull_request",
            action="opened",
            payload={
                "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
                "pull_request": {"number": 10, "labels": [], "head": {"ref": "feat/issue-42"}},
            },
        )
        await r._route_event(event)

        assert handler_called.is_set(), "Bot PR opened event must reach handlers"

    async def test_bot_permitted_with_empty_maintainers_list(self, router_no_maintainers, registry):
        """Bot identity is permitted even when maintainers list is empty."""
        r, _ = router_no_maintainers
        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

        event = GitHubEvent(
            delivery_id="d-bot-no-maint",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
                "issue": {"number": 5, "labels": []},
            },
        )
        await r._route_event(event)
        assert handler_called.is_set(), "Bot must be permitted even with empty maintainers list"


class TestMaintainerFilter:
    """Tests for the inbound maintainer gate (issue #137)."""

    async def test_listed_maintainer_event_is_processed(self, registry):
        """Event from a listed maintainer is processed normally."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["alice", "bob"]},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

        event = GitHubEvent(
            delivery_id="m-listed-1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "alice", "type": "User"},
                "issue": {"number": 10, "labels": []},
            },
        )
        await r._route_event(event)
        assert handler_called.is_set(), "Listed maintainer event must be processed"

    async def test_unlisted_user_event_is_dropped(self, registry):
        """Event from an unlisted user is dropped; no handler is called."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["alice"]},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

        event = GitHubEvent(
            delivery_id="m-unlisted-1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "mallory", "type": "User"},
                "issue": {"number": 11, "labels": []},
            },
        )
        await r._route_event(event)
        assert not handler_called.is_set(), "Unlisted user event must be silently dropped"

    async def test_unlisted_user_comment_is_dropped(self, registry):
        """Comment from an unlisted user is dropped — no command spawning occurs."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["alice"]},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_COMMENT, mock_handler)

        event = GitHubEvent(
            delivery_id="m-unlisted-comment-1",
            event_type="issue_comment",
            action="created",
            payload={
                "sender": {"login": "attacker", "type": "User"},
                "issue": {"number": 42, "labels": []},
                "comment": {"body": "@squadron-dev pm: please spawn an agent"},
            },
        )
        await r._route_event(event)
        assert not handler_called.is_set(), "Unlisted user command must be silently dropped"

    async def test_unlisted_user_label_event_is_dropped(self, registry):
        """Label event from an unlisted user does not trigger agent spawning."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["alice"]},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_LABELED, mock_handler)

        event = GitHubEvent(
            delivery_id="m-unlisted-label-1",
            event_type="issues",
            action="labeled",
            payload={
                "sender": {"login": "external-contributor", "type": "User"},
                "issue": {"number": 20, "labels": [{"name": "feature"}]},
                "label": {"name": "feature"},
            },
        )
        await r._route_event(event)
        assert not handler_called.is_set(), "Label event from unlisted user must be dropped"

    async def test_bot_identity_always_permitted(self, registry):
        """The squadron-dev[bot] identity is always permitted regardless of maintainers list."""
        # Even with empty maintainers, bot events pass through
        config = SquadronConfig(project={"name": "test"})
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

        event = GitHubEvent(
            delivery_id="m-bot-always-1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
                "issue": {"number": 30, "labels": []},
            },
        )
        await r._route_event(event)
        assert handler_called.is_set(), "Bot identity must always be permitted"

    async def test_empty_maintainers_list_drops_all_human_events(self, registry):
        """With no maintainers group, all non-bot human events are dropped."""
        config = SquadronConfig(project={"name": "test"})
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

        event = GitHubEvent(
            delivery_id="m-empty-1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "any-human-user", "type": "User"},
                "issue": {"number": 50, "labels": []},
            },
        )
        await r._route_event(event)
        assert not handler_called.is_set(), "Empty maintainers list must drop all human events"

    async def test_maintainer_username_matching_is_case_insensitive(self, registry):
        """Maintainer username matching is case-insensitive."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["Alice"]},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

        # Sender login is lowercase, config has uppercase — should still match
        event = GitHubEvent(
            delivery_id="m-case-1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "alice", "type": "User"},
                "issue": {"number": 60, "labels": []},
            },
        )
        await r._route_event(event)
        assert handler_called.is_set(), "Maintainer matching must be case-insensitive"

    async def test_none_sender_event_is_dropped(self, registry):
        """Events with no sender key in payload are dropped (sender is None)."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["alice"]},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        handler_called = asyncio.Event()

        async def mock_handler(event: SquadronEvent):
            handler_called.set()

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

        # Payload has no "sender" key — event.sender returns None
        event = GitHubEvent(
            delivery_id="m-none-sender-1",
            event_type="issues",
            action="opened",
            payload={
                "issue": {"number": 70, "labels": []},
            },
        )
        await r._route_event(event)
        assert not handler_called.is_set(), "Event with no sender must be dropped"

    def test_is_actor_permitted_listed_user(self, registry):
        """_is_actor_permitted returns True for a listed maintainer."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["alice", "bob"]},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        event = GitHubEvent(
            delivery_id="perm-1",
            event_type="issues",
            action="opened",
            payload={"sender": {"login": "alice", "type": "User"}, "issue": {"number": 1}},
        )
        assert r._is_actor_permitted(event) is True

    def test_is_actor_permitted_unlisted_user(self, registry):
        """_is_actor_permitted returns False for an unlisted user."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["alice"]},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        event = GitHubEvent(
            delivery_id="perm-2",
            event_type="issues",
            action="opened",
            payload={"sender": {"login": "mallory", "type": "User"}, "issue": {"number": 1}},
        )
        assert r._is_actor_permitted(event) is False

    def test_is_actor_permitted_bot_identity(self, registry):
        """_is_actor_permitted returns True for the configured bot identity."""
        config = SquadronConfig(
            project={"name": "test", "bot_username": "squadron-dev[bot]"},
        )
        queue = asyncio.Queue()
        r = EventRouter(event_queue=queue, registry=registry, config=config)

        event = GitHubEvent(
            delivery_id="perm-3",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
                "issue": {"number": 1},
            },
        )
        assert r._is_actor_permitted(event) is True


class TestDeduplication:
    async def test_duplicate_event_filtered(self, router, registry):
        r, _ = router
        received = []

        async def mock_handler(event: SquadronEvent):
            received.append(event)

        r.on(SquadronEventType.ISSUE_OPENED, mock_handler)

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
        assert len(received) == 1

        # Second time with same delivery_id — should be filtered
        await r._route_event(event)
        assert len(received) == 1


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
        received = []

        async def handler(event: SquadronEvent):
            received.append(event)

        # Register handler for all types — none should fire for unknown
        r.on(SquadronEventType.ISSUE_OPENED, handler)

        event = GitHubEvent(
            delivery_id="u1",
            event_type="unknown",
            action="whatever",
            payload={"sender": {"login": "alice"}},
        )
        await r._route_event(event)
        assert len(received) == 0

    async def test_multiple_handlers_called(self, router, registry):
        """Multiple handlers for the same event type should all be called."""
        r, _ = router
        calls = []

        async def handler_a(event: SquadronEvent):
            calls.append("a")

        async def handler_b(event: SquadronEvent):
            calls.append("b")

        r.on(SquadronEventType.ISSUE_OPENED, handler_a)
        r.on(SquadronEventType.ISSUE_OPENED, handler_b)

        event = GitHubEvent(
            delivery_id="multi-1",
            event_type="issues",
            action="opened",
            payload={
                "sender": {"login": "alice"},
                "issue": {"number": 1, "labels": []},
            },
        )
        await r._route_event(event)
        assert calls == ["a", "b"]
