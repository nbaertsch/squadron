"""Host-side auth broker — single async service that holds GitHub credentials.

The broker is a long-running asyncio task that processes requests from
all ToolProxy instances. Credentials never leave this module.

Request/response cycle:
  1. ToolProxy enqueues a BrokerRequest on the shared asyncio.Queue.
  2. Broker dequeues, validates session token, injects credentials, calls API.
  3. Broker puts BrokerResponse on the per-request response queue.
  4. ToolProxy awaits the response and returns it to the agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from squadron.github_client import GitHubClient

logger = logging.getLogger(__name__)


@dataclass
class BrokerRequest:
    """A validated, scoped tool-call request forwarded from a ToolProxy."""

    agent_id: str
    session_token: bytes  # raw token — validated by proxy before submission
    tool: str
    params: dict[str, Any]
    response_queue: asyncio.Queue  # receives exactly one BrokerResponse


@dataclass
class BrokerResponse:
    """Response from the auth broker to a ToolProxy."""

    ok: bool
    data: Any = None
    error: str = ""


class AuthBroker:
    """Single-instance async auth broker.

    Holds the GitHub token in memory (host-side only) and executes
    authenticated API calls on behalf of validated, scoped requests.

    The broker validates the session token on every request — an expired
    or unknown token results in an immediate error response without any
    API call being made.
    """

    def __init__(self, github: GitHubClient) -> None:
        self._github = github
        self._queue: asyncio.Queue[BrokerRequest] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._running = False

        # Map of session_token_hash -> agent_id for validation.
        # Populated by register_session(), cleaned by unregister_session().
        self._sessions: dict[str, str] = {}  # token_hash -> agent_id

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the broker background task."""
        self._running = True
        self._task = asyncio.create_task(self._run(), name="auth-broker")
        logger.info("AuthBroker started")

    async def stop(self) -> None:
        """Stop the broker gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AuthBroker stopped")

    # ── Session Management ────────────────────────────────────────────────────

    def register_session(self, agent_id: str, session_token: bytes) -> None:
        """Register a new agent session.  Called when a sandbox is created."""
        token_hash = _token_hash(session_token)
        self._sessions[token_hash] = agent_id
        logger.debug("AuthBroker: registered session for agent %s", agent_id)

    def unregister_session(self, session_token: bytes) -> None:
        """Unregister a session when the sandbox is torn down."""
        token_hash = _token_hash(session_token)
        self._sessions.pop(token_hash, None)
        logger.debug("AuthBroker: unregistered session token hash %s...", token_hash[:8])

    def is_valid_session(self, session_token: bytes, agent_id: str) -> bool:
        """Return True if the token is registered for this agent."""
        token_hash = _token_hash(session_token)
        return self._sessions.get(token_hash) == agent_id

    # ── Request Queue ─────────────────────────────────────────────────────────

    async def submit(self, request: BrokerRequest) -> BrokerResponse:
        """Submit a request and wait for the response.

        This is called by ToolProxy — it blocks the proxy coroutine until
        the broker processes the request.
        """
        await self._queue.put(request)
        response: BrokerResponse = await request.response_queue.get()
        return response

    # ── Background Processor ──────────────────────────────────────────────────

    async def _run(self) -> None:
        """Process broker requests one at a time."""
        while self._running:
            try:
                request = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                response = await self._handle(request)
            except Exception as exc:
                logger.exception("AuthBroker: unhandled error for %s/%s", request.agent_id, request.tool)
                response = BrokerResponse(ok=False, error=f"broker internal error: {exc}")

            try:
                request.response_queue.put_nowait(response)
            except asyncio.QueueFull:
                logger.error("AuthBroker: response queue full for %s", request.agent_id)

    async def _handle(self, request: BrokerRequest) -> BrokerResponse:
        """Handle a single validated broker request.

        At this point the request has already been validated by the
        ToolProxy (token, allowlist, parameter scope).  The broker's job
        is solely to inject credentials and execute the API call.
        """
        # Double-check session is still valid (could have been unregistered)
        if not self.is_valid_session(request.session_token, request.agent_id):
            logger.warning(
                "AuthBroker: request from unregistered/mismatched session "
                "(agent=%s, tool=%s)",
                request.agent_id,
                request.tool,
            )
            return BrokerResponse(ok=False, error="session token invalid or expired")

        tool = request.tool
        params = request.params

        try:
            result = await self._dispatch(tool, params)
            return BrokerResponse(ok=True, data=result)
        except Exception as exc:
            logger.exception("AuthBroker: API call failed for tool=%s", tool)
            return BrokerResponse(ok=False, error=str(exc))

    async def _dispatch(self, tool: str, params: dict[str, Any]) -> Any:
        """Dispatch a validated tool call to the GitHub API.

        Only tools that agents are explicitly allowed to call via the
        proxy reach this point.  The dispatch table maps tool names to
        GitHubClient methods.
        """
        gh = self._github
        owner = params.get("_owner", "")
        repo = params.get("_repo", "")

        # Strip internal routing params before passing to API
        api_params = {k: v for k, v in params.items() if not k.startswith("_")}

        # Tool dispatch table — maps tool name -> (method, positional_arg_keys)
        # All GitHub API calls are routed through this table.
        dispatch: dict[str, Any] = {
            "read_issue": lambda: gh.get_issue(owner, repo, api_params["issue_number"]),
            "list_issue_comments": lambda: gh.list_issue_comments(
                owner, repo, api_params["issue_number"]
            ),
            "comment_on_issue": lambda: gh.create_issue_comment(
                owner, repo, api_params["issue_number"], api_params["body"]
            ),
            "comment_on_pr": lambda: gh.create_pr_comment(
                owner, repo, api_params["pr_number"], api_params["body"]
            ),
            "open_pr": lambda: gh.create_pull_request(
                owner,
                repo,
                title=api_params["title"],
                body=api_params.get("body", ""),
                head=api_params["head"],
                base=api_params.get("base", "main"),
            ),
            "get_pr_details": lambda: gh.get_pull_request(
                owner, repo, api_params["pr_number"]
            ),
            "get_pr_feedback": lambda: gh.get_pull_request_reviews(
                owner, repo, api_params["pr_number"]
            ),
            "list_pr_files": lambda: gh.list_pull_request_files(
                owner, repo, api_params["pr_number"]
            ),
            "list_pr_reviews": lambda: gh.get_pull_request_reviews(
                owner, repo, api_params["pr_number"]
            ),
            "list_issue_comments_api": lambda: gh.list_issue_comments(
                owner, repo, api_params["issue_number"]
            ),
            "git_push": lambda: _noop("git_push handled by host"),
        }

        if tool not in dispatch:
            raise ValueError(f"tool '{tool}' not in broker dispatch table")

        return await dispatch[tool]()


def _token_hash(token: bytes) -> str:
    return hashlib.sha256(token).hexdigest()


async def _noop(msg: str) -> dict:
    return {"status": msg}
