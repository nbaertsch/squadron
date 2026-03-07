"""GitHub API client for Squadron.

Handles GitHub App authentication (JWT → installation token),
rate limit tracking, and async API operations via httpx.
See AD-012 for GitHub App design decisions.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubClient:
    """Async GitHub API client with App authentication."""

    def __init__(
        self,
        *,
        app_id: str | None = None,
        private_key: str | None = None,
        webhook_secret: str | None = None,
        installation_id: str | None = None,
    ):
        self.app_id = app_id
        self.private_key = private_key
        self.webhook_secret = webhook_secret
        self.installation_id = installation_id

        # Installation access token (cached, 1-hour TTL)
        self._token: str | None = None
        self._token_expires_at: float = 0

        # Rate limit tracking
        self._rate_limit_remaining: int = 5000
        self._rate_limit_reset: float = 0
        self._rate_limit_reserve: int = 50
        self._rate_limit_lock: asyncio.Lock | None = None

        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Initialize HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Squadron/0.1.0",
            },
            timeout=30.0,
        )
        self._rate_limit_lock = asyncio.Lock()
        logger.info("GitHub client started")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("GitHub client not started")
        return self._client

    # ── Authentication ───────────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        """Get a valid installation access token, refreshing if expired.

        GitHub App auth flow (AD-012):
        1. Generate JWT from App ID + private key
        2. Exchange JWT for installation access token
        3. Token valid for 1 hour (5000 req/hr)

        Retries on 401 with exponential backoff — GitHub may throttle
        rapid JWT exchanges and return "exp too far in the future".
        """
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        if not self.app_id or not self.private_key or not self.installation_id:
            raise RuntimeError(
                "GitHub App credentials not configured. "
                "Set GITHUB_APP_ID, GITHUB_PRIVATE_KEY, GITHUB_INSTALLATION_ID"
            )

        last_error = None
        max_retries = 5
        for attempt in range(max_retries):
            jwt = self._generate_jwt()
            resp = await self.client.post(
                f"/app/installations/{self.installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {jwt}"},
            )
            if resp.status_code == 201:
                data = resp.json()
                self._token = data["token"]
                self._token_expires_at = time.time() + 3500  # ~58 min (conservative)
                logger.info("Refreshed GitHub installation token (expires in ~58m)")
                return self._token
            else:
                last_error = resp
                wait = min(2**attempt, 16)  # 1s, 2s, 4s, 8s, 16s
                logger.warning(
                    "Token exchange attempt %d/%d failed (%d): %s — retrying in %ds",
                    attempt + 1,
                    max_retries,
                    resp.status_code,
                    resp.text[:100],
                    wait,
                )
                await asyncio.sleep(wait)

        # All retries failed
        last_error.raise_for_status()

    def _generate_jwt(self) -> str:
        """Generate JWT for GitHub App authentication.

        Uses PyJWT if available, otherwise raises with instructions.
        """
        try:
            import jwt as pyjwt
        except ImportError:
            raise RuntimeError("PyJWT required for GitHub App auth: pip install PyJWT cryptography")

        now = int(time.time())
        payload = {
            "iat": now - 10,  # Issued 10 seconds in the past for clock skew
            "exp": now + 540,  # Expires in 9 minutes (keep under 10-min GitHub limit)
            "iss": self.app_id,
        }
        return pyjwt.encode(payload, self.private_key, algorithm="RS256")

    async def _auth_headers(self) -> dict[str, str]:
        """Get authorization headers with current token."""
        token = await self._ensure_token()
        return {"Authorization": f"token {token}"}

    # ── Webhook Verification ─────────────────────────────────────────────

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """Verify HMAC-SHA256 webhook signature (AD-012).

        Args:
            payload: Raw request body bytes.
            signature: X-Hub-Signature-256 header value.
        """
        if not self.webhook_secret:
            logger.warning("No webhook secret configured — skipping signature verification")
            return True

        expected = (
            "sha256="
            + hmac.new(
                self.webhook_secret.encode(),
                payload,
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(expected, signature)

    # ── Rate Limit Tracking ──────────────────────────────────────────────

    def _update_rate_limit(self, response: httpx.Response) -> None:
        """Track rate limits from response headers."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining:
            self._rate_limit_remaining = int(remaining)
        if reset:
            self._rate_limit_reset = float(reset)

        if self._rate_limit_remaining < 100:
            logger.warning(
                "GitHub API rate limit low: %d remaining (resets at %s)",
                self._rate_limit_remaining,
                datetime.fromtimestamp(self._rate_limit_reset, tz=timezone.utc).isoformat(),
            )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an authenticated API request with rate limit throttling.

        When remaining quota drops below the reserve threshold, requests
        are serialized through a lock to avoid burning through the budget.
        If quota is fully exhausted, we sleep until the reset window.
        """
        if self._rate_limit_lock and self._rate_limit_remaining <= self._rate_limit_reserve:
            async with self._rate_limit_lock:
                await self._wait_for_rate_limit_reset()
                return await self._do_request(method, path, **kwargs)
        return await self._do_request(method, path, **kwargs)

    async def _do_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Execute an authenticated request and track rate limits."""
        headers = await self._auth_headers()
        headers.update(kwargs.pop("headers", {}))
        resp = await self.client.request(method, path, headers=headers, **kwargs)
        self._update_rate_limit(resp)
        resp.raise_for_status()
        return resp

    async def _wait_for_rate_limit_reset(self) -> None:
        """Sleep until the rate limit reset window if quota is exhausted."""
        if self._rate_limit_remaining > 0:
            return
        wait = max(0, self._rate_limit_reset - time.time()) + 1  # +1s buffer
        logger.warning("Rate limit exhausted — sleeping %.1fs until reset", wait)
        await asyncio.sleep(wait)
        self._rate_limit_remaining = 100  # optimistic reset

    # ── Issue Operations ─────────────────────────────────────────────────

    async def list_issues(
        self,
        owner: str,
        repo: str,
        *,
        labels: str | None = None,
        state: str = "open",
        per_page: int = 100,
    ) -> list[dict]:
        """List issues for a repository, optionally filtered by labels.

        Args:
            labels: Comma-separated label names, e.g. ``"in-progress,blocked"``.
            state: ``"open"``, ``"closed"``, or ``"all"``.
        """
        params: dict[str, str | int] = {"state": state, "per_page": per_page}
        if labels:
            params["labels"] = labels
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues",
            params=params,
        )
        # Filter out pull requests (GitHub returns PRs in the issues endpoint)
        return [i for i in resp.json() if "pull_request" not in i]

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        head: str | None = None,
        per_page: int = 100,
    ) -> list[dict]:
        """List pull requests for a repository.

        Args:
            state: ``"open"``, ``"closed"``, or ``"all"``.
            head: Filter by head user/branch, e.g. ``"user:branch"``.
        """
        params: dict[str, str | int] = {"state": state, "per_page": per_page}
        if head:
            params["head"] = head
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params=params,
        )
        return resp.json()

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> dict:
        resp = await self._request("GET", f"/repos/{owner}/{repo}/issues/{issue_number}")
        return resp.json()

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict:
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues",
            json={
                "title": title,
                "body": body,
                "labels": labels or [],
                "assignees": assignees or [],
            },
        )
        return resp.json()

    async def add_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> None:
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
            json={"labels": labels},
        )

    async def comment_on_issue(self, owner: str, repo: str, issue_number: int, body: str) -> dict:
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return resp.json()

    async def comment_on_pr(self, owner: str, repo: str, pr_number: int, body: str) -> dict:
        """Post a comment on a pull request."""
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )
        return resp.json()

    async def list_issue_comments(
        self, owner: str, repo: str, issue_number: int, *, per_page: int = 30
    ) -> list[dict]:
        """List comments on an issue (most recent last)."""
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            params={"per_page": per_page},
        )
        return resp.json()

    async def assign_issue(
        self, owner: str, repo: str, issue_number: int, assignees: list[str]
    ) -> None:
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/assignees",
            json={"assignees": assignees},
        )

    # ── PR Operations ────────────────────────────────────────────────────

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        resp = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
        return resp.json()

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        return resp.json()

    async def submit_pr_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        event: str = "COMMENT",  # APPROVE, REQUEST_CHANGES, COMMENT
        comments: list[dict] | None = None,
    ) -> dict:
        payload: dict = {"body": body, "event": event}
        if comments:
            payload["comments"] = comments
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json=payload,
        )
        return resp.json()

    async def get_pr_reviews(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """List reviews on a pull request.

        Returns a list of review dicts with 'id', 'user', 'state', 'body',
        'submitted_at' keys.  States: APPROVED, CHANGES_REQUESTED, COMMENTED.
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        )
        return resp.json()

    async def get_pr_review_comments(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """List inline review comments on a pull request.

        Returns a list of comment dicts with 'path', 'line', 'body',
        'user', 'created_at', 'diff_hunk' keys.
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
        )
        return resp.json()

    async def get_review_details(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> dict:
        """Get details of a specific review including its comments.

        Returns review dict with 'id', 'user', 'state', 'body', 'submitted_at'.
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}",
        )
        return resp.json()

    async def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict]:
        """Get inline comments for a specific review.

        Returns list of comment dicts with 'path', 'line', 'body', etc.
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}/comments",
        )
        return resp.json()

    async def list_requested_reviewers(self, owner: str, repo: str, pr_number: int) -> dict:
        """List requested reviewers for a pull request.

        Returns dict with 'users' and 'teams' lists.
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
        )
        return resp.json()

    async def create_pr_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        commit_id: str,
        path: str,
        line: int,
        *,
        side: str = "RIGHT",
    ) -> dict:
        """Create a review comment on a specific line of a PR.

        Args:
            body: Comment text (markdown).
            commit_id: SHA of the commit to comment on.
            path: Relative path of the file to comment on.
            line: Line number in the diff to comment on.
            side: 'LEFT' for deletions, 'RIGHT' for additions (default).
        """
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            json={
                "body": body,
                "commit_id": commit_id,
                "path": path,
                "line": line,
                "side": side,
            },
        )
        return resp.json()

    async def reply_to_pr_review_comment(
        self, owner: str, repo: str, pr_number: int, comment_id: int, body: str
    ) -> dict:
        """Reply to an existing PR review comment.

        Args:
            comment_id: The ID of the comment to reply to.
            body: Reply text (markdown).
        """
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
            json={"body": body},
        )
        return resp.json()

    async def update_pr_review_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> dict:
        """Update an existing PR review comment.

        Args:
            comment_id: The ID of the comment to update.
            body: New comment text (markdown).
        """
        resp = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
            json={"body": body},
        )
        return resp.json()

    async def delete_pr_review_comment(self, owner: str, repo: str, comment_id: int) -> None:
        """Delete a PR review comment.

        Args:
            comment_id: The ID of the comment to delete.
        """
        await self._request(
            "DELETE",
            f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
        )

    # ── Repository Operations ────────────────────────────────────────────

    async def get_repo(self, owner: str, repo: str) -> dict:
        resp = await self._request("GET", f"/repos/{owner}/{repo}")
        return resp.json()

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> dict:
        """Close a GitHub issue."""
        resp = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/{issue_number}",
            json={"state": "closed"},
        )
        return resp.json()

    async def update_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict:
        """Update a GitHub issue's fields."""
        payload: dict = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if labels is not None:
            payload["labels"] = labels
        if assignees is not None:
            payload["assignees"] = assignees
        resp = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/{issue_number}",
            json=payload,
        )
        return resp.json()

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        merge_method: str = "squash",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> dict:
        """Merge a pull request.

        Args:
            merge_method: 'merge', 'squash', or 'rebase'.
        """
        payload: dict = {"merge_method": merge_method}
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message
        resp = await self._request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            json=payload,
        )
        return resp.json()

    async def list_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """List files changed in a pull request.

        Returns a list of file dicts with 'filename', 'status', 'additions',
        'deletions', 'changes', 'patch' keys.
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
        )
        return resp.json()

    async def ensure_labels_exist(self, owner: str, repo: str, labels: list[str]) -> None:
        """Create labels if they don't exist (idempotent)."""
        for label_name in labels:
            try:
                await self._request(
                    "POST",
                    f"/repos/{owner}/{repo}/labels",
                    json={"name": label_name},
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 422:  # Already exists
                    continue
                raise

    async def delete_branch(self, owner: str, repo: str, branch: str) -> bool:
        """Delete a branch from the repository.

        Args:
            owner: Repository owner.
            repo: Repository name.
            branch: Branch name to delete (not the ref path, just the name).

        Returns:
            True if deleted successfully, False if branch didn't exist.
        """
        try:
            await self._request(
                "DELETE",
                f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
            )
            logger.info("Deleted branch %s/%s:%s", owner, repo, branch)
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:  # Reference does not exist
                logger.debug("Branch %s does not exist (already deleted?)", branch)
                return False
            raise

    async def get_combined_status(self, owner: str, repo: str, ref: str) -> dict:
        """Get combined status for a reference (commit SHA or branch).

        Returns dict with 'state' (success, pending, failure) and 'statuses' list.
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/commits/{ref}/status",
        )
        return resp.json()

    async def list_check_runs(self, owner: str, repo: str, ref: str) -> list[dict]:
        """List check runs for a reference (commit SHA or branch).

        Returns list of check run dicts with 'name', 'status', 'conclusion' keys.
        """
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/commits/{ref}/check-runs",
        )
        return resp.json().get("check_runs", [])

    # ── GraphQL (Projects V2) ────────────────────────────────────────────────

    async def graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GitHub GraphQL API query.

        Args:
            query: GraphQL query or mutation string.
            variables: Optional variables dict for parameterised queries.

        Returns:
            The ``data`` portion of the GraphQL response.

        Raises:
            RuntimeError: If the response contains a top-level ``errors`` key.
        """
        token = await self._ensure_token()
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.github.com/graphql",
                json=payload,
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "Squadron/0.1.0",
                },
            )
            resp.raise_for_status()
            body = resp.json()

        if "errors" in body:
            raise RuntimeError(f"GraphQL errors: {body['errors']}")

        return body.get("data", {})

    async def get_repo_owner_id(self, owner: str, repo: str) -> str:
        """Resolve the owner's global node ID (needed for createProjectV2)."""
        query = (
            "query($login: String!) {"
            "  repositoryOwner(login: $login) { id }"
            "}"
        )
        data = await self.graphql(query, {"login": owner})
        return data["repositoryOwner"]["id"]

    async def create_project(self, owner_id: str, title: str) -> dict:
        """Create a new Projects V2 board."""
        mutation = (
            "mutation($ownerId: ID!, $title: String!) {"
            "  createProjectV2(input: {ownerId: $ownerId, title: $title}) {"
            "    projectV2 { id number title url }"
            "  }"
            "}"
        )
        data = await self.graphql(mutation, {"ownerId": owner_id, "title": title})
        return data["createProjectV2"]["projectV2"]

    async def get_project_by_number(self, owner: str, repo: str, number: int) -> dict:
        """Read project metadata by project number."""
        query = (
            "query($owner: String!, $repo: String!, $number: Int!) {"
            "  repository(owner: $owner, name: $repo) {"
            "    projectV2(number: $number) { id number title url }"
            "  }"
            "}"
        )
        data = await self.graphql(query, {"owner": owner, "repo": repo, "number": number})
        return data["repository"]["projectV2"]

    async def get_project_by_id(self, project_id: str) -> dict:
        """Read project metadata by global node ID."""
        query = (
            "query($id: ID!) {"
            "  node(id: $id) {"
            "    ... on ProjectV2 { id number title url }"
            "  }"
            "}"
        )
        data = await self.graphql(query, {"id": project_id})
        return data["node"]

    async def list_projects(self, owner: str, repo: str, limit: int = 20) -> list[dict]:
        """List all Projects V2 boards linked to the repository."""
        query = (
            "query($owner: String!, $repo: String!, $limit: Int!) {"
            "  repository(owner: $owner, name: $repo) {"
            "    projectsV2(first: $limit) {"
            "      nodes { id number title url }"
            "    }"
            "  }"
            "}"
        )
        data = await self.graphql(query, {"owner": owner, "repo": repo, "limit": limit})
        return data["repository"]["projectsV2"]["nodes"]

    async def add_project_field(
        self,
        project_id: str,
        name: str,
        data_type: str,
        options: list[str] | None = None,
    ) -> dict:
        """Create a custom field on a Projects V2 board."""
        mutation = (
            "mutation($projectId: ID!, $name: String!, $dataType: ProjectV2CustomFieldType!,"
            "         $singleSelectOptions: [ProjectV2SingleSelectFieldOptionInput!]) {"
            "  createProjectV2Field(input: {"
            "    projectId: $projectId, name: $name, dataType: $dataType,"
            "    singleSelectOptions: $singleSelectOptions"
            "  }) {"
            "    projectV2Field {"
            "      ... on ProjectV2Field { id name dataType }"
            "      ... on ProjectV2SingleSelectField {"
            "        id name dataType options { id name }"
            "      }"
            "    }"
            "  }"
            "}"
        )
        variables: dict = {
            "projectId": project_id,
            "name": name,
            "dataType": data_type,
            "singleSelectOptions": [{"name": o} for o in options] if options else None,
        }
        data = await self.graphql(mutation, variables)
        return data["createProjectV2Field"]["projectV2Field"]

    async def list_project_fields(self, project_id: str, limit: int = 50) -> list[dict]:
        """List all fields on a Projects V2 board."""
        query = (
            "query($id: ID!, $limit: Int!) {"
            "  node(id: $id) {"
            "    ... on ProjectV2 {"
            "      fields(first: $limit) {"
            "        nodes {"
            "          ... on ProjectV2Field { id name dataType }"
            "          ... on ProjectV2SingleSelectField {"
            "            id name dataType options { id name }"
            "          }"
            "          ... on ProjectV2IterationField { id name dataType }"
            "        }"
            "      }"
            "    }"
            "  }"
            "}"
        )
        data = await self.graphql(query, {"id": project_id, "limit": limit})
        return data["node"]["fields"]["nodes"]

    async def get_issue_node_id(self, owner: str, repo: str, issue_number: int) -> str:
        """Resolve an issue's global node ID."""
        query = (
            "query($owner: String!, $repo: String!, $number: Int!) {"
            "  repository(owner: $owner, name: $repo) {"
            "    issue(number: $number) { id }"
            "  }"
            "}"
        )
        data = await self.graphql(query, {"owner": owner, "repo": repo, "number": issue_number})
        return data["repository"]["issue"]["id"]

    async def add_issue_to_project(self, project_id: str, issue_node_id: str) -> dict:
        """Add an issue as a card on a Projects V2 board."""
        mutation = (
            "mutation($projectId: ID!, $contentId: ID!) {"
            "  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {"
            "    item { id }"
            "  }"
            "}"
        )
        data = await self.graphql(mutation, {"projectId": project_id, "contentId": issue_node_id})
        return data["addProjectV2ItemById"]["item"]

    async def remove_issue_from_project(self, project_id: str, item_id: str) -> None:
        """Remove a card from a Projects V2 board."""
        mutation = (
            "mutation($projectId: ID!, $itemId: ID!) {"
            "  deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {"
            "    deletedItemId"
            "  }"
            "}"
        )
        await self.graphql(mutation, {"projectId": project_id, "itemId": item_id})

    async def update_project_item_field(
        self, project_id: str, item_id: str, field_id: str, value: dict
    ) -> dict:
        """Set a field value on a board item."""
        mutation = (
            "mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {"
            "  updateProjectV2ItemFieldValue(input: {"
            "    projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: $value"
            "  }) {"
            "    projectV2Item { id }"
            "  }"
            "}"
        )
        data = await self.graphql(
            mutation,
            {"projectId": project_id, "itemId": item_id, "fieldId": field_id, "value": value},
        )
        return data["updateProjectV2ItemFieldValue"]["projectV2Item"]

    async def get_project_items(self, project_id: str, limit: int = 50) -> list[dict]:
        """List items on a Projects V2 board with their field values."""
        query = (
            "query($id: ID!, $limit: Int!) {"
            "  node(id: $id) {"
            "    ... on ProjectV2 {"
            "      items(first: $limit) {"
            "        nodes {"
            "          id"
            "          fieldValues(first: 20) {"
            "            nodes {"
            "              ... on ProjectV2ItemFieldTextValue {"
            "                text field { ... on ProjectV2FieldCommon { name } }"
            "              }"
            "              ... on ProjectV2ItemFieldNumberValue {"
            "                number field { ... on ProjectV2FieldCommon { name } }"
            "              }"
            "              ... on ProjectV2ItemFieldDateValue {"
            "                date field { ... on ProjectV2FieldCommon { name } }"
            "              }"
            "              ... on ProjectV2ItemFieldSingleSelectValue {"
            "                name field { ... on ProjectV2FieldCommon { name } }"
            "              }"
            "            }"
            "          }"
            "          content {"
            "            ... on Issue { title number }"
            "            ... on PullRequest { title number }"
            "          }"
            "        }"
            "      }"
            "    }"
            "  }"
            "}"
        )
        data = await self.graphql(query, {"id": project_id, "limit": limit})
        raw_items = data["node"]["items"]["nodes"]

        items = []
        for raw in raw_items:
            fields: dict = {}
            for fv in raw.get("fieldValues", {}).get("nodes", []):
                field_info = fv.get("field", {})
                field_name = field_info.get("name") if field_info else None
                if not field_name:
                    continue
                if "text" in fv:
                    fields[field_name] = fv["text"]
                elif "number" in fv:
                    fields[field_name] = fv["number"]
                elif "date" in fv:
                    fields[field_name] = fv["date"]
                elif "name" in fv:
                    fields[field_name] = fv["name"]
            content = raw.get("content") or {}
            items.append({
                "id": raw["id"],
                "title": content.get("title", ""),
                "number": content.get("number"),
                "fields": fields,
            })
        return items

