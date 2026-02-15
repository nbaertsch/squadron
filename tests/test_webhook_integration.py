"""Integration tests using realistic GitHub webhook payloads.

Tests the full chain: webhook endpoint → event router → handler dispatch.
Uses real payloads (from tests/fixtures/github_payloads.json) that match
GitHub's actual webhook format including all the nested fields that
downstream code depends on (repository, sender, installation, etc.).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from squadron.config import ProjectConfig, SquadronConfig
from squadron.event_router import EventRouter
from squadron.models import GitHubEvent, SquadronEventType
from squadron.registry import AgentRegistry
from squadron.webhook import configure as configure_webhook
from squadron.webhook import router as webhook_router


# ── Fixtures ────────────────────────────────────────────────────────────────


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def payloads() -> dict:
    """Load real GitHub webhook payloads."""
    with open(FIXTURES_DIR / "github_payloads.json") as f:
        return json.load(f)


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_integration.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


@pytest.fixture
def config():
    return SquadronConfig(
        project=ProjectConfig(name="squadron", owner="noahbaertsch", repo="squadron"),
    )


@pytest.fixture
def github_mock():
    github = MagicMock()
    github.verify_webhook_signature = MagicMock(return_value=True)
    return github


@pytest.fixture
def event_queue():
    return asyncio.Queue()


@pytest.fixture
def app(event_queue, github_mock):
    """FastAPI app wired with webhook endpoint."""
    app = FastAPI()
    app.include_router(webhook_router)
    configure_webhook(event_queue, github_mock)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _send_webhook(client, event_type: str, payload: dict, delivery_id: str = "test-123") -> int:
    """Send a webhook through the real FastAPI endpoint."""
    resp = client.post(
        "/webhook",
        json=payload,
        headers={
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": "sha256=placeholder",
        },
    )
    return resp.status_code


# ── Full Chain Tests: Webhook → EventRouter → SquadronEvent ────────────────


class TestIssuePayloads:
    """Verify that real GitHub issue payloads parse correctly through the full chain."""

    async def test_issue_opened_extracts_fields(
        self, client, event_queue, registry, config, payloads
    ):
        payload = payloads["issues_opened"]
        status = _send_webhook(client, "issues", payload)
        assert status == 200

        # Verify event was enqueued
        assert not event_queue.empty()
        raw: GitHubEvent = event_queue.get_nowait()

        # Verify GitHubEvent properties work with real payload shape
        assert raw.full_type == "issues.opened"
        assert raw.sender == "noahbaertsch"
        assert raw.is_bot is False
        assert raw.repo_full_name == "noahbaertsch/squadron"
        assert raw.issue["number"] == 42
        assert raw.issue["title"] == "Add OAuth2 support"
        assert len(raw.issue["labels"]) == 2

        # Route through EventRouter
        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.ISSUE_OPENED, handler)

        await router._route_event(raw)

        # Verify handler was called with correct SquadronEvent
        handler.assert_called_once()
        squadron_event = handler.call_args[0][0]
        assert squadron_event.event_type == SquadronEventType.ISSUE_OPENED
        assert squadron_event.issue_number == 42
        assert squadron_event.data["sender"] == "noahbaertsch"
        assert squadron_event.data["payload"]["issue"]["title"] == "Add OAuth2 support"

    async def test_issue_assigned_bot_extracts_assignee(
        self, client, event_queue, registry, config, payloads
    ):
        payload = payloads["issues_assigned_bot"]
        _send_webhook(client, "issues", payload)

        raw: GitHubEvent = event_queue.get_nowait()
        assert raw.full_type == "issues.assigned"
        assert raw.issue["assignees"][0]["login"] == "squadron[bot]"

        # Route
        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.ISSUE_ASSIGNED, handler)
        await router._route_event(raw)

        handler.assert_called_once()
        evt = handler.call_args[0][0]
        assert evt.issue_number == 42
        # Agent manager reads assignee from payload
        assert evt.data["payload"]["assignee"]["login"] == "squadron[bot]"

    async def test_issue_closed_routes_correctly(
        self, client, event_queue, registry, config, payloads
    ):
        payload = payloads["issues_closed"]
        _send_webhook(client, "issues", payload)

        raw: GitHubEvent = event_queue.get_nowait()
        assert raw.full_type == "issues.closed"

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.ISSUE_CLOSED, handler)
        await router._route_event(raw)

        handler.assert_called_once()
        evt = handler.call_args[0][0]
        assert evt.issue_number == 99
        # Verify state_reason is accessible
        assert evt.data["payload"]["issue"]["state_reason"] == "completed"

    async def test_bot_sender_filtered(self, client, event_queue, registry, config, payloads):
        """When squadron[bot] is the sender, the event should be filtered."""
        payload = payloads["pull_request_opened"]
        _send_webhook(client, "pull_request", payload)

        raw: GitHubEvent = event_queue.get_nowait()
        assert raw.sender == "squadron[bot]"
        assert raw.is_bot is True

        # Route — should be filtered
        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.PR_OPENED, handler)
        await router._route_event(raw)

        handler.assert_not_called()


class TestPullRequestPayloads:
    """Verify real PR payloads parse correctly."""

    async def test_pr_opened_extracts_head_base(
        self, client, event_queue, registry, config, payloads
    ):
        # Use a modified payload with human sender
        payload = json.loads(json.dumps(payloads["pull_request_opened"]))
        payload["sender"]["login"] = "noahbaertsch"
        payload["sender"]["type"] = "User"

        _send_webhook(client, "pull_request", payload, delivery_id="pr-open-1")

        raw: GitHubEvent = event_queue.get_nowait()

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.PR_OPENED, handler)
        await router._route_event(raw)

        handler.assert_called_once()
        evt = handler.call_args[0][0]
        assert evt.pr_number == 10
        # Agent manager reads head/base from payload
        pr_data = evt.data["payload"]["pull_request"]
        assert pr_data["head"]["ref"] == "feat/issue-42"
        assert pr_data["base"]["ref"] == "main"

    async def test_pr_merged_has_merge_fields(
        self, client, event_queue, registry, config, payloads
    ):
        payload = payloads["pull_request_closed_merged"]
        _send_webhook(client, "pull_request", payload, delivery_id="pr-merge-1")

        raw: GitHubEvent = event_queue.get_nowait()

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.PR_CLOSED, handler)
        await router._route_event(raw)

        handler.assert_called_once()
        evt = handler.call_args[0][0]
        assert evt.pr_number == 10
        pr_data = evt.data["payload"]["pull_request"]
        assert pr_data["merged"] is True
        assert pr_data["merged_by"]["login"] == "noahbaertsch"

    async def test_pr_closed_not_merged(self, client, event_queue, registry, config, payloads):
        payload = payloads["pull_request_closed_not_merged"]
        _send_webhook(client, "pull_request", payload, delivery_id="pr-close-1")

        raw: GitHubEvent = event_queue.get_nowait()

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.PR_CLOSED, handler)
        await router._route_event(raw)

        handler.assert_called_once()
        evt = handler.call_args[0][0]
        pr_data = evt.data["payload"]["pull_request"]
        assert pr_data["merged"] is False

    async def test_pr_synchronize_has_before_after(
        self, client, event_queue, registry, config, payloads
    ):
        payload = json.loads(json.dumps(payloads["pull_request_synchronize"]))
        payload["sender"]["login"] = "noahbaertsch"
        payload["sender"]["type"] = "User"

        _send_webhook(client, "pull_request", payload, delivery_id="pr-sync-1")

        raw: GitHubEvent = event_queue.get_nowait()

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.PR_SYNCHRONIZED, handler)
        await router._route_event(raw)

        handler.assert_called_once()
        evt = handler.call_args[0][0]
        assert evt.pr_number == 10
        assert evt.data["payload"]["before"] == "abc123def456"
        assert evt.data["payload"]["after"] == "def456ghi789"


class TestReviewPayloads:
    async def test_review_changes_requested(self, client, event_queue, registry, config, payloads):
        payload = payloads["pull_request_review_changes_requested"]
        _send_webhook(client, "pull_request_review", payload, delivery_id="review-1")

        raw: GitHubEvent = event_queue.get_nowait()
        assert raw.full_type == "pull_request_review.submitted"

        # Bot sender — normally filtered, but let's test the event structure
        # Override sender to test routing
        raw.payload["sender"]["login"] = "reviewer-human"
        # Need to reconstruct with non-bot sender
        raw2 = GitHubEvent(
            delivery_id="review-1b",
            event_type=raw.event_type,
            action=raw.action,
            payload={**raw.payload, "sender": {"login": "reviewer-human", "type": "User"}},
        )

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.PR_REVIEW_SUBMITTED, handler)
        await router._route_event(raw2)

        handler.assert_called_once()
        evt = handler.call_args[0][0]
        assert evt.pr_number == 10
        review = evt.data["payload"]["review"]
        assert review["state"] == "changes_requested"


class TestCommentPayloads:
    async def test_issue_comment_by_human(self, client, event_queue, registry, config, payloads):
        payload = payloads["issue_comment_by_human"]
        _send_webhook(client, "issue_comment", payload, delivery_id="comment-1")

        raw: GitHubEvent = event_queue.get_nowait()
        assert raw.full_type == "issue_comment.created"
        assert raw.sender == "noahbaertsch"

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.ISSUE_COMMENT, handler)
        await router._route_event(raw)

        handler.assert_called_once()
        evt = handler.call_args[0][0]
        assert evt.issue_number == 42
        assert evt.data["payload"]["comment"]["body"] == "@squadron can you also add PKCE support?"


# ── PM Queue Routing ─────────────────────────────────────────────────────────


class TestPMQueueRouting:
    """Verify that the right events land in the PM queue."""

    async def test_issue_events_routed_to_pm(self, client, event_queue, registry, config, payloads):
        for event_name in ["issues_opened", "issues_closed"]:
            payload = payloads[event_name]
            gh_event_type = "issues"
            _send_webhook(client, gh_event_type, payload, delivery_id=f"pm-{event_name}")

        raw1: GitHubEvent = event_queue.get_nowait()
        raw2: GitHubEvent = event_queue.get_nowait()

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        await router._route_event(raw1)
        await router._route_event(raw2)

        # Both should be in PM queue
        assert router.pm_queue.qsize() == 2


# ── Deduplication ────────────────────────────────────────────────────────────


class TestDeduplicationWithRealPayloads:
    async def test_duplicate_delivery_filtered(
        self, client, event_queue, registry, config, payloads
    ):
        payload = payloads["issues_opened"]
        _send_webhook(client, "issues", payload, delivery_id="same-id")
        _send_webhook(client, "issues", payload, delivery_id="same-id")

        raw1: GitHubEvent = event_queue.get_nowait()
        raw2: GitHubEvent = event_queue.get_nowait()

        router = EventRouter(event_queue=asyncio.Queue(), registry=registry, config=config)
        handler = AsyncMock()
        router.on(SquadronEventType.ISSUE_OPENED, handler)

        await router._route_event(raw1)
        await router._route_event(raw2)  # Should be filtered

        handler.assert_called_once()  # Only first one
