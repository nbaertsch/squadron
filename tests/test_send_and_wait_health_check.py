"""Tests for _send_and_wait_with_health_check.

The SDK's send_and_wait() blocks on an asyncio.Event that only fires when
the CLI emits SESSION_IDLE.  If the CLI process crashes before emitting
that event, send_and_wait blocks until the circuit-breaker timeout (up to
1800s).  The health-check wrapper polls the CLI process and raises
immediately when it exits.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_manager():
    """Create a minimal AgentManager with mocked dependencies."""
    from squadron.agent_manager import AgentManager

    mgr = AgentManager.__new__(AgentManager)
    return mgr


def _make_copilot(*, process_poll_returns=None, stderr=""):
    """Create a mock CopilotAgent with controllable process behavior.

    Args:
        process_poll_returns: Value that process.poll() returns.
            None = process still running, int = exited with that code.
        stderr: String returned by get_cli_stderr().
    """
    copilot = MagicMock()
    copilot.get_cli_stderr.return_value = stderr

    # Build the nested access path:
    # copilot._client → CopilotClient
    # copilot._client._client → JsonRpcClient
    # copilot._client._client.process → subprocess.Popen
    process = MagicMock()
    process.poll.return_value = process_poll_returns
    process.pid = 12345  # Required for health check to recognize as real process

    rpc_client = MagicMock()
    rpc_client.process = process

    sdk_client = MagicMock()
    sdk_client._client = rpc_client

    copilot._client = sdk_client
    return copilot, process


def _make_session(*, result=None, hang_forever=False, delay=0.0):
    """Create a mock session with controllable send_and_wait behavior."""
    session = MagicMock()

    if hang_forever:

        async def _hang(*args, **kwargs):
            await asyncio.sleep(999999)

        session.send_and_wait = _hang
    elif delay > 0:

        async def _delayed(*args, **kwargs):
            await asyncio.sleep(delay)
            return result

        session.send_and_wait = _delayed
    else:

        async def _immediate(*args, **kwargs):
            return result

        session.send_and_wait = _immediate

    return session


# ── Tests: Happy Path ─────────────────────────────────────────────────────────


class TestHealthCheckHappyPath:
    """send_and_wait completes normally while process stays alive."""

    @pytest.mark.asyncio
    async def test_returns_result_when_process_alive(self):
        """When send_and_wait returns normally, we get the result."""
        mgr = _make_manager()
        copilot, process = _make_copilot(process_poll_returns=None)
        sentinel = object()
        session = _make_session(result=sentinel)

        result = await mgr._send_and_wait_with_health_check(
            session,
            copilot,
            "test prompt",
            timeout=10.0,
            agent_id="test-agent",
            poll_interval=0.05,
        )
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_returns_none_when_session_returns_none(self):
        """send_and_wait can return None (no assistant message)."""
        mgr = _make_manager()
        copilot, _ = _make_copilot(process_poll_returns=None)
        session = _make_session(result=None)

        result = await mgr._send_and_wait_with_health_check(
            session,
            copilot,
            "test prompt",
            timeout=10.0,
            agent_id="test-agent",
            poll_interval=0.05,
        )
        assert result is None


# ── Tests: Process Dies ───────────────────────────────────────────────────────


class TestHealthCheckProcessDeath:
    """CLI process exits while send_and_wait is blocking."""

    @pytest.mark.asyncio
    async def test_raises_on_process_exit(self):
        """When CLI exits, wrapper raises RuntimeError immediately."""
        mgr = _make_manager()
        copilot, process = _make_copilot(process_poll_returns=1, stderr="segfault in node")
        session = _make_session(hang_forever=True)

        with pytest.raises(RuntimeError, match="CLI process exited with code 1"):
            await mgr._send_and_wait_with_health_check(
                session,
                copilot,
                "test prompt",
                timeout=60.0,
                agent_id="test-agent",
                poll_interval=0.05,
            )

    @pytest.mark.asyncio
    async def test_includes_stderr_in_error(self):
        """Error message includes CLI stderr output."""
        mgr = _make_manager()
        copilot, _ = _make_copilot(process_poll_returns=137, stderr="Killed by OOM")
        session = _make_session(hang_forever=True)

        with pytest.raises(RuntimeError, match="Killed by OOM"):
            await mgr._send_and_wait_with_health_check(
                session,
                copilot,
                "test prompt",
                timeout=60.0,
                agent_id="test-agent",
                poll_interval=0.05,
            )

    @pytest.mark.asyncio
    async def test_exit_code_zero_still_raises(self):
        """Even exit code 0 is unexpected during send_and_wait."""
        mgr = _make_manager()
        copilot, _ = _make_copilot(process_poll_returns=0, stderr="")
        session = _make_session(hang_forever=True)

        with pytest.raises(RuntimeError, match="CLI process exited with code 0"):
            await mgr._send_and_wait_with_health_check(
                session,
                copilot,
                "test prompt",
                timeout=60.0,
                agent_id="test-agent",
                poll_interval=0.05,
            )

    @pytest.mark.asyncio
    async def test_process_dies_mid_execution(self):
        """Process starts alive then dies — detected on next poll."""
        mgr = _make_manager()
        copilot, process = _make_copilot(process_poll_returns=None)
        copilot.get_cli_stderr.return_value = "fatal error"
        session = _make_session(hang_forever=True)

        # Process starts alive, dies after 0.1s
        call_count = 0

        def _poll_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                return 1  # died
            return None  # still alive

        process.poll.side_effect = _poll_side_effect

        with pytest.raises(RuntimeError, match="CLI process exited"):
            await mgr._send_and_wait_with_health_check(
                session,
                copilot,
                "test prompt",
                timeout=60.0,
                agent_id="test-agent",
                poll_interval=0.05,
            )


# ── Tests: Timeout Passthrough ────────────────────────────────────────────────


class TestHealthCheckTimeoutPassthrough:
    """Circuit breaker timeout still works through the wrapper."""

    @pytest.mark.asyncio
    async def test_timeout_error_propagated(self):
        """asyncio.TimeoutError from send_and_wait passes through."""
        mgr = _make_manager()
        copilot, _ = _make_copilot(process_poll_returns=None)

        session = MagicMock()

        async def _timeout(*args, **kwargs):
            raise asyncio.TimeoutError("Timeout after 10s")

        session.send_and_wait = _timeout

        with pytest.raises(asyncio.TimeoutError):
            await mgr._send_and_wait_with_health_check(
                session,
                copilot,
                "test prompt",
                timeout=10.0,
                agent_id="test-agent",
                poll_interval=0.05,
            )


# ── Tests: Fallback when process handle unavailable ──────────────────────────


class TestHealthCheckFallback:
    """When we can't access the CLI process, fall back to plain send_and_wait."""

    @pytest.mark.asyncio
    async def test_fallback_when_no_client(self):
        """If copilot._client is missing, fall back gracefully."""
        mgr = _make_manager()
        copilot = MagicMock()
        copilot._client = None  # no SDK client
        sentinel = object()
        session = _make_session(result=sentinel)

        result = await mgr._send_and_wait_with_health_check(
            session,
            copilot,
            "test prompt",
            timeout=10.0,
            agent_id="test-agent",
            poll_interval=0.05,
        )
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_fallback_when_no_rpc_client(self):
        """If copilot._client._client is missing, fall back gracefully."""
        mgr = _make_manager()
        copilot = MagicMock()
        sdk_client = MagicMock(spec=[])  # no _client attribute
        copilot._client = sdk_client
        sentinel = object()
        session = _make_session(result=sentinel)

        result = await mgr._send_and_wait_with_health_check(
            session,
            copilot,
            "test prompt",
            timeout=10.0,
            agent_id="test-agent",
            poll_interval=0.05,
        )
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_fallback_when_no_process(self):
        """If rpc_client.process is missing, fall back gracefully."""
        mgr = _make_manager()
        copilot = MagicMock()
        rpc_client = MagicMock(spec=[])  # no process attribute
        sdk_client = MagicMock()
        sdk_client._client = rpc_client
        copilot._client = sdk_client
        sentinel = object()
        session = _make_session(result=sentinel)

        result = await mgr._send_and_wait_with_health_check(
            session,
            copilot,
            "test prompt",
            timeout=10.0,
            agent_id="test-agent",
            poll_interval=0.05,
        )
        assert result is sentinel


# ── Tests: Race Condition ─────────────────────────────────────────────────────


class TestHealthCheckRaceCondition:
    """Ensure correct behavior when send_and_wait and process exit race."""

    @pytest.mark.asyncio
    async def test_send_completes_just_before_poll_detects_exit(self):
        """If send_and_wait wins the race, we get the result (no error)."""
        mgr = _make_manager()
        copilot, process = _make_copilot(process_poll_returns=None)
        sentinel = object()
        # send_and_wait completes after a short delay
        session = _make_session(result=sentinel, delay=0.05)

        # Process exits after send_and_wait would have finished
        call_count = 0

        def _poll_delayed_exit():
            nonlocal call_count
            call_count += 1
            if call_count >= 100:  # way after send completes
                return 1
            return None

        process.poll.side_effect = _poll_delayed_exit

        result = await mgr._send_and_wait_with_health_check(
            session,
            copilot,
            "test prompt",
            timeout=60.0,
            agent_id="test-agent",
            poll_interval=0.01,
        )
        assert result is sentinel
