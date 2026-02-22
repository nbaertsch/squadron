"""Tests for agent subprocess environment isolation (issue #117).

Verifies that build_agent_env() strips secret env vars from agent CLI
subprocesses while preserving operational env vars (PATH, HOME, etc.).
"""

from __future__ import annotations

import os
from unittest.mock import patch

from squadron.copilot import _SECRET_ENV_VARS, build_agent_env


# -- build_agent_env --------------------------------------------------------


class TestBuildAgentEnv:
    """Tests for the build_agent_env() function."""

    def test_strips_all_known_secrets(self):
        """Every var in _SECRET_ENV_VARS must be absent from the result."""
        fake_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "GITHUB_APP_ID": "12345",
            "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----...",
            "GITHUB_WEBHOOK_SECRET": "whsec_xxx",
            "GITHUB_INSTALLATION_ID": "67890",
            "COPILOT_GITHUB_TOKEN": "ghu_xxxx",
            "GITHUB_TOKEN": "ghp_yyyy",
            "GH_TOKEN": "ghp_zzzz",
            "SQUADRON_DASHBOARD_API_KEY": "dash_key",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            result = build_agent_env()

        # Operational vars preserved
        assert result["PATH"] == "/usr/bin"
        assert result["HOME"] == "/home/test"

        # All secrets stripped
        for secret in _SECRET_ENV_VARS:
            assert secret not in result, f"{secret} should be stripped"

    def test_strips_extra_blocked_vars(self):
        """Extra blocked vars (e.g. BYOK api_key_env) are also stripped."""
        fake_env = {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-ant-xxx",
            "OPENAI_API_KEY": "sk-oai-yyy",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            result = build_agent_env(extra_blocked={"ANTHROPIC_API_KEY"})

        assert "ANTHROPIC_API_KEY" not in result
        # Non-blocked BYOK key is preserved (it wasn't in extra_blocked)
        assert result["OPENAI_API_KEY"] == "sk-oai-yyy"
        assert result["PATH"] == "/usr/bin"

    def test_preserves_all_non_secret_vars(self):
        """Non-secret env vars must pass through unchanged."""
        operational_vars = {
            "PATH": "/usr/local/bin:/usr/bin",
            "HOME": "/home/agent",
            "TMPDIR": "/tmp",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C",
            "SHELL": "/bin/bash",
            "USER": "agent",
            "TERM": "xterm-256color",
            "EDITOR": "vim",
            "GIT_AUTHOR_NAME": "Squadron Bot",
            "GIT_AUTHOR_EMAIL": "bot@example.com",
        }
        with patch.dict(os.environ, operational_vars, clear=True):
            result = build_agent_env()

        assert result == operational_vars

    def test_empty_extra_blocked_is_noop(self):
        """Passing empty set or None for extra_blocked is equivalent."""
        fake_env = {"PATH": "/usr/bin", "FOO": "bar"}
        with patch.dict(os.environ, fake_env, clear=True):
            result_none = build_agent_env(extra_blocked=None)
            result_empty = build_agent_env(extra_blocked=set())

        assert result_none == result_empty

    def test_returns_new_dict_not_reference(self):
        """Result must be a new dict, not a reference to os.environ."""
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            result = build_agent_env()

        # Mutating result must not affect os.environ
        result["INJECTED"] = "evil"
        assert "INJECTED" not in os.environ

    def test_secret_env_vars_is_frozen(self):
        """_SECRET_ENV_VARS must be a frozenset (immutable)."""
        assert isinstance(_SECRET_ENV_VARS, frozenset)

    def test_known_secret_list_is_complete(self):
        """Verify the expected secret var names are all present."""
        expected = {
            "GITHUB_APP_ID",
            "GITHUB_PRIVATE_KEY",
            "GITHUB_WEBHOOK_SECRET",
            "GITHUB_INSTALLATION_ID",
            "COPILOT_GITHUB_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "SQUADRON_DASHBOARD_API_KEY",
        }
        assert expected == _SECRET_ENV_VARS


# -- CopilotAgent env plumbing --------------------------------------------


class TestCopilotAgentEnv:
    """Tests that CopilotAgent stores and passes env to CopilotClient."""

    def test_env_stored_on_init(self):
        """CopilotAgent.__init__ stores env in _env attribute."""
        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        env = {"PATH": "/usr/bin", "HOME": "/home/test"}
        agent = CopilotAgent(
            runtime_config=RuntimeConfig(),
            working_directory="/tmp/test",
            env=env,
        )
        assert agent._env is env

    def test_env_default_is_none(self):
        """CopilotAgent defaults to env=None (backward compat)."""
        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        agent = CopilotAgent(
            runtime_config=RuntimeConfig(),
            working_directory="/tmp/test",
        )
        assert agent._env is None

    def test_github_token_resolved_from_environ(self):
        """CopilotAgent resolves COPILOT_GITHUB_TOKEN at construction time."""
        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "ghu_test_token"}, clear=False):
            agent = CopilotAgent(
                runtime_config=RuntimeConfig(),
                working_directory="/tmp/test",
            )
        assert agent._github_token == "ghu_test_token"

    def test_github_token_none_when_missing(self):
        """CopilotAgent._github_token is None when env var is absent."""
        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        env_without_token = {k: v for k, v in os.environ.items() if k != "COPILOT_GITHUB_TOKEN"}
        with patch.dict(os.environ, env_without_token, clear=True):
            agent = CopilotAgent(
                runtime_config=RuntimeConfig(),
                working_directory="/tmp/test",
            )
        assert agent._github_token is None

    def test_start_passes_github_token_to_client(self):
        """CopilotAgent.start() passes github_token in client_opts."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "ghu_pass_test"}, clear=False):
            agent = CopilotAgent(
                runtime_config=RuntimeConfig(),
                working_directory="/tmp/test",
                env={"PATH": "/usr/bin"},
            )

        # Mock CopilotClient to capture the options it receives
        captured_opts = {}

        def fake_client_init(opts=None):
            captured_opts.update(opts or {})
            mock = MagicMock()
            mock.start = AsyncMock()
            return mock

        with patch("squadron.copilot.CopilotClient", side_effect=fake_client_init):
            asyncio.run(agent.start())

        assert captured_opts.get("github_token") == "ghu_pass_test"
        assert captured_opts.get("cwd") == "/tmp/test"
        assert captured_opts.get("env") == {"PATH": "/usr/bin"}

    def test_start_warns_when_token_missing(self, caplog):
        """CopilotAgent.start() logs WARNING when no COPILOT_GITHUB_TOKEN."""
        import asyncio
        import logging
        from unittest.mock import AsyncMock, MagicMock

        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        env_without_token = {k: v for k, v in os.environ.items() if k != "COPILOT_GITHUB_TOKEN"}
        with patch.dict(os.environ, env_without_token, clear=True):
            agent = CopilotAgent(
                runtime_config=RuntimeConfig(),
                working_directory="/tmp/test",
                env={"PATH": "/usr/bin"},
            )

        def fake_client_init(opts=None):
            mock = MagicMock()
            mock.start = AsyncMock()
            return mock

        with (
            patch("squadron.copilot.CopilotClient", side_effect=fake_client_init),
            caplog.at_level(logging.WARNING, logger="squadron.copilot"),
        ):
            asyncio.run(agent.start())

        assert any("COPILOT_GITHUB_TOKEN" in r.message for r in caplog.records)


class TestCopilotAgentStderr:
    """Tests for CopilotAgent.get_cli_stderr() method."""

    def test_get_cli_stderr_returns_empty_when_no_client(self):
        """get_cli_stderr returns '' when client is not started."""
        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        agent = CopilotAgent(
            runtime_config=RuntimeConfig(),
            working_directory="/tmp/test",
        )
        assert agent.get_cli_stderr() == ""

    def test_get_cli_stderr_returns_captured_output(self):
        """get_cli_stderr returns stderr captured by the SDK's JsonRpcClient."""
        from unittest.mock import MagicMock

        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        agent = CopilotAgent(
            runtime_config=RuntimeConfig(),
            working_directory="/tmp/test",
        )
        # Simulate a started client with a _client (JsonRpcClient) that has stderr
        mock_rpc_client = MagicMock()
        mock_rpc_client.get_stderr_output.return_value = "Error: auth failed"
        mock_copilot_client = MagicMock()
        mock_copilot_client._client = mock_rpc_client
        agent._client = mock_copilot_client

        assert agent.get_cli_stderr() == "Error: auth failed"

    def test_get_cli_stderr_handles_exception(self):
        """get_cli_stderr returns '' if accessing stderr raises."""
        from unittest.mock import MagicMock

        from squadron.config import RuntimeConfig
        from squadron.copilot import CopilotAgent

        agent = CopilotAgent(
            runtime_config=RuntimeConfig(),
            working_directory="/tmp/test",
        )
        mock_rpc_client = MagicMock()
        mock_rpc_client.get_stderr_output.side_effect = RuntimeError("pipe broken")
        mock_copilot_client = MagicMock()
        mock_copilot_client._client = mock_rpc_client
        agent._client = mock_copilot_client

        assert agent.get_cli_stderr() == ""
