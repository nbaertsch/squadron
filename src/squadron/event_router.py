"""Event Router — consumes raw GitHub events and dispatches to agents.

Runs as an async consumer loop. Handles:
- Bot self-event filtering (squadron[bot] events)
- Webhook deduplication (X-GitHub-Delivery UUID)
- Event type → handler dispatch
- PM queue routing for triage events
- Agent inbox routing for targeted events

See event-routing.md and AD-013 for design details.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Awaitable

from squadron.models import GitHubEvent, SquadronEvent, SquadronEventType

if TYPE_CHECKING:
    from squadron.config import SquadronConfig
    from squadron.registry import AgentRegistry
    from squadron.workflow_engine import WorkflowEngine

logger = logging.getLogger(__name__)

# Map GitHub event types to internal event types
EVENT_MAP: dict[str, SquadronEventType] = {
    "issues.opened": SquadronEventType.ISSUE_OPENED,
    "issues.closed": SquadronEventType.ISSUE_CLOSED,
    "issues.assigned": SquadronEventType.ISSUE_ASSIGNED,
    "issues.labeled": SquadronEventType.ISSUE_LABELED,
    "issue_comment.created": SquadronEventType.ISSUE_COMMENT,
    "pull_request.opened": SquadronEventType.PR_OPENED,
    "pull_request.closed": SquadronEventType.PR_CLOSED,
    "pull_request.synchronize": SquadronEventType.PR_SYNCHRONIZED,
    "pull_request_review.submitted": SquadronEventType.PR_REVIEW_SUBMITTED,
    "push": SquadronEventType.PUSH,
}


class EventRouter:
    """Async consumer loop that routes GitHub events to the right handler."""

    def __init__(
        self,
        event_queue: asyncio.Queue[GitHubEvent],
        registry: AgentRegistry,
        config: SquadronConfig,
        bot_username: str = "squadron[bot]",
    ):
        self.event_queue = event_queue
        self.registry = registry
        self.config = config
        self.bot_username = bot_username

        # Handler callbacks, registered by the Agent Manager
        self._handlers: dict[
            SquadronEventType, list[Callable[[SquadronEvent], Awaitable[None]]]
        ] = {}

        # PM event queue — batched events for PM processing
        self.pm_queue: asyncio.Queue[SquadronEvent] = asyncio.Queue()

        # Workflow engine (optional — set via set_workflow_engine)
        self._workflow_engine: WorkflowEngine | None = None

        self._running = False
        self._task: asyncio.Task | None = None

    def set_workflow_engine(self, engine: WorkflowEngine) -> None:
        """Attach the workflow engine for event-driven pipeline triggers."""
        self._workflow_engine = engine

    def on(
        self, event_type: SquadronEventType, handler: Callable[[SquadronEvent], Awaitable[None]]
    ) -> None:
        """Register an event handler."""
        self._handlers.setdefault(event_type, []).append(handler)

    async def start(self) -> None:
        """Start the event consumer loop."""
        self._running = True
        self._task = asyncio.create_task(self._consumer_loop(), name="event-router")
        logger.info("Event router started")

    async def stop(self) -> None:
        """Stop the event consumer loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Event router stopped")

    async def _consumer_loop(self) -> None:
        """Main consumer loop — dequeue and route events."""
        while self._running:
            try:
                event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._route_event(event)
            except Exception:
                logger.exception("Error routing event %s", event.delivery_id)

    async def _route_event(self, event: GitHubEvent) -> None:
        """Route a single GitHub event."""
        # 1. Bot self-event filter
        if event.sender == self.bot_username:
            logger.debug("Filtered bot self-event: %s", event.full_type)
            return

        # 2. Webhook deduplication
        if await self.registry.has_seen_event(event.delivery_id):
            logger.debug("Duplicate event filtered: %s", event.delivery_id)
            return
        await self.registry.mark_event_seen(event.delivery_id, event.full_type)

        # 3. Map to internal event type
        internal_type = EVENT_MAP.get(event.full_type)
        if internal_type is None:
            logger.debug("Unhandled event type: %s", event.full_type)
            return

        # 4. Create internal event
        squadron_event = self._to_squadron_event(event, internal_type)

        # 5. Workflow engine evaluation (triggers pipelines before normal dispatch)
        if self._workflow_engine:
            try:
                triggered = await self._workflow_engine.evaluate_event(
                    event.full_type,
                    event.payload,
                    squadron_event,
                )
                if triggered:
                    logger.info(
                        "Workflow triggered for %s — skipping approval flow", event.full_type
                    )

                # For PR review events, check if this advances a workflow stage
                if (
                    internal_type == SquadronEventType.PR_REVIEW_SUBMITTED
                    and squadron_event.pr_number
                ):
                    review = event.payload.get("review", {})
                    await self._workflow_engine.handle_pr_review(
                        pr_number=squadron_event.pr_number,
                        reviewer=review.get("user", {}).get("login", ""),
                        review_state=review.get("state", ""),
                        payload=event.payload,
                        squadron_event=squadron_event,
                    )
            except Exception:
                logger.exception("Workflow engine error for %s", event.full_type)

        # 6. Dispatch to handlers
        await self._dispatch(squadron_event)

    def _to_squadron_event(
        self, event: GitHubEvent, event_type: SquadronEventType
    ) -> SquadronEvent:
        """Convert a GitHub event to an internal SquadronEvent."""
        issue_number = None
        pr_number = None

        if event.issue:
            issue_number = event.issue.get("number")
        if event.pull_request:
            pr_number = event.pull_request.get("number")
        # issue_comment events on PRs have both issue and pull_request
        if event.payload.get("issue", {}).get("pull_request"):
            pr_number = event.payload["issue"]["number"]

        return SquadronEvent(
            event_type=event_type,
            source_delivery_id=event.delivery_id,
            issue_number=issue_number,
            pr_number=pr_number,
            data={
                "action": event.action,
                "sender": event.sender,
                "payload": event.payload,
            },
        )

    async def _dispatch(self, event: SquadronEvent) -> None:
        """Dispatch an event to registered handlers and route to PM/agent inboxes."""
        logger.info(
            "Dispatching event: %s (issue=#%s, pr=#%s)",
            event.event_type,
            event.issue_number,
            event.pr_number,
        )

        # PM-bound events: issue triage, comments with @-pings, blocker resolution
        pm_events = {
            SquadronEventType.ISSUE_OPENED,
            SquadronEventType.ISSUE_CLOSED,
            SquadronEventType.ISSUE_COMMENT,
            SquadronEventType.ISSUE_LABELED,
        }

        if event.event_type in pm_events:
            await self.pm_queue.put(event)

        # PR events: route to approval flow / review agents
        pr_events = {
            SquadronEventType.PR_OPENED,
            SquadronEventType.PR_REVIEW_SUBMITTED,
            SquadronEventType.PR_SYNCHRONIZED,
        }

        if event.event_type in pr_events:
            # Also send to PM for awareness
            await self.pm_queue.put(event)

        # Call registered handlers
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception("Handler error for %s", event.event_type)
