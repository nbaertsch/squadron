"""Tests for PR review comment webhook events."""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from squadron.models import GitHubEvent, SquadronEventType
from squadron.webhook import configure, router
from squadron.event_router import EventRouter


@pytest.fixture
def app():
    """Create a test FastAPI app with the webhook router."""
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.fixture
def event_queue():
    return asyncio.Queue()


@pytest.fixture
def github_client():
    """Mock GitHub client that accepts all signatures."""
    client = MagicMock()
    client.verify_webhook_signature = MagicMock(return_value=True)
    return client


@pytest.fixture
def client(app, event_queue, github_client):
    """Configure webhook and return test client."""
    configure(event_queue, github_client)
    return TestClient(app)


class TestPRReviewCommentWebhooks:
    """Test webhook handling for PR review comment events."""

    def test_pr_review_comment_created(self, client, event_queue):
        """Test pull_request_review_comment.created webhook."""
        payload = {
            "action": "created",
            "comment": {
                "id": 123456,
                "body": "This looks good to me!",
                "path": "src/example.py",
                "line": 42,
                "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/5",
                "user": {"login": "reviewer", "type": "User"},
            },
            "pull_request": {
                "number": 5,
                "title": "Fix bug in authentication",
                "user": {"login": "developer", "type": "User"},
            },
            "sender": {"login": "reviewer", "type": "User"},
        }
        
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": "test-pr-review-comment-1",
                "X-Hub-Signature-256": "sha256=dummy",
            },
        )
        
        assert response.status_code == 200
        assert not event_queue.empty()

        event = event_queue.get_nowait()
        assert isinstance(event, GitHubEvent)
        assert event.delivery_id == "test-pr-review-comment-1"
        assert event.event_type == "pull_request_review_comment"
        assert event.action == "created"
        assert event.full_type == "pull_request_review_comment.created"
        assert event.sender == "reviewer"

    def test_pr_review_comment_edited(self, client, event_queue):
        """Test pull_request_review_comment.edited webhook."""
        payload = {
            "action": "edited",
            "comment": {
                "id": 123456,
                "body": "This looks good to me! (Updated comment)",
                "path": "src/example.py",
                "line": 42,
                "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/5",
                "user": {"login": "reviewer", "type": "User"},
            },
            "pull_request": {
                "number": 5,
                "title": "Fix bug in authentication",
            },
            "sender": {"login": "reviewer", "type": "User"},
        }
        
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": "test-pr-review-comment-2",
                "X-Hub-Signature-256": "sha256=dummy",
            },
        )
        
        assert response.status_code == 200
        event = event_queue.get_nowait()
        assert event.full_type == "pull_request_review_comment.edited"

    def test_pr_review_comment_deleted(self, client, event_queue):
        """Test pull_request_review_comment.deleted webhook."""
        payload = {
            "action": "deleted",
            "comment": {
                "id": 123456,
                "body": "(deleted comment)",
                "path": "src/example.py",
                "line": 42,
                "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/5",
                "user": {"login": "reviewer", "type": "User"},
            },
            "pull_request": {
                "number": 5,
                "title": "Fix bug in authentication",
            },
            "sender": {"login": "reviewer", "type": "User"},
        }
        
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": "test-pr-review-comment-3",
                "X-Hub-Signature-256": "sha256=dummy",
            },
        )
        
        assert response.status_code == 200
        event = event_queue.get_nowait()
        assert event.full_type == "pull_request_review_comment.deleted"

    def test_pr_review_comment_with_squadron_command(self, client, event_queue):
        """Test PR review comment with @squadron-dev command."""
        payload = {
            "action": "created",
            "comment": {
                "id": 123456,
                "body": "@squadron-dev feat-dev: Please address this security concern in line 42",
                "path": "src/example.py",
                "line": 42,
                "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/5",
                "user": {"login": "security-reviewer", "type": "User"},
            },
            "pull_request": {
                "number": 5,
                "title": "Add new feature",
            },
            "sender": {"login": "security-reviewer", "type": "User"},
        }
        
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": "test-pr-command-1",
                "X-Hub-Signature-256": "sha256=dummy",
            },
        )
        
        assert response.status_code == 200
        event = event_queue.get_nowait()
        assert event.full_type == "pull_request_review_comment.created"
        assert "squadron-dev" in payload["comment"]["body"]


class TestEventRouterPRReviewComments:
    """Test event router handling of PR review comment events."""

    def test_pr_review_comment_event_mapping(self):
        """Test that new PR review comment events map correctly."""
        from squadron.event_router import EVENT_MAP
        
        assert "pull_request_review_comment.created" in EVENT_MAP
        assert "pull_request_review_comment.edited" in EVENT_MAP
        assert "pull_request_review_comment.deleted" in EVENT_MAP
        
        assert EVENT_MAP["pull_request_review_comment.created"] == SquadronEventType.PR_REVIEW_COMMENT_CREATED
        assert EVENT_MAP["pull_request_review_comment.edited"] == SquadronEventType.PR_REVIEW_COMMENT_EDITED
        assert EVENT_MAP["pull_request_review_comment.deleted"] == SquadronEventType.PR_REVIEW_COMMENT_DELETED

    def test_squadron_event_creation_for_review_comment(self):
        """Test SquadronEvent creation from PR review comment GitHubEvent."""
        from squadron.event_router import EventRouter
        from squadron.models import GitHubEvent, SquadronEventType
        from squadron.config import SquadronConfig
        from squadron.registry import AgentRegistry
        
        # Mock dependencies
        event_queue = asyncio.Queue()
        registry = MagicMock()
        config = MagicMock()
        
        router = EventRouter(event_queue, registry, config)
        
        # Create a PR review comment event
        github_event = GitHubEvent(
            delivery_id="test-delivery",
            event_type="pull_request_review_comment",
            action="created",
            payload={
                "comment": {
                    "id": 123456,
                    "body": "@squadron-dev pm: Please coordinate this change",
                    "path": "src/example.py",
                    "line": 42,
                    "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/7",
                    "user": {"login": "reviewer"},
                },
                "pull_request": {
                    "number": 7,
                    "title": "Important fix",
                },
                "sender": {"login": "reviewer", "type": "User"},
            }
        )
        
        squadron_event = router._to_squadron_event(
            github_event, SquadronEventType.PR_REVIEW_COMMENT_CREATED
        )
        
        assert squadron_event.event_type == SquadronEventType.PR_REVIEW_COMMENT_CREATED
        assert squadron_event.pr_number == 7
        assert squadron_event.issue_number is None
        assert squadron_event.source_delivery_id == "test-delivery"
        assert squadron_event.command is not None  # Should parse @squadron-dev command
        assert "payload" in squadron_event.data

    def test_pr_number_extraction_from_review_comment_url(self):
        """Test PR number extraction from pull_request_url in review comments."""
        from squadron.event_router import EventRouter
        from squadron.models import GitHubEvent, SquadronEventType
        
        # Mock dependencies
        event_queue = asyncio.Queue()
        registry = MagicMock()
        config = MagicMock()
        
        router = EventRouter(event_queue, registry, config)
        
        # Test with pull_request_url only (no direct pull_request object)
        github_event = GitHubEvent(
            delivery_id="test-delivery",
            event_type="pull_request_review_comment",
            action="created",
            payload={
                "comment": {
                    "id": 123456,
                    "body": "Good catch!",
                    "path": "src/example.py",
                    "line": 42,
                    "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/999",
                    "user": {"login": "reviewer"},
                },
                "sender": {"login": "reviewer", "type": "User"},
                # No pull_request object - only the URL
            }
        )
        
        squadron_event = router._to_squadron_event(
            github_event, SquadronEventType.PR_REVIEW_COMMENT_CREATED
        )
        
        assert squadron_event.pr_number == 999


class TestExistingEventsStillWork:
    """Ensure existing webhook events still work after changes."""

    def test_existing_pr_review_submitted_still_works(self, client, event_queue):
        """Test that existing pull_request_review.submitted events still work."""
        payload = {
            "action": "submitted",
            "review": {
                "id": 789,
                "body": "Overall looks good, but needs some changes",
                "state": "changes_requested",
                "user": {"login": "reviewer", "type": "User"},
            },
            "pull_request": {
                "number": 10,
                "title": "Feature implementation",
            },
            "sender": {"login": "reviewer", "type": "User"},
        }
        
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "pull_request_review",
                "X-GitHub-Delivery": "test-review-submitted-1",
                "X-Hub-Signature-256": "sha256=dummy",
            },
        )
        
        assert response.status_code == 200
        event = event_queue.get_nowait()
        assert event.full_type == "pull_request_review.submitted"
        assert event.pull_request["number"] == 10

    def test_existing_issue_comment_still_works(self, client, event_queue):
        """Test that existing issue comment events still work."""
        payload = {
            "action": "created",
            "issue": {
                "number": 15,
                "title": "Bug report",
                "user": {"login": "reporter"},
            },
            "comment": {
                "body": "@squadron-dev bug-fix: Please investigate this issue",
                "user": {"login": "maintainer"},
            },
            "sender": {"login": "maintainer", "type": "User"},
        }
        
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "test-issue-comment-1",
                "X-Hub-Signature-256": "sha256=dummy",
            },
        )
        
        assert response.status_code == 200
        event = event_queue.get_nowait()
        assert event.full_type == "issue_comment.created"
        assert event.issue["number"] == 15
