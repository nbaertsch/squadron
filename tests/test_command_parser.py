"""Tests for the CommandParser class and updated ParsedCommand model.

Covers:
- CommandParser construction and configuration
- parse() method: help, action, and agent commands
- Config-driven agent list (not hardcoded)
- configurable command_prefix
- Code span stripping (backtick-wrapped mentions ignored)
- Edge cases: empty input, None, multiline, case sensitivity
- Backward compatibility: parse_command() shim
- CommandDefinition migration validator
- CommandPermissions enforcement logic
- inject_message action (via ParsedCommand action_name)
- ParsedCommand.is_action property
"""

from __future__ import annotations

import pytest

from squadron.config import CommandDefinition, CommandPermissions, SquadronConfig
from squadron.models import (
    CommandParser,
    ParsedCommand,
    _DEFAULT_KNOWN_AGENTS,
    parse_command,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def default_parser() -> CommandParser:
    """A CommandParser with the standard known agents."""
    return CommandParser(known_agents={"pm", "feat-dev", "bug-fix", "pr-review"})


@pytest.fixture
def custom_prefix_parser() -> CommandParser:
    """A CommandParser with a custom command prefix."""
    return CommandParser(
        command_prefix="@my-bot",
        known_agents={"pm", "feat-dev"},
    )


@pytest.fixture
def parser_with_commands() -> CommandParser:
    """A CommandParser with configured action commands."""
    from squadron.config import CommandDefinition, CommandPermissions

    commands = {
        "status": CommandDefinition(type="action", description="Show status"),
        "cancel": CommandDefinition(
            type="action", args=["role"], permissions=CommandPermissions(require_human=True)
        ),
        "retry": CommandDefinition(
            type="action", args=["role"], permissions=CommandPermissions(require_human=True)
        ),
    }
    return CommandParser(
        known_agents={"pm", "feat-dev"},
        commands=commands,
    )


# ── CommandParser Construction ────────────────────────────────────────────────


class TestCommandParserConstruction:
    def test_default_prefix(self):
        parser = CommandParser()
        assert parser.command_prefix == "@squadron-dev"

    def test_custom_prefix(self):
        parser = CommandParser(command_prefix="@my-bot")
        assert parser.command_prefix == "@my-bot"

    def test_default_known_agents(self):
        parser = CommandParser()
        assert parser.known_agents == _DEFAULT_KNOWN_AGENTS

    def test_custom_known_agents(self):
        parser = CommandParser(known_agents={"pm", "feat-dev"})
        assert parser.known_agents == frozenset({"pm", "feat-dev"})

    def test_empty_known_agents_falls_back_to_defaults(self):
        """None known_agents should use defaults, not empty set."""
        parser = CommandParser(known_agents=None)
        assert "pm" in parser.known_agents

    def test_action_names_include_builtins(self):
        parser = CommandParser()
        for action in ("status", "cancel", "retry"):
            assert action in parser.action_names

    def test_action_names_include_config_actions(self):
        from squadron.config import CommandDefinition

        cmds = {"custom-action": CommandDefinition(type="action")}
        parser = CommandParser(commands=cmds)
        assert "custom-action" in parser.action_names

    def test_agent_commands_not_in_action_names(self):
        from squadron.config import CommandDefinition

        cmds = {"pm": CommandDefinition(type="agent")}
        parser = CommandParser(commands=cmds)
        assert "pm" not in parser.action_names


# ── Help Command ─────────────────────────────────────────────────────────────


class TestHelpCommand:
    def test_help_basic(self, default_parser):
        result = default_parser.parse("@squadron-dev help")
        assert result is not None
        assert result.is_help is True
        assert result.agent_name is None
        assert result.action_name is None

    def test_help_case_insensitive(self, default_parser):
        assert default_parser.parse("@squadron-dev HELP").is_help is True
        assert default_parser.parse("@SQUADRON-DEV help").is_help is True
        assert default_parser.parse("@Squadron-Dev Help").is_help is True

    def test_help_with_text_before(self, default_parser):
        result = default_parser.parse("Hey team, @squadron-dev help please")
        assert result is not None
        assert result.is_help is True

    def test_help_takes_priority_over_agent(self, default_parser):
        """'help' should be treated as help even if 'help' were a known agent."""
        result = default_parser.parse("@squadron-dev help")
        assert result.is_help is True
        assert result.agent_name is None

    def test_help_in_code_span_ignored(self, default_parser):
        result = default_parser.parse("`@squadron-dev help`")
        assert result is None

    def test_help_with_custom_prefix(self, custom_prefix_parser):
        result = custom_prefix_parser.parse("@my-bot help")
        assert result is not None
        assert result.is_help is True

    def test_wrong_prefix_not_matched(self, default_parser):
        """@other-bot should not match @squadron-dev parser."""
        result = default_parser.parse("@other-bot help")
        assert result is None


# ── Action Commands ──────────────────────────────────────────────────────────


class TestActionCommands:
    def test_status_no_args(self, default_parser):
        result = default_parser.parse("@squadron-dev status")
        assert result is not None
        assert result.is_action is True
        assert result.action_name == "status"
        assert result.action_args == []

    def test_cancel_with_role(self, default_parser):
        result = default_parser.parse("@squadron-dev cancel feat-dev")
        assert result is not None
        assert result.is_action is True
        assert result.action_name == "cancel"
        assert result.action_args == ["feat-dev"]

    def test_retry_with_role(self, default_parser):
        result = default_parser.parse("@squadron-dev retry pm")
        assert result is not None
        assert result.is_action is True
        assert result.action_name == "retry"
        assert result.action_args == ["pm"]

    def test_action_case_insensitive(self, default_parser):
        result = default_parser.parse("@squadron-dev STATUS")
        assert result is not None
        assert result.action_name == "status"

    def test_action_with_text_before(self, default_parser):
        result = default_parser.parse("Please @squadron-dev status now")
        assert result is not None
        assert result.action_name == "status"

    def test_action_not_matched_without_prefix(self, default_parser):
        result = default_parser.parse("status")
        assert result is None

    def test_action_in_code_span_ignored(self, default_parser):
        result = default_parser.parse("`@squadron-dev status`")
        assert result is None

    def test_action_in_fenced_block_ignored(self, default_parser):
        text = "```\n@squadron-dev cancel feat-dev\n```"
        result = default_parser.parse(text)
        assert result is None

    def test_cancel_no_args_returns_action(self, default_parser):
        """cancel without args still returns action_name='cancel' with empty args."""
        result = default_parser.parse("@squadron-dev cancel")
        assert result is not None
        assert result.action_name == "cancel"
        assert result.action_args == []

    def test_retry_multiple_args(self, default_parser):
        """Extra args are preserved in action_args."""
        result = default_parser.parse("@squadron-dev retry feat-dev extra")
        assert result is not None
        assert result.action_args == ["feat-dev", "extra"]

    def test_action_with_custom_prefix(self, custom_prefix_parser):
        result = custom_prefix_parser.parse("@my-bot status")
        assert result is not None
        assert result.action_name == "status"

    def test_status_not_matched_as_agent(self, default_parser):
        """'status' is an action, not an agent — should not return agent_name."""
        result = default_parser.parse("@squadron-dev status")
        assert result.agent_name is None
        assert result.action_name == "status"

    def test_config_defined_action(self, parser_with_commands):
        """Action commands defined in config should be recognised."""
        result = parser_with_commands.parse("@squadron-dev status")
        assert result is not None
        assert result.action_name == "status"


# ── Agent Commands ────────────────────────────────────────────────────────────


class TestAgentCommands:
    def test_agent_with_colon(self, default_parser):
        result = default_parser.parse("@squadron-dev pm: please triage this")
        assert result is not None
        assert result.agent_name == "pm"
        assert result.message == "please triage this"

    def test_agent_without_colon_known(self, default_parser):
        """Known agent without colon should still be parsed."""
        result = default_parser.parse("@squadron-dev pm please help")
        assert result is not None
        assert result.agent_name == "pm"

    def test_agent_without_colon_unknown(self, default_parser):
        """Unknown agent without colon should not be parsed."""
        result = default_parser.parse("@squadron-dev unknown-agent do something")
        assert result is None

    def test_agent_message_multiline(self, default_parser):
        result = default_parser.parse("@squadron-dev pm: first line\n\nsecond line")
        assert result is not None
        assert result.agent_name == "pm"
        assert "first line" in result.message

    def test_agent_case_insensitive_prefix(self, default_parser):
        result = default_parser.parse("@SQUADRON-DEV PM: do stuff")
        assert result is not None
        assert result.agent_name == "pm"

    def test_agent_config_driven(self):
        """known_agents from config, not hardcoded."""
        parser = CommandParser(known_agents={"my-custom-agent"})
        result = parser.parse("@squadron-dev my-custom-agent do work")
        assert result is not None
        assert result.agent_name == "my-custom-agent"

    def test_unknown_hardcoded_agent_not_in_config(self):
        """Agent not in config known_agents should not parse (no colon)."""
        parser = CommandParser(known_agents={"pm"})
        result = parser.parse("@squadron-dev feat-dev do something")
        # No colon, not in known_agents — should return None
        assert result is None

    def test_unknown_agent_with_colon_still_parsed(self):
        """Colon makes it a definite command regardless of known_agents."""
        parser = CommandParser(known_agents={"pm"})
        result = parser.parse("@squadron-dev unknown-agent: do something")
        assert result is not None
        assert result.agent_name == "unknown-agent"

    def test_agent_message_stripped(self, default_parser):
        result = default_parser.parse("@squadron-dev pm:   leading spaces  ")
        assert result is not None
        assert result.message == "leading spaces"

    def test_agent_empty_message(self, default_parser):
        result = default_parser.parse("@squadron-dev pm:")
        assert result is not None
        assert result.agent_name == "pm"
        assert result.message == ""

    def test_inline_text_before_command(self, default_parser):
        result = default_parser.parse("Hey @squadron-dev pm: can you look at this?")
        assert result is not None
        assert result.agent_name == "pm"
        assert result.message == "can you look at this?"

    def test_custom_prefix_agent(self, custom_prefix_parser):
        result = custom_prefix_parser.parse("@my-bot pm: triage this")
        assert result is not None
        assert result.agent_name == "pm"


# ── Code Span Stripping ───────────────────────────────────────────────────────


class TestCodeSpanStripping:
    def test_inline_code_span_ignored(self, default_parser):
        result = default_parser.parse("`@squadron-dev pm: triage`")
        assert result is None

    def test_fenced_block_ignored(self, default_parser):
        text = "```\n@squadron-dev pm: triage this\n```"
        result = default_parser.parse(text)
        assert result is None

    def test_tilde_fenced_block_ignored(self, default_parser):
        text = "~~~\n@squadron-dev pm: triage this\n~~~"
        result = default_parser.parse(text)
        assert result is None

    def test_outside_code_span_matched(self, default_parser):
        text = "Here is some code: `@squadron-dev` but @squadron-dev pm: triage this"
        result = default_parser.parse(text)
        assert result is not None
        assert result.agent_name == "pm"

    def test_code_block_with_lang_specifier(self, default_parser):
        text = "```yaml\n@squadron-dev pm: ignored\n```"
        result = default_parser.parse(text)
        assert result is None


# ── ParsedCommand Model ────────────────────────────────────────────────────────


class TestParsedCommand:
    def test_default_fields(self):
        cmd = ParsedCommand()
        assert cmd.is_help is False
        assert cmd.agent_name is None
        assert cmd.message is None
        assert cmd.action_name is None
        assert cmd.action_args == []
        assert cmd.is_action is False

    def test_help_command(self):
        cmd = ParsedCommand(is_help=True)
        assert cmd.is_help is True
        assert cmd.is_action is False

    def test_agent_command(self):
        cmd = ParsedCommand(agent_name="pm", message="triage this")
        assert cmd.agent_name == "pm"
        assert cmd.message == "triage this"
        assert cmd.is_action is False

    def test_action_command(self):
        cmd = ParsedCommand(action_name="status")
        assert cmd.is_action is True
        assert cmd.action_name == "status"

    def test_action_with_args(self):
        cmd = ParsedCommand(action_name="cancel", action_args=["feat-dev"])
        assert cmd.is_action is True
        assert cmd.action_args == ["feat-dev"]


# ── Null/Empty Inputs ─────────────────────────────────────────────────────────


class TestNullEmptyInputs:
    def test_none_text(self, default_parser):
        result = default_parser.parse(None)
        assert result is None

    def test_empty_string(self, default_parser):
        result = default_parser.parse("")
        assert result is None

    def test_whitespace_only(self, default_parser):
        result = default_parser.parse("   ")
        assert result is None

    def test_unrelated_comment(self, default_parser):
        result = default_parser.parse("This is just a regular comment")
        assert result is None

    def test_partial_mention(self, default_parser):
        result = default_parser.parse("@squadron")
        assert result is None


# ── Backward Compatibility ─────────────────────────────────────────────────────


class TestBackwardCompatibility:
    def test_parse_command_agent(self):
        result = parse_command("@squadron-dev pm: please triage")
        assert result is not None
        assert result.agent_name == "pm"

    def test_parse_command_help(self):
        result = parse_command("@squadron-dev help")
        assert result is not None
        assert result.is_help is True

    def test_parse_command_status(self):
        """parse_command should recognise built-in action commands."""
        result = parse_command("@squadron-dev status")
        assert result is not None
        assert result.action_name == "status"

    def test_parse_command_cancel(self):
        result = parse_command("@squadron-dev cancel feat-dev")
        assert result is not None
        assert result.action_name == "cancel"
        assert result.action_args == ["feat-dev"]

    def test_parse_command_none_input(self):
        result = parse_command(None)
        assert result is None

    def test_parse_command_empty_input(self):
        result = parse_command("")
        assert result is None

    def test_parse_command_default_agents_still_work(self):
        """The hardcoded default agents should all still parse without colon."""
        default_agents = [
            "pm",
            "bug-fix",
            "feat-dev",
            "docs-dev",
            "infra-dev",
            "security-review",
            "test-coverage",
            "pr-review",
        ]
        for agent in default_agents:
            result = parse_command(f"@squadron-dev {agent} do work")
            assert result is not None, f"Expected {agent} to parse"
            assert result.agent_name == agent


# ── Config-Driven Agent List ──────────────────────────────────────────────────


class TestConfigDrivenAgents:
    def test_custom_agent_in_config_parsed(self):
        parser = CommandParser(known_agents={"my-bot", "code-helper"})
        result = parser.parse("@squadron-dev my-bot review this")
        assert result is not None
        assert result.agent_name == "my-bot"

    def test_custom_agent_not_in_config_ignored_without_colon(self):
        parser = CommandParser(known_agents={"pm"})
        result = parser.parse("@squadron-dev feat-dev do work")
        assert result is None

    def test_custom_agent_with_colon_always_parsed(self):
        parser = CommandParser(known_agents={"pm"})
        result = parser.parse("@squadron-dev feat-dev: do work")
        assert result is not None
        assert result.agent_name == "feat-dev"

    def test_parser_from_config(self):
        """CommandParser should work when initialized from SquadronConfig attributes."""
        config = SquadronConfig(
            project={"name": "test", "owner": "x", "repo": "y"},
            agent_roles={
                "pm": {"agent_definition": "agents/pm.md"},
                "my-agent": {"agent_definition": "agents/my-agent.md"},
            },
        )
        parser = CommandParser(
            command_prefix=config.command_prefix,
            known_agents=set(config.agent_roles.keys()),
        )
        result = parser.parse("@squadron-dev my-agent do work")
        assert result is not None
        assert result.agent_name == "my-agent"

    def test_configurable_prefix_in_parser(self):
        """command_prefix from config is used in regex."""
        parser = CommandParser(command_prefix="@custom-prefix")
        result = parser.parse("@custom-prefix status")
        assert result is not None
        assert result.action_name == "status"

    def test_old_prefix_not_matched_with_custom_prefix(self):
        parser = CommandParser(command_prefix="@custom-prefix", known_agents={"pm"})
        result = parser.parse("@squadron-dev pm: triage")
        assert result is None


# ── CommandDefinition Migration ───────────────────────────────────────────────


class TestCommandDefinitionMigration:
    def test_new_style_type_field(self):
        cmd = CommandDefinition(type="action", description="Show status")
        assert cmd.type == "action"

    def test_new_style_static(self):
        cmd = CommandDefinition(type="static", response="Hello!")
        assert cmd.type == "static"
        assert cmd.response == "Hello!"

    def test_legacy_invoke_agent_false_becomes_static(self):
        cmd = CommandDefinition(invoke_agent=False, response="Hello!")
        assert cmd.type == "static"

    def test_legacy_delegate_to_becomes_agent(self):
        cmd = CommandDefinition(invoke_agent=True, delegate_to="pm")
        assert cmd.type == "agent"
        assert cmd.delegate_to == "pm"

    def test_legacy_plain_becomes_agent(self):
        cmd = CommandDefinition(enabled=True, invoke_agent=True)
        assert cmd.type == "agent"

    def test_permissions_default(self):
        cmd = CommandDefinition(type="action")
        assert cmd.permissions.require_human is False

    def test_permissions_require_human(self):
        cmd = CommandDefinition(type="action", permissions=CommandPermissions(require_human=True))
        assert cmd.permissions.require_human is True

    def test_args_field(self):
        cmd = CommandDefinition(type="action", args=["role"])
        assert cmd.args == ["role"]

    def test_config_from_dict(self):
        raw = {"type": "action", "args": ["role"], "permissions": {"require_human": True}}
        cmd = CommandDefinition(**raw)
        assert cmd.type == "action"
        assert cmd.permissions.require_human is True


# ── CommandPermissions ────────────────────────────────────────────────────────


class TestCommandPermissions:
    def test_defaults(self):
        perms = CommandPermissions()
        assert perms.require_human is False

    def test_require_human_true(self):
        perms = CommandPermissions(require_human=True)
        assert perms.require_human is True

    def test_require_human_false_explicit(self):
        perms = CommandPermissions(require_human=False)
        assert perms.require_human is False


# ── SquadronConfig Integration ────────────────────────────────────────────────


class TestSquadronConfigIntegration:
    def test_command_prefix_default(self):
        config = SquadronConfig(project={"name": "test", "owner": "x", "repo": "y"})
        assert config.command_prefix == "@squadron-dev"

    def test_command_prefix_custom(self):
        config = SquadronConfig(
            project={"name": "test", "owner": "x", "repo": "y"},
            command_prefix="@my-bot",
        )
        assert config.command_prefix == "@my-bot"

    def test_commands_empty_by_default(self):
        config = SquadronConfig(project={"name": "test", "owner": "x", "repo": "y"})
        assert config.commands == {}

    def test_commands_with_action(self):
        config = SquadronConfig(
            project={"name": "test", "owner": "x", "repo": "y"},
            commands={
                "status": {"type": "action"},
                "cancel": {"type": "action", "permissions": {"require_human": True}},
            },
        )
        assert config.commands["status"].type == "action"
        assert config.commands["cancel"].permissions.require_human is True

    def test_commands_legacy_format(self):
        """Old config format with invoke_agent/delegate_to should still load."""
        config = SquadronConfig(
            project={"name": "test", "owner": "x", "repo": "y"},
            commands={
                "help": {"enabled": True, "invoke_agent": False, "response": "help text"},
                "status": {"enabled": True, "invoke_agent": True, "delegate_to": "pm"},
            },
        )
        assert config.commands["help"].type == "static"
        assert config.commands["status"].type == "agent"


# ── inject_message action ─────────────────────────────────────────────────────


class TestInjectMessageAction:
    """Tests for the inject_message action handling.

    inject_message is delivered via ParsedCommand with action_name="inject_message"
    and the target agent + message body in action_args.
    """

    def test_inject_message_parsed_as_action(self):
        """inject_message is a built-in action and should parse correctly."""

        # inject_message may not be in default built-ins (it's a framework-internal action)
        # but if configured, it should parse
        from squadron.config import CommandDefinition

        cmds = {"inject_message": CommandDefinition(type="action", args=["agent", "message"])}
        parser = CommandParser(known_agents={"pm"}, commands=cmds)
        result = parser.parse("@squadron-dev inject_message pm hello")
        assert result is not None
        assert result.action_name == "inject_message"
        assert result.action_args == ["pm", "hello"]

    def test_parsedcommand_action_name_inject(self):
        cmd = ParsedCommand(action_name="inject_message", action_args=["pm", "test message"])
        assert cmd.is_action is True
        assert cmd.action_name == "inject_message"
        assert cmd.action_args == ["pm", "test message"]

    def test_inject_message_not_parsed_without_config(self, default_parser):
        """inject_message should NOT parse unless configured (not a default built-in)."""
        # This validates that inject_message isn't accidentally exposed.
        # inject_message is NOT in known_agents, has no colon, and is NOT a built-in
        # action, so the parser should return None.
        result = default_parser.parse("@squadron-dev inject_message pm hello")
        assert result is None


# ── Edge Cases ────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_multiple_mentions_first_wins(self, default_parser):
        """First valid command in comment should be parsed."""
        result = default_parser.parse("@squadron-dev pm: first\n@squadron-dev feat-dev: second")
        assert result is not None
        assert result.agent_name == "pm"

    def test_command_with_unicode_message(self, default_parser):
        result = default_parser.parse("@squadron-dev pm: 你好世界 🎉")
        assert result is not None
        assert result.agent_name == "pm"

    def test_command_with_special_chars_in_message(self, default_parser):
        result = default_parser.parse("@squadron-dev pm: review this `code` and [link](url)")
        assert result is not None
        assert result.agent_name == "pm"

    def test_agent_name_with_hyphens(self, default_parser):
        result = default_parser.parse("@squadron-dev feat-dev: implement feature")
        assert result is not None
        assert result.agent_name == "feat-dev"

    def test_agent_name_with_digits_not_parsed(self, default_parser):
        """Names starting with digit do not match the command pattern."""
        result = default_parser.parse("@squadron-dev 123agent do work")
        assert result is None

    def test_very_long_message(self, default_parser):
        long_msg = "x" * 10000
        result = default_parser.parse(f"@squadron-dev pm: {long_msg}")
        assert result is not None
        assert result.agent_name == "pm"
        assert len(result.message) == len(long_msg)

    def test_only_prefix_no_command(self, default_parser):
        result = default_parser.parse("@squadron-dev")
        assert result is None

    def test_newline_before_command(self, default_parser):
        result = default_parser.parse("\n\n@squadron-dev pm: do work")
        assert result is not None
        assert result.agent_name == "pm"
