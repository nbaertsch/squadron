"""Tests for the webhook endpoint."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from squadron.models import GitHubEvent
from squadron.webhook import configure, router

import asyncio
from fastapi import FastAPI


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


class TestWebhookEndpoint:
    def test_valid_webhook(self, client, event_queue):
        payload = {
            "action": "opened",
            "issue": {"number": 1, "title": "Test issue"},
            "sender": {"login": "alice", "type": "User"},
        }
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "test-delivery-1",
                "X-Hub-Signature-256": "sha256=dummy",
            },
        )
        assert response.status_code == 200
        assert not event_queue.empty()

        event = event_queue.get_nowait()
        assert isinstance(event, GitHubEvent)
        assert event.delivery_id == "test-delivery-1"
        assert event.event_type == "issues"
        assert event.action == "opened"
        assert event.full_type == "issues.opened"

    def test_missing_event_header(self, client):
        """Missing X-GitHub-Event header should return 422."""
        response = client.post(
            "/webhook",
            json={"action": "opened"},
            headers={
                "X-GitHub-Delivery": "test-delivery-2",
            },
        )
        assert response.status_code == 422

    def test_missing_delivery_header(self, client):
        """Missing X-GitHub-Delivery header should return 422."""
        response = client.post(
            "/webhook",
            json={"action": "opened"},
            headers={
                "X-GitHub-Event": "issues",
            },
        )
        assert response.status_code == 422

    def test_invalid_signature(self, client, github_client):
        """Invalid signature should return 401."""
        github_client.verify_webhook_signature.return_value = False
        response = client.post(
            "/webhook",
            json={"action": "opened"},
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "test-delivery-3",
                "X-Hub-Signature-256": "sha256=invalid",
            },
        )
        assert response.status_code == 401

    def test_push_event_no_action(self, client, event_queue):
        """Push events don't have an action field."""
        payload = {
            "ref": "refs/heads/main",
            "commits": [],
            "sender": {"login": "alice", "type": "User"},
        }
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "test-delivery-4",
            },
        )
        assert response.status_code == 200
        event = event_queue.get_nowait()
        assert event.event_type == "push"
        assert event.action is None
        assert event.full_type == "push"

    def test_pr_event(self, client, event_queue):
        payload = {
            "action": "opened",
            "pull_request": {"number": 5, "title": "My PR"},
            "sender": {"login": "bob", "type": "User"},
        }
        response = client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "test-delivery-5",
            },
        )
        assert response.status_code == 200
        event = event_queue.get_nowait()
        assert event.full_type == "pull_request.opened"
        assert event.pull_request["number"] == 5


class TestSignatureVerification:
    def test_hmac_verification_flow(self, app, event_queue):
        """Test with actual HMAC calculation (no mock)."""
        from squadron.github_client import GitHubClient

        secret = "test-secret-123"
        real_client = GitHubClient(webhook_secret=secret)

        configure(event_queue, real_client)
        tc = TestClient(app)

        payload = json.dumps({"action": "opened", "sender": {"login": "alice"}}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        response = tc.post(
            "/webhook",
            content=payload,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "hmac-test-1",
                "X-Hub-Signature-256": sig,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
