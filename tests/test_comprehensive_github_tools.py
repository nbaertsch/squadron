"""Tests for comprehensive GitHub tools for reading PR reviews and change requests.

Tests the new GitHub client methods and agent tools that provide detailed
review information, inline comments, and comprehensive PR/issue data.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.squadron.github_client import GitHubClient
from src.squadron.tools.squadron_tools import (
    ReadIssueComprehensiveParams,
    ReadPRComprehensiveParams,
    ListPRReviewsParams,
    ReadReviewDetailsParams,
    GetInlineCommentsParams,
    GetReviewThreadsParams,
    GetPRReviewStatusParams,
    ListRequestedReviewersParams,
    GetPRChangeRequestsParams,
    GetReviewSummaryParams,
)


@pytest.fixture
def github_client():
    """Mock GitHub client for testing."""
    client = GitHubClient()
    client._client = AsyncMock()
    client._ensure_token = AsyncMock(return_value="test-token")
    client._update_rate_limit = MagicMock()
    return client


@pytest.fixture
def sample_pr_data():
    """Sample PR data for testing."""
    return {
        "number": 42,
        "title": "Add comprehensive GitHub tools",
        "state": "open", 
        "user": {"login": "squadron-dev"},
        "head": {"ref": "feature-branch", "sha": "abc123"},
        "base": {"ref": "main"},
        "body": "This PR adds comprehensive GitHub tools for reading reviews.",
        "mergeable": True,
        "mergeable_state": "clean",
        "created_at": "2024-01-01T10:00:00Z",
        "updated_at": "2024-01-01T12:00:00Z",
        "requested_reviewers": [{"login": "reviewer1"}],
        "requested_teams": []
    }


@pytest.fixture
def sample_review_data():
    """Sample review data for testing."""
    return [
        {
            "id": 1,
            "user": {"login": "reviewer1"},
            "state": "APPROVED",
            "body": "Looks good to me!",
            "submitted_at": "2024-01-01T11:00:00Z"
        },
        {
            "id": 2,
            "user": {"login": "reviewer2"},
            "state": "CHANGES_REQUESTED",
            "body": "Please fix the documentation.",
            "submitted_at": "2024-01-01T11:30:00Z"
        }
    ]


@pytest.fixture
def sample_review_comments():
    """Sample inline review comments for testing."""
    return [
        {
            "id": 101,
            "path": "src/main.py",
            "line": 42,
            "body": "Consider using a more descriptive variable name",
            "user": {"login": "reviewer1"},
            "created_at": "2024-01-01T11:15:00Z",
            "pull_request_review_id": 1,
            "diff_hunk": "@@ -40,3 +40,5 @@\n def process():\n+    x = 1\n+    return x"
        },
        {
            "id": 102,
            "path": "README.md",
            "line": 10,
            "body": "This section needs more detail",
            "user": {"login": "reviewer2"},
            "created_at": "2024-01-01T11:25:00Z",
            "pull_request_review_id": 2,
            "diff_hunk": "@@ -8,2 +8,4 @@\n # Usage\n+\n+Basic usage:"
        }
    ]


class TestGitHubClientComprehensiveMethods:
    """Test the new comprehensive methods in GitHubClient."""

    async def test_get_pr_review_status(self, github_client, sample_pr_data, sample_review_data):
        """Test getting comprehensive PR review status."""
        # Mock the API calls
        github_client.get_pr_reviews = AsyncMock(return_value=sample_review_data)
        github_client.get_pull_request = AsyncMock(return_value=sample_pr_data)
        
        result = await github_client.get_pr_review_status("owner", "repo", 42)
        
        assert result["overall_status"] == "changes_requested"
        assert len(result["approvals"]) == 1
        assert len(result["change_requests"]) == 1
        assert result["approvals"][0]["user"] == "reviewer1"
        assert result["change_requests"][0]["user"] == "reviewer2"
        assert result["requested_reviewers"] == ["reviewer1"]
        assert result["review_summary"]["approval_count"] == 1
        assert result["review_summary"]["change_request_count"] == 1

    async def test_get_pr_change_requests(self, github_client, sample_review_data, sample_review_comments):
        """Test getting detailed change requests."""
        # Filter to only change requests
        change_review_data = [r for r in sample_review_data if r["state"] == "CHANGES_REQUESTED"]
        
        github_client.get_pr_reviews = AsyncMock(return_value=change_review_data)
        github_client.get_pr_review_comments = AsyncMock(return_value=sample_review_comments)
        
        result = await github_client.get_pr_change_requests("owner", "repo", 42)
        
        assert len(result) == 1
        assert result[0]["user"] == "reviewer2"
        assert result[0]["state"] == "CHANGES_REQUESTED"
        assert len(result[0]["inline_comments"]) == 1  # Only comments from this review

    async def test_get_pr_review_threads(self, github_client, sample_review_comments):
        """Test getting threaded review discussions."""
        issue_comments = [
            {
                "id": 201,
                "user": {"login": "author"},
                "body": "Thanks for the feedback!",
                "created_at": "2024-01-01T12:00:00Z"
            }
        ]
        
        github_client.get_pr_review_comments = AsyncMock(return_value=sample_review_comments)
        github_client.list_issue_comments = AsyncMock(return_value=issue_comments)
        
        result = await github_client.get_pr_review_threads("owner", "repo", 42)
        
        # Should have review threads + general discussion
        assert len(result) >= 2
        thread_types = [t["type"] for t in result]
        assert "review_thread" in thread_types
        assert "general_discussion" in thread_types

    async def test_list_requested_reviewers(self, github_client, sample_pr_data):
        """Test listing requested reviewers."""
        github_client.get_pull_request = AsyncMock(return_value=sample_pr_data)
        
        result = await github_client.list_requested_reviewers("owner", "repo", 42)
        
        assert result["total_pending"] == 1
        assert len(result["users"]) == 1
        assert result["users"][0]["login"] == "reviewer1"
        assert len(result["teams"]) == 0

    async def test_get_review_details(self, github_client):
        """Test getting specific review details."""
        review_detail = {
            "id": 1,
            "user": {"login": "reviewer1"},
            "state": "APPROVED",
            "body": "Great work!",
            "submitted_at": "2024-01-01T11:00:00Z"
        }
        review_comments = [
            {
                "path": "src/main.py",
                "line": 42,
                "body": "Nice fix here"
            }
        ]
        
        github_client._request = AsyncMock()
        github_client._request.side_effect = [
            MagicMock(json=lambda: review_detail),
            MagicMock(json=lambda: review_comments)
        ]
        
        result = await github_client.get_review_details("owner", "repo", 42, 1)
        
        assert result["review"]["id"] == 1
        assert result["review"]["user"]["login"] == "reviewer1"
        assert len(result["comments"]) == 1

    async def test_get_issue_comprehensive(self, github_client):
        """Test getting comprehensive issue data."""
        issue_data = {
            "number": 123,
            "title": "Bug in feature X",
            "state": "open",
            "user": {"login": "user1"},
            "assignees": [{"login": "assignee1"}],
            "labels": [{"name": "bug"}],
            "body": "There's a bug in feature X",
            "created_at": "2024-01-01T09:00:00Z",
            "updated_at": "2024-01-01T10:00:00Z"
        }
        comments = [
            {
                "id": 1,
                "user": {"login": "commenter"},
                "body": "I can reproduce this",
                "created_at": "2024-01-01T09:30:00Z"
            }
        ]
        
        github_client.get_issue = AsyncMock(return_value=issue_data)
        github_client.list_issue_comments = AsyncMock(return_value=comments)
        github_client._request = AsyncMock(
            return_value=MagicMock(json=lambda: [])  # Empty timeline
        )
        
        result = await github_client.get_issue_comprehensive("owner", "repo", 123)
        
        assert result["issue"]["number"] == 123
        assert len(result["comments"]) == 1
        assert result["summary"]["comment_count"] == 1
        assert result["summary"]["assignees"] == ["assignee1"]

    async def test_get_pr_comprehensive(self, github_client, sample_pr_data):
        """Test getting comprehensive PR data."""
        github_client.get_pull_request = AsyncMock(return_value=sample_pr_data)
        github_client.get_pr_review_status = AsyncMock(return_value={
            "overall_status": "pending",
            "review_summary": {"approval_count": 0, "change_request_count": 0}
        })
        github_client.get_pr_review_threads = AsyncMock(return_value=[])
        github_client.list_pull_request_files = AsyncMock(return_value=[
            {"filename": "src/main.py", "status": "modified"}
        ])
        github_client.get_combined_status = AsyncMock(return_value={
            "state": "pending"
        })
        
        result = await github_client.get_pr_comprehensive("owner", "repo", 42)
        
        assert result["pr"]["number"] == 42
        assert result["summary"]["file_count"] == 1
        assert "review_status" in result
        assert "review_threads" in result
        assert "files" in result


class TestSquadronToolsComprehensive:
    """Test the new comprehensive squadron tools."""

    @pytest.fixture
    def mock_tools(self):
        """Create mock SquadronTools instance."""
        from src.squadron.tools.squadron_tools import SquadronTools
        tools = SquadronTools(
            owner="test-owner",
            repo="test-repo", 
            github=AsyncMock(),
            registry=AsyncMock()
        )
        return tools

    async def test_read_issue_comprehensive_tool(self, mock_tools):
        """Test the read_issue_comprehensive tool."""
        comprehensive_data = {
            "issue": {
                "number": 123,
                "title": "Test issue",
                "body": "Test description",
                "user": {"login": "user1"}
            },
            "comments": [
                {
                    "user": {"login": "commenter"},
                    "body": "Test comment",
                    "created_at": "2024-01-01T10:00:00Z"
                }
            ],
            "events": [],
            "summary": {
                "number": 123,
                "title": "Test issue",
                "state": "open",
                "author": "user1",
                "assignees": [],
                "labels": ["bug"],
                "comment_count": 1,
                "created_at": "2024-01-01T09:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            }
        }
        
        mock_tools.github.get_issue_comprehensive = AsyncMock(return_value=comprehensive_data)
        
        params = ReadIssueComprehensiveParams(issue_number=123)
        result = await mock_tools.read_issue_comprehensive("agent-123", params)
        
        assert "Issue #123" in result
        assert "Test issue" in result
        assert "Test description" in result
        assert "commenter" in result
        assert "Test comment" in result

    async def test_get_pr_review_status_tool(self, mock_tools):
        """Test the get_pr_review_status tool."""
        status_data = {
            "overall_status": "changes_requested",
            "approvals": [{"user": "reviewer1", "submitted_at": "2024-01-01T11:00:00Z", "body": "LGTM"}],
            "change_requests": [{"user": "reviewer2", "submitted_at": "2024-01-01T11:30:00Z", "body": "Fix docs"}],
            "requested_reviewers": ["reviewer3"],
            "requested_teams": [],
            "review_summary": {
                "total_reviews": 2,
                "unique_reviewers": 2,
                "approval_count": 1,
                "change_request_count": 1,
                "comment_count": 0
            }
        }
        
        mock_tools.github.get_pr_review_status = AsyncMock(return_value=status_data)
        
        params = GetPRReviewStatusParams(pr_number=42)
        result = await mock_tools.get_pr_review_status("agent-123", params)
        
        assert "Review Status for PR #42" in result
        assert "changes_requested" in result
        assert "reviewer1" in result
        assert "reviewer2" in result
        assert "2" in result  # total reviews

    async def test_get_pr_change_requests_tool(self, mock_tools):
        """Test the get_pr_change_requests tool."""
        change_requests = [
            {
                "review_id": 1,
                "user": "reviewer1",
                "submitted_at": "2024-01-01T11:00:00Z",
                "body": "Please fix the documentation",
                "state": "CHANGES_REQUESTED",
                "inline_comments": [
                    {
                        "path": "README.md",
                        "line": 10,
                        "body": "This needs more detail"
                    }
                ]
            }
        ]
        
        mock_tools.github.get_pr_change_requests = AsyncMock(return_value=change_requests)
        
        params = GetPRChangeRequestsParams(pr_number=42)
        result = await mock_tools.get_pr_change_requests("agent-123", params)
        
        assert "Change Requests for PR #42" in result
        assert "reviewer1" in result
        assert "fix the documentation" in result
        assert "README.md:10" in result
        assert "needs more detail" in result

    async def test_get_review_summary_tool(self, mock_tools):
        """Test the get_review_summary tool."""
        status_data = {
            "overall_status": "approved",
            "approvals": [{"user": "reviewer1"}],
            "change_requests": [],
            "requested_reviewers": [],
            "requested_teams": [],
            "review_summary": {
                "approval_count": 1,
                "change_request_count": 0,
                "comment_count": 0
            }
        }
        
        mock_tools.github.get_pr_review_status = AsyncMock(return_value=status_data)
        
        params = GetReviewSummaryParams(pr_number=42)
        result = await mock_tools.get_review_summary("agent-123", params)
        
        assert "Review Summary for PR #42" in result
        assert "✅" in result  # approved emoji
        assert "Ready to merge" in result
        assert "1 approval(s)" in result


if __name__ == "__main__":
    # Simple test runner for development
    import asyncio
    
    async def run_basic_test():
        """Run a basic test to verify functionality."""
        client = GitHubClient()
        client._client = AsyncMock()
        client._ensure_token = AsyncMock(return_value="test-token")
        client._update_rate_limit = MagicMock()
        
        # Mock basic data
        client.get_pr_reviews = AsyncMock(return_value=[])
        client.get_pull_request = AsyncMock(return_value={
            "number": 42,
            "requested_reviewers": [],
            "requested_teams": []
        })
        
        # Test the method
        result = await client.get_pr_review_status("owner", "repo", 42)
        assert "overall_status" in result
        print("✓ Basic comprehensive GitHub tools test passed")
    
    asyncio.run(run_basic_test())
