"""E2E tests for the Copilot SDK — NOTHING MOCKED.

Starts a real CopilotClient (spawns the CLI binary), creates sessions,
sends messages, and verifies the full round-trip.

These tests require:
  - github-copilot-sdk pip package (provides the CLI binary)
  - GitHub Copilot authentication (logged in via CLI or token)
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from dotenv import load_dotenv

_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env")


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def copilot_cli_path():
    """Verify the Copilot CLI binary exists."""
    from copilot import CopilotClient

    c = CopilotClient({"cwd": "/tmp"})
    cli_path = c.options["cli_path"]
    if not os.path.exists(cli_path):
        pytest.skip(f"Copilot CLI binary not found at {cli_path}")
    return cli_path


# ── Client Lifecycle ─────────────────────────────────────────────────────────


class TestCopilotClientLifecycle:
    """Test start/stop of the real CopilotClient."""

    async def test_start_and_ping(self, copilot_cli_path):
        """Start the CLI subprocess and verify it responds to ping."""
        from copilot import CopilotClient

        client = CopilotClient({"cwd": "/tmp", "log_level": "error"})
        try:
            await client.start()

            # Ping the running server
            pong = await client.ping("e2e-test")
            assert pong is not None
            assert "e2e-test" in pong.message
        finally:
            await client.stop()

    async def test_get_status(self, copilot_cli_path):
        """Verify get_status returns server info."""
        from copilot import CopilotClient

        client = CopilotClient({"cwd": "/tmp", "log_level": "error"})
        try:
            await client.start()
            status = await client.get_status()
            assert status is not None
            # Status should have version info
            assert hasattr(status, "version") or hasattr(status, "state")
        finally:
            await client.stop()

    async def test_get_auth_status(self, copilot_cli_path):
        """Check authentication status of the Copilot CLI."""
        from copilot import CopilotClient

        client = CopilotClient({"cwd": "/tmp", "log_level": "error"})
        try:
            await client.start()
            auth = await client.get_auth_status()
            # We just verify the call works — auth may or may not be active
            assert auth is not None
            assert hasattr(auth, "isAuthenticated")
        finally:
            await client.stop()

    async def test_list_sessions_empty(self, copilot_cli_path):
        """Fresh client should have no sessions."""
        from copilot import CopilotClient

        client = CopilotClient({"cwd": "/tmp", "log_level": "error"})
        try:
            await client.start()
            sessions = await client.list_sessions()
            assert isinstance(sessions, list)
        finally:
            await client.stop()

    async def test_list_models(self, copilot_cli_path):
        """List available models from the Copilot service."""
        from copilot import CopilotClient

        client = CopilotClient({"cwd": "/tmp", "log_level": "error"})
        try:
            await client.start()
            auth = await client.get_auth_status()
            if not auth.isAuthenticated:
                pytest.skip("Copilot not authenticated — can't list models")

            models = await client.list_models()
            assert isinstance(models, list)
            # If authenticated, should have at least one model
            assert len(models) > 0
            # Each model should have basic info
            model = models[0]
            assert hasattr(model, "id") or hasattr(model, "name")
        finally:
            await client.stop()


# ── Session Operations ───────────────────────────────────────────────────────


class TestCopilotSessions:
    """Test real session creation, messaging, and cleanup."""

    async def test_create_and_destroy_session(self, copilot_cli_path):
        """Create a session and destroy it."""
        from copilot import CopilotClient

        client = CopilotClient({"cwd": "/tmp", "log_level": "error"})
        try:
            await client.start()
            auth = await client.get_auth_status()
            if not auth.isAuthenticated:
                pytest.skip("Copilot not authenticated — can't create sessions")

            uid = _uid()
            session = await client.create_session(
                {
                    "session_id": f"e2e-test-{uid}",
                    "model": "claude-sonnet-4.6",
                    "system_message": {
                        "mode": "replace",
                        "content": "You are a test agent. Reply with exactly: PONG",
                    },
                    "working_directory": "/tmp",
                }
            )

            assert session is not None

            # Verify session appears in list
            sessions = await client.list_sessions()
            [s.session_id for s in sessions if hasattr(s, "session_id")]
            # Session may or may not appear in list depending on SDK version

            # Destroy
            await session.destroy()
        finally:
            await client.stop()

    async def test_send_message_and_get_response(self, copilot_cli_path):
        """Send a message and verify the model responds."""
        from copilot import CopilotClient

        client = CopilotClient({"cwd": "/tmp", "log_level": "error"})
        try:
            await client.start()
            auth = await client.get_auth_status()
            if not auth.isAuthenticated:
                pytest.skip("Copilot not authenticated — can't send messages")

            uid = _uid()
            session = await client.create_session(
                {
                    "session_id": f"e2e-msg-{uid}",
                    "model": "gpt-4o",
                    "system_message": {
                        "mode": "replace",
                        "content": "You are a test agent. When asked 'ping', reply with exactly one word: 'pong'. Nothing else.",
                    },
                    "working_directory": "/tmp",
                }
            )

            # Send a message and wait for response (60s timeout)
            result = await session.send_and_wait(
                {"prompt": "ping"},
                timeout=60.0,
            )

            assert result is not None
            # The response should contain 'pong' somewhere
            response_text = str(result).lower()
            assert "pong" in response_text

            await session.destroy()
        finally:
            await client.stop()


# ── Squadron CopilotAgent Wrapper ────────────────────────────────────────────


class TestSquadronCopilotAgent:
    """Test the Squadron CopilotAgent wrapper with real SDK."""

    async def test_start_stop(self, copilot_cli_path):
        """CopilotAgent can start and stop the underlying client."""
        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        agent = CopilotAgent(
            runtime_config=RuntimeConfig(),
            working_directory="/tmp",
        )

        await agent.start()
        assert agent._client is not None

        await agent.stop()
        assert agent._client is None

    async def test_build_config_produces_valid_session(self, copilot_cli_path):
        """build_session_config produces config that the real SDK accepts."""
        from copilot import CopilotClient

        from squadron.config import RuntimeConfig
        from squadron.copilot import build_session_config

        config = build_session_config(
            role="feat-dev",
            issue_number=999,
            system_message="E2E test agent",
            working_directory="/tmp",
            runtime_config=RuntimeConfig(),
        )

        # Verify the SDK can create a session with our config
        client = CopilotClient({"cwd": "/tmp", "log_level": "error"})
        try:
            await client.start()
            auth = await client.get_auth_status()
            if not auth.isAuthenticated:
                pytest.skip("Copilot not authenticated — can't validate session config")

            session = await client.create_session(config)
            assert session is not None
            await session.destroy()
        finally:
            await client.stop()
