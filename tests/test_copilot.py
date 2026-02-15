"""Tests for the Copilot SDK integration layer.

Tests here cover config-building logic only — build_session_config()
and build_resume_config() return SDK-compatible TypedDicts.
CopilotAgent lifecycle tests require a running Copilot CLI
and are covered by integration tests.
"""

from squadron.config import ProviderConfig, RuntimeConfig, ModelOverride
from squadron.copilot import build_session_config, build_resume_config


class TestBuildSessionConfig:
    def test_build_for_dev_agent(self):
        runtime = RuntimeConfig(
            default_model="claude-sonnet-4",
            models={"feat-dev": ModelOverride(model="claude-sonnet-4", reasoning_effort="high")},
            provider=ProviderConfig(type="anthropic", api_key_env="ANTHROPIC_API_KEY"),
        )
        config = build_session_config(
            role="feat-dev",
            issue_number=42,
            system_message="You are a feature developer.",
            working_directory="/tmp/worktree",
            runtime_config=runtime,
        )
        assert config["session_id"] == "squadron-feat-dev-issue-42"
        assert config["model"] == "claude-sonnet-4"
        assert config["reasoning_effort"] == "high"
        assert config["system_message"] == {
            "mode": "replace",
            "content": "You are a feature developer.",
        }
        assert config["working_directory"] == "/tmp/worktree"
        # Provider omitted when no API key available (Copilot-native auth)
        assert "provider" not in config

    def test_build_for_pm_with_override(self):
        runtime = RuntimeConfig(
            default_model="claude-sonnet-4",
            models={"pm": ModelOverride(model="claude-sonnet-4", reasoning_effort="low")},
        )
        config = build_session_config(
            role="pm",
            issue_number=None,
            system_message="You are a PM.",
            working_directory="/repo",
            runtime_config=runtime,
            session_id_override="squadron-pm-batch-123",
        )
        assert config["session_id"] == "squadron-pm-batch-123"
        assert config["model"] == "claude-sonnet-4"
        assert config["reasoning_effort"] == "low"

    def test_build_uses_default_model(self):
        runtime = RuntimeConfig(default_model="gpt-5")
        config = build_session_config(
            role="bug-fix",
            issue_number=7,
            system_message="Fix bugs.",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert config["model"] == "gpt-5"
        # reasoning_effort not included unless explicitly configured per role
        assert "reasoning_effort" not in config

    def test_infinite_sessions_enabled(self):
        runtime = RuntimeConfig()
        config = build_session_config(
            role="feat-dev",
            issue_number=1,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert config["infinite_sessions"] is not None
        assert config["infinite_sessions"]["enabled"] is True
        assert config["infinite_sessions"]["background_compaction_threshold"] == 0.80

    def test_session_id_without_issue(self):
        runtime = RuntimeConfig()
        config = build_session_config(
            role="pm",
            issue_number=None,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert config["session_id"] == "squadron-pm"

    def test_session_id_with_issue(self):
        runtime = RuntimeConfig()
        config = build_session_config(
            role="feat-dev",
            issue_number=99,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert config["session_id"] == "squadron-feat-dev-issue-99"

    def test_session_id_override_takes_precedence(self):
        runtime = RuntimeConfig()
        config = build_session_config(
            role="pm",
            issue_number=42,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
            session_id_override="custom-id-123",
        )
        assert config["session_id"] == "custom-id-123"

    def test_tools_and_hooks_passed_through(self):
        runtime = RuntimeConfig()
        tools = [{"name": "create_issue", "description": "Create a GitHub issue"}]
        hooks = {"on_pre_tool_use": lambda e: None}
        config = build_session_config(
            role="pm",
            issue_number=None,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
            tools=tools,
            hooks=hooks,
        )
        assert config["tools"] == tools
        assert "on_pre_tool_use" in config["hooks"]

    def test_provider_config_from_runtime_with_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        runtime = RuntimeConfig(
            provider=ProviderConfig(
                type="openai",
                base_url="https://api.openai.com",
                api_key_env="OPENAI_API_KEY",
            ),
        )
        config = build_session_config(
            role="feat-dev",
            issue_number=1,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert config["provider"]["type"] == "openai"
        assert config["provider"]["base_url"] == "https://api.openai.com"
        assert config["provider"]["api_key"] == "sk-openai-test"

    def test_provider_omitted_without_api_key(self):
        runtime = RuntimeConfig(
            provider=ProviderConfig(
                type="openai",
                base_url="https://api.openai.com",
                api_key_env="NONEXISTENT_OPENAI_KEY_12345",
            ),
        )
        config = build_session_config(
            role="feat-dev",
            issue_number=1,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        # No API key → provider omitted → Copilot-native auth
        assert "provider" not in config

    def test_provider_omitted_for_copilot_type(self, monkeypatch):
        monkeypatch.setenv("COPILOT_KEY", "some-key")
        runtime = RuntimeConfig(
            provider=ProviderConfig(
                type="copilot",
                base_url="https://api.githubcopilot.com",
                api_key_env="COPILOT_KEY",
            ),
        )
        config = build_session_config(
            role="pm",
            issue_number=None,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        # "copilot" type → always omit provider
        assert "provider" not in config

    def test_system_message_is_replace_config(self):
        runtime = RuntimeConfig()
        config = build_session_config(
            role="pm",
            issue_number=None,
            system_message="You are a project manager.",
            working_directory="/repo",
            runtime_config=runtime,
        )
        sm = config["system_message"]
        assert sm["mode"] == "replace"
        assert sm["content"] == "You are a project manager."

    def test_provider_api_key_resolved_from_env(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test-123")
        runtime = RuntimeConfig(
            provider=ProviderConfig(
                type="anthropic",
                base_url="https://api.anthropic.com",
                api_key_env="TEST_KEY",
            ),
        )
        config = build_session_config(
            role="pm",
            issue_number=None,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert config["provider"]["api_key"] == "sk-test-123"

    def test_no_provider_when_env_not_set(self):
        runtime = RuntimeConfig(
            provider=ProviderConfig(
                type="anthropic",
                base_url="https://api.anthropic.com",
                api_key_env="NONEXISTENT_KEY_12345",
            ),
        )
        config = build_session_config(
            role="pm",
            issue_number=None,
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        # No API key → provider omitted entirely
        assert "provider" not in config


class TestBuildResumeConfig:
    def test_no_session_id_in_resume_config(self):
        runtime = RuntimeConfig(default_model="gpt-5")
        config = build_resume_config(
            role="feat-dev",
            system_message="Resume work.",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert "session_id" not in config
        assert config["model"] == "gpt-5"

    def test_resume_config_has_provider_with_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        runtime = RuntimeConfig(
            provider=ProviderConfig(
                type="anthropic",
                base_url="https://api.anthropic.com",
                api_key_env="ANTHROPIC_API_KEY",
            ),
        )
        config = build_resume_config(
            role="feat-dev",
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert config["provider"]["type"] == "anthropic"
        assert config["provider"]["api_key"] == "sk-ant-test"

    def test_resume_config_no_provider_without_key(self):
        runtime = RuntimeConfig(
            provider=ProviderConfig(
                type="anthropic",
                base_url="https://api.anthropic.com",
                api_key_env="NONEXISTENT_ANT_KEY",
            ),
        )
        config = build_resume_config(
            role="feat-dev",
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert "provider" not in config

    def test_resume_config_has_infinite_sessions(self):
        runtime = RuntimeConfig()
        config = build_resume_config(
            role="feat-dev",
            system_message="test",
            working_directory="/repo",
            runtime_config=runtime,
        )
        assert config["infinite_sessions"]["enabled"] is True
