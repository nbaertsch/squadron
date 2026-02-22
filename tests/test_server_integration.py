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

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from squadron.config import ProjectConfig, RuntimeConfig, SquadronConfig, load_config
from squadron.models import AgentRecord, AgentStatus
from squadron.registry import AgentRegistry
from squadron.server import SquadronServer


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
        with patch.dict(
            os.environ,
            {
                "GITHUB_APP_ID": "12345",
                "GITHUB_PRIVATE_KEY": "fake-key",
                "GITHUB_WEBHOOK_SECRET": "test-secret",
                "GITHUB_INSTALLATION_ID": "67890",
            },
        ):
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
        """ACTIVE agents from a previous crash should be marked FAILED on boot."""
        from squadron.server import SquadronServer

        # Pre-populate DB with a stale ACTIVE agent
        data_dir = squadron_dir / ".squadron-data"
        data_dir.mkdir()
        db_path = str(data_dir / "registry.db")

        reg = AgentRegistry(db_path)
        await reg.initialize()
        stale_agent = AgentRecord(
            agent_id="feat-dev-issue-99",
            role="feat-dev",
            issue_number=99,
            status=AgentStatus.ACTIVE,
        )
        await reg.create_agent(stale_agent)
        await reg.close()

        # Boot server
        server = SquadronServer(repo_root=squadron_dir)
        with patch.dict(
            os.environ,
            {
                "GITHUB_APP_ID": "12345",
                "GITHUB_PRIVATE_KEY": "fake-key",
                "GITHUB_INSTALLATION_ID": "67890",
            },
        ):
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
                    assert recovered.status == AgentStatus.FAILED

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

        with patch.dict(
            os.environ,
            {
                "GITHUB_APP_ID": "12345",
                "GITHUB_PRIVATE_KEY": "fake-key",
                "GITHUB_INSTALLATION_ID": "67890",
            },
        ):
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

        with patch.dict(
            os.environ,
            {
                "GITHUB_APP_ID": "12345",
                "GITHUB_PRIVATE_KEY": "fake-key",
                "GITHUB_INSTALLATION_ID": "67890",
            },
        ):
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
        assert "provider" not in config
        # reasoning_effort only included when explicitly configured per role
        assert "reasoning_effort" not in config

        # Infinite sessions config
        assert "infinite_sessions" in config
        assert config["infinite_sessions"]["enabled"] is True

    def test_resume_config_matches_sdk_type(self):
        """Verify build_resume_config output has all required SDK fields."""

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
        assert "provider" not in config

    def test_provider_config_shape(self, monkeypatch):
        """Verify provider dict matches SDK ProviderConfig type when BYOK key set."""

        from squadron.copilot import _build_provider_dict
        from squadron.config import ProviderConfig

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-provider-shape")
        runtime_config = RuntimeConfig(
            provider=ProviderConfig(
                type="anthropic",
                base_url="https://api.anthropic.com",
                api_key_env="ANTHROPIC_API_KEY",
            )
        )
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

    def test_squadron_pm_tools_are_define_tool_decorated(self):
        """Verify Squadron's SquadronTools.get_tools() returns callable define_tool-decorated tools."""
        from squadron.tools.squadron_tools import SquadronTools

        registry_mock = AsyncMock()
        github_mock = AsyncMock()
        tools = SquadronTools(
            registry=registry_mock,
            github=github_mock,
            agent_inboxes={},
            owner="testowner",
            repo="testrepo",
        )
        # Explicitly request tools (no defaults)
        requested_tools = [
            "create_issue",
            "assign_issue",
            "label_issue",
            "comment_on_issue",
            "check_registry",
            "read_issue",
            "escalate_to_human",
            "report_complete",
        ]
        sdk_tools = tools.get_tools("pm-agent", requested_tools)
        assert len(sdk_tools) == 8
        for tool in sdk_tools:
            assert hasattr(tool, "name"), f"Tool missing 'name': {tool}"
            assert hasattr(tool, "handler"), f"Tool missing 'handler': {tool}"
            assert callable(tool.handler), f"Tool handler not callable: {tool.name}"


# ── Config Loading Integration ───────────────────────────────────────────────


class TestConfigLoading:
    """Test config loading with a synthetic .squadron/ directory."""

    def test_loads_squadron_config(self, tmp_path):
        """Load a .squadron/config.yaml and verify parsing."""
        squadron_dir = tmp_path / ".squadron"
        squadron_dir.mkdir()
        (squadron_dir / "config.yaml").write_text(
            "project:\n"
            "  name: test-project\n"
            "  owner: test-owner\n"
            "  repo: test-repo\n"
            "labels:\n"
            "  types:\n"
            "    - bug\n"
            "    - feature\n"
            "  priorities:\n"
            "    - P0\n"
            "    - P1\n"
        )

        config = load_config(squadron_dir)
        assert config.project.name == "test-project"
        assert config.project.owner == "test-owner"
        assert config.project.repo == "test-repo"
        assert len(config.labels.types) == 2
        assert len(config.labels.priorities) == 2

    def test_loads_agent_definitions(self, tmp_path):
        """Load agent .md files from a synthetic agents/ directory."""
        from squadron.config import load_agent_definitions

        squadron_dir = tmp_path / ".squadron"
        agents_dir = squadron_dir / "agents"
        agents_dir.mkdir(parents=True)

        (agents_dir / "pm.md").write_text(
            "---\n"
            "name: PM Agent\n"
            "tools:\n"
            "  - create_issue\n"
            "  - label_issue\n"
            "---\n"
            "You are the PM agent.\n"
        )
        (agents_dir / "feat-dev.md").write_text(
            "---\n"
            "name: Feature Developer\n"
            "tools:\n"
            "  - read_file\n"
            "  - write_file\n"
            "---\n"
            "You are a feature developer agent.\n"
        )

        defs = load_agent_definitions(squadron_dir)
        assert "pm" in defs
        assert "feat-dev" in defs
        assert defs["pm"].prompt  # Not empty
        assert defs["pm"].tools  # Should be populated from frontmatter
        assert defs["feat-dev"].tools  # Should be populated


# ── Config Hot-Reload Tests ──────────────────────────────────────────────────


class TestConfigHotReload:
    """Test _handle_config_reload on the server."""

    async def test_reload_skips_non_default_branch(self):
        """Push to non-default branch should not trigger reload."""
        from squadron.models import SquadronEvent, SquadronEventType

        server = SquadronServer.__new__(SquadronServer)
        server.config = SquadronConfig(project=ProjectConfig(name="test", default_branch="main"))

        event = SquadronEvent(
            event_type=SquadronEventType.PUSH,
            data={
                "payload": {
                    "ref": "refs/heads/feature-branch",
                    "commits": [{"modified": [".squadron/config.yaml"]}],
                }
            },
        )

        # Should return early (no git pull attempted)
        await server._handle_config_reload(event)

    async def test_reload_skips_non_squadron_changes(self):
        """Push that doesn't touch .squadron/ should not reload."""
        from squadron.models import SquadronEvent, SquadronEventType

        server = SquadronServer.__new__(SquadronServer)
        server.config = SquadronConfig(project=ProjectConfig(name="test", default_branch="main"))

        event = SquadronEvent(
            event_type=SquadronEventType.PUSH,
            data={
                "payload": {
                    "ref": "refs/heads/main",
                    "commits": [
                        {
                            "added": ["src/app.py"],
                            "modified": ["README.md"],
                            "removed": [],
                        }
                    ],
                }
            },
        )

        # Should return early — no .squadron files changed
        await server._handle_config_reload(event)

    async def test_reload_detects_squadron_config_change(self, tmp_path):
        """Push modifying .squadron/config.yaml triggers reload."""
        from squadron.models import SquadronEvent, SquadronEventType

        # Set up a minimal config file
        sq_dir = tmp_path / ".squadron"
        agents_dir = sq_dir / "agents"
        agents_dir.mkdir(parents=True)
        (sq_dir / "config.yaml").write_text(
            "project:\n  name: updated-project\n  owner: o\n  repo: r\n"
        )
        (agents_dir / "pm.md").write_text("# PM\nYou are PM.\n")

        server = SquadronServer.__new__(SquadronServer)
        server.repo_root = tmp_path
        server.squadron_dir = sq_dir
        server.config = SquadronConfig(
            project=ProjectConfig(name="original", default_branch="main")
        )
        server._config_version = None
        server.agent_manager = MagicMock()
        server.agent_manager._register_pipeline_handlers = MagicMock()
        server.reconciliation = MagicMock()
        server.router = MagicMock()
        server.pipeline_engine = None

        event = SquadronEvent(
            event_type=SquadronEventType.PUSH,
            data={
                "payload": {
                    "ref": "refs/heads/main",
                    "after": "abc123def456",
                    "commits": [
                        {
                            "added": [],
                            "modified": [".squadron/config.yaml"],
                            "removed": [],
                        }
                    ],
                }
            },
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            await server._handle_config_reload(event)

        # Config should be updated
        assert server.config.project.name == "updated-project"
        assert server._config_version == "abc123def456"
        server.agent_manager._register_pipeline_handlers.assert_called_once()
