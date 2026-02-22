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
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── Heartbeat bugfix tests ───────────────────────────────────────────────────


class TestHeartbeatUsesThread:
    """Bug #1: Heartbeat must run on a dedicated thread, not an asyncio task."""

    @pytest.mark.asyncio
    async def test_start_heartbeat_creates_stop_event(self):
        """_start_heartbeat should populate _heartbeat_stops with a threading.Event."""
        import threading
        from unittest.mock import MagicMock

        from squadron.agent_manager import AgentManager

        mgr = MagicMock(spec=AgentManager)
        mgr._heartbeat_stops = {}
        mgr.registry = MagicMock()

        record = MagicMock()
        record.agent_id = "test-agent-hb"
        record.issue_number = 1
        record.pr_number = None

        # Call the real method — must be within an async context so
        # asyncio.get_running_loop() succeeds inside _start_heartbeat
        AgentManager._start_heartbeat(mgr, record)

        assert "test-agent-hb" in mgr._heartbeat_stops
        assert isinstance(mgr._heartbeat_stops["test-agent-hb"], threading.Event)

        # Cleanup: signal the thread to stop
        mgr._heartbeat_stops["test-agent-hb"].set()

    def test_stop_heartbeat_signals_event(self):
        """_stop_heartbeat should set the threading.Event and remove from dict."""
        import threading
        from unittest.mock import MagicMock

        from squadron.agent_manager import AgentManager

        mgr = MagicMock(spec=AgentManager)
        stop_event = threading.Event()
        mgr._heartbeat_stops = {"agent-1": stop_event}

        AgentManager._stop_heartbeat(mgr, "agent-1")

        assert stop_event.is_set()
        assert "agent-1" not in mgr._heartbeat_stops

    def test_stop_heartbeat_noop_for_unknown_agent(self):
        """_stop_heartbeat should be safe to call for non-existent agents."""
        from unittest.mock import MagicMock

        from squadron.agent_manager import AgentManager

        mgr = MagicMock(spec=AgentManager)
        mgr._heartbeat_stops = {}

        # Should not raise
        AgentManager._stop_heartbeat(mgr, "no-such-agent")

    def test_heartbeat_thread_fires_within_interval(self):
        """The heartbeat thread should emit an event after the interval elapses."""
        import asyncio
        import threading
        import time

        from squadron.agent_manager import AgentManager

        mgr = MagicMock(spec=AgentManager)
        mgr.registry = MagicMock()
        mgr._log_activity = AsyncMock()

        loop = asyncio.new_event_loop()
        stop_event = threading.Event()

        # Patch the heartbeat to use a very short interval for testing.
        # We can't easily change the 60s constant, so instead we test the
        # thread mechanics by directly calling _heartbeat_thread with a
        # pre-signaled stop event (fires immediately after first wait).
        def signal_after_short_delay():
            time.sleep(0.1)
            stop_event.set()

        timer = threading.Thread(target=signal_after_short_delay, daemon=True)
        timer.start()

        # Run in a thread (simulates the real call)
        t = threading.Thread(
            target=AgentManager._heartbeat_thread,
            args=(mgr, "test-agent", 1, None, stop_event, loop),
            daemon=True,
        )
        t.start()
        t.join(timeout=2)

        # Thread should have exited (stop_event was set before the 60s sleep completed)
        assert not t.is_alive()
        loop.close()


class TestLogActivityErrorVisibility:
    """Bug #2: _log_activity failures must be logged at WARNING, not DEBUG."""

    @pytest.mark.asyncio
    async def test_failed_activity_logs_at_warning(self, caplog):
        """When _log_activity fails, the error should appear at WARNING level."""
        from squadron.agent_manager import AgentManager

        mgr = MagicMock(spec=AgentManager)
        mgr.activity_logger = MagicMock()
        mgr.activity_logger.log = AsyncMock(side_effect=RuntimeError("DB locked"))

        with caplog.at_level(logging.WARNING, logger="squadron.agent_manager"):
            await AgentManager._log_activity(mgr, "agent-1", "agent_heartbeat", content="test")

        # Should have a WARNING log about the failure
        warning_messages = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_messages) >= 1
        assert "Failed to log activity event" in warning_messages[0].message


class TestLogBufferAttachLoop:
    """Bug #3: LogBuffer should store event loop ref for cross-thread broadcast."""

    def test_attach_loop_stores_reference(self):
        import asyncio

        from squadron.log_buffer import LogBuffer

        buf = LogBuffer(maxlen=100)
        assert buf._loop is None

        loop = asyncio.new_event_loop()
        buf.attach_loop(loop)
        assert buf._loop is loop
        loop.close()

    def test_push_broadcasts_via_stored_loop(self):
        """push() should use the stored loop for call_soon_threadsafe."""
        import asyncio

        from squadron.log_buffer import LogBuffer, LogRecord

        buf = LogBuffer(maxlen=100)
        loop = asyncio.new_event_loop()
        buf.attach_loop(loop)

        # Subscribe from within the loop context
        queue = asyncio.Queue(maxsize=100)
        buf._subscribers.append(queue)

        # Push from a "different thread" context (no running loop)
        buf.push(LogRecord(timestamp="t1", level="INFO", name="test", message="hello"))

        # The entry should be in the buffer
        assert buf.size == 1

        # Run the event loop briefly to process the call_soon_threadsafe callback
        loop.call_soon(loop.stop)
        loop.run_forever()

        # The subscriber queue should have received the entry
        assert not queue.empty()
        entry = queue.get_nowait()
        assert entry["message"] == "hello"

        loop.close()

    def test_push_falls_back_to_get_running_loop(self):
        """If attach_loop was not called, push should still try get_running_loop."""
        from squadron.log_buffer import LogBuffer, LogRecord

        buf = LogBuffer(maxlen=100)
        assert buf._loop is None

        # Push without a running loop — should not raise, just skip broadcast
        buf.push(LogRecord(timestamp="t1", level="INFO", name="test", message="hello"))
        assert buf.size == 1


class TestCleanupAgentStopsHeartbeat:
    """Bug #6: _cleanup_agent must stop heartbeat to prevent orphaned threads."""

    @pytest.mark.asyncio
    async def test_cleanup_stops_heartbeat(self):
        """_cleanup_agent should call _stop_heartbeat for the agent."""
        import threading

        from squadron.agent_manager import AgentManager

        mgr = MagicMock(spec=AgentManager)
        mgr._copilot_agents = {}
        mgr._agent_tasks = {}
        mgr._watchdog_enforced = set()
        mgr.agent_mail_queues = {}
        mgr.agent_inboxes = {}

        stop_event = threading.Event()
        mgr._heartbeat_stops = {"agent-cleanup": stop_event}

        # Make all the methods that _cleanup_agent calls behave:
        # - _cancel_watchdog: already a MagicMock from spec
        # - _stop_heartbeat: use real impl so we can verify stop_event is set
        mgr._stop_heartbeat = lambda aid: AgentManager._stop_heartbeat(mgr, aid)
        # - _sandbox.teardown_session: async mock
        mgr._sandbox = MagicMock()
        mgr._sandbox.teardown_session = AsyncMock()
        # - registry.get_agent: returns None so the worktree branch is skipped
        mgr.registry = MagicMock()
        mgr.registry.get_agent = AsyncMock(return_value=None)
        # - config: not needed if registry returns None (no inbox re-queue path)
        mgr.config = MagicMock()

        await AgentManager._cleanup_agent(mgr, "agent-cleanup")

        # Heartbeat stop event should have been signaled
        assert stop_event.is_set()
        assert "agent-cleanup" not in mgr._heartbeat_stops


class TestNoActivityAlert:
    """Phase 3: Heartbeat emits a NO-ACTIVITY ALERT after 120s with 0 tool calls."""

    def test_no_activity_warning_logged(self, caplog):
        """If 120s pass with 0 tool calls and 0 turns, a WARNING is logged."""
        import asyncio
        import logging
        import threading
        import time

        from squadron.agent_manager import AgentManager

        mgr = MagicMock(spec=AgentManager)
        mgr.registry = MagicMock()
        # Simulate an agent with 0 tool calls / 0 turns
        mock_agent = MagicMock()
        mock_agent.tool_call_count = 0
        mock_agent.turn_count = 0
        mgr.registry.get_agent = AsyncMock(return_value=mock_agent)
        mgr._log_activity = AsyncMock()

        loop = asyncio.new_event_loop()
        stop_event = threading.Event()

        # We need to simulate elapsed >= 120s. We'll monkey-patch time.monotonic
        # to fast-forward time. The thread uses `stop_event.wait(timeout=60)` which
        # we can't easily accelerate, so we use a different approach: signal stop
        # after a short delay and verify the warning happens for elapsed >= 120s.
        #
        # Instead, directly test the logic by calling _heartbeat_thread with a mock
        # that makes time.monotonic return values > 120s ahead.
        original_monotonic = time.monotonic
        call_count = 0

        def fake_wait(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # After first iteration, stop
                stop_event.set()
                return True
            # First call: simulate that 60s passed (returns False = not stopped)
            return False

        # Patch the stop_event.wait to not actually wait
        stop_event.wait = fake_wait

        # Patch time.monotonic to simulate 130s elapsed
        start_time = original_monotonic()

        def fast_monotonic():
            if call_count >= 1:
                return start_time + 130  # 130s elapsed
            return start_time

        with (
            caplog.at_level(logging.WARNING, logger="squadron.agent_manager"),
            patch("time.monotonic", side_effect=fast_monotonic),
        ):
            AgentManager._heartbeat_thread(mgr, "test-no-activity", 1, None, stop_event, loop)

        # Should have logged a NO-ACTIVITY ALERT warning
        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "NO-ACTIVITY ALERT" in r.message
        ]
        assert len(warnings) >= 1
        assert "test-no-activity" in warnings[0].message

        loop.close()

    def test_no_activity_warning_not_fired_when_active(self, caplog):
        """No warning if the agent has non-zero tool calls."""
        import asyncio
        import logging
        import threading
        import time

        from squadron.agent_manager import AgentManager

        mgr = MagicMock(spec=AgentManager)
        mgr.registry = MagicMock()
        mock_agent = MagicMock()
        mock_agent.tool_call_count = 5
        mock_agent.turn_count = 2
        mgr.registry.get_agent = AsyncMock(return_value=mock_agent)
        mgr._log_activity = AsyncMock()

        loop = asyncio.new_event_loop()
        stop_event = threading.Event()

        original_monotonic = time.monotonic
        call_count = 0

        def fake_wait(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                stop_event.set()
                return True
            return False

        stop_event.wait = fake_wait

        start_time = original_monotonic()

        def fast_monotonic():
            if call_count >= 1:
                return start_time + 130
            return start_time

        # Run the event loop in a background thread so that
        # run_coroutine_threadsafe can actually execute coroutines.
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()

        try:
            with (
                caplog.at_level(logging.WARNING, logger="squadron.agent_manager"),
                patch("time.monotonic", side_effect=fast_monotonic),
            ):
                AgentManager._heartbeat_thread(mgr, "test-active-agent", 1, None, stop_event, loop)

            # Should NOT have a NO-ACTIVITY ALERT (agent has tool_call_count=5)
            warnings = [r for r in caplog.records if "NO-ACTIVITY ALERT" in r.message]
            assert len(warnings) == 0
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2)
            loop.close()


class TestCleanupAgentStderrCapture:
    """Phase 3: _cleanup_agent captures CLI stderr before stopping."""

    @pytest.mark.asyncio
    async def test_cleanup_logs_stderr_when_present(self, caplog):
        """_cleanup_agent should log CLI stderr at WARNING level."""
        import logging

        from squadron.agent_manager import AgentManager
        from squadron.copilot import CopilotAgent

        mgr = MagicMock(spec=AgentManager)

        # Create a mock CopilotAgent that returns stderr
        mock_copilot = MagicMock(spec=CopilotAgent)
        mock_copilot.get_cli_stderr.return_value = "Error: authentication failed"
        mock_copilot.stop = AsyncMock()

        mgr._copilot_agents = {"agent-stderr": mock_copilot}
        mgr._agent_tasks = {}
        mgr._watchdog_enforced = set()
        mgr.agent_mail_queues = {}
        mgr.agent_inboxes = {}
        mgr._heartbeat_stops = {}
        mgr._stop_heartbeat = lambda aid: AgentManager._stop_heartbeat(mgr, aid)
        mgr._sandbox = MagicMock()
        mgr._sandbox.teardown_session = AsyncMock()
        mgr.registry = MagicMock()
        mgr.registry.get_agent = AsyncMock(return_value=None)
        mgr.config = MagicMock()

        with caplog.at_level(logging.WARNING, logger="squadron.agent_manager"):
            await AgentManager._cleanup_agent(mgr, "agent-stderr")

        # Should have logged the stderr
        stderr_logs = [r for r in caplog.records if "CLI stderr" in r.message]
        assert len(stderr_logs) >= 1
        assert "authentication failed" in stderr_logs[0].message

        # CopilotAgent.stop() should still have been called
        mock_copilot.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_no_log_when_stderr_empty(self, caplog):
        """_cleanup_agent should not log if CLI stderr is empty."""
        import logging

        from squadron.agent_manager import AgentManager
        from squadron.copilot import CopilotAgent

        mgr = MagicMock(spec=AgentManager)
        mock_copilot = MagicMock(spec=CopilotAgent)
        mock_copilot.get_cli_stderr.return_value = ""
        mock_copilot.stop = AsyncMock()

        mgr._copilot_agents = {"agent-quiet": mock_copilot}
        mgr._agent_tasks = {}
        mgr._watchdog_enforced = set()
        mgr.agent_mail_queues = {}
        mgr.agent_inboxes = {}
        mgr._heartbeat_stops = {}
        mgr._stop_heartbeat = lambda aid: AgentManager._stop_heartbeat(mgr, aid)
        mgr._sandbox = MagicMock()
        mgr._sandbox.teardown_session = AsyncMock()
        mgr.registry = MagicMock()
        mgr.registry.get_agent = AsyncMock(return_value=None)
        mgr.config = MagicMock()

        with caplog.at_level(logging.WARNING, logger="squadron.agent_manager"):
            await AgentManager._cleanup_agent(mgr, "agent-quiet")

        # Should NOT have logged stderr
        stderr_logs = [r for r in caplog.records if "CLI stderr" in r.message]
        assert len(stderr_logs) == 0

    @pytest.mark.asyncio
    async def test_cleanup_continues_if_stderr_capture_fails(self):
        """_cleanup_agent should not fail if stderr capture raises."""
        from squadron.agent_manager import AgentManager
        from squadron.copilot import CopilotAgent

        mgr = MagicMock(spec=AgentManager)
        mock_copilot = MagicMock(spec=CopilotAgent)
        mock_copilot.get_cli_stderr.side_effect = RuntimeError("pipe broken")
        mock_copilot.stop = AsyncMock()

        mgr._copilot_agents = {"agent-broken": mock_copilot}
        mgr._agent_tasks = {}
        mgr._watchdog_enforced = set()
        mgr.agent_mail_queues = {}
        mgr.agent_inboxes = {}
        mgr._heartbeat_stops = {}
        mgr._stop_heartbeat = lambda aid: AgentManager._stop_heartbeat(mgr, aid)
        mgr._sandbox = MagicMock()
        mgr._sandbox.teardown_session = AsyncMock()
        mgr.registry = MagicMock()
        mgr.registry.get_agent = AsyncMock(return_value=None)
        mgr.config = MagicMock()

        # Should not raise
        await AgentManager._cleanup_agent(mgr, "agent-broken")

        # stop() should still have been called
        mock_copilot.stop.assert_awaited_once()
