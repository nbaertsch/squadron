"""Tests for slash command system (issue #122).

Validates:
- CommandParser parses /squadron <command> [args...] syntax
- CommandParser falls back to @mention syntax
- Code-span exemption applies to both syntaxes
- CommandParser uses config-driven known_agents (no hardcoded set)
- Slash commands dispatch to agent/action/static handlers in AgentManager
- Built-in actions: status, cancel, retry, list
- Permission enforcement: require_human blocks bot-authored commands
- Unknown slash command posts error message
- Config migration: old invoke_agent/delegate_to → new type schema
- command_prefix in SquadronConfig is configurable
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from squadron.config import (
    AgentRoleConfig,
    AgentTrigger,
    CommandDefinition,
    CommandPermissions,
    ProjectConfig,
    SquadronConfig,
)
from squadron.agent_manager import AgentManager
from squadron.event_router import EventRouter
from squadron.models import (
    AgentRecord,
    AgentStatus,
    CommandParser,
    GitHubEvent,
    ParsedCommand,
    SquadronEvent,
    SquadronEventType,
    parse_command,
)
from squadron.registry import AgentRegistry


# ── CommandParser unit tests ─────────────────────────────────────────────────


class TestCommandParserSlash:
    """Unit tests for CommandParser slash syntax."""

    def setup_method(self):
        self.parser = CommandParser(
            command_prefix="/squadron",
            known_agents={"pm", "feat-dev", "pr-review"},
            known_commands={"status", "cancel", "retry", "review", "triage"},
        )

    def test_slash_simple_command(self):
        result = self.parser.parse("/squadron status")
        assert result is not None
        assert result.source == "slash"
        assert result.command_name == "status"
        assert result.args == []
        assert result.is_help is False

    def test_slash_command_with_args(self):
        result = self.parser.parse("/squadron cancel feat-dev")
        assert result is not None
        assert result.source == "slash"
        assert result.command_name == "cancel"
        assert result.args == ["feat-dev"]

    def test_slash_command_multiple_args(self):
        result = self.parser.parse("/squadron retry pr-review")
        assert result is not None
        assert result.source == "slash"
        assert result.command_name == "retry"
        assert result.args == ["pr-review"]

    def test_slash_help_command(self):
        result = self.parser.parse("/squadron help")
        assert result is not None
        assert result.is_help is True
        assert result.source == "slash"

    def test_slash_case_insensitive(self):
        result = self.parser.parse("/Squadron STATUS")
        assert result is not None
        assert result.source == "slash"
        assert result.command_name == "status"

    def test_slash_at_start_of_line(self):
        result = self.parser.parse("Hey,\n/squadron status\nmore text")
        assert result is not None
        assert result.source == "slash"
        assert result.command_name == "status"

    def test_slash_in_middle_of_line_not_matched(self):
        """Slash command must be at start of line."""
        result = self.parser.parse("some text /squadron status other text")
        # Should not match if not at line start
        assert result is None or result.command_name != "status" or result.source != "slash"

    def test_slash_returns_none_for_no_match(self):
        result = self.parser.parse("Just a regular comment")
        assert result is None

    def test_slash_empty_string(self):
        result = self.parser.parse("")
        assert result is None

    def test_slash_in_code_span_ignored(self):
        """Slash commands inside backtick code spans are ignored."""
        result = self.parser.parse("Run `/squadron status` to check")
        assert result is None

    def test_slash_in_fenced_block_ignored(self):
        """Slash commands inside fenced code blocks are ignored."""
        result = self.parser.parse("```\n/squadron cancel feat-dev\n```")
        assert result is None

    def test_custom_prefix(self):
        """Custom command_prefix is honoured."""
        parser = CommandParser(command_prefix="/sq")
        result = parser.parse("/sq status")
        assert result is not None
        assert result.command_name == "status"
        # Standard prefix should not match
        result2 = parser.parse("/squadron status")
        assert result2 is None


class TestCommandParserMention:
    """Unit tests for CommandParser @mention syntax (backward compat)."""

    def setup_method(self):
        self.parser = CommandParser(
            known_agents={"pm", "feat-dev"},
        )

    def test_mention_agent_with_colon(self):
        result = self.parser.parse("@squadron-dev pm: triage this issue")
        assert result is not None
        assert result.source == "mention"
        assert result.agent_name == "pm"
        assert result.message == "triage this issue"
        assert result.is_help is False

    def test_mention_agent_with_hyphen(self):
        result = self.parser.parse("@squadron-dev feat-dev: implement feature")
        assert result is not None
        assert result.source == "mention"
        assert result.agent_name == "feat-dev"

    def test_mention_help(self):
        result = self.parser.parse("@squadron-dev help")
        assert result is not None
        assert result.is_help is True
        assert result.source == "mention"

    def test_mention_known_agent_no_colon(self):
        """Known agents are recognised without a colon."""
        result = self.parser.parse("@squadron-dev pm please help")
        assert result is not None
        assert result.agent_name == "pm"

    def test_mention_unknown_agent_no_colon_not_matched(self):
        """Unknown agents without a colon are not matched (prevents false positives)."""
        result = self.parser.parse("@squadron-dev nobody something")
        assert result is None

    def test_mention_no_command(self):
        result = self.parser.parse("Just a regular comment")
        assert result is None

    def test_mention_in_code_span_ignored(self):
        result = self.parser.parse("run `@squadron-dev pm: test` to check")
        assert result is None


class TestCommandParserPrecedence:
    """Slash syntax takes precedence over @mention syntax."""

    def setup_method(self):
        self.parser = CommandParser(
            known_agents={"pm"},
            known_commands={"status"},
        )

    def test_slash_takes_precedence(self):
        """When both slash and mention appear, slash wins."""
        result = self.parser.parse("/squadron status\n@squadron-dev pm: something")
        assert result is not None
        assert result.source == "slash"
        assert result.command_name == "status"

    def test_mention_fallback_when_no_slash(self):
        """Falls back to mention when no slash command found."""
        result = self.parser.parse("@squadron-dev pm: triage this")
        assert result is not None
        assert result.source == "mention"


class TestParsedCommandFields:
    """ParsedCommand model has correct default field values."""

    def test_default_source_is_mention(self):
        cmd = ParsedCommand(agent_name="pm")
        assert cmd.source == "mention"

    def test_slash_source(self):
        cmd = ParsedCommand(source="slash", command_name="status")
        assert cmd.source == "slash"
        assert cmd.command_name == "status"
        assert cmd.args == []

    def test_args_default_empty(self):
        cmd = ParsedCommand(source="slash", command_name="cancel", args=["feat-dev"])
        assert cmd.args == ["feat-dev"]


# ── Config model tests ────────────────────────────────────────────────────────


class TestCommandDefinitionMigration:
    """CommandDefinition migrates old invoke_agent/delegate_to schema."""

    def test_new_agent_type(self):
        cmd = CommandDefinition(type="agent", agent="pr-review", description="Code review")
        assert cmd.type == "agent"
        assert cmd.agent == "pr-review"

    def test_new_action_type(self):
        cmd = CommandDefinition(type="action", action="status")
        assert cmd.type == "action"
        assert cmd.action == "status"

    def test_new_static_type(self):
        cmd = CommandDefinition(type="static", response="Hello!")
        assert cmd.type == "static"
        assert cmd.response == "Hello!"

    def test_migrate_invoke_agent_with_delegate(self):
        """Old invoke_agent=True, delegate_to=pm → type=agent, agent=pm."""
        cmd = CommandDefinition(invoke_agent=True, delegate_to="pm")
        assert cmd.type == "agent"
        assert cmd.agent == "pm"

    def test_migrate_invoke_agent_false_with_response(self):
        """Old invoke_agent=False → type=static."""
        cmd = CommandDefinition(invoke_agent=False, response="Use @mention syntax.")
        assert cmd.type == "static"
        assert cmd.response == "Use @mention syntax."

    def test_permissions_require_human(self):
        cmd = CommandDefinition(
            type="action",
            action="cancel",
            permissions=CommandPermissions(require_human=True),
        )
        assert cmd.permissions.require_human is True

    def test_permissions_default(self):
        cmd = CommandDefinition(type="static", response="hi")
        assert cmd.permissions.require_human is False

    def test_inject_message(self):
        cmd = CommandDefinition(type="agent", agent="pm", inject_message="Triage this")
        assert cmd.inject_message == "Triage this"


class TestSquadronConfigCommandPrefix:
    """SquadronConfig.command_prefix defaults to /squadron."""

    def test_default_prefix(self):
        config = SquadronConfig(project=ProjectConfig(name="test"))
        assert config.command_prefix == "/squadron"

    def test_custom_prefix(self):
        config = SquadronConfig(
            project=ProjectConfig(name="test"),
            command_prefix="/mybot",
        )
        assert config.command_prefix == "/mybot"


# ── Integration tests — slash commands in AgentManager ───────────────────────


def _make_config(extra_commands: dict | None = None) -> SquadronConfig:
    """Build a minimal SquadronConfig for command routing tests."""
    commands = {
        "status": CommandDefinition(type="action", action="status", enabled=True),
        "cancel": CommandDefinition(
            type="action",
            action="cancel",
            args=["role"],
            enabled=True,
            permissions=CommandPermissions(require_human=True),
        ),
        "retry": CommandDefinition(
            type="action",
            action="retry",
            args=["role"],
            enabled=True,
            permissions=CommandPermissions(require_human=True),
        ),
        "docs": CommandDefinition(
            type="static",
            response="Documentation: https://example.com/docs",
            enabled=True,
        ),
        "review": CommandDefinition(type="agent", agent="pr-review", enabled=True),
        "triage": CommandDefinition(
            type="agent",
            agent="pm",
            inject_message="Triage this issue",
            enabled=True,
        ),
    }
    if extra_commands:
        commands.update(extra_commands)

    return SquadronConfig(
        project=ProjectConfig(name="test-project", owner="owner", repo="repo"),
        agent_roles={
            "pm": AgentRoleConfig(
                agent_definition="agents/pm.md",
                singleton=True,
                lifecycle="ephemeral",
            ),
            "feat-dev": AgentRoleConfig(
                agent_definition="agents/feat-dev.md",
                lifecycle="stateful",
            ),
            "pr-review": AgentRoleConfig(
                agent_definition="agents/pr-review.md",
                lifecycle="stateful",
            ),
        },
        commands=commands,
        command_prefix="/squadron",
    )


def _make_agent_defs():
    """Create mock agent definitions (using MagicMock to avoid requiring all fields)."""
    def _def(display_name, description="", tools=None, emoji="🤖"):
        return MagicMock(
            display_name=display_name,
            description=description,
            tools=tools or [],
            emoji=emoji,
        )

    return {
        "pm": _def("Project Manager", "Manages projects", ["create_issue"], "🎯"),
        "feat-dev": _def("Feature Developer", "Builds features", ["read_file"], "👨‍💻"),
        "pr-review": _def("PR Reviewer", "Reviews PRs", ["list_files"], "🔍"),
    }


def _slash_comment_event(
    body: str,
    issue_number: int = 1,
    sender: str = "alice",
    sender_type: str = "User",
) -> GitHubEvent:
    """Build a GitHubEvent for an issue_comment containing a slash command."""
    return GitHubEvent(
        delivery_id=f"test-{issue_number}-{body[:20]}",
        event_type="issue_comment",
        action="created",
        payload={
            "action": "created",
            "issue": {"number": issue_number, "title": "Test issue"},
            "comment": {
                "id": 42,
                "body": body,
                "user": {"login": sender, "type": sender_type},
            },
            "sender": {"login": sender, "type": sender_type},
            "repository": {"full_name": "owner/repo"},
        },
    )


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest_asyncio.fixture
async def registry(db_path):
    from squadron.registry import AgentRegistry

    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


@pytest.mark.asyncio
class TestSlashCommandRouting:
    """Integration tests for slash command routing through AgentManager."""

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_status_no_agents(self, mock_copilot_cls, registry, tmp_path):
        """``/squadron status`` posts empty status when no agents exist."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron status", issue_number=5)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "Agent Status" in body
        assert "#5" in body

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_status_with_agents(self, mock_copilot_cls, registry, tmp_path):
        """``/squadron status`` lists agents in a table."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Plant an agent record
        agent = AgentRecord(
            agent_id="feat-dev-issue-5",
            role="feat-dev",
            issue_number=5,
            status=AgentStatus.ACTIVE,
            turn_count=3,
        )
        await registry.create_agent(agent)

        event = _slash_comment_event("/squadron status", issue_number=5)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "feat-dev-issue-5" in body
        assert "feat-dev" in body

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_static_command(self, mock_copilot_cls, registry, tmp_path):
        """``/squadron docs`` posts the configured static response."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron docs", issue_number=7)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "https://example.com/docs" in body

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_unknown_command_posts_error(self, mock_copilot_cls, registry, tmp_path):
        """Unknown slash command posts error with available commands."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron unknown-cmd", issue_number=8)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "Unknown command" in body
        assert "unknown-cmd" in body

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_help_command(self, mock_copilot_cls, registry, tmp_path):
        """``/squadron help`` posts agent list and command table."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron help", issue_number=9)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "Agents" in body
        assert "Slash Commands" in body
        assert "/squadron" in body

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_list_alias_for_help(self, mock_copilot_cls, registry, tmp_path):
        """``/squadron list`` is an alias for help."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron list", issue_number=9)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "Agents" in body

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_cancel_missing_role_posts_usage(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """``/squadron cancel`` without role posts usage hint."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron cancel", issue_number=10)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "Usage" in body

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_cancel_no_active_agent(self, mock_copilot_cls, registry, tmp_path):
        """``/squadron cancel feat-dev`` when no agent exists posts warning."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron cancel feat-dev", issue_number=11)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "No active" in body or "no active" in body.lower()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_cancel_active_agent(self, mock_copilot_cls, registry, tmp_path):
        """``/squadron cancel feat-dev`` cancels an active agent and posts confirmation."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Create an active agent
        agent = AgentRecord(
            agent_id="feat-dev-issue-12",
            role="feat-dev",
            issue_number=12,
            status=AgentStatus.ACTIVE,
        )
        await registry.create_agent(agent)

        event = _slash_comment_event("/squadron cancel feat-dev", issue_number=12)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "Cancelled" in body or "cancelled" in body.lower()

        # Agent should be completed in registry
        updated = await registry.get_agent("feat-dev-issue-12")
        assert updated.status == AgentStatus.COMPLETED

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_retry_no_completed_agent(self, mock_copilot_cls, registry, tmp_path):
        """``/squadron retry feat-dev`` when no completed agent exists posts warning."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron retry feat-dev", issue_number=13)
        await router._route_event(event)

        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "No completed" in body or "no completed" in body.lower()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_require_human_blocks_bot(self, mock_copilot_cls, registry, tmp_path):
        """Commands with require_human=True are blocked when posted by a bot."""
        config = _make_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Post /squadron cancel as a bot (feat-dev agent)
        event = _slash_comment_event(
            body=f"🤖 **Feature Developer**\n\n/squadron cancel feat-dev",
            issue_number=14,
            sender="squadron-dev[bot]",
            sender_type="Bot",
        )
        await router._route_event(event)

        # Should NOT post any comment (blocked silently)
        github.comment_on_issue.assert_not_called()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_disabled_command_ignored(self, mock_copilot_cls, registry, tmp_path):
        """Disabled commands are treated as unknown."""
        config = _make_config(
            extra_commands={
                "secret": CommandDefinition(type="static", response="secret!", enabled=False)
            }
        )
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)
        github = AsyncMock()

        manager = AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _slash_comment_event("/squadron secret", issue_number=15)
        await router._route_event(event)

        # Should post unknown command error
        github.comment_on_issue.assert_called_once()
        body = github.comment_on_issue.call_args[0][3]
        assert "Unknown command" in body


# ── Backward compatibility: parse_command() still works ──────────────────────


class TestParseCommandBackwardCompat:
    """parse_command() wrapper still accepts old callers."""

    def test_mention_still_works(self):
        result = parse_command("@squadron-dev pm: triage this")
        assert result is not None
        assert result.agent_name == "pm"

    def test_help_still_works(self):
        result = parse_command("@squadron-dev help")
        assert result is not None
        assert result.is_help is True

    def test_no_command_returns_none(self):
        result = parse_command("Just a comment")
        assert result is None

    def test_custom_known_agents(self):
        """parse_command accepts custom known_agents set."""
        result = parse_command("@squadron-dev custom-agent please help", known_agents={"custom-agent"})
        assert result is not None
        assert result.agent_name == "custom-agent"

    def test_unknown_agent_no_colon_still_ignored(self):
        result = parse_command("@squadron-dev nobody something")
        assert result is None


# ── EventRouter uses CommandParser ────────────────────────────────────────────


class TestEventRouterCommandParser:
    """EventRouter initialises CommandParser from config."""

    def test_command_parser_initialized(self):
        """EventRouter creates a CommandParser with config-derived agents/commands."""
        import asyncio
        from unittest.mock import MagicMock

        config = _make_config()
        registry = MagicMock()
        queue = asyncio.Queue()
        router = EventRouter(queue, registry, config)

        # The command parser should know about configured agents and commands
        assert isinstance(router._command_parser, CommandParser)
        assert "pm" in router._command_parser.known_agents
        assert "feat-dev" in router._command_parser.known_agents
        assert "status" in router._command_parser.known_commands
        assert "cancel" in router._command_parser.known_commands
        assert router._command_parser.command_prefix == "/squadron"

    def test_slash_command_parsed_in_squadron_event(self):
        """_to_squadron_event populates command for slash syntax."""
        import asyncio
        from unittest.mock import MagicMock

        config = _make_config()
        registry = MagicMock()
        queue = asyncio.Queue()
        router = EventRouter(queue, registry, config)

        github_event = _slash_comment_event("/squadron status", issue_number=1)
        squadron_event = router._to_squadron_event(github_event, SquadronEventType.ISSUE_COMMENT)

        assert squadron_event.command is not None
        assert squadron_event.command.source == "slash"
        assert squadron_event.command.command_name == "status"

    def test_mention_command_parsed_in_squadron_event(self):
        """_to_squadron_event populates command for @mention syntax."""
        import asyncio
        from unittest.mock import MagicMock

        config = _make_config()
        registry = MagicMock()
        queue = asyncio.Queue()
        router = EventRouter(queue, registry, config)

        github_event = _slash_comment_event("@squadron-dev pm: triage this", issue_number=2)
        squadron_event = router._to_squadron_event(github_event, SquadronEventType.ISSUE_COMMENT)

        assert squadron_event.command is not None
        assert squadron_event.command.source == "mention"
        assert squadron_event.command.agent_name == "pm"
