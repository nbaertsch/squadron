"""Server boot and Copilot SDK integration tests.

These tests verify:
1. Server creates the full app and boots all components
2. Health/agents endpoints work through real TestClient
3. Stale agent recovery works during boot
4. Copilot SDK types match our config builders (no mock — real import)

The server tests mock ONLY the external services (GitHub API, Copilot CLI)
but run everything else for real: SQLite, FastAPI, EventRouter, config loading.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from squadron.config import ProjectConfig, RuntimeConfig, SquadronConfig, load_config
from squadron.models import AgentRecord, AgentRole, AgentStatus
from squadron.registry import AgentRegistry


# ── Server Boot Tests ────────────────────────────────────────────────────────


class TestServerBoot:
    """Test the SquadronServer startup/shutdown lifecycle."""

    @pytest.fixture
    def squadron_dir(self, tmp_path):
        """Create a minimal .squadron/ directory for testing."""
        sq_dir = tmp_path / ".squadron"
        agents_dir = sq_dir / "agents"
        agents_dir.mkdir(parents=True)

        (sq_dir / "config.yaml").write_text(
            "project:\n  name: test\n  owner: testowner\n  repo: testrepo\n"
        )
        (agents_dir / "pm.md").write_text("# PM\n## System Prompt\nYou are PM.\n")
        return tmp_path

    async def test_server_starts_and_stops(self, squadron_dir):
        from squadron.server import SquadronServer

        server = SquadronServer(repo_root=squadron_dir)

        # Mock external dependencies
        with patch.dict(os.environ, {
            "GITHUB_APP_ID": "12345",
            "GITHUB_PRIVATE_KEY": "fake-key",
            "GITHUB_WEBHOOK_SECRET": "test-secret",
            "GITHUB_INSTALLATION_ID": "67890",
        }):
            # Mock GitHub client (no real API calls)
            with patch("squadron.server.GitHubClient") as MockGH:
                mock_github = AsyncMock()
                mock_github.ensure_labels_exist = AsyncMock()
                mock_github.start = AsyncMock()
                mock_github.close = AsyncMock()
                MockGH.return_value = mock_github

                # Mock CopilotAgent (no real SDK subprocess)
                with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                    mock_copilot = AsyncMock()
                    mock_copilot.start = AsyncMock()
                    mock_copilot.stop = AsyncMock()
                    MockCA.return_value = mock_copilot

                    await server.start()

                    # Verify components were initialized
                    assert server.config is not None
                    assert server.config.project.name == "test"
                    assert server.registry is not None
                    assert server.event_queue is not None
                    assert server.router is not None
                    assert server.agent_manager is not None
                    assert server.reconciliation is not None

                    await server.stop()

                    # Verify cleanup
                    mock_github.close.assert_called_once()

    async def test_stale_agent_recovery(self, squadron_dir):
        """ACTIVE agents from a previous crash should be marked SLEEPING on boot."""
        from squadron.server import SquadronServer

        # Pre-populate DB with a stale ACTIVE agent
        data_dir = squadron_dir / ".squadron-data"
        data_dir.mkdir()
        db_path = str(data_dir / "registry.db")

        reg = AgentRegistry(db_path)
        await reg.initialize()
        stale_agent = AgentRecord(
            agent_id="feat-dev-issue-99",
            role=AgentRole.FEAT_DEV,
            issue_number=99,
            status=AgentStatus.ACTIVE,
        )
        await reg.create_agent(stale_agent)
        await reg.close()

        # Boot server
        server = SquadronServer(repo_root=squadron_dir)
        with patch.dict(os.environ, {
            "GITHUB_APP_ID": "12345",
            "GITHUB_PRIVATE_KEY": "fake-key",
            "GITHUB_INSTALLATION_ID": "67890",
        }):
            with patch("squadron.server.GitHubClient") as MockGH:
                mock_github = AsyncMock()
                mock_github.ensure_labels_exist = AsyncMock()
                mock_github.start = AsyncMock()
                mock_github.close = AsyncMock()
                MockGH.return_value = mock_github

                with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                    mock_copilot = AsyncMock()
                    mock_copilot.start = AsyncMock()
                    mock_copilot.stop = AsyncMock()
                    MockCA.return_value = mock_copilot

                    await server.start()

                    # Check that stale agent was recovered
                    recovered = await server.registry.get_agent("feat-dev-issue-99")
                    assert recovered.status == AgentStatus.SLEEPING

                    await server.stop()


class TestServerEndpoints:
    """Test HTTP endpoints through the real FastAPI app."""

    @pytest.fixture
    def squadron_dir(self, tmp_path):
        sq_dir = tmp_path / ".squadron"
        agents_dir = sq_dir / "agents"
        agents_dir.mkdir(parents=True)

        (sq_dir / "config.yaml").write_text(
            "project:\n  name: test\n  owner: testowner\n  repo: testrepo\n"
        )
        (agents_dir / "pm.md").write_text("# PM\n## System Prompt\nYou are PM.\n")
        return tmp_path

    def test_health_endpoint(self, squadron_dir):
        from squadron.server import create_app

        with patch.dict(os.environ, {
            "GITHUB_APP_ID": "12345",
            "GITHUB_PRIVATE_KEY": "fake-key",
            "GITHUB_INSTALLATION_ID": "67890",
        }):
            with patch("squadron.server.GitHubClient") as MockGH:
                mock_github = AsyncMock()
                mock_github.ensure_labels_exist = AsyncMock()
                mock_github.start = AsyncMock()
                mock_github.close = AsyncMock()
                MockGH.return_value = mock_github

                with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                    mock_copilot = AsyncMock()
                    mock_copilot.start = AsyncMock()
                    mock_copilot.stop = AsyncMock()
                    MockCA.return_value = mock_copilot

                    app = create_app(repo_root=squadron_dir)
                    with TestClient(app) as client:
                        resp = client.get("/health")
                        assert resp.status_code == 200
                        data = resp.json()
                        assert data["status"] == "ok"
                        assert data["project"] == "test"

    def test_agents_endpoint_empty(self, squadron_dir):
        from squadron.server import create_app

        with patch.dict(os.environ, {
            "GITHUB_APP_ID": "12345",
            "GITHUB_PRIVATE_KEY": "fake-key",
            "GITHUB_INSTALLATION_ID": "67890",
        }):
            with patch("squadron.server.GitHubClient") as MockGH:
                mock_github = AsyncMock()
                mock_github.ensure_labels_exist = AsyncMock()
                mock_github.start = AsyncMock()
                mock_github.close = AsyncMock()
                MockGH.return_value = mock_github

                with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                    mock_copilot = AsyncMock()
                    mock_copilot.start = AsyncMock()
                    mock_copilot.stop = AsyncMock()
                    MockCA.return_value = mock_copilot

                    app = create_app(repo_root=squadron_dir)
                    with TestClient(app) as client:
                        resp = client.get("/agents")
                        assert resp.status_code == 200
                        assert resp.json()["agents"] == []


# ── Copilot SDK Type Validation ──────────────────────────────────────────────


class TestCopilotSDKTypes:
    """Verify our config builders produce dicts compatible with actual SDK types.

    These tests import the REAL SDK types (not mocks) and validate that
    build_session_config / build_resume_config return valid TypedDicts.
    """

    def test_session_config_matches_sdk_type(self):
        """Verify build_session_config output has all required SDK fields."""
        from copilot.types import SessionConfig as SDKSessionConfig

        from squadron.copilot import build_session_config

        config = build_session_config(
            role="feat-dev",
            issue_number=42,
            system_message="You are a dev agent.",
            working_directory="/tmp/test",
            runtime_config=RuntimeConfig(),
        )

        # These are the fields the SDK actually requires
        assert "session_id" in config
        assert "model" in config
        assert "system_message" in config
        assert config["system_message"]["mode"] == "replace"
        assert "working_directory" in config
        # Provider omitted when no BYOK API key (Copilot-native auth)
        # assert "provider" in config
        # reasoning_effort only included when explicitly configured per role
        # assert "reasoning_effort" in config

        # Infinite sessions config
        assert "infinite_sessions" in config
        assert config["infinite_sessions"]["enabled"] is True

    def test_resume_config_matches_sdk_type(self):
        """Verify build_resume_config output has all required SDK fields."""
        from copilot.types import ResumeSessionConfig as SDKResumeConfig

        from squadron.copilot import build_resume_config

        config = build_resume_config(
            role="feat-dev",
            system_message="You are a dev agent.",
            working_directory="/tmp/test",
            runtime_config=RuntimeConfig(),
        )

        # Resume config should NOT have session_id (passed separately)
        assert "session_id" not in config
        assert "model" in config
        assert "system_message" in config
        # Provider omitted when no BYOK API key (Copilot-native auth)
        # assert "provider" in config

    def test_provider_config_shape(self, monkeypatch):
        """Verify provider dict matches SDK ProviderConfig type when BYOK key set."""
        from copilot.types import ProviderConfig as SDKProviderConfig

        from squadron.copilot import _build_provider_dict

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-provider-shape")
        runtime_config = RuntimeConfig()
        provider = _build_provider_dict(runtime_config)

        assert provider is not None
        assert "type" in provider
        assert "base_url" in provider
        assert provider["type"] == "anthropic"
        assert provider["api_key"] == "sk-test-provider-shape"

    def test_provider_none_without_key(self):
        """Verify provider is None when no API key available."""
        from squadron.copilot import _build_provider_dict

        runtime_config = RuntimeConfig()
        provider = _build_provider_dict(runtime_config)
        assert provider is None

    def test_define_tool_decorator_exists(self):
        """Verify we can import the define_tool decorator from copilot."""
        from copilot import define_tool

        assert callable(define_tool)


# ── Config Loading Integration ───────────────────────────────────────────────


class TestConfigLoading:
    """Test config loading with a real .squadron/ directory."""

    def test_loads_real_squadron_config(self):
        """Load the actual .squadron/config.yaml from the project root."""
        project_root = Path(__file__).parent.parent
        squadron_dir = project_root / ".squadron"

        if not squadron_dir.exists():
            pytest.skip("No .squadron/ directory in project root")

        config = load_config(squadron_dir)
        assert config.project.name == "squadron"
        assert config.project.owner == "noahbaertsch"
        assert config.project.repo == "squadron"
        assert len(config.labels.types) > 0
        assert len(config.labels.priorities) > 0

    def test_loads_real_agent_definitions(self):
        """Load actual agent .md files from the project root."""
        from squadron.config import load_agent_definitions

        project_root = Path(__file__).parent.parent
        squadron_dir = project_root / ".squadron"

        if not squadron_dir.exists():
            pytest.skip("No .squadron/ directory in project root")

        defs = load_agent_definitions(squadron_dir)
        assert "pm" in defs
        assert "feat-dev" in defs
        assert defs["pm"].prompt  # Not empty
        assert defs["pm"].tools  # Should now be populated
        assert defs["feat-dev"].tools  # Should now be populated
        assert defs["feat-dev"].constraints  # Should now be populated
