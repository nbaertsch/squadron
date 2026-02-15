"""Webhook receiver — FastAPI endpoint for GitHub webhook delivery.

Validates HMAC-SHA256 signatures, parses events, and enqueues
them for the Event Router. Responds 200 immediately per GitHub's
10-second timeout requirement (AD-012).
"""

from __future__ import annotations

import logging
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


def configure(event_queue: asyncio.Queue[GitHubEvent], github_client: GitHubClient) -> None:
    """Wire the webhook endpoint to the event queue and GitHub client."""
    global _event_queue, _github_client
    _event_queue = event_queue
    _github_client = github_client


@router.post("/webhook")
async def handle_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str = Header(default=""),
) -> Response:
    """Receive and enqueue a GitHub webhook event.

    1. Verify HMAC-SHA256 signature
    2. Parse event type + action
    3. Enqueue for async processing
    4. Return 200 immediately
    """
    body = await request.body()

    # Signature verification
    if _github_client and not _github_client.verify_webhook_signature(body, x_hub_signature_256):
        logger.warning("Invalid webhook signature for delivery %s", x_github_delivery)
        return Response(status_code=401, content="Invalid signature")

    # Parse payload
    payload = await request.json()
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

    # Enqueue for async processing
    if _event_queue is not None:
        await _event_queue.put(event)
    else:
        logger.error("Event queue not configured — dropping event %s", x_github_delivery)

    return Response(status_code=200, content="ok")
