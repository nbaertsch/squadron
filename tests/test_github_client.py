"""Contract tests for GitHubClient — verify HTTP request shapes.

Uses `respx` to intercept httpx requests at the transport level.
These tests verify that every GitHubClient method sends the correct:
- HTTP method (GET/POST)
- URL path (/repos/{owner}/{repo}/issues/{number})
- JSON payload structure
- Authorization headers

This catches request shape regressions WITHOUT hitting real GitHub.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from squadron.github_client import GitHubClient


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def github():
    """GitHubClient wired to a fake token (skips JWT auth)."""
    client = GitHubClient(
        app_id="12345",
        private_key="fake",
        webhook_secret="test-secret",
        installation_id="67890",
    )
    # Pre-set a fake token so we skip the JWT exchange in tests
    client._token = "ghs_fake_installation_token"
    client._token_expires_at = time.time() + 3600
    return client


@pytest.fixture
async def started_github(github):
    """Client with httpx started."""
    await github.start()
    yield github
    await github.close()


# ── Issue Operations ─────────────────────────────────────────────────────────


class TestGetIssue:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.get("https://api.github.com/repos/acme/widgets/issues/42").mock(
            return_value=httpx.Response(200, json={
                "number": 42, "title": "Fix bug", "state": "open",
                "labels": [], "assignees": [], "body": "Details here",
            })
        )

        result = await started_github.get_issue("acme", "widgets", 42)

        assert route.called
        assert result["number"] == 42
        assert result["title"] == "Fix bug"
        # Verify auth header was sent
        request = route.calls[0].request
        assert request.headers["Authorization"] == "token ghs_fake_installation_token"


class TestCreateIssue:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.post("https://api.github.com/repos/acme/widgets/issues").mock(
            return_value=httpx.Response(201, json={"number": 99, "title": "New issue"})
        )

        result = await started_github.create_issue(
            "acme", "widgets",
            title="New issue",
            body="Description here",
            labels=["bug", "high"],
            assignees=["squadron[bot]"],
        )

        assert route.called
        request = route.calls[0].request
        import json
        body = json.loads(request.content)
        assert body["title"] == "New issue"
        assert body["body"] == "Description here"
        assert body["labels"] == ["bug", "high"]
        assert body["assignees"] == ["squadron[bot]"]
        assert result["number"] == 99

    @respx.mock
    async def test_defaults_empty_lists(self, started_github):
        route = respx.post("https://api.github.com/repos/acme/widgets/issues").mock(
            return_value=httpx.Response(201, json={"number": 100})
        )

        await started_github.create_issue("acme", "widgets", title="Minimal", body="")

        import json
        body = json.loads(route.calls[0].request.content)
        assert body["labels"] == []
        assert body["assignees"] == []


class TestAddLabels:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.post(
            "https://api.github.com/repos/acme/widgets/issues/42/labels"
        ).mock(return_value=httpx.Response(200, json=[]))

        await started_github.add_labels("acme", "widgets", 42, ["bug", "critical"])

        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["labels"] == ["bug", "critical"]


class TestCommentOnIssue:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.post(
            "https://api.github.com/repos/acme/widgets/issues/42/comments"
        ).mock(return_value=httpx.Response(201, json={"id": 1}))

        result = await started_github.comment_on_issue("acme", "widgets", 42, "Hello world")

        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["body"] == "Hello world"
        assert result["id"] == 1


class TestAssignIssue:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.post(
            "https://api.github.com/repos/acme/widgets/issues/42/assignees"
        ).mock(return_value=httpx.Response(201, json={}))

        await started_github.assign_issue("acme", "widgets", 42, ["squadron[bot]"])

        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["assignees"] == ["squadron[bot]"]


# ── PR Operations ────────────────────────────────────────────────────────────


class TestGetPullRequest:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.get(
            "https://api.github.com/repos/acme/widgets/pulls/10"
        ).mock(return_value=httpx.Response(200, json={"number": 10, "merged": False}))

        result = await started_github.get_pull_request("acme", "widgets", 10)

        assert route.called
        assert result["number"] == 10


class TestCreatePullRequest:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.post(
            "https://api.github.com/repos/acme/widgets/pulls"
        ).mock(return_value=httpx.Response(201, json={"number": 11}))

        result = await started_github.create_pull_request(
            "acme", "widgets",
            title="Add auth",
            body="Implements OAuth flow",
            head="feat/issue-42",
            base="main",
        )

        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["title"] == "Add auth"
        assert body["body"] == "Implements OAuth flow"
        assert body["head"] == "feat/issue-42"
        assert body["base"] == "main"
        assert result["number"] == 11


class TestSubmitPRReview:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.post(
            "https://api.github.com/repos/acme/widgets/pulls/10/reviews"
        ).mock(return_value=httpx.Response(200, json={"id": 1}))

        await started_github.submit_pr_review(
            "acme", "widgets", 10,
            body="LGTM",
            event="APPROVE",
        )

        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["body"] == "LGTM"
        assert body["event"] == "APPROVE"

    @respx.mock
    async def test_with_line_comments(self, started_github):
        route = respx.post(
            "https://api.github.com/repos/acme/widgets/pulls/10/reviews"
        ).mock(return_value=httpx.Response(200, json={"id": 2}))

        line_comments = [{"path": "src/auth.py", "position": 5, "body": "Missing null check"}]
        await started_github.submit_pr_review(
            "acme", "widgets", 10,
            body="Needs changes",
            event="REQUEST_CHANGES",
            comments=line_comments,
        )

        import json
        body = json.loads(route.calls[0].request.content)
        assert body["comments"] == line_comments
        assert body["event"] == "REQUEST_CHANGES"


# ── Repository Operations ───────────────────────────────────────────────────


class TestEnsureLabels:
    @respx.mock
    async def test_creates_new_labels(self, started_github):
        route = respx.post(
            "https://api.github.com/repos/acme/widgets/labels"
        ).mock(return_value=httpx.Response(201, json={}))

        await started_github.ensure_labels_exist("acme", "widgets", ["bug", "feature"])

        assert route.call_count == 2

    @respx.mock
    async def test_ignores_422_already_exists(self, started_github):
        # First label already exists (422), second is new (201)
        route = respx.post(
            "https://api.github.com/repos/acme/widgets/labels"
        ).mock(side_effect=[
            httpx.Response(422, json={"message": "Validation Failed"}),
            httpx.Response(201, json={}),
        ])

        # Should not raise
        await started_github.ensure_labels_exist("acme", "widgets", ["existing", "new"])

        assert route.call_count == 2


# ── New Issue/PR Operations ─────────────────────────────────────────────────


class TestCloseIssue:
    @respx.mock
    async def test_request_shape(self, started_github):
        route = respx.patch(
            "https://api.github.com/repos/acme/widgets/issues/42"
        ).mock(return_value=httpx.Response(200, json={"number": 42, "state": "closed"}))

        result = await started_github.close_issue("acme", "widgets", 42)

        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["state"] == "closed"
        assert result["state"] == "closed"


class TestUpdateIssue:
    @respx.mock
    async def test_updates_title_and_labels(self, started_github):
        route = respx.patch(
            "https://api.github.com/repos/acme/widgets/issues/42"
        ).mock(return_value=httpx.Response(200, json={"number": 42}))

        result = await started_github.update_issue(
            "acme", "widgets", 42,
            title="New title",
            labels=["bug", "critical"],
        )

        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["title"] == "New title"
        assert body["labels"] == ["bug", "critical"]
        # Fields not passed should not be in the payload
        assert "body" not in body
        assert "state" not in body

    @respx.mock
    async def test_updates_state(self, started_github):
        route = respx.patch(
            "https://api.github.com/repos/acme/widgets/issues/10"
        ).mock(return_value=httpx.Response(200, json={"number": 10, "state": "closed"}))

        await started_github.update_issue("acme", "widgets", 10, state="closed")

        import json
        body = json.loads(route.calls[0].request.content)
        assert body["state"] == "closed"


class TestMergePullRequest:
    @respx.mock
    async def test_squash_merge(self, started_github):
        route = respx.put(
            "https://api.github.com/repos/acme/widgets/pulls/10/merge"
        ).mock(return_value=httpx.Response(200, json={"merged": True, "sha": "abc123"}))

        result = await started_github.merge_pull_request(
            "acme", "widgets", 10,
            merge_method="squash",
            commit_title="feat: add auth (#10)",
        )

        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["merge_method"] == "squash"
        assert body["commit_title"] == "feat: add auth (#10)"
        assert result["merged"] is True

    @respx.mock
    async def test_default_merge_method(self, started_github):
        route = respx.put(
            "https://api.github.com/repos/acme/widgets/pulls/5/merge"
        ).mock(return_value=httpx.Response(200, json={"merged": True}))

        await started_github.merge_pull_request("acme", "widgets", 5)

        import json
        body = json.loads(route.calls[0].request.content)
        assert body["merge_method"] == "squash"


class TestListPullRequestFiles:
    @respx.mock
    async def test_request_shape(self, started_github):
        files = [
            {"filename": "src/auth.py", "status": "modified", "additions": 10, "deletions": 2},
            {"filename": "tests/test_auth.py", "status": "added", "additions": 50, "deletions": 0},
        ]
        route = respx.get(
            "https://api.github.com/repos/acme/widgets/pulls/10/files"
        ).mock(return_value=httpx.Response(200, json=files))

        result = await started_github.list_pull_request_files("acme", "widgets", 10)

        assert route.called
        assert len(result) == 2
        assert result[0]["filename"] == "src/auth.py"
        assert result[1]["status"] == "added"


# ── Auth Headers ─────────────────────────────────────────────────────────────


class TestAuthHeaders:
    @respx.mock
    async def test_includes_accept_header(self, started_github):
        route = respx.get(
            "https://api.github.com/repos/acme/widgets"
        ).mock(return_value=httpx.Response(200, json={}))

        await started_github.get_repo("acme", "widgets")

        request = route.calls[0].request
        assert "application/vnd.github.v3+json" in request.headers["Accept"]
        assert request.headers["User-Agent"] == "Squadron/0.1.0"


# ── Rate Limit Tracking ─────────────────────────────────────────────────────


class TestRateLimitTracking:
    @respx.mock
    async def test_updates_from_response_headers(self, started_github):
        route = respx.get(
            "https://api.github.com/repos/acme/widgets"
        ).mock(return_value=httpx.Response(
            200, json={},
            headers={
                "X-RateLimit-Remaining": "4500",
                "X-RateLimit-Reset": "1700000000",
            },
        ))

        await started_github.get_repo("acme", "widgets")

        assert started_github._rate_limit_remaining == 4500
        assert started_github._rate_limit_reset == 1700000000.0


# ── Token Refresh ────────────────────────────────────────────────────────────


class TestTokenRefresh:
    @respx.mock
    async def test_refreshes_expired_token(self, started_github):
        # Expire the token
        started_github._token_expires_at = time.time() - 100

        # Mock JWT generation (needs PyJWT + real private key, so skip it)
        import unittest.mock
        started_github._generate_jwt = unittest.mock.MagicMock(return_value="fake.jwt.token")

        # Mock the token exchange endpoint
        token_route = respx.post(
            "https://api.github.com/app/installations/67890/access_tokens"
        ).mock(return_value=httpx.Response(201, json={
            "token": "ghs_new_token_abc123",
            "expires_at": "2025-01-01T00:00:00Z",
        }))

        # Mock the actual API call
        api_route = respx.get(
            "https://api.github.com/repos/acme/widgets"
        ).mock(return_value=httpx.Response(200, json={}))

        await started_github.get_repo("acme", "widgets")

        # Verify token exchange happened
        assert token_route.called
        token_request = token_route.calls[0].request
        assert token_request.headers["Authorization"] == "Bearer fake.jwt.token"

        # Verify new token was used for API call
        assert api_route.called
        api_request = api_route.calls[0].request
        assert api_request.headers["Authorization"] == "token ghs_new_token_abc123"

        # Verify token is cached
        assert started_github._token == "ghs_new_token_abc123"


# ── Webhook Signature Verification ──────────────────────────────────────────


class TestWebhookSignature:
    def test_valid_signature(self):
        client = GitHubClient(webhook_secret="secret123")
        import hashlib, hmac as hmac_mod
        payload = b'{"action": "opened"}'
        sig = "sha256=" + hmac_mod.new(b"secret123", payload, hashlib.sha256).hexdigest()

        assert client.verify_webhook_signature(payload, sig) is True

    def test_invalid_signature(self):
        client = GitHubClient(webhook_secret="secret123")
        assert client.verify_webhook_signature(b"payload", "sha256=wrong") is False

    def test_no_secret_configured_allows_all(self):
        client = GitHubClient()
        assert client.verify_webhook_signature(b"anything", "sha256=whatever") is True


# ── Error Handling ───────────────────────────────────────────────────────────


class TestErrorHandling:
    @respx.mock
    async def test_404_raises_status_error(self, started_github):
        respx.get(
            "https://api.github.com/repos/acme/nonexistent/issues/1"
        ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await started_github.get_issue("acme", "nonexistent", 1)
        assert exc_info.value.response.status_code == 404

    @respx.mock
    async def test_500_raises_status_error(self, started_github):
        respx.post(
            "https://api.github.com/repos/acme/widgets/issues"
        ).mock(return_value=httpx.Response(500, json={"message": "Internal Server Error"}))

        with pytest.raises(httpx.HTTPStatusError):
            await started_github.create_issue("acme", "widgets", title="Test", body="Body")

    @respx.mock
    async def test_no_token_raises_runtime_error(self):
        client = GitHubClient()  # No credentials at all
        await client.start()
        try:
            with pytest.raises(RuntimeError, match="credentials not configured"):
                await client.get_issue("acme", "widgets", 1)
        finally:
            await client.close()
