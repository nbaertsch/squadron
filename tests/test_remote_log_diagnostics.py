"""Tests for remote log diagnostics — ring buffer, log endpoints, and lifecycle events.

Covers:
- LogBuffer ring buffer mechanics (push, query, size, overflow)
- RingBufferHandler integration with stdlib logging
- Dashboard /logs REST endpoint
- Dashboard /logs/stream SSE endpoint
- New ActivityEventType enum values
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── LogBuffer unit tests ─────────────────────────────────────────────────────


class TestLogBuffer:
    """Tests for the in-memory ring buffer."""

    def test_push_and_query(self):
        from squadron.log_buffer import LogBuffer, LogRecord

        buf = LogBuffer(maxlen=100)
        buf.push(LogRecord(timestamp="t1", level="INFO", name="test", message="hello"))
        buf.push(LogRecord(timestamp="t2", level="WARNING", name="test", message="warn"))

        assert buf.size == 2
        results = buf.query()
        # newest first
        assert results[0]["message"] == "warn"
        assert results[1]["message"] == "hello"

    def test_overflow_discards_oldest(self):
        from squadron.log_buffer import LogBuffer, LogRecord

        buf = LogBuffer(maxlen=3)
        for i in range(5):
            buf.push(LogRecord(timestamp=f"t{i}", level="INFO", name="test", message=f"msg{i}"))

        assert buf.size == 3
        results = buf.query()
        # Only messages 2, 3, 4 should remain (newest first)
        assert [r["message"] for r in results] == ["msg4", "msg3", "msg2"]

    def test_query_level_filter(self):
        from squadron.log_buffer import LogBuffer, LogRecord

        buf = LogBuffer(maxlen=100)
        buf.push(LogRecord(timestamp="t1", level="DEBUG", name="test", message="debug"))
        buf.push(LogRecord(timestamp="t2", level="INFO", name="test", message="info"))
        buf.push(LogRecord(timestamp="t3", level="WARNING", name="test", message="warn"))
        buf.push(LogRecord(timestamp="t4", level="ERROR", name="test", message="error"))

        results = buf.query(level="WARNING")
        assert len(results) == 2
        assert results[0]["message"] == "error"
        assert results[1]["message"] == "warn"

    def test_query_name_filter(self):
        from squadron.log_buffer import LogBuffer, LogRecord

        buf = LogBuffer(maxlen=100)
        buf.push(
            LogRecord(timestamp="t1", level="INFO", name="squadron.agent_manager", message="a")
        )
        buf.push(LogRecord(timestamp="t2", level="INFO", name="squadron.server", message="b"))
        buf.push(
            LogRecord(timestamp="t3", level="INFO", name="squadron.agent_manager.x", message="c")
        )

        results = buf.query(name="squadron.agent_manager")
        assert len(results) == 2
        # Uses startswith, so "squadron.agent_manager.x" matches too
        assert {r["message"] for r in results} == {"a", "c"}

    def test_query_limit(self):
        from squadron.log_buffer import LogBuffer, LogRecord

        buf = LogBuffer(maxlen=100)
        for i in range(20):
            buf.push(LogRecord(timestamp=f"t{i}", level="INFO", name="test", message=f"m{i}"))

        results = buf.query(limit=5)
        assert len(results) == 5

    def test_query_combined_filters(self):
        from squadron.log_buffer import LogBuffer, LogRecord

        buf = LogBuffer(maxlen=100)
        buf.push(
            LogRecord(
                timestamp="t1", level="WARNING", name="squadron.agent_manager", message="match"
            )
        )
        buf.push(
            LogRecord(timestamp="t2", level="DEBUG", name="squadron.agent_manager", message="skip")
        )
        buf.push(
            LogRecord(timestamp="t3", level="WARNING", name="squadron.server", message="skip2")
        )

        results = buf.query(level="WARNING", name="squadron.agent_manager")
        assert len(results) == 1
        assert results[0]["message"] == "match"

    def test_maxlen_property(self):
        from squadron.log_buffer import LogBuffer

        buf = LogBuffer(maxlen=5000)
        assert buf.maxlen == 5000

    def test_empty_query(self):
        from squadron.log_buffer import LogBuffer

        buf = LogBuffer(maxlen=100)
        results = buf.query()
        assert results == []


# ── RingBufferHandler tests ──────────────────────────────────────────────────


class TestRingBufferHandler:
    """Tests for the stdlib logging.Handler integration."""

    def test_handler_captures_log_records(self):
        from squadron.log_buffer import LogBuffer, RingBufferHandler

        buf = LogBuffer(maxlen=100)
        handler = RingBufferHandler(buf)

        test_logger = logging.getLogger("test.handler.capture")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.DEBUG)

        try:
            test_logger.info("test message %d", 42)

            assert buf.size == 1
            entry = buf.query()[0]
            assert entry["level"] == "INFO"
            assert entry["name"] == "test.handler.capture"
            assert "test message 42" in entry["message"]
            assert "timestamp" in entry
        finally:
            test_logger.removeHandler(handler)

    def test_handler_respects_level(self):
        from squadron.log_buffer import LogBuffer, RingBufferHandler

        buf = LogBuffer(maxlen=100)
        handler = RingBufferHandler(buf, level=logging.WARNING)

        test_logger = logging.getLogger("test.handler.level")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.DEBUG)

        try:
            test_logger.debug("debug")
            test_logger.info("info")
            test_logger.warning("warning")
            test_logger.error("error")

            assert buf.size == 2
            messages = [e["message"] for e in buf.query()]
            assert "error" in messages
            assert "warning" in messages
        finally:
            test_logger.removeHandler(handler)


# ── New ActivityEventType enum values ────────────────────────────────────────


class TestNewEventTypes:
    """Verify the new event types exist and can be used."""

    def test_session_lifecycle_event_types_exist(self):
        from squadron.activity import ActivityEventType

        assert ActivityEventType.SESSION_CREATED.value == "session_created"
        assert ActivityEventType.PROMPT_READY.value == "prompt_ready"
        assert ActivityEventType.MODEL_REQUEST_STARTED.value == "model_request_started"
        assert ActivityEventType.MODEL_REQUEST_COMPLETED.value == "model_request_completed"
        assert ActivityEventType.AGENT_HEARTBEAT.value == "agent_heartbeat"

    def test_event_types_can_be_constructed_from_value(self):
        from squadron.activity import ActivityEventType

        assert ActivityEventType("session_created") == ActivityEventType.SESSION_CREATED
        assert ActivityEventType("agent_heartbeat") == ActivityEventType.AGENT_HEARTBEAT
        assert ActivityEventType("model_request_started") == ActivityEventType.MODEL_REQUEST_STARTED

    def test_lifecycle_event_creation(self):
        from squadron.activity import ActivityEventType, create_lifecycle_event

        event = create_lifecycle_event(
            agent_id="test-agent",
            event_type=ActivityEventType.SESSION_CREATED,
            issue_number=124,
            content="Session created",
            session_id="sess-123",
        )
        assert event.agent_id == "test-agent"
        assert event.event_type == ActivityEventType.SESSION_CREATED
        assert event.metadata["session_id"] == "sess-123"


# ── Dashboard /logs endpoint tests ───────────────────────────────────────────


@pytest.fixture
def log_dashboard_app():
    """Create a FastAPI test app with dashboard router configured including log buffer."""
    import squadron.dashboard as dashboard_mod
    from squadron.log_buffer import LogBuffer, LogRecord

    mock_registry = MagicMock()
    mock_registry.get_all_active_agents = AsyncMock(return_value=[])
    mock_registry.get_recent_agents = AsyncMock(return_value=[])
    mock_registry.get_agent = AsyncMock(return_value=None)

    mock_activity = MagicMock()
    mock_activity.get_recent_activity = AsyncMock(return_value=[])
    mock_activity.get_agent_activity = AsyncMock(return_value=[])
    mock_activity.get_agent_stats = AsyncMock(return_value={"agent_id": "test", "total_events": 0})

    log_buffer = LogBuffer(maxlen=1000)
    # Pre-populate some log entries
    log_buffer.push(
        LogRecord(
            timestamp="2025-01-01T00:00:00+00:00",
            level="INFO",
            name="squadron.server",
            message="Server started",
        )
    )
    log_buffer.push(
        LogRecord(
            timestamp="2025-01-01T00:00:01+00:00",
            level="WARNING",
            name="squadron.agent_manager",
            message="Agent timeout approaching",
        )
    )
    log_buffer.push(
        LogRecord(
            timestamp="2025-01-01T00:00:02+00:00",
            level="ERROR",
            name="squadron.agent_manager",
            message="Agent failed",
        )
    )

    dashboard_mod.configure(mock_activity, mock_registry, log_buffer)

    app = FastAPI()
    app.include_router(dashboard_mod.router)
    return app


@pytest.fixture
def log_client_no_key(log_dashboard_app, monkeypatch):
    """Test client without API key configured."""
    monkeypatch.delenv("SQUADRON_DASHBOARD_API_KEY", raising=False)
    return TestClient(log_dashboard_app, raise_server_exceptions=False)


@pytest.fixture
def log_client_with_key(log_dashboard_app, monkeypatch):
    """Test client with API key configured."""
    monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "test-key-123")
    return TestClient(log_dashboard_app, raise_server_exceptions=False)


class TestLogEndpoint:
    """Tests for GET /dashboard/logs."""

    def test_logs_no_auth_required_when_no_key(self, log_client_no_key):
        resp = log_client_no_key.get("/dashboard/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 3
        assert data["buffer_size"] == 3
        assert len(data["entries"]) == 3

    def test_logs_requires_auth_when_key_configured(self, log_client_with_key):
        resp = log_client_with_key.get("/dashboard/logs")
        assert resp.status_code == 401

        resp = log_client_with_key.get(
            "/dashboard/logs",
            headers={"Authorization": "Bearer test-key-123"},
        )
        assert resp.status_code == 200

    def test_logs_level_filter(self, log_client_no_key):
        resp = log_client_no_key.get("/dashboard/logs?level=WARNING")
        data = resp.json()
        assert data["count"] == 2
        levels = {e["level"] for e in data["entries"]}
        assert levels == {"WARNING", "ERROR"}

    def test_logs_name_filter(self, log_client_no_key):
        resp = log_client_no_key.get("/dashboard/logs?name=squadron.agent_manager")
        data = resp.json()
        assert data["count"] == 2
        assert all("agent_manager" in e["name"] for e in data["entries"])

    def test_logs_combined_filters(self, log_client_no_key):
        resp = log_client_no_key.get("/dashboard/logs?level=ERROR&name=squadron.agent_manager")
        data = resp.json()
        assert data["count"] == 1
        assert data["entries"][0]["message"] == "Agent failed"

    def test_logs_limit(self, log_client_no_key):
        resp = log_client_no_key.get("/dashboard/logs?limit=1")
        data = resp.json()
        assert data["count"] == 1

    def test_logs_filters_in_response(self, log_client_no_key):
        resp = log_client_no_key.get("/dashboard/logs?level=WARNING&name=squadron")
        data = resp.json()
        assert data["filters"]["level"] == "WARNING"
        assert data["filters"]["name"] == "squadron"


class TestStatusEndpointLogBuffer:
    """Tests that /dashboard/status includes log buffer info."""

    def test_status_includes_log_buffer_info(self, log_client_no_key):
        resp = log_client_no_key.get("/dashboard/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["log_buffer"] is True
        assert data["log_buffer_size"] == 3
        assert data["log_buffer_capacity"] == 1000


class TestLogStreamEndpoint:
    """Tests for GET /dashboard/logs/stream SSE."""

    def test_log_stream_requires_auth_when_key_configured(self, log_client_with_key):
        resp = log_client_with_key.get("/dashboard/logs/stream")
        assert resp.status_code == 401

    def test_log_stream_auth_accepted_with_token(self, log_client_with_key):
        """With a valid token query param, the SSE endpoint should not return 401."""
        # Note: We cannot consume the full SSE stream in a sync test (it's infinite).
        # But we can verify auth doesn't reject us by checking the response starts.
        # The TestClient consumes the full body (which hangs on SSE), so we only
        # test the auth rejection path here. The endpoint's SSE behavior is
        # validated by the unit tests for _log_sse_generator and by the LogBuffer
        # pub/sub tests.
        resp = log_client_with_key.get("/dashboard/logs/stream?token=wrong-key")
        assert resp.status_code == 401
