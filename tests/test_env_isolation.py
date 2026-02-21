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
