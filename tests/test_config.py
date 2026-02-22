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


class TestSkillDefinition:
    def test_basic_skill_definition(self):
        from squadron.config import SkillDefinition

        skill = SkillDefinition(path="squadron-internals", description="Framework architecture")
        assert skill.path == "squadron-internals"
        assert skill.description == "Framework architecture"

    def test_skill_definition_default_description(self):
        from squadron.config import SkillDefinition

        skill = SkillDefinition(path="some-skill")
        assert skill.path == "some-skill"
        assert skill.description == ""


class TestSkillsConfig:
    def test_default_base_path(self):
        from squadron.config import SkillsConfig

        sc = SkillsConfig()
        assert sc.base_path == ".squadron/skills"
        assert sc.definitions == {}

    def test_with_definitions(self):
        from squadron.config import SkillDefinition, SkillsConfig

        sc = SkillsConfig(
            base_path=".squadron/skills",
            definitions={
                "squadron-internals": SkillDefinition(
                    path="squadron-internals", description="Framework arch"
                ),
                "squadron-dev-guide": SkillDefinition(path="squadron-dev-guide"),
            },
        )
        assert "squadron-internals" in sc.definitions
        assert "squadron-dev-guide" in sc.definitions
        assert sc.definitions["squadron-internals"].path == "squadron-internals"

    def test_custom_base_path(self):
        from squadron.config import SkillsConfig

        # base_path must be a relative path — absolute paths are rejected
        sc = SkillsConfig(base_path="custom/skills/dir")
        assert sc.base_path == "custom/skills/dir"


class TestAgentDefinitionSkills:
    def test_skills_field_default(self):
        defn = parse_agent_definition("test", "---\nname: test\n---\nBody.")
        assert defn.skills == []

    def test_skills_parsed_from_frontmatter(self):
        content = (
            "---\n"
            "name: feat-dev\n"
            "skills: [squadron-internals, squadron-dev-guide]\n"
            "---\n\nYou are a feature developer.\n"
        )
        defn = parse_agent_definition("feat-dev", content)
        assert defn.skills == ["squadron-internals", "squadron-dev-guide"]

    def test_single_skill_parsed(self):
        content = "---\nname: pm\nskills:\n  - squadron-internals\n---\nYou are PM.\n"
        defn = parse_agent_definition("pm", content)
        assert defn.skills == ["squadron-internals"]

    def test_empty_skills_list(self):
        content = "---\nname: code-search\nskills: []\n---\nSearch code.\n"
        defn = parse_agent_definition("code-search", content)
        assert defn.skills == []

    def test_skills_null_frontmatter_defaults_to_empty(self):
        content = "---\nname: agent\nskills:\n---\nBody.\n"
        defn = parse_agent_definition("agent", content)
        assert defn.skills == []


class TestSquadronConfigSkills:
    def test_skills_field_defaults(self, squadron_dir: Path):
        config = load_config(squadron_dir)
        assert hasattr(config, "skills")
        assert config.skills.base_path == ".squadron/skills"
        assert config.skills.definitions == {}

    def test_skills_config_loaded_from_yaml(self, squadron_dir: Path):
        import yaml

        from squadron.config import load_config

        # Add skills section to config
        config_path = squadron_dir / "config.yaml"
        existing = yaml.safe_load(config_path.read_text())
        existing["skills"] = {
            "base_path": ".squadron/skills",
            "definitions": {
                "my-skill": {"path": "my-skill", "description": "A test skill"},
                "another-skill": {"path": "another-skill"},
            },
        }
        config_path.write_text(yaml.dump(existing))

        config = load_config(squadron_dir)
        assert "my-skill" in config.skills.definitions
        assert config.skills.definitions["my-skill"].path == "my-skill"
        assert config.skills.definitions["my-skill"].description == "A test skill"
        assert "another-skill" in config.skills.definitions
        assert config.skills.definitions["another-skill"].description == ""


class TestSkillPathTraversalValidation:
    """Regression tests for path traversal prevention in skill config models.

    These tests verify that SkillDefinition.path and SkillsConfig.base_path
    reject absolute paths and directory traversal components (../).
    """

    def test_skill_definition_rejects_absolute_path(self):
        from pydantic import ValidationError

        from squadron.config import SkillDefinition

        with pytest.raises(ValidationError, match="absolute"):
            SkillDefinition(path="/etc/ssh")

    def test_skill_definition_rejects_dotdot_path(self):
        from pydantic import ValidationError

        from squadron.config import SkillDefinition

        with pytest.raises(ValidationError, match="traversal"):
            SkillDefinition(path="../outside-repo")

    def test_skill_definition_rejects_nested_dotdot(self):
        from pydantic import ValidationError

        from squadron.config import SkillDefinition

        with pytest.raises(ValidationError, match="traversal"):
            SkillDefinition(path="skills/../../etc/passwd")

    def test_skill_definition_accepts_relative_path(self):
        from squadron.config import SkillDefinition

        skill = SkillDefinition(path="squadron-internals")
        assert skill.path == "squadron-internals"

    def test_skill_definition_accepts_nested_relative_path(self):
        from squadron.config import SkillDefinition

        skill = SkillDefinition(path="subdir/my-skill")
        assert skill.path == "subdir/my-skill"

    def test_skills_config_rejects_absolute_base_path(self):
        from pydantic import ValidationError

        from squadron.config import SkillsConfig

        with pytest.raises(ValidationError, match="absolute"):
            SkillsConfig(base_path="/custom/skills/dir")

    def test_skills_config_rejects_dotdot_base_path(self):
        from pydantic import ValidationError

        from squadron.config import SkillsConfig

        with pytest.raises(ValidationError, match="traversal"):
            SkillsConfig(base_path="../outside")

    def test_skills_config_accepts_relative_base_path(self):
        from squadron.config import SkillsConfig

        sc = SkillsConfig(base_path="custom/skills")
        assert sc.base_path == "custom/skills"

    def test_skills_config_accepts_default_base_path(self):
        from squadron.config import SkillsConfig

        sc = SkillsConfig()
        assert sc.base_path == ".squadron/skills"


# ── CommandPermissions & CommandDefinition ─────────────────────────────────────


class TestCommandPermissions:
    def test_default_require_human_false(self):
        from squadron.config import CommandPermissions

        perms = CommandPermissions()
        assert perms.require_human is False

    def test_require_human_true(self):
        from squadron.config import CommandPermissions

        perms = CommandPermissions(require_human=True)
        assert perms.require_human is True


class TestCommandDefinition:
    def test_new_style_action(self):
        from squadron.config import CommandDefinition

        cmd = CommandDefinition(type="action", description="Show status")
        assert cmd.type == "action"
        assert cmd.description == "Show status"

    def test_new_style_static(self):
        from squadron.config import CommandDefinition

        cmd = CommandDefinition(type="static", response="Hello!")
        assert cmd.type == "static"
        assert cmd.response == "Hello!"

    def test_legacy_invoke_agent_false_migrates_to_static(self):
        from squadron.config import CommandDefinition

        cmd = CommandDefinition(invoke_agent=False, response="Hello!")
        assert cmd.type == "static"

    def test_legacy_delegate_to_migrates_to_agent(self):
        from squadron.config import CommandDefinition

        cmd = CommandDefinition(invoke_agent=True, delegate_to="pm")
        assert cmd.type == "agent"
        assert cmd.delegate_to == "pm"

    def test_permissions_in_definition(self):
        from squadron.config import CommandDefinition, CommandPermissions

        cmd = CommandDefinition(
            type="action",
            permissions=CommandPermissions(require_human=True),
        )
        assert cmd.permissions.require_human is True

    def test_args_field(self):
        from squadron.config import CommandDefinition

        cmd = CommandDefinition(type="action", args=["role"])
        assert cmd.args == ["role"]

    def test_enabled_field_default(self):
        from squadron.config import CommandDefinition

        cmd = CommandDefinition(type="action")
        assert cmd.enabled is True


class TestSquadronConfigCommandPrefix:
    def test_default_command_prefix(self):
        from squadron.config import SquadronConfig

        config = SquadronConfig(project={"name": "test", "owner": "x", "repo": "y"})
        assert config.command_prefix == "@squadron-dev"

    def test_custom_command_prefix(self):
        from squadron.config import SquadronConfig

        config = SquadronConfig(
            project={"name": "test", "owner": "x", "repo": "y"},
            command_prefix="@my-custom-bot",
        )
        assert config.command_prefix == "@my-custom-bot"

    def test_command_prefix_loaded_from_yaml(self, squadron_dir):
        import yaml

        config_file = squadron_dir / "config.yaml"
        data = yaml.safe_load(config_file.read_text())
        data["command_prefix"] = "@custom-bot"
        config_file.write_text(yaml.dump(data))

        config = load_config(squadron_dir)
        assert config.command_prefix == "@custom-bot"

    def test_commands_loaded_from_yaml(self, squadron_dir):
        import yaml

        config_file = squadron_dir / "config.yaml"
        data = yaml.safe_load(config_file.read_text())
        data["commands"] = {
            "status": {"type": "action"},
            "cancel": {"type": "action", "permissions": {"require_human": True}},
        }
        config_file.write_text(yaml.dump(data))

        config = load_config(squadron_dir)
        assert "status" in config.commands
        assert "cancel" in config.commands
        assert config.commands["cancel"].permissions.require_human is True

    def test_commands_legacy_format_loaded(self, squadron_dir):
        import yaml

        config_file = squadron_dir / "config.yaml"
        data = yaml.safe_load(config_file.read_text())
        data["commands"] = {
            "help": {"enabled": True, "invoke_agent": False, "response": "help text"},
        }
        config_file.write_text(yaml.dump(data))

        config = load_config(squadron_dir)
        assert config.commands["help"].type == "static"
