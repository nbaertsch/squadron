"""E2E tests for GitHub API operations — NOTHING MOCKED.

Tests the real GitHubClient against the real GitHub API using
the squadron-dev App installed on nbaertsch/squadron-e2e-test.

Every test creates resources with a unique prefix and cleans up after itself.
"""

from __future__ import annotations

import time
import uuid

import pytest


def _uid() -> str:
    """Short unique ID for test isolation."""
    return uuid.uuid4().hex[:8]


# ── Authentication ───────────────────────────────────────────────────────────


class TestGitHubAppAuth:
    """Verify real JWT → installation token → API call flow."""

    async def test_jwt_to_installation_token(self, github_client):
        """The client obtains a real installation token."""
        # Token is pre-warmed by the fixture
        token = await github_client._ensure_token()

        assert token is not None
        assert token.startswith("ghs_")
        assert github_client._token_expires_at > time.time()

    async def test_rate_limit_tracked(self, github_client, e2e_owner, e2e_repo):
        """Rate limit headers are parsed from real GitHub responses."""
        await github_client.get_repo(e2e_owner, e2e_repo)

        assert github_client._rate_limit_remaining > 0
        assert github_client._rate_limit_remaining <= 5000

    async def test_token_reused_across_calls(self, github_client, e2e_owner, e2e_repo):
        """Token is cached and reused — not refreshed on every call."""
        await github_client.get_repo(e2e_owner, e2e_repo)
        token1 = github_client._token

        await github_client.get_repo(e2e_owner, e2e_repo)
        token2 = github_client._token

        assert token1 == token2


# ── Repository ───────────────────────────────────────────────────────────────


class TestRepoOperations:
    async def test_get_repo(self, github_client, e2e_owner, e2e_repo):
        repo = await github_client.get_repo(e2e_owner, e2e_repo)

        assert repo["full_name"] == f"{e2e_owner}/{e2e_repo}"
        assert repo["owner"]["login"] == e2e_owner
        assert "default_branch" in repo


# ── Issues ───────────────────────────────────────────────────────────────────


class TestIssueOperations:
    async def test_create_and_get_issue(self, github_client, e2e_owner, e2e_repo):
        uid = _uid()
        title = f"[E2E-{uid}] Test issue"
        body = "Created by squadron E2E tests. Will be closed automatically."

        # Create
        created = await github_client.create_issue(
            e2e_owner, e2e_repo, title=title, body=body,
        )
        issue_number = created["number"]

        assert created["title"] == title
        assert created["state"] == "open"

        try:
            # Get
            fetched = await github_client.get_issue(e2e_owner, e2e_repo, issue_number)
            assert fetched["number"] == issue_number
            assert fetched["title"] == title
        finally:
            # Cleanup — close the issue
            await github_client._request(
                "PATCH",
                f"/repos/{e2e_owner}/{e2e_repo}/issues/{issue_number}",
                json={"state": "closed"},
            )

    async def test_add_labels(self, github_client, e2e_owner, e2e_repo):
        uid = _uid()

        # Ensure label exists
        await github_client.ensure_labels_exist(e2e_owner, e2e_repo, [f"e2e-{uid}"])

        # Create issue
        created = await github_client.create_issue(
            e2e_owner, e2e_repo, title=f"[E2E-{uid}] Label test", body="test",
        )
        issue_number = created["number"]

        try:
            # Add label
            await github_client.add_labels(e2e_owner, e2e_repo, issue_number, [f"e2e-{uid}"])

            # Verify
            issue = await github_client.get_issue(e2e_owner, e2e_repo, issue_number)
            label_names = [l["name"] for l in issue["labels"]]
            assert f"e2e-{uid}" in label_names
        finally:
            await github_client._request(
                "PATCH",
                f"/repos/{e2e_owner}/{e2e_repo}/issues/{issue_number}",
                json={"state": "closed"},
            )
            # Clean up label
            try:
                await github_client._request(
                    "DELETE",
                    f"/repos/{e2e_owner}/{e2e_repo}/labels/e2e-{uid}",
                )
            except Exception:
                pass

    async def test_comment_on_issue(self, github_client, e2e_owner, e2e_repo):
        uid = _uid()

        created = await github_client.create_issue(
            e2e_owner, e2e_repo, title=f"[E2E-{uid}] Comment test", body="test",
        )
        issue_number = created["number"]

        try:
            comment = await github_client.comment_on_issue(
                e2e_owner, e2e_repo, issue_number, body=f"E2E comment {uid}",
            )
            assert comment["body"] == f"E2E comment {uid}"
            assert comment["id"] > 0
        finally:
            await github_client._request(
                "PATCH",
                f"/repos/{e2e_owner}/{e2e_repo}/issues/{issue_number}",
                json={"state": "closed"},
            )

    async def test_assign_issue(self, github_client, e2e_owner, e2e_repo):
        uid = _uid()

        created = await github_client.create_issue(
            e2e_owner, e2e_repo, title=f"[E2E-{uid}] Assign test", body="test",
        )
        issue_number = created["number"]

        try:
            # Assign to the repo owner (the user who installed the app)
            await github_client.assign_issue(
                e2e_owner, e2e_repo, issue_number, assignees=[e2e_owner],
            )

            issue = await github_client.get_issue(e2e_owner, e2e_repo, issue_number)
            assignee_logins = [a["login"] for a in issue["assignees"]]
            assert e2e_owner in assignee_logins
        finally:
            await github_client._request(
                "PATCH",
                f"/repos/{e2e_owner}/{e2e_repo}/issues/{issue_number}",
                json={"state": "closed"},
            )


# ── Labels ───────────────────────────────────────────────────────────────────


class TestLabelOperations:
    async def test_ensure_labels_idempotent(self, github_client, e2e_owner, e2e_repo):
        uid = _uid()
        label = f"e2e-label-{uid}"

        # Create — should succeed
        await github_client.ensure_labels_exist(e2e_owner, e2e_repo, [label])

        # Create again — should not raise (422 swallowed)
        await github_client.ensure_labels_exist(e2e_owner, e2e_repo, [label])

        # Cleanup
        try:
            await github_client._request(
                "DELETE", f"/repos/{e2e_owner}/{e2e_repo}/labels/{label}",
            )
        except Exception:
            pass

    async def test_ensure_multiple_labels(self, github_client, e2e_owner, e2e_repo):
        uid = _uid()
        labels = [f"e2e-a-{uid}", f"e2e-b-{uid}", f"e2e-c-{uid}"]

        await github_client.ensure_labels_exist(e2e_owner, e2e_repo, labels)

        # Verify they exist by trying to use them
        created = await github_client.create_issue(
            e2e_owner, e2e_repo,
            title=f"[E2E-{uid}] Multi-label test",
            body="test",
            labels=labels,
        )

        label_names = [l["name"] for l in created["labels"]]
        for label in labels:
            assert label in label_names

        # Cleanup
        await github_client._request(
            "PATCH",
            f"/repos/{e2e_owner}/{e2e_repo}/issues/{created['number']}",
            json={"state": "closed"},
        )
        for label in labels:
            try:
                await github_client._request(
                    "DELETE", f"/repos/{e2e_owner}/{e2e_repo}/labels/{label}",
                )
            except Exception:
                pass


# ── Webhook Signature ────────────────────────────────────────────────────────


class TestWebhookSignature:
    """Test real HMAC-SHA256 verification (no network needed, but no mocks)."""

    def test_valid_signature(self, app_id, private_key, installation_id):
        import hashlib
        import hmac

        from squadron.github_client import GitHubClient

        secret = "test-webhook-secret-e2e"
        client = GitHubClient(
            app_id=app_id,
            private_key=private_key,
            installation_id=installation_id,
            webhook_secret=secret,
        )

        payload = b'{"action": "opened", "issue": {"number": 1}}'
        sig = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256,
        ).hexdigest()

        assert client.verify_webhook_signature(payload, sig) is True

    def test_invalid_signature(self, app_id, private_key, installation_id):
        from squadron.github_client import GitHubClient

        client = GitHubClient(
            app_id=app_id,
            private_key=private_key,
            installation_id=installation_id,
            webhook_secret="real-secret",
        )

        payload = b'{"action": "opened"}'
        assert client.verify_webhook_signature(payload, "sha256=bad") is False
