"""Tests for command-based comment routing (Layer 2).

Validates:
- parse_command() extracts @squadron-dev <agent>: <message> syntax
- parse_command() detects @squadron-dev help
- Self-loop guard prevents agents from re-triggering themselves
- Command routing spawns, wakes, or delivers events correctly
- Help command posts agent list
- Unknown agent error handling
- Comments without @squadron-dev commands are silently ignored
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
    ProjectConfig,
    SquadronConfig,
)
from squadron.agent_manager import AgentManager
from squadron.event_router import EventRouter
from squadron.models import (
    AgentRecord,
    AgentStatus,
    GitHubEvent,
    SquadronEvent,
    SquadronEventType,
    parse_command,
)
from squadron.registry import AgentRegistry


# ── parse_command() unit tests ──────────────────────────────────────────────


class TestParseCommand:
    """Unit tests for parse_command()."""

    def test_agent_command_basic(self):
        result = parse_command("@squadron-dev pm: please triage this")
        assert result is not None
        assert result.is_help is False
        assert result.agent_name == "pm"
        assert result.message == "please triage this"

    def test_agent_command_with_hyphen(self):
        result = parse_command("@squadron-dev feat-dev: implement the feature")
        assert result is not None
        assert result.agent_name == "feat-dev"
        assert result.message == "implement the feature"

    def test_help_command(self):
        result = parse_command("@squadron-dev help")
        assert result is not None
        assert result.is_help is True
        assert result.agent_name is None

    def test_help_command_case_insensitive(self):
        result = parse_command("@Squadron-Dev HELP")
        assert result is not None
        assert result.is_help is True

    def test_no_command(self):
        result = parse_command("Just a regular comment")
        assert result is None

    def test_empty_string(self):
        result = parse_command("")
        assert result is None

    def test_command_case_insensitive(self):
        result = parse_command("@SQUADRON-DEV PM: do stuff")
        assert result is not None
        assert result.agent_name == "pm"

    def test_command_multiline_message(self):
        result = parse_command("@squadron-dev pm: triage this\n\nMore details here")
        assert result is not None
        assert result.agent_name == "pm"
        assert "triage this" in result.message
        assert "More details here" in result.message

    def test_command_in_middle_of_text(self):
        result = parse_command("Hey team, @squadron-dev pm: can you look at this?")
        assert result is not None
        assert result.agent_name == "pm"

    def test_mention_without_colon_not_matched(self):
        """@squadron-dev agent without colon should not match."""
        result = parse_command("@squadron-dev pm please help")
        assert result is None

    def test_help_with_trailing_text(self):
        """@squadron-dev help followed by other text still matches."""
        result = parse_command("@squadron-dev help please show agents")
        assert result is not None
        assert result.is_help is True


# ── Self-loop guard unit tests ───────────────────────────────────────────────


class TestSelfLoopGuard:
    """Unit tests for _get_sender_agent_role()."""

    def _make_manager(self) -> AgentManager:
        """Create a minimal AgentManager with mocked dependencies."""
        config = SquadronConfig(
            project=ProjectConfig(
                name="test",
                owner="testowner",
                repo="testrepo",
                bot_username="squadron-dev[bot]",
            ),
            agent_roles={
                "pm": AgentRoleConfig(
                    agent_definition="agents/pm.md",
                    singleton=True,
                    lifecycle="ephemeral",
                ),
                "feat-dev": AgentRoleConfig(
                    agent_definition="agents/feat-dev.md",
                ),
            },
        )
        agent_defs = {
            "pm": MagicMock(display_name="Project Manager", emoji="🎯"),
            "feat-dev": MagicMock(display_name="Feature Developer", emoji="👨‍💻"),
        }
        manager = AgentManager(
            config=config,
            registry=MagicMock(),
            github=MagicMock(),
            router=MagicMock(),
            agent_definitions=agent_defs,
            repo_root=Path("/tmp"),
        )
        return manager

    def test_human_sender_returns_none(self):
        manager = self._make_manager()
        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_COMMENT,
            data={
                "payload": {
                    "comment": {
                        "user": {"login": "alice", "type": "User"},
                        "body": "@squadron-dev pm: what's up?",
                    }
                }
            },
        )
        assert manager._get_sender_agent_role(event) is None

    def test_bot_pm_comment_returns_pm(self):
        manager = self._make_manager()
        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_COMMENT,
            data={
                "payload": {
                    "comment": {
                        "user": {"login": "squadron-dev[bot]", "type": "Bot"},
                        "body": "🎯 **Project Manager**\n\nTriage complete.",
                    }
                }
            },
        )
        assert manager._get_sender_agent_role(event) == "pm"

    def test_bot_feat_dev_comment_returns_feat_dev(self):
        manager = self._make_manager()
        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_COMMENT,
            data={
                "payload": {
                    "comment": {
                        "user": {"login": "squadron-dev[bot]", "type": "Bot"},
                        "body": "👨‍💻 **Feature Developer**\n\nWorking on implementation.",
                    }
                }
            },
        )
        assert manager._get_sender_agent_role(event) == "feat-dev"

    def test_bot_comment_without_prefix_returns_none(self):
        manager = self._make_manager()
        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_COMMENT,
            data={
                "payload": {
                    "comment": {
                        "user": {"login": "squadron-dev[bot]", "type": "Bot"},
                        "body": "Just a generic bot comment.",
                    }
                }
            },
        )
        assert manager._get_sender_agent_role(event) is None


# ── Command routing integration tests ────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_mention.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _command_config() -> SquadronConfig:
    """Config for command routing tests."""
    return SquadronConfig(
        project=ProjectConfig(
            name="test-project",
            owner="testowner",
            repo="testrepo",
            default_branch="main",
            bot_username="squadron-dev[bot]",
        ),
        agent_roles={
            "pm": AgentRoleConfig(
                agent_definition="agents/pm.md",
                singleton=True,
                lifecycle="ephemeral",
                triggers=[
                    AgentTrigger(event="issues.opened"),
                ],
            ),
            "feat-dev": AgentRoleConfig(
                agent_definition="agents/feat-dev.md",
                triggers=[
                    AgentTrigger(event="issues.labeled", label="feature"),
                ],
            ),
            "docs-dev": AgentRoleConfig(
                agent_definition="agents/docs-dev.md",
                triggers=[
                    AgentTrigger(event="issues.labeled", label="documentation"),
                ],
            ),
        },
    )


def _comment_event(
    body: str,
    issue_number: int = 42,
    sender_login: str = "alice",
    sender_type: str = "User",
    delivery_id: str = "delivery-123",
) -> GitHubEvent:
    """Build a GitHub issue_comment.created event."""
    return GitHubEvent(
        delivery_id=delivery_id,
        event_type="issue_comment",
        action="created",
        payload={
            "action": "created",
            "issue": {"number": issue_number},
            "comment": {
                "body": body,
                "user": {"login": sender_login, "type": sender_type},
            },
            "sender": {"login": sender_login, "type": sender_type},
        },
    )


def _make_agent_defs():
    """Create mock agent definitions with display_name and emoji."""
    return {
        "pm": MagicMock(
            prompt="test prompt",
            raw_content="test",
            tools=None,
            display_name="Project Manager",
            emoji="🎯",
            description="Triages issues",
        ),
        "feat-dev": MagicMock(
            prompt="test",
            raw_content="test",
            tools=None,
            display_name="Feature Developer",
            emoji="👨‍💻",
            description="Implements features",
        ),
        "docs-dev": MagicMock(
            prompt="test",
            raw_content="test",
            tools=None,
            display_name="Documentation Developer",
            emoji="📝",
            description="Writes documentation",
        ),
    }


@pytest.mark.asyncio
class TestCommandRouting:
    """Integration tests for command-based routing through EventRouter → AgentManager."""

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_command_with_pm_spawns_pm(self, mock_copilot_cls, registry, tmp_path):
        """@squadron-dev pm: spawns an ephemeral PM agent."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        mock_copilot = AsyncMock()
        mock_copilot.create_session = AsyncMock(
            return_value=MagicMock(
                send_and_wait=AsyncMock(return_value=MagicMock(type=MagicMock(value="text"))),
            )
        )
        mock_copilot_cls.return_value = mock_copilot

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Route a command event
        event = _comment_event("@squadron-dev pm: please triage this issue", issue_number=10)
        await router._route_event(event)

        # PM agent should be created
        agents = await registry.get_agents_for_issue(10)
        pm_agents = [a for a in agents if a.role == "pm"]
        assert len(pm_agents) == 1
        assert pm_agents[0].status in (AgentStatus.ACTIVE, AgentStatus.COMPLETED)

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_comment_without_command_ignored(self, mock_copilot_cls, registry, tmp_path):
        """Comment without @squadron-dev command does not spawn any agents."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions={},
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _comment_event("Just a regular comment, no commands", issue_number=10)
        await router._route_event(event)

        agents = await registry.get_agents_for_issue(10)
        assert len(agents) == 0

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_self_loop_guard_blocks_pm_retriggering_itself(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """PM posting @squadron-dev pm: does NOT re-trigger PM (self-loop guard)."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Simulate bot-authored comment from PM role with new signature format
        event = _comment_event(
            body="🎯 **Project Manager**\n\nTriage complete. @squadron-dev pm: should follow up.",
            issue_number=10,
            sender_login="squadron-dev[bot]",
            sender_type="Bot",
        )
        await router._route_event(event)

        # No PM agent should be spawned
        agents = await registry.get_agents_for_issue(10)
        pm_agents = [a for a in agents if a.role == "pm"]
        assert len(pm_agents) == 0

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_pm_commanding_feat_dev_spawns_feat_dev(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """PM posting @squadron-dev feat-dev: DOES spawn feat-dev (cross-role allowed)."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        mock_copilot = AsyncMock()
        mock_copilot.create_session = AsyncMock(
            return_value=MagicMock(
                send_and_wait=AsyncMock(return_value=MagicMock(type=MagicMock(value="text"))),
            )
        )
        mock_copilot_cls.return_value = mock_copilot

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # PM posts command for feat-dev
        event = _comment_event(
            body="🎯 **Project Manager**\n\n@squadron-dev feat-dev: please implement the feature.",
            issue_number=10,
            sender_login="squadron-dev[bot]",
            sender_type="Bot",
        )
        await router._route_event(event)

        # feat-dev agent should be created (not PM)
        agents = await registry.get_agents_for_issue(10)
        feat_agents = [a for a in agents if a.role == "feat-dev"]
        pm_agents = [a for a in agents if a.role == "pm"]
        assert len(feat_agents) == 1
        assert len(pm_agents) == 0

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_command_wakes_sleeping_persistent_agent(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """@squadron-dev feat-dev: wakes a sleeping feat-dev agent for the same issue."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        mock_copilot = AsyncMock()
        mock_copilot.create_session = AsyncMock(
            return_value=MagicMock(
                send_and_wait=AsyncMock(return_value=MagicMock(type=MagicMock(value="text"))),
            )
        )
        mock_copilot.resume_session = AsyncMock(
            return_value=MagicMock(
                send_and_wait=AsyncMock(return_value=MagicMock(type=MagicMock(value="text"))),
            )
        )
        mock_copilot_cls.return_value = mock_copilot

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Pre-create a sleeping feat-dev agent
        sleeping_agent = AgentRecord(
            agent_id="feat-dev-issue-10",
            role="feat-dev",
            issue_number=10,
            session_id="squadron-feat-dev-issue-10",
            status=AgentStatus.SLEEPING,
            sleeping_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(sleeping_agent)

        # Human commands feat-dev
        event = _comment_event(
            body="@squadron-dev feat-dev: please continue working on this",
            issue_number=10,
        )
        await router._route_event(event)

        # Agent should be woken (ACTIVE)
        agent = await registry.get_agent("feat-dev-issue-10")
        assert agent is not None
        assert agent.status == AgentStatus.ACTIVE

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_command_delivers_to_active_agent_inbox(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """@squadron-dev feat-dev: when it's ACTIVE delivers event to its inbox."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions={},
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Pre-create an active feat-dev agent with inbox
        active_agent = AgentRecord(
            agent_id="feat-dev-issue-10",
            role="feat-dev",
            issue_number=10,
            session_id="squadron-feat-dev-issue-10",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(active_agent)
        manager.agent_inboxes["feat-dev-issue-10"] = asyncio.Queue()

        # Human commands feat-dev
        event = _comment_event(
            body="@squadron-dev feat-dev: can you check the test results?",
            issue_number=10,
        )
        await router._route_event(event)

        # Event should be in the inbox
        inbox = manager.agent_inboxes["feat-dev-issue-10"]
        assert not inbox.empty()
        queued_event = await inbox.get()
        assert queued_event.command is not None
        assert queued_event.command.agent_name == "feat-dev"

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_command_spawns_new_persistent_agent_if_none_exists(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """@squadron-dev feat-dev: when no agent exists for the issue spawns a new one."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        mock_copilot = AsyncMock()
        mock_copilot.create_session = AsyncMock(
            return_value=MagicMock(
                send_and_wait=AsyncMock(return_value=MagicMock(type=MagicMock(value="text"))),
            )
        )
        mock_copilot_cls.return_value = mock_copilot

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _comment_event(
            body="@squadron-dev feat-dev: can you implement this?",
            issue_number=10,
        )
        await router._route_event(event)

        agents = await registry.get_agents_for_issue(10)
        feat_agents = [a for a in agents if a.role == "feat-dev"]
        assert len(feat_agents) == 1

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_help_command_posts_agent_list(self, mock_copilot_cls, registry, tmp_path):
        """@squadron-dev help posts a markdown table of available agents."""
        config = _command_config()
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

        event = _comment_event(
            body="@squadron-dev help",
            issue_number=10,
        )
        await router._route_event(event)

        # Should have posted a comment
        github.comment_on_issue.assert_called_once()
        call_args = github.comment_on_issue.call_args
        body = call_args[0][3] if len(call_args[0]) > 3 else call_args[1].get("body", "")
        assert "Available Agents" in body
        assert "pm" in body.lower()
        assert "feat-dev" in body.lower()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_unknown_agent_posts_error(self, mock_copilot_cls, registry, tmp_path):
        """@squadron-dev unknown-agent: posts an error with available agents."""
        config = _command_config()
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

        event = _comment_event(
            body="@squadron-dev nonexistent-agent: do something",
            issue_number=10,
        )
        await router._route_event(event)

        # Should have posted an error comment
        github.comment_on_issue.assert_called_once()
        call_args = github.comment_on_issue.call_args
        body = call_args[0][3] if len(call_args[0]) > 3 else call_args[1].get("body", "")
        assert "Unknown agent" in body
        assert "nonexistent-agent" in body

        # No agent should be spawned
        agents = await registry.get_agents_for_issue(10)
        assert len(agents) == 0

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_singleton_guard_prevents_duplicate_ephemeral(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """Singleton PM: second @squadron-dev pm: while first PM is active is blocked."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions={},
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Pre-create an active PM agent
        active_pm = AgentRecord(
            agent_id="pm-issue-5-12345",
            role="pm",
            issue_number=5,
            session_id="squadron-pm-issue-5",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(active_pm)

        # Another command should be blocked by singleton guard
        event = _comment_event("@squadron-dev pm: triage this too", issue_number=10)
        await router._route_event(event)

        # Only the original PM agent should exist
        all_agents = await registry.get_all_active_agents()
        pm_agents = [a for a in all_agents if a.role == "pm"]
        assert len(pm_agents) == 1
        assert pm_agents[0].agent_id == "pm-issue-5-12345"

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_command_respawns_after_completed_agent(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """Issue #13 regression: commanding @squadron-dev feat-dev: when a COMPLETED agent
        exists for the same role+issue should clean up the stale record and spawn fresh."""
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        mock_copilot = AsyncMock()
        mock_copilot.create_session = AsyncMock(
            return_value=MagicMock(
                send_and_wait=AsyncMock(return_value=MagicMock(type=MagicMock(value="text"))),
            )
        )
        mock_copilot_cls.return_value = mock_copilot

        manager = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions=_make_agent_defs(),
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Pre-create a COMPLETED agent (simulates a previous run that finished)
        completed = AgentRecord(
            agent_id="feat-dev-issue-12",
            role="feat-dev",
            issue_number=12,
            session_id="squadron-feat-dev-issue-12",
            status=AgentStatus.COMPLETED,
        )
        await registry.create_agent(completed)

        # Command should respawn cleanly
        event = _comment_event(
            body="@squadron-dev feat-dev: please revisit this",
            issue_number=12,
        )
        await router._route_event(event)

        # New agent should be active
        agents = await registry.get_agents_for_issue(12)
        feat_agents = [a for a in agents if a.role == "feat-dev"]
        assert len(feat_agents) == 1
        assert feat_agents[0].status == AgentStatus.ACTIVE

        await manager.stop()


# ── EventRouter command parsing tests ────────────────────────────────────────


@pytest.mark.asyncio
class TestEventRouterCommandParsing:
    """Tests that EventRouter populates command on SquadronEvent."""

    async def test_comment_event_populates_command(self, registry):
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        event = _comment_event("@squadron-dev pm: please look at this", issue_number=10)
        internal_event = router._to_squadron_event(event, SquadronEventType.ISSUE_COMMENT)

        assert internal_event.command is not None
        assert internal_event.command.agent_name == "pm"
        assert "please look at this" in internal_event.command.message

    async def test_help_command_detected(self, registry):
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        event = _comment_event("@squadron-dev help", issue_number=10)
        internal_event = router._to_squadron_event(event, SquadronEventType.ISSUE_COMMENT)

        assert internal_event.command is not None
        assert internal_event.command.is_help is True

    async def test_non_comment_event_has_no_command(self, registry):
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        event = GitHubEvent(
            delivery_id="d-1",
            event_type="issues",
            action="opened",
            payload={"issue": {"number": 10}},
        )
        internal_event = router._to_squadron_event(event, SquadronEventType.ISSUE_OPENED)

        assert internal_event.command is None

    async def test_comment_without_command_has_no_command(self, registry):
        config = _command_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        event = _comment_event("just a regular comment", issue_number=10)
        internal_event = router._to_squadron_event(event, SquadronEventType.ISSUE_COMMENT)

        assert internal_event.command is None
