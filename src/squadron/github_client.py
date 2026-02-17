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
