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
            "issues.reopened",
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


class TestBotEvents:
    """All events — including from the bot — are routed.  Loop protection
    relies on dedup, singleton, duplicate-agent, and circuit-breaker guards,
    NOT on filtering the sender."""

    async def test_routes_bot_events(self, router, registry):
        """Bot-originated events are NOT filtered — they pass through."""
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
                "sender": {"login": "squadron[bot]", "type": "Bot"},
                "issue": {"number": 1, "labels": []},
            },
        )
        await r._route_event(event)
        assert handler_called.is_set(), "Bot events must be routed (no sender filtering)"

    async def test_passes_human_events(self, router, registry):
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
                "sender": {"login": "squadron[bot]", "type": "Bot"},
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
                "sender": {"login": "squadron[bot]", "type": "Bot"},
                "pull_request": {"number": 10, "labels": [], "head": {"ref": "feat/issue-42"}},
            },
        )
        await r._route_event(event)

        assert handler_called.is_set(), "Bot PR opened event must reach handlers"


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


class TestPRReviewCommentEventRouting:
    """Test event routing for new PR review comment events."""

    @pytest.fixture
    def event_router(self):
        """Create an EventRouter for testing."""
        event_queue = asyncio.Queue()
        registry = MagicMock()
        config = MagicMock()
        return EventRouter(event_queue, registry, config)

    def test_pr_review_comment_events_in_event_map(self):
        """Test that new events are properly mapped."""
        from squadron.event_router import EVENT_MAP
        from squadron.models import SquadronEventType
        
        expected_mappings = {
            "pull_request_review_comment.created": SquadronEventType.PR_REVIEW_COMMENT_CREATED,
            "pull_request_review_comment.edited": SquadronEventType.PR_REVIEW_COMMENT_EDITED,
            "pull_request_review_comment.deleted": SquadronEventType.PR_REVIEW_COMMENT_DELETED,
        }
        
        for github_event, squadron_type in expected_mappings.items():
            assert github_event in EVENT_MAP
            assert EVENT_MAP[github_event] == squadron_type

    def test_pr_review_comment_created_squadron_event(self, event_router):
        """Test conversion of PR review comment created event."""
        from squadron.models import GitHubEvent, SquadronEventType
        
        github_event = GitHubEvent(
            delivery_id="test-123",
            event_type="pull_request_review_comment",
            action="created",
            payload={
                "comment": {
                    "id": 456,
                    "body": "This needs refactoring @squadron-dev feat-dev: please update",
                    "path": "src/module.py",
                    "line": 25,
                    "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/42",
                    "user": {"login": "reviewer"},
                },
                "pull_request": {"number": 42, "title": "Feature addition"},
                "sender": {"login": "reviewer", "type": "User"},
            }
        )
        
        squadron_event = event_router._to_squadron_event(
            github_event, SquadronEventType.PR_REVIEW_COMMENT_CREATED
        )
        
        assert squadron_event.event_type == SquadronEventType.PR_REVIEW_COMMENT_CREATED
        assert squadron_event.pr_number == 42
        assert squadron_event.issue_number is None
        assert squadron_event.command is not None
        assert squadron_event.source_delivery_id == "test-123"

    def test_pr_review_comment_edited_squadron_event(self, event_router):
        """Test conversion of PR review comment edited event."""
        from squadron.models import GitHubEvent, SquadronEventType
        
        github_event = GitHubEvent(
            delivery_id="test-456",
            event_type="pull_request_review_comment",
            action="edited",
            payload={
                "comment": {
                    "id": 789,
                    "body": "Updated comment: This looks better now!",
                    "path": "src/module.py",
                    "line": 25,
                    "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/43",
                    "user": {"login": "reviewer"},
                },
                "pull_request": {"number": 43, "title": "Bug fix"},
                "sender": {"login": "reviewer", "type": "User"},
            }
        )
        
        squadron_event = event_router._to_squadron_event(
            github_event, SquadronEventType.PR_REVIEW_COMMENT_EDITED
        )
        
        assert squadron_event.event_type == SquadronEventType.PR_REVIEW_COMMENT_EDITED
        assert squadron_event.pr_number == 43

    def test_pr_review_comment_deleted_squadron_event(self, event_router):
        """Test conversion of PR review comment deleted event."""
        from squadron.models import GitHubEvent, SquadronEventType
        
        github_event = GitHubEvent(
            delivery_id="test-789",
            event_type="pull_request_review_comment",
            action="deleted",
            payload={
                "comment": {
                    "id": 999,
                    "body": "(deleted)",
                    "path": "src/module.py",
                    "line": 30,
                    "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/44",
                    "user": {"login": "reviewer"},
                },
                "pull_request": {"number": 44, "title": "Hotfix"},
                "sender": {"login": "reviewer", "type": "User"},
            }
        )
        
        squadron_event = event_router._to_squadron_event(
            github_event, SquadronEventType.PR_REVIEW_COMMENT_DELETED
        )
        
        assert squadron_event.event_type == SquadronEventType.PR_REVIEW_COMMENT_DELETED
        assert squadron_event.pr_number == 44

    def test_pr_number_extraction_from_url_only(self, event_router):
        """Test PR number extraction when only pull_request_url is available."""
        from squadron.models import GitHubEvent, SquadronEventType
        
        github_event = GitHubEvent(
            delivery_id="test-url-extraction",
            event_type="pull_request_review_comment", 
            action="created",
            payload={
                "comment": {
                    "id": 111,
                    "body": "Comment without direct PR object",
                    "path": "src/test.py",
                    "line": 10,
                    "pull_request_url": "https://api.github.com/repos/test/repo/pulls/777",
                    "user": {"login": "commenter"},
                },
                "sender": {"login": "commenter", "type": "User"},
                # Note: No pull_request object in payload, only URL
            }
        )
        
        squadron_event = event_router._to_squadron_event(
            github_event, SquadronEventType.PR_REVIEW_COMMENT_CREATED
        )
        
        assert squadron_event.pr_number == 777

    def test_command_parsing_in_review_comments(self, event_router):
        """Test that @squadron-dev commands are parsed in PR review comments."""
        from squadron.models import GitHubEvent, SquadronEventType
        
        github_event = GitHubEvent(
            delivery_id="test-command-parsing",
            event_type="pull_request_review_comment",
            action="created", 
            payload={
                "comment": {
                    "id": 222,
                    "body": "@squadron-dev security-review: Please check this for vulnerabilities",
                    "path": "src/auth.py",
                    "line": 15,
                    "pull_request_url": "https://api.github.com/repos/test/repo/pulls/555",
                    "user": {"login": "developer"},
                },
                "pull_request": {"number": 555, "title": "Auth improvements"},
                "sender": {"login": "developer", "type": "User"},
            }
        )
        
        squadron_event = event_router._to_squadron_event(
            github_event, SquadronEventType.PR_REVIEW_COMMENT_CREATED
        )
        
        assert squadron_event.command is not None
        assert squadron_event.command.agent == "security-review"
        assert "vulnerabilities" in squadron_event.command.message

    def test_reverse_event_map_includes_new_events(self):
        """Test that REVERSE_EVENT_MAP includes new PR review comment events."""
        from squadron.event_router import REVERSE_EVENT_MAP
        from squadron.models import SquadronEventType
        
        assert SquadronEventType.PR_REVIEW_COMMENT_CREATED in REVERSE_EVENT_MAP
        assert SquadronEventType.PR_REVIEW_COMMENT_EDITED in REVERSE_EVENT_MAP
        assert SquadronEventType.PR_REVIEW_COMMENT_DELETED in REVERSE_EVENT_MAP
        
        assert REVERSE_EVENT_MAP[SquadronEventType.PR_REVIEW_COMMENT_CREATED] == "pull_request_review_comment.created"
        assert REVERSE_EVENT_MAP[SquadronEventType.PR_REVIEW_COMMENT_EDITED] == "pull_request_review_comment.edited"
        assert REVERSE_EVENT_MAP[SquadronEventType.PR_REVIEW_COMMENT_DELETED] == "pull_request_review_comment.deleted"
