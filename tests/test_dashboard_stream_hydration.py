"""Tests for dashboard SSE stream history hydration (issue #113).

Verifies that:
- New SSE connections are hydrated with recent history before live events
- The 'hydrated' marker event is sent after history
- Agent-specific streams hydrate with agent-scoped history
- Global streams hydrate with global history
- The _HYDRATION_LIMIT constant controls how many history events are sent
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from squadron.activity import ActivityEvent, ActivityEventType
from squadron.dashboard import _HYDRATION_LIMIT


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_event(
    agent_id: str = "agent-1", event_type: ActivityEventType = ActivityEventType.INFO
) -> ActivityEvent:
    return ActivityEvent(
        id=1,
        agent_id=agent_id,
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
    )


async def collect_sse_output(gen, max_items: int = 50) -> list[str]:
    """Collect SSE output lines from an async generator, stopping after max_items."""
    results = []
    async for chunk in gen:
        results.append(chunk)
        if len(results) >= max_items:
            break
    return results


# ── Tests: _HYDRATION_LIMIT ───────────────────────────────────────────────────


def test_hydration_limit_is_positive_integer():
    """_HYDRATION_LIMIT must be a positive integer."""
    assert isinstance(_HYDRATION_LIMIT, int)
    assert _HYDRATION_LIMIT > 0


def test_hydration_limit_is_reasonable():
    """_HYDRATION_LIMIT should be at least 50 events for useful hydration."""
    assert _HYDRATION_LIMIT >= 50, (
        f"_HYDRATION_LIMIT={_HYDRATION_LIMIT} is too low for useful history hydration"
    )


# ── Tests: SSE Generator Hydration ───────────────────────────────────────────


class TestSseGeneratorHydration:
    """The SSE generator must hydrate new connections with history."""

    @pytest.mark.asyncio
    async def test_connected_event_sent_first(self):
        """The 'connected' event must be sent before any history or live events."""
        import squadron.dashboard as dashboard_mod

        history_events = [make_event()]
        mock_logger = MagicMock()
        mock_logger.subscribe = AsyncMock(return_value=asyncio.Queue())
        mock_logger.unsubscribe = AsyncMock()
        mock_logger.get_recent_activity = AsyncMock(return_value=history_events)
        dashboard_mod._activity_logger = mock_logger

        gen = dashboard_mod._sse_generator(agent_id=None)
        # First item must be the 'connected' event
        first = await gen.__anext__()
        assert "event: connected" in first

    @pytest.mark.asyncio
    async def test_history_events_sent_before_hydrated_marker(self):
        """History events must precede the 'hydrated' marker event."""
        import squadron.dashboard as dashboard_mod

        history_events = [make_event(agent_id="a1"), make_event(agent_id="a2")]
        mock_logger = MagicMock()
        mock_logger.subscribe = AsyncMock(return_value=asyncio.Queue())
        mock_logger.unsubscribe = AsyncMock()
        mock_logger.get_recent_activity = AsyncMock(return_value=history_events)
        dashboard_mod._activity_logger = mock_logger

        gen = dashboard_mod._sse_generator(agent_id=None)
        collected = []
        # Collect connected + 2 history + hydrated (4 items)
        for _ in range(4):
            collected.append(await gen.__anext__())

        event_lines = [c for c in collected if c.startswith("event:")]
        assert event_lines[0].startswith("event: connected"), (
            f"Expected connected first, got: {event_lines}"
        )
        # History activity events should come before hydrated
        hydrated_idx = next(i for i, c in enumerate(collected) if "event: hydrated" in c)
        activity_indices = [i for i, c in enumerate(collected) if "event: activity" in c]
        assert all(i < hydrated_idx for i in activity_indices), (
            "All history activity events must precede the 'hydrated' marker"
        )

    @pytest.mark.asyncio
    async def test_hydrated_event_sent_after_history(self):
        """A 'hydrated' SSE event must be sent once history has been replayed."""
        import squadron.dashboard as dashboard_mod

        mock_logger = MagicMock()
        mock_logger.subscribe = AsyncMock(return_value=asyncio.Queue())
        mock_logger.unsubscribe = AsyncMock()
        mock_logger.get_recent_activity = AsyncMock(return_value=[])
        dashboard_mod._activity_logger = mock_logger

        gen = dashboard_mod._sse_generator(agent_id=None)
        # Skip 'connected'
        await gen.__anext__()
        # Next should be 'hydrated' (no history events)
        hydrated = await gen.__anext__()
        assert "event: hydrated" in hydrated
        assert '"status": "hydrated"' in hydrated

    @pytest.mark.asyncio
    async def test_history_sent_in_chronological_order(self):
        """History events must be sent oldest-first (chronological order).

        The DB returns newest-first; the generator must reverse before sending.
        """
        import squadron.dashboard as dashboard_mod

        # Create events with distinct agent_ids to track order
        newer_event = make_event(agent_id="newer")
        older_event = make_event(agent_id="older")
        # DB returns newest first
        history_events = [newer_event, older_event]

        mock_logger = MagicMock()
        mock_logger.subscribe = AsyncMock(return_value=asyncio.Queue())
        mock_logger.unsubscribe = AsyncMock()
        mock_logger.get_recent_activity = AsyncMock(return_value=history_events)
        dashboard_mod._activity_logger = mock_logger

        gen = dashboard_mod._sse_generator(agent_id=None)
        # Skip 'connected'
        await gen.__anext__()
        # Collect 2 history events
        first_history = await gen.__anext__()
        second_history = await gen.__anext__()

        # Oldest event (older) should be sent first
        assert '"agent_id": "older"' in first_history, (
            "Oldest event must be sent first (chronological order)"
        )
        assert '"agent_id": "newer"' in second_history

    @pytest.mark.asyncio
    async def test_agent_specific_stream_uses_agent_activity(self):
        """Agent-specific streams must use get_agent_activity, not get_recent_activity."""
        import squadron.dashboard as dashboard_mod

        mock_logger = MagicMock()
        mock_logger.subscribe = AsyncMock(return_value=asyncio.Queue())
        mock_logger.unsubscribe = AsyncMock()
        mock_logger.get_agent_activity = AsyncMock(return_value=[])
        mock_logger.get_recent_activity = AsyncMock(return_value=[])
        dashboard_mod._activity_logger = mock_logger

        gen = dashboard_mod._sse_generator(agent_id="agent-123")
        await gen.__anext__()  # connected
        await gen.__anext__()  # hydrated

        mock_logger.get_agent_activity.assert_called_once_with("agent-123", limit=_HYDRATION_LIMIT)
        mock_logger.get_recent_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_global_stream_uses_recent_activity(self):
        """Global streams (agent_id=None) must use get_recent_activity."""
        import squadron.dashboard as dashboard_mod

        mock_logger = MagicMock()
        mock_logger.subscribe = AsyncMock(return_value=asyncio.Queue())
        mock_logger.unsubscribe = AsyncMock()
        mock_logger.get_recent_activity = AsyncMock(return_value=[])
        mock_logger.get_agent_activity = AsyncMock(return_value=[])
        dashboard_mod._activity_logger = mock_logger

        gen = dashboard_mod._sse_generator(agent_id=None)
        await gen.__anext__()  # connected
        await gen.__anext__()  # hydrated

        mock_logger.get_recent_activity.assert_called_once_with(limit=_HYDRATION_LIMIT)
        mock_logger.get_agent_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_subscribe_called_before_history_fetch(self):
        """subscribe() must be called BEFORE fetching history to avoid missing events."""
        import squadron.dashboard as dashboard_mod

        call_order = []

        async def track_subscribe(agent_id):
            call_order.append("subscribe")
            return asyncio.Queue()

        async def track_get_recent_activity(limit):
            call_order.append("get_recent_activity")
            return []

        mock_logger = MagicMock()
        mock_logger.subscribe = track_subscribe
        mock_logger.unsubscribe = AsyncMock()
        mock_logger.get_recent_activity = track_get_recent_activity
        dashboard_mod._activity_logger = mock_logger

        gen = dashboard_mod._sse_generator(agent_id=None)
        await gen.__anext__()  # connected (triggers subscribe + history fetch)
        await gen.__anext__()  # hydrated

        assert call_order.index("subscribe") < call_order.index("get_recent_activity"), (
            "subscribe() must be called before get_recent_activity() to avoid missing live events"
        )

    @pytest.mark.asyncio
    async def test_error_when_activity_logger_not_configured(self):
        """Generator must yield an error event if activity_logger is None."""
        import squadron.dashboard as dashboard_mod

        dashboard_mod._activity_logger = None
        gen = dashboard_mod._sse_generator(agent_id=None)
        first = await gen.__anext__()
        assert "event: error" in first
        assert '"error"' in first

    @pytest.mark.asyncio
    async def test_live_events_delivered_after_hydration(self):
        """Live events from the queue must be delivered after the hydrated marker."""
        import squadron.dashboard as dashboard_mod

        queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
        live_event = make_event(agent_id="live-agent")

        mock_logger = MagicMock()
        mock_logger.subscribe = AsyncMock(return_value=queue)
        mock_logger.unsubscribe = AsyncMock()
        mock_logger.get_recent_activity = AsyncMock(return_value=[])
        dashboard_mod._activity_logger = mock_logger

        gen = dashboard_mod._sse_generator(agent_id=None)
        await gen.__anext__()  # connected
        await gen.__anext__()  # hydrated (no history)

        # Now push a live event to the queue
        await queue.put(live_event)
        live_chunk = await gen.__anext__()
        assert "event: activity" in live_chunk
        assert '"live-agent"' in live_chunk


# ── Tests: ActivityEventType completeness ─────────────────────────────────────


class TestActivityEventTypes:
    """Verify tool_call_start and tool_call_end event types exist as expected."""

    def test_tool_call_start_exists(self):
        assert ActivityEventType.TOOL_CALL_START == "tool_call_start"

    def test_tool_call_end_exists(self):
        assert ActivityEventType.TOOL_CALL_END == "tool_call_end"

    def test_tool_call_start_in_all_values(self):
        values = [e.value for e in ActivityEventType]
        assert "tool_call_start" in values

    def test_tool_call_end_in_all_values(self):
        values = [e.value for e in ActivityEventType]
        assert "tool_call_end" in values
