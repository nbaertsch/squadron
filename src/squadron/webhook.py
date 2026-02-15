"""Webhook receiver — FastAPI endpoint for GitHub webhook delivery.

Validates HMAC-SHA256 signatures, installation ID, repository scope,
and rate limits before enqueuing events for the Event Router.
Responds 200 immediately per GitHub's 10-second timeout (AD-012).

Security model (single-tenant):
- HMAC-SHA256 signature verification (webhook secret)
- Installation ID validation (reject webhooks from other installations)
- Repository scope validation (reject webhooks for unexpected repos)
- Request rate limiting (cap bursts to prevent resource exhaustion)
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Header, Request, Response

from squadron.models import GitHubEvent

if TYPE_CHECKING:
    import asyncio

    from squadron.github_client import GitHubClient

logger = logging.getLogger(__name__)

router = APIRouter()

# These are set during server startup (see server.py)
_event_queue: asyncio.Queue[GitHubEvent] | None = None
_github_client: GitHubClient | None = None
_expected_installation_id: str | None = None
_expected_repo_full_name: str | None = None

# Rate limiting state
_rate_limit_max: int = 60  # max webhook deliveries per window
_rate_limit_window: float = 60.0  # window in seconds
_rate_limit_timestamps: list[float] = []


def configure(
    event_queue: asyncio.Queue[GitHubEvent],
    github_client: GitHubClient,
    *,
    expected_installation_id: str | None = None,
    expected_repo_full_name: str | None = None,
    rate_limit_max: int = 60,
) -> None:
    """Wire the webhook endpoint to the event queue and GitHub client.

    Args:
        event_queue: Queue for async event processing.
        github_client: GitHub client for signature verification.
        expected_installation_id: If set, reject webhooks from other installations.
        expected_repo_full_name: If set, reject webhooks for other repos (owner/repo).
        rate_limit_max: Max webhook deliveries per minute (0 = unlimited).
    """
    global _event_queue, _github_client, _expected_installation_id
    global _expected_repo_full_name, _rate_limit_max, _rate_limit_timestamps
    _event_queue = event_queue
    _github_client = github_client
    _expected_installation_id = expected_installation_id
    _expected_repo_full_name = expected_repo_full_name
    _rate_limit_max = rate_limit_max
    _rate_limit_timestamps = []


def _check_rate_limit() -> bool:
    """Return True if the request is within rate limits."""
    global _rate_limit_timestamps
    if _rate_limit_max <= 0:
        return True

    now = time.monotonic()
    cutoff = now - _rate_limit_window
    _rate_limit_timestamps = [t for t in _rate_limit_timestamps if t > cutoff]

    if len(_rate_limit_timestamps) >= _rate_limit_max:
        return False

    _rate_limit_timestamps.append(now)
    return True


@router.post("/webhook")
async def handle_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str = Header(default=""),
) -> Response:
    """Receive and enqueue a GitHub webhook event.

    Security checks (in order):
    1. Rate limit
    2. HMAC-SHA256 signature verification
    3. Installation ID validation
    4. Repository scope validation
    5. Parse + enqueue for async processing
    6. Return 200 immediately
    """
    # 1. Rate limiting
    if not _check_rate_limit():
        logger.warning("Webhook rate limit exceeded (delivery=%s)", x_github_delivery)
        return Response(status_code=429, content="Rate limit exceeded")

    body = await request.body()

    # 2. Signature verification
    if _github_client and not _github_client.verify_webhook_signature(body, x_hub_signature_256):
        logger.warning("Invalid webhook signature for delivery %s", x_github_delivery)
        return Response(status_code=401, content="Invalid signature")

    # Parse payload
    payload = await request.json()

    # 3. Installation ID validation (single-tenant security)
    if _expected_installation_id:
        webhook_installation_id = str(payload.get("installation", {}).get("id", ""))
        if webhook_installation_id != _expected_installation_id:
            logger.warning(
                "Webhook from unexpected installation %s (expected %s, delivery=%s)",
                webhook_installation_id,
                _expected_installation_id,
                x_github_delivery,
            )
            return Response(status_code=403, content="Unknown installation")

    # 4. Repository scope validation (single-tenant security)
    if _expected_repo_full_name:
        webhook_repo = payload.get("repository", {}).get("full_name", "")
        if webhook_repo and webhook_repo != _expected_repo_full_name:
            logger.warning(
                "Webhook for unexpected repo %s (expected %s, delivery=%s)",
                webhook_repo,
                _expected_repo_full_name,
                x_github_delivery,
            )
            return Response(status_code=403, content="Unknown repository")

    action = payload.get("action")

    event = GitHubEvent(
        delivery_id=x_github_delivery,
        event_type=x_github_event,
        action=action,
        payload=payload,
    )

    logger.info(
        "Webhook received: %s (delivery=%s, sender=%s)",
        event.full_type,
        x_github_delivery,
        event.sender,
    )

    # 5. Enqueue for async processing
    if _event_queue is not None:
        await _event_queue.put(event)
    else:
        logger.error("Event queue not configured — dropping event %s", x_github_delivery)

    return Response(status_code=200, content="ok")
