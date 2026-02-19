"""Event Router — consumes raw GitHub events and dispatches to agents.

Runs as an async consumer loop.  Two routing layers:

**Layer 1 — Structural events** (config-driven triggers):
  PR opened/closed/merged, issue labeled/closed/reopened, push, etc.
  These fire based on ``config.yaml agent_roles.triggers``.

**Layer 2 — Command-based routing**:
  ``issue_comment.created`` events are parsed for ``@squadron-dev`` commands:
  - ``@squadron-dev help`` — lists available agents
  - ``@squadron-dev <agent>: <message>`` — routes to specific agent

  A self-loop guard prevents an agent from re-triggering itself.

Other responsibilities:
- Webhook deduplication (X-GitHub-Delivery UUID)
- Command parsing (populated on ``SquadronEvent.command``)

See event-routing.md, AD-013 for design details.
"""

from __future__ import annotations

import re
import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Awaitable

from squadron.models import (
    GitHubEvent,
    ParsedCommand,
    SquadronEvent,
    SquadronEventType,
    parse_command,
)

if TYPE_CHECKING:
    from squadron.config import SquadronConfig
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)

# Map GitHub event types to internal event types
EVENT_MAP: dict[str, SquadronEventType] = {
    "issues.opened": SquadronEventType.ISSUE_OPENED,
    "issues.reopened": SquadronEventType.ISSUE_REOPENED,
    "issues.closed": SquadronEventType.ISSUE_CLOSED,
    "issues.assigned": SquadronEventType.ISSUE_ASSIGNED,
    "issues.labeled": SquadronEventType.ISSUE_LABELED,
    "issue_comment.created": SquadronEventType.ISSUE_COMMENT,
    "pull_request.opened": SquadronEventType.PR_OPENED,
    "pull_request.closed": SquadronEventType.PR_CLOSED,
    "pull_request.synchronize": SquadronEventType.PR_SYNCHRONIZED,
    "pull_request_review.submitted": SquadronEventType.PR_REVIEW_SUBMITTED,
    "pull_request_review_comment.created": SquadronEventType.PR_REVIEW_COMMENT,
    "push": SquadronEventType.PUSH,
}

# Reverse map: SquadronEventType → GitHub event type string.
# Used by trigger matching to compare config triggers against internal events.
REVERSE_EVENT_MAP: dict[SquadronEventType, str] = {v: k for k, v in EVENT_MAP.items()}


class EventRouter:
    """Async consumer loop that routes GitHub events to the right handler."""

    def __init__(
        self,
        event_queue: asyncio.Queue[GitHubEvent],
        registry: AgentRegistry,
        config: SquadronConfig,
    ):
        self.event_queue = event_queue
        self.registry = registry
        self.config = config

        # Handler callbacks, registered by the Agent Manager
        self._handlers: dict[
            SquadronEventType, list[Callable[[SquadronEvent], Awaitable[None]]]
        ] = {}

        self._running = False
        self._task: asyncio.Task | None = None
        self.last_event_time: str | None = None  # ISO timestamp of last dispatched event

    def on(
        self, event_type: SquadronEventType, handler: Callable[[SquadronEvent], Awaitable[None]]
    ) -> None:
        """Register an event handler."""
        self._handlers.setdefault(event_type, []).append(handler)

    def clear_handlers_for(self, event_type: SquadronEventType) -> None:
        """Remove all handlers for a given event type."""
        self._handlers.pop(event_type, None)

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
        """Route a single GitHub event.

        Structural events (PR, label, issue lifecycle) are routed via
        config-driven triggers.  Comment events are routed via command
        parsing — only comments with ``@squadron-dev <agent>: <message>``
        or ``@squadron-dev help`` syntax are dispatched.  A self-loop
        guard in the AgentManager prevents an agent from re-triggering itself.
        """
        # 1. Webhook deduplication
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

        # 5. Dispatch to handlers (all routing is config-driven via AgentManager)
        await self._dispatch(squadron_event)

    def _to_squadron_event(
        self, event: GitHubEvent, event_type: SquadronEventType
    ) -> SquadronEvent:
        """Convert a GitHub event to an internal SquadronEvent.

        For comment events, parses ``@squadron-dev <agent>: <message>``
        command syntax and populates ``command``.
        """
        issue_number = None
        pr_number = None

        if event.issue:
            issue_number = event.issue.get("number")
        if event.pull_request:
            pr_number = event.pull_request.get("number")
        # issue_comment events on PRs have both issue and pull_request
        if event.payload.get("issue", {}).get("pull_request"):
            pr_number = event.payload["issue"]["number"]

        # Parse @squadron-dev command syntax from comment body
        command: ParsedCommand | None = None
        if event_type == SquadronEventType.ISSUE_COMMENT:
            comment_body = (event.comment or {}).get("body", "")
            command = parse_command(comment_body)

        # Build event data
        data = {
            "action": event.action,
            "sender": event.sender,
            "payload": event.payload,
            "issue_creator": event.issue_creator,
        }

        # Include review data for PR review events
        if event_type == SquadronEventType.PR_REVIEW_SUBMITTED:
            review = event.review or {}
            data["review"] = {
                "id": review.get("id"),
                "state": review.get("state"),  # APPROVED, CHANGES_REQUESTED, COMMENTED
                "body": review.get("body"),
                "user": review.get("user", {}).get("login"),
            }

        # Include comment data for PR review comment events
        if event_type == SquadronEventType.PR_REVIEW_COMMENT:
            comment = event.comment or {}
            data["review_comment"] = {
                "id": comment.get("id"),
                "body": comment.get("body"),
                "path": comment.get("path"),
                "line": comment.get("line") or comment.get("original_line"),
                "user": comment.get("user", {}).get("login"),
                "in_reply_to_id": comment.get("in_reply_to_id"),
            }

        return SquadronEvent(
            event_type=event_type,
            source_delivery_id=event.delivery_id,
            issue_number=issue_number,
            pr_number=pr_number,
            command=command,
            data=data,
        )

    def _is_command_comment(self, comment_body: str) -> tuple[bool, str | None]:
        """Check if a comment is a squadron command.

        Returns:
            (is_command, command_name) where is_command is True if this is a command,
            and command_name is the command if found.
        """
        if not comment_body:
            return False, None

        # Check for @squadron-dev mentions
        mention_pattern = r"@squadron-dev\s+(\w+)"
        match = re.search(mention_pattern, comment_body, re.IGNORECASE)

        if match:
            command_name = match.group(1).lower()
            return True, command_name

        return False, None

    async def _handle_command(self, event: SquadronEvent, command_name: str) -> bool:
        """Handle a squadron command.

        Returns:
            True if the command was handled (skip normal routing), False otherwise.
        """
        command_config = self.config.commands.get(command_name)

        if not command_config or not command_config.enabled:
            # Unknown or disabled command, treat as regular comment
            return False

        if not command_config.invoke_agent:
            # Command doesn't invoke agent - post response and stop routing
            if command_config.response and event.issue_number:
                # TODO: Post command response as comment (requires github client access)
                logger.info(
                    "Command '%s' handled with static response (issue #%s)",
                    command_name,
                    event.issue_number,
                )
            return True  # Skip normal routing

        # Command should invoke agent - check delegation
        if command_config.delegate_to:
            # TODO: Route to specific agent role
            logger.info(
                "Command '%s' delegated to %s agent (issue #%s)",
                command_name,
                command_config.delegate_to,
                event.issue_number,
            )

        return False  # Continue with normal routing

    async def _dispatch(self, event: SquadronEvent) -> None:
        """Dispatch an event to registered handlers.

        All routing logic is config-driven — handlers are registered by the
        AgentManager based on config.yaml trigger definitions.  The router
        itself has no opinion about which events go where.
        """
        self.last_event_time = datetime.now(timezone.utc).isoformat()

        logger.info(
            "Dispatching event: %s (issue=#%s, pr=#%s)",
            event.event_type,
            event.issue_number,
            event.pr_number,
        )

        # Handle comment events that might be commands
        if event.event_type == SquadronEventType.ISSUE_COMMENT:
            comment_body = event.data.get("payload", {}).get("comment", {}).get("body", "")
            is_command, command_name = self._is_command_comment(comment_body)

            if is_command:
                command_handled = await self._handle_command(event, command_name)
                if command_handled:
                    # Command was handled, skip normal routing
                    logger.info(
                        "Command '%s' handled, skipping PM routing (issue #%s)",
                        command_name,
                        event.issue_number,
                    )
                    # Still call registered handlers for command events
                    handlers = self._handlers.get(event.event_type, [])
                    for handler in handlers:
                        try:
                            await handler(event)
                        except Exception:
                            logger.exception("Handler error for %s", event.event_type)
                    return

        # Call registered handlers
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception("Handler error for %s", event.event_type)
