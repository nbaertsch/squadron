"""Tests for new PR review comment tools."""

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


# ── PR Review Comment Tests ─────────────────────────────────────────────────


@respx.mock
async def test_add_pr_line_comment(started_github):
    """Test adding inline comment to specific line."""
    # Mock the PR details call first (to get commit SHA)
    respx.get("https://api.github.com/repos/owner/repo/pulls/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "head": {"sha": "abc123"},
                "number": 123,
            },
        )
    )

    # Mock the line comment creation
    respx.post("https://api.github.com/repos/owner/repo/pulls/123/comments").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 12345,
                "body": "Test comment",
                "path": "src/test.py",
                "line": 42,
            },
        )
    )

    result = await started_github.add_pr_line_comment(
        "owner",
        "repo", 
        123,
        "src/test.py",
        42,
        "Test comment"
    )

    assert result["id"] == 12345
    assert result["body"] == "Test comment"
    assert result["path"] == "src/test.py"
    assert result["line"] == 42

    # Verify the request was made correctly
    requests = respx.calls
    comment_request = next(r for r in requests if r.request.method == "POST")
    payload = comment_request.request.json()
    
    assert payload["body"] == "Test comment"
    assert payload["commit_id"] == "abc123"
    assert payload["path"] == "src/test.py"
    assert payload["line"] == 42
    assert payload["side"] == "RIGHT"


@respx.mock
async def test_suggest_code_change(started_github):
    """Test suggesting code change with GitHub suggestion syntax."""
    # Mock the PR details call first
    respx.get("https://api.github.com/repos/owner/repo/pulls/123").mock(
        return_value=httpx.Response(
            200,
            json={"head": {"sha": "abc123"}},
        )
    )

    # Mock the suggestion creation
    respx.post("https://api.github.com/repos/owner/repo/pulls/123/comments").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 67890,
                "body": "```suggestion\nfixed_code = True\n```",
                "path": "src/test.py",
                "line": 10,
                "start_line": 8,
            },
        )
    )

    result = await started_github.suggest_code_change(
        "owner",
        "repo",
        123,
        "src/test.py", 
        8,
        10,
        "fixed_code = True"
    )

    assert result["id"] == 67890

    # Verify the request payload
    requests = respx.calls
    comment_request = next(r for r in requests if r.request.method == "POST")
    payload = comment_request.request.json()
    
    assert payload["body"] == "```suggestion\nfixed_code = True\n```"
    assert payload["path"] == "src/test.py"
    assert payload["line"] == 10  # End line
    assert payload["start_line"] == 8
    assert payload["start_side"] == "RIGHT"


@respx.mock
async def test_submit_review_with_comments(started_github):
    """Test submitting review with multiple inline comments."""
    respx.post("https://api.github.com/repos/owner/repo/pulls/123/reviews").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 98765,
                "state": "CHANGES_REQUESTED",
                "body": "Overall review comment",
            },
        )
    )

    comments = [
        {"path": "src/file1.py", "line": 5, "body": "Fix this"},
        {"path": "src/file2.py", "line": 10, "body": "Add validation"},
    ]

    result = await started_github.submit_review_with_comments(
        "owner",
        "repo",
        123,
        "REQUEST_CHANGES",
        "Overall review comment",
        comments
    )

    assert result["id"] == 98765
    assert result["state"] == "CHANGES_REQUESTED"

    # Verify the request payload
    requests = respx.calls
    review_request = requests[0]
    payload = review_request.request.json()

    assert payload["event"] == "REQUEST_CHANGES"
    assert payload["body"] == "Overall review comment"
    assert len(payload["comments"]) == 2
    
    # Check comment format
    comment1 = payload["comments"][0]
    assert comment1["path"] == "src/file1.py"
    assert comment1["line"] == 5
    assert comment1["body"] == "Fix this"
    assert comment1["side"] == "RIGHT"


@respx.mock
async def test_update_pr_review_comment(started_github):
    """Test updating an existing review comment."""
    respx.patch("https://api.github.com/repos/owner/repo/pulls/comments/12345").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "body": "Updated comment text",
                "updated_at": "2024-01-01T12:00:00Z",
            },
        )
    )

    result = await started_github.update_pr_review_comment(
        "owner",
        "repo",
        12345,
        "Updated comment text"
    )

    assert result["id"] == 12345
    assert result["body"] == "Updated comment text"

    # Verify the request payload
    requests = respx.calls
    update_request = requests[0]
    payload = update_request.request.json()
    
    assert payload["body"] == "Updated comment text"


@respx.mock  
async def test_delete_pr_review_comment(started_github):
    """Test deleting a review comment."""
    respx.delete("https://api.github.com/repos/owner/repo/pulls/comments/12345").mock(
        return_value=httpx.Response(204)
    )

    result = await started_github.delete_pr_review_comment(
        "owner",
        "repo", 
        12345
    )

    assert result is True


@respx.mock
async def test_delete_pr_review_comment_not_found(started_github):
    """Test deleting a comment that doesn't exist."""
    respx.delete("https://api.github.com/repos/owner/repo/pulls/comments/99999").mock(
        return_value=httpx.Response(404)
    )

    result = await started_github.delete_pr_review_comment(
        "owner",
        "repo",
        99999
    )

    assert result is False


@respx.mock
async def test_reply_to_review_comment(started_github):
    """Test replying to a review comment."""
    respx.post("https://api.github.com/repos/owner/repo/pulls/comments/12345/replies").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 67890,
                "body": "Reply to the comment", 
                "in_reply_to_id": 12345,
            },
        )
    )

    result = await started_github.reply_to_review_comment(
        "owner",
        "repo",
        12345,
        "Reply to the comment"
    )

    assert result["id"] == 67890
    assert result["body"] == "Reply to the comment"
    assert result["in_reply_to_id"] == 12345

    # Verify the request payload
    requests = respx.calls
    reply_request = requests[0]
    payload = reply_request.request.json()
    
    assert payload["body"] == "Reply to the comment"


@respx.mock
async def test_start_pr_review(started_github):
    """Test starting a pending review session."""
    respx.post("https://api.github.com/repos/owner/repo/pulls/123/reviews").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 11111,
                "state": "PENDING",
                "user": {"login": "squadron-dev[bot]"},
            },
        )
    )

    result = await started_github.start_pr_review(
        "owner",
        "repo",
        123
    )

    assert result["id"] == 11111
    assert result["state"] == "PENDING"

    # Verify the request payload
    requests = respx.calls
    review_request = requests[0]
    payload = review_request.request.json()
    
    assert payload["event"] == "PENDING"


@respx.mock
async def test_add_pr_diff_comment(started_github):
    """Test adding comment to specific diff position."""
    respx.post("https://api.github.com/repos/owner/repo/pulls/123/comments").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 55555,
                "body": "Diff comment",
                "path": "src/test.py",
                "position": 15,
            },
        )
    )

    result = await started_github.add_pr_diff_comment(
        "owner",
        "repo",
        123,
        "abc123",
        "src/test.py", 
        15,
        "Diff comment"
    )

    assert result["id"] == 55555
    assert result["body"] == "Diff comment"
    assert result["position"] == 15

    # Verify the request payload
    requests = respx.calls
    comment_request = requests[0]
    payload = comment_request.request.json()
    
    assert payload["body"] == "Diff comment"
    assert payload["commit_id"] == "abc123"
    assert payload["path"] == "src/test.py"
    assert payload["position"] == 15
