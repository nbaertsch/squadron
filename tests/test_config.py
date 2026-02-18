"""Tests for Squadron config loading."""

from pathlib import Path

import pytest
import yaml

from squadron.config import (
    _split_frontmatter,
    load_agent_definitions,
    load_config,
    parse_agent_definition,
)


@pytest.fixture
def squadron_dir(tmp_path: Path) -> Path:
    """Create a minimal .squadron/ directory for testing."""
    sq = tmp_path / ".squadron"
    sq.mkdir()

    config = {
        "project": {"name": "test-project", "default_branch": "main"},
        "labels": {
            "types": ["feature", "bug"],
            "priorities": ["high", "low"],
            "states": ["needs-triage"],
        },
        "human_groups": {"maintainers": ["@alice"]},
        "agent_roles": {
            "pm": {"agent_definition": "agents/pm.md", "singleton": True},
            "feat-dev": {
                "agent_definition": "agents/feat-dev.md",
                "triggers": [{"event": "issues.labeled", "label": "feature"}],
            },
        },
        "circuit_breakers": {
            "defaults": {"max_iterations": 5, "max_tool_calls": 200},
            "roles": {"pm": {"max_tool_calls": 50, "max_turns": 10}},
        },
        "runtime": {
            "default_model": "claude-sonnet-4.6",
            "provider": {"type": "anthropic", "api_key_env": "TEST_API_KEY"},
        },
        "escalation": {"default_notify": "maintainers", "max_issue_depth": 3},
    }
    (sq / "config.yaml").write_text(yaml.dump(config))

    agents_dir = sq / "agents"
    agents_dir.mkdir()
    (agents_dir / "pm.md").write_text(
        "---\nname: pm\ndisplay_name: Project Manager\ndescription: Manages stuff\n"
        "infer: true\ntools:\n  - create_issue\n  - assign_issue\n"
        "---\n\nYou are the PM.\n"
    )
    (agents_dir / "feat-dev.md").write_text(
        "---\nname: feat-dev\ndisplay_name: Feature Developer\n"
        "tools:\n  - read_file\n  - write_file\n"
        "mcp_servers:\n  github:\n    type: http\n    url: https://api.githubcopilot.com/mcp/\n"
        "---\n\nYou are a feature developer.\n"
    )

    return sq


class TestLoadConfig:
    def test_loads_valid_config(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        assert config.project.name == "test-project"
        assert config.project.default_branch == "main"

    def test_labels(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        assert "feature" in config.labels.types
        assert "bug" in config.labels.types

    def test_agent_roles(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        assert "pm" in config.agent_roles
        assert config.agent_roles["pm"].singleton is True
        assert "feat-dev" in config.agent_roles

    def test_circuit_breakers_defaults(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        assert config.circuit_breakers.defaults.max_iterations == 5
        assert config.circuit_breakers.defaults.max_tool_calls == 200

    def test_circuit_breakers_role_override(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        pm_limits = config.circuit_breakers.for_role("pm")
        assert pm_limits.max_tool_calls == 50  # Overridden
        assert pm_limits.max_iterations == 5  # From defaults
        assert pm_limits.max_turns == 10  # Overridden

    def test_circuit_breakers_unknown_role_gets_defaults(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        limits = config.circuit_breakers.for_role("nonexistent")
        assert limits.max_tool_calls == 200  # Pure defaults

    def test_runtime_config(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        assert config.runtime.default_model == "claude-sonnet-4.6"
        assert config.runtime.provider.type == "anthropic"

    def test_bot_username_default(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        assert config.project.bot_username == "squadron-dev[bot]"

    def test_escalation(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        assert config.escalation.default_notify == "maintainers"
        assert config.escalation.max_issue_depth == 3

    def test_missing_config_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent")


class TestLoadAgentDefinitions:
    def test_loads_all_definitions(self, squadron_dir: Path):
        defs = load_agent_definitions(squadron_dir)
        assert "pm" in defs
        assert "feat-dev" in defs
        assert len(defs) == 2

    def test_extracts_prompt_from_body(self, squadron_dir: Path):
        defs = load_agent_definitions(squadron_dir)
        assert "You are the PM." in defs["pm"].prompt
        assert "You are a feature developer." in defs["feat-dev"].prompt

    def test_extracts_frontmatter_fields(self, squadron_dir: Path):
        defs = load_agent_definitions(squadron_dir)
        pm = defs["pm"]
        assert pm.name == "pm"
        assert pm.display_name == "Project Manager"
        assert pm.description == "Manages stuff"
        assert pm.infer is True
        assert "create_issue" in pm.tools
        assert "assign_issue" in pm.tools

    def test_extracts_mcp_servers(self, squadron_dir: Path):
        defs = load_agent_definitions(squadron_dir)
        fd = defs["feat-dev"]
        assert "github" in fd.mcp_servers
        assert fd.mcp_servers["github"].type == "http"
        assert fd.mcp_servers["github"].url == "https://api.githubcopilot.com/mcp/"

    def test_raw_content_preserved(self, squadron_dir: Path):
        defs = load_agent_definitions(squadron_dir)
        assert "---" in defs["pm"].raw_content

    def test_empty_dir(self, tmp_path: Path):
        defs = load_agent_definitions(tmp_path / "nonexistent")
        assert defs == {}


class TestSplitFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\nname: test\ntools:\n  - foo\n---\n\nBody text."
        fm, body = _split_frontmatter(content)
        assert fm["name"] == "test"
        assert fm["tools"] == ["foo"]
        assert body == "Body text."

    def test_no_frontmatter(self):
        content = "Just plain markdown.\nNo frontmatter here."
        fm, body = _split_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_unclosed_frontmatter(self):
        content = "---\nname: test\nNo closing delimiter."
        fm, body = _split_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_empty_frontmatter(self):
        content = "---\n---\n\nBody only."
        fm, body = _split_frontmatter(content)
        assert fm == {}
        assert body == "Body only."


class TestParseAgentDefinition:
    def test_parses_frontmatter_and_body(self):
        content = "---\nname: test-agent\ndisplay_name: Test Agent\ndescription: A test\ninfer: false\ntools:\n  - tool1\n  - tool2\n---\n\nDo stuff.\nMore stuff."
        defn = parse_agent_definition("test", content)
        assert defn.role == "test"
        assert defn.name == "test-agent"
        assert defn.display_name == "Test Agent"
        assert defn.description == "A test"
        assert defn.infer is False
        assert defn.tools == ["tool1", "tool2"]
        assert "Do stuff." in defn.prompt
        assert "More stuff." in defn.prompt
        assert defn.raw_content == content

    def test_no_frontmatter_fallback(self):
        content = "Just some text.\nNo YAML here."
        defn = parse_agent_definition("test", content)
        assert defn.prompt == content  # No frontmatter — full content is the prompt
        assert defn.raw_content == content
        assert defn.name == "test"  # Defaults to role

    def test_ignores_unknown_frontmatter_fields(self):
        """Non-SDK fields in frontmatter are silently ignored."""
        content = "---\nname: pm\nsubagents:\n  - feat-dev\nconstraints:\n  max_time: 300\ntool_restrictions:\n  denied_commands:\n    - rm -rf\n---\n\nPrompt body."
        defn = parse_agent_definition("pm", content)
        assert defn.name == "pm"
        assert "Prompt body." in defn.prompt
        assert not hasattr(defn, "subagents")
        assert not hasattr(defn, "constraints")
        assert not hasattr(defn, "tool_restrictions")

    def test_mcp_servers_parsing(self):
        content = "---\nname: dev\nmcp_servers:\n  github:\n    type: http\n    url: https://example.com/mcp/\n    timeout: 60\n---\n\nBody."
        defn = parse_agent_definition("dev", content)
        assert "github" in defn.mcp_servers
        assert defn.mcp_servers["github"].type == "http"
        assert defn.mcp_servers["github"].url == "https://example.com/mcp/"
        assert defn.mcp_servers["github"].timeout == 60

    def test_to_custom_agent_config(self):
        content = "---\nname: reviewer\ndisplay_name: Code Reviewer\ndescription: Reviews code\ninfer: true\ntools:\n  - read_file\n  - grep\nmcp_servers:\n  github:\n    type: http\n    url: https://mcp.example.com\n---\n\nYou review code."
        defn = parse_agent_definition("reviewer", content)
        config = defn.to_custom_agent_config()
        assert config["name"] == "reviewer"
        assert config["display_name"] == "Code Reviewer"
        assert config["description"] == "Reviews code"
        assert config["infer"] is True
        assert config["tools"] == ["read_file", "grep"]
        assert "You review code." in config["prompt"]
        assert "github" in config["mcp_servers"]
        assert config["mcp_servers"]["github"]["type"] == "http"
