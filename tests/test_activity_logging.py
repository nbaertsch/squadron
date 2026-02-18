"""Tests for activity logging and dashboard endpoints.

Covers:
  - ActivityLogger persistence (SQLite)
  - Event broadcasting to subscribers
  - Dashboard REST endpoints
  - Dashboard security (API key validation)
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from squadron.activity import (
    ActivityEvent,
    ActivityEventType,
    ActivityLogger,
    create_lifecycle_event,
    create_tool_start_event,
    create_tool_end_event,
    create_error_event,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def activity_logger(tmp_path):
    """Create an activity logger with temp database."""
    db_path = str(tmp_path / "test_activity.db")
    logger = ActivityLogger(db_path)
    await logger.initialize()
    yield logger
    await logger.close()


# ── ActivityEvent Model ──────────────────────────────────────────────────────


class TestActivityEvent:
    def test_create_event(self):
        event = ActivityEvent(
            agent_id="test-agent",
            event_type=ActivityEventType.AGENT_SPAWNED,
            issue_number=42,
        )
        assert event.agent_id == "test-agent"
        assert event.event_type == ActivityEventType.AGENT_SPAWNED
        assert event.issue_number == 42
        assert event.timestamp is not None

    def test_to_sse_data(self):
        event = ActivityEvent(
            agent_id="test-agent",
            event_type=ActivityEventType.TOOL_CALL_START,
            tool_name="bash",
            tool_args={"command": "ls -la"},
        )
        sse_data = event.to_sse_data()
        assert "test-agent" in sse_data
        assert "tool_call_start" in sse_data
        assert "bash" in sse_data

    def test_sse_truncates_long_content(self):
        long_content = "x" * 5000
        event = ActivityEvent(
            agent_id="test-agent",
            event_type=ActivityEventType.REASONING,
            content=long_content,
        )
        sse_data = event.to_sse_data()
        # Should be truncated
        assert len(sse_data) < 5000
        assert "truncated" in sse_data


# ── ActivityLogger ───────────────────────────────────────────────────────────


class TestActivityLogger:
    async def test_log_event(self, activity_logger):
        event = ActivityEvent(
            agent_id="test-agent",
            event_type=ActivityEventType.AGENT_SPAWNED,
            issue_number=1,
        )
        logged = await activity_logger.log(event)
        assert logged.id is not None

    async def test_get_agent_activity(self, activity_logger):
        # Log some events
        for i in range(5):
            event = ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.TOOL_CALL_START,
                tool_name=f"tool_{i}",
            )
            await activity_logger.log(event)

        # Query
        events = await activity_logger.get_agent_activity("test-agent", limit=3)
        assert len(events) == 3

    async def test_get_agent_activity_with_filter(self, activity_logger):
        # Log mixed events
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.TOOL_CALL_START,
                tool_name="bash",
            )
        )
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )

        # Filter by type
        events = await activity_logger.get_agent_activity(
            "test-agent",
            event_types=[ActivityEventType.TOOL_CALL_START],
        )
        assert len(events) == 1
        assert events[0].event_type == ActivityEventType.TOOL_CALL_START

    async def test_get_recent_activity(self, activity_logger):
        # Log events for multiple agents
        await activity_logger.log(
            ActivityEvent(
                agent_id="agent-1",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )
        await activity_logger.log(
            ActivityEvent(
                agent_id="agent-2",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )

        events = await activity_logger.get_recent_activity(limit=10)
        assert len(events) == 2
        # Should have both agents
        agent_ids = {e.agent_id for e in events}
        assert "agent-1" in agent_ids
        assert "agent-2" in agent_ids

    async def test_get_agent_stats(self, activity_logger):
        # Log some events
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.TOOL_CALL_END,
                tool_duration_ms=100,
            )
        )
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.TOOL_CALL_END,
                tool_duration_ms=200,
            )
        )
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.ERROR,
                content="test error",
            )
        )

        stats = await activity_logger.get_agent_stats("test-agent")
        assert stats["agent_id"] == "test-agent"
        assert stats["total_events"] == 4
        assert stats["tool_calls"] == 2
        assert stats["errors"] == 1
        assert stats["avg_tool_duration_ms"] == 150.0

    async def test_prune_old_activity(self, activity_logger):
        # Log an event
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )

        # Prune with 0 hours (should delete all)
        pruned = await activity_logger.prune_old_activity(hours=0)
        assert pruned == 1


# ── Subscription/Broadcast ───────────────────────────────────────────────────


class TestActivityBroadcast:
    async def test_subscribe_per_agent(self, activity_logger):
        queue = await activity_logger.subscribe("test-agent")
        assert queue is not None

        # Log event for this agent
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )

        # Should receive event
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event.agent_id == "test-agent"

    async def test_subscribe_global(self, activity_logger):
        queue = await activity_logger.subscribe(None)  # Global subscription

        # Log event for any agent
        await activity_logger.log(
            ActivityEvent(
                agent_id="random-agent",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )

        # Should receive event
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event.agent_id == "random-agent"

    async def test_per_agent_subscription_filters(self, activity_logger):
        queue = await activity_logger.subscribe("agent-1")

        # Log event for different agent
        await activity_logger.log(
            ActivityEvent(
                agent_id="agent-2",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )

        # Should NOT receive event (timeout)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.1)

    async def test_unsubscribe(self, activity_logger):
        queue = await activity_logger.subscribe("test-agent")
        await activity_logger.unsubscribe(queue, "test-agent")

        # Log event
        await activity_logger.log(
            ActivityEvent(
                agent_id="test-agent",
                event_type=ActivityEventType.AGENT_SPAWNED,
            )
        )

        # Queue should be empty (unsubscribed)
        assert queue.empty()


# ── Helper Functions ─────────────────────────────────────────────────────────


class TestHelperFunctions:
    def test_create_lifecycle_event(self):
        event = create_lifecycle_event(
            agent_id="test-agent",
            event_type=ActivityEventType.AGENT_SPAWNED,
            issue_number=42,
            role="feat-dev",
        )
        assert event.agent_id == "test-agent"
        assert event.event_type == ActivityEventType.AGENT_SPAWNED
        assert event.issue_number == 42
        assert event.metadata["role"] == "feat-dev"

    def test_create_tool_start_event(self):
        event = create_tool_start_event(
            agent_id="test-agent",
            tool_name="bash",
            tool_args={"command": "ls"},
        )
        assert event.event_type == ActivityEventType.TOOL_CALL_START
        assert event.tool_name == "bash"
        assert event.tool_args == {"command": "ls"}

    def test_create_tool_end_event(self):
        event = create_tool_end_event(
            agent_id="test-agent",
            tool_name="bash",
            success=True,
            duration_ms=150,
            result="output",
        )
        assert event.event_type == ActivityEventType.TOOL_CALL_END
        assert event.tool_name == "bash"
        assert event.tool_success is True
        assert event.tool_duration_ms == 150
        assert event.tool_result == "output"

    def test_create_error_event(self):
        event = create_error_event(
            agent_id="test-agent",
            error_message="Something went wrong",
            issue_number=42,
            error_type="RuntimeError",
        )
        assert event.event_type == ActivityEventType.ERROR
        assert event.content == "Something went wrong"
        assert event.metadata["error_type"] == "RuntimeError"


# ── Dashboard Security ───────────────────────────────────────────────────────


class TestDashboardSecurity:
    def test_get_security_config_no_key(self, monkeypatch):
        monkeypatch.delenv("SQUADRON_DASHBOARD_API_KEY", raising=False)
        from squadron.dashboard_security import get_security_config

        config = get_security_config()
        assert config["authentication_required"] is False
        assert config["api_key_configured"] is False

    def test_get_security_config_with_key(self, monkeypatch):
        monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "test-secret-key")
        from squadron.dashboard_security import get_security_config

        config = get_security_config()
        assert config["authentication_required"] is True
        assert config["api_key_configured"] is True

    def test_generate_api_key(self):
        from squadron.dashboard_security import generate_api_key

        key1 = generate_api_key()
        key2 = generate_api_key()
        assert len(key1) >= 32
        assert key1 != key2  # Should be unique


# ── Integration Tests ────────────────────────────────────────────────────────


class TestActivityLoggingIntegration:
    async def test_full_agent_lifecycle(self, activity_logger):
        """Test logging a complete agent lifecycle."""
        agent_id = "integration-test-agent"

        # Spawn
        await activity_logger.log(
            create_lifecycle_event(
                agent_id=agent_id,
                event_type=ActivityEventType.AGENT_SPAWNED,
                issue_number=100,
                role="feat-dev",
            )
        )

        # Tool calls
        await activity_logger.log(
            create_tool_start_event(
                agent_id=agent_id,
                tool_name="read_file",
                tool_args={"path": "/src/main.py"},
            )
        )
        await activity_logger.log(
            create_tool_end_event(
                agent_id=agent_id,
                tool_name="read_file",
                success=True,
                duration_ms=50,
            )
        )

        # Complete
        await activity_logger.log(
            create_lifecycle_event(
                agent_id=agent_id,
                event_type=ActivityEventType.AGENT_COMPLETED,
                issue_number=100,
                content="Task completed successfully",
            )
        )

        # Verify full history
        events = await activity_logger.get_agent_activity(agent_id, limit=100)
        assert len(events) == 4

        # Check chronological order (newest first)
        event_types = [e.event_type for e in events]
        assert event_types[0] == ActivityEventType.AGENT_COMPLETED
        assert event_types[-1] == ActivityEventType.AGENT_SPAWNED

        # Verify stats
        stats = await activity_logger.get_agent_stats(agent_id)
        assert stats["total_events"] == 4
        assert stats["tool_calls"] == 1  # Only TOOL_CALL_END counts
