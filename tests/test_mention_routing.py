"""Tests for mention-based comment routing (Layer 2).

Validates:
- parse_mentions() extracts @role and /role from comment text
- Self-loop guard prevents agents from re-triggering themselves
- Mention routing spawns, wakes, or delivers events correctly
- Comments without mentions are silently ignored
- End-to-end: comment event → EventRouter → AgentManager mention handler
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
    parse_mentions,
)
from squadron.registry import AgentRegistry


# ── parse_mentions() unit tests ──────────────────────────────────────────────


class TestParseMentions:
    """Unit tests for parse_mentions()."""

    KNOWN_ROLES = {"pm", "feat-dev", "bug-fix", "pr-review", "docs-dev", "security-review"}

    def test_at_mention_single(self):
        assert parse_mentions("Hey @pm can you triage this?", self.KNOWN_ROLES) == ["pm"]

    def test_slash_mention_single(self):
        assert parse_mentions("/pm please triage", self.KNOWN_ROLES) == ["pm"]

    def test_multiple_mentions(self):
        text = "@pm please assign this to @feat-dev for implementation"
        assert parse_mentions(text, self.KNOWN_ROLES) == ["pm", "feat-dev"]

    def test_mixed_at_and_slash(self):
        text = "@pm and /docs-dev need to look at this"
        assert parse_mentions(text, self.KNOWN_ROLES) == ["pm", "docs-dev"]

    def test_unknown_role_ignored(self):
        text = "@random-person please check @pm"
        assert parse_mentions(text, self.KNOWN_ROLES) == ["pm"]

    def test_no_mentions(self):
        assert parse_mentions("Just a regular comment", self.KNOWN_ROLES) == []

    def test_empty_text(self):
        assert parse_mentions("", self.KNOWN_ROLES) == []

    def test_none_text(self):
        # parse_mentions handles empty strings; None would be a caller error
        # but we guard for it
        assert parse_mentions("", set()) == []

    def test_deduplication(self):
        text = "@pm do this. Also @pm check that."
        assert parse_mentions(text, self.KNOWN_ROLES) == ["pm"]

    def test_hyphenated_roles(self):
        text = "@feat-dev and @security-review"
        assert parse_mentions(text, self.KNOWN_ROLES) == ["feat-dev", "security-review"]

    def test_case_insensitive(self):
        text = "@PM please triage"
        # Role matching is case-insensitive (lowered)
        assert parse_mentions(text, self.KNOWN_ROLES) == ["pm"]

    def test_mention_at_line_start(self):
        text = "@feat-dev\nPlease implement this feature"
        assert parse_mentions(text, self.KNOWN_ROLES) == ["feat-dev"]

    def test_mention_in_code_block_still_matches(self):
        # We parse naively — code blocks are not excluded.
        # This is acceptable: users rarely put @mentions in code blocks.
        text = "```\n@pm do something\n```"
        assert parse_mentions(text, self.KNOWN_ROLES) == ["pm"]

    def test_mention_with_punctuation_after(self):
        text = "@pm, can you look at this? And @feat-dev."
        assert parse_mentions(text, self.KNOWN_ROLES) == ["pm", "feat-dev"]

    def test_email_address_not_matched(self):
        # user@pm should NOT match (no word boundary before @)
        text = "Email user@pm.com for details"
        assert parse_mentions(text, self.KNOWN_ROLES) == []


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
        manager = AgentManager(
            config=config,
            registry=MagicMock(),
            github=MagicMock(),
            router=MagicMock(),
            agent_definitions={},
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
                        "body": "Hey @pm what's up?",
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
                        "body": "**[squadron:pm]** Triage complete.",
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
                        "body": "**[squadron:feat-dev]** Working on implementation.",
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


# ── Mention routing integration tests ────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_mention.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _mention_config() -> SquadronConfig:
    """Config for mention routing tests."""
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


@pytest.mark.asyncio
class TestMentionRouting:
    """Integration tests for mention-based routing through EventRouter → AgentManager."""

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_comment_with_pm_mention_spawns_pm(self, mock_copilot_cls, registry, tmp_path):
        """Human @pm mention spawns an ephemeral PM agent."""
        config = _mention_config()
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
            agent_definitions={
                "pm": MagicMock(prompt="test prompt", raw_content="test", tools=None)
            },
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Route a comment event with @pm mention
        event = _comment_event("@pm please triage this issue", issue_number=10)
        await router._route_event(event)

        # PM agent should be created
        agents = await registry.get_agents_for_issue(10)
        pm_agents = [a for a in agents if a.role == "pm"]
        assert len(pm_agents) == 1
        assert pm_agents[0].status in (AgentStatus.ACTIVE, AgentStatus.COMPLETED)

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_comment_without_mention_ignored(self, mock_copilot_cls, registry, tmp_path):
        """Comment with no role mentions does not spawn any agents."""
        config = _mention_config()
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

        event = _comment_event("Just a regular comment, no mentions", issue_number=10)
        await router._route_event(event)

        agents = await registry.get_agents_for_issue(10)
        assert len(agents) == 0

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_self_loop_guard_blocks_pm_retriggering_itself(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """PM comment mentioning @pm does NOT re-trigger PM (self-loop guard)."""
        config = _mention_config()
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

        # Simulate bot-authored comment from PM role
        event = _comment_event(
            body="**[squadron:pm]** Triage complete. @pm should follow up.",
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
    async def test_pm_mentioning_feat_dev_spawns_feat_dev(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """PM comment mentioning @feat-dev DOES spawn feat-dev (cross-role allowed)."""
        config = _mention_config()
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
            agent_definitions={
                "feat-dev": MagicMock(prompt="test", raw_content="test", tools=None)
            },
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # PM posts comment mentioning @feat-dev
        event = _comment_event(
            body="**[squadron:pm]** @feat-dev please implement the feature described above.",
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
    async def test_mention_wakes_sleeping_persistent_agent(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """Mentioning @feat-dev wakes a sleeping feat-dev agent for the same issue."""
        config = _mention_config()
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
            agent_definitions={
                "feat-dev": MagicMock(prompt="test", raw_content="test", tools=None)
            },
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

        # Human mentions @feat-dev
        event = _comment_event(
            body="@feat-dev please continue working on this",
            issue_number=10,
        )
        await router._route_event(event)

        # Agent should be woken (ACTIVE)
        agent = await registry.get_agent("feat-dev-issue-10")
        assert agent is not None
        assert agent.status == AgentStatus.ACTIVE

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_mention_delivers_to_active_agent_inbox(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """Mentioning @feat-dev when it's ACTIVE delivers event to its inbox."""
        config = _mention_config()
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

        # Human mentions @feat-dev
        event = _comment_event(
            body="@feat-dev can you check the test results?",
            issue_number=10,
        )
        await router._route_event(event)

        # Event should be in the inbox
        inbox = manager.agent_inboxes["feat-dev-issue-10"]
        assert not inbox.empty()
        queued_event = await inbox.get()
        assert queued_event.mentioned_roles == ["feat-dev"]

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_mention_spawns_new_persistent_agent_if_none_exists(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """Mentioning @feat-dev when no agent exists for the issue spawns a new one."""
        config = _mention_config()
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
            agent_definitions={
                "feat-dev": MagicMock(prompt="test", raw_content="test", tools=None)
            },
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _comment_event(
            body="@feat-dev can you implement this?",
            issue_number=10,
        )
        await router._route_event(event)

        agents = await registry.get_agents_for_issue(10)
        feat_agents = [a for a in agents if a.role == "feat-dev"]
        assert len(feat_agents) == 1

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_slash_mention_works_same_as_at(self, mock_copilot_cls, registry, tmp_path):
        """/pm mention works identically to @pm."""
        config = _mention_config()
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
            agent_definitions={"pm": MagicMock(prompt="test", raw_content="test", tools=None)},
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _comment_event("/pm triage this please", issue_number=10)
        await router._route_event(event)

        agents = await registry.get_agents_for_issue(10)
        pm_agents = [a for a in agents if a.role == "pm"]
        assert len(pm_agents) == 1

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_multiple_mentions_spawn_multiple_agents(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """Comment mentioning @pm and @feat-dev spawns both agents."""
        config = _mention_config()
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
            agent_definitions={
                "pm": MagicMock(prompt="test", raw_content="test", tools=None),
                "feat-dev": MagicMock(prompt="test", raw_content="test", tools=None),
            },
            repo_root=Path(tmp_path),
        )
        await manager.start()

        event = _comment_event(
            body="@pm please triage and @feat-dev start work",
            issue_number=10,
        )
        await router._route_event(event)

        # Give background agent tasks a moment to start, then stop cleanly
        await asyncio.sleep(0.1)
        await manager.stop()

        # Check both active AND completed agents (ephemeral PM may have
        # already finished its one-shot run before we check)
        active = await registry.get_agents_for_issue(10)
        recent = await registry.get_recent_agents(limit=10)
        roles = {a.role for a in active} | {a.role for a in recent if a.issue_number == 10}
        assert "pm" in roles
        assert "feat-dev" in roles

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_singleton_guard_prevents_duplicate_ephemeral(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """Singleton PM: second @pm mention while first PM is active is blocked."""
        config = _mention_config()
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

        # Another @pm mention should be blocked by singleton guard
        event = _comment_event("@pm triage this too", issue_number=10)
        await router._route_event(event)

        # Only the original PM agent should exist
        all_agents = await registry.get_all_active_agents()
        pm_agents = [a for a in all_agents if a.role == "pm"]
        assert len(pm_agents) == 1
        assert pm_agents[0].agent_id == "pm-issue-5-12345"

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_mention_respawns_after_completed_agent(
        self, mock_copilot_cls, registry, tmp_path
    ):
        """Issue #13 regression: mentioning @feat-dev when a COMPLETED agent exists
        for the same role+issue should clean up the stale record and spawn fresh,
        not crash with sqlite3.IntegrityError."""
        config = _mention_config()
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
            agent_definitions={
                "feat-dev": MagicMock(prompt="test", raw_content="test", tools=None)
            },
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

        # Verify the stale record is in the DB
        stale = await registry.get_agent("feat-dev-issue-12")
        assert stale is not None
        assert stale.status == AgentStatus.COMPLETED

        # Mention @feat-dev on the same issue — should NOT crash with IntegrityError
        event = _comment_event(
            body="@feat-dev please revisit this",
            issue_number=12,
        )
        await router._route_event(event)

        # New agent should be active
        agents = await registry.get_agents_for_issue(12)
        feat_agents = [a for a in agents if a.role == "feat-dev"]
        assert len(feat_agents) == 1
        assert feat_agents[0].status == AgentStatus.ACTIVE

        await manager.stop()

    @patch("squadron.agent_manager.CopilotAgent")
    async def test_mention_respawns_after_failed_agent(self, mock_copilot_cls, registry, tmp_path):
        """Similar to #13 but for FAILED status — stale record should be cleaned up."""
        config = _mention_config()
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
            agent_definitions={
                "feat-dev": MagicMock(prompt="test", raw_content="test", tools=None)
            },
            repo_root=Path(tmp_path),
        )
        await manager.start()

        # Pre-create a FAILED agent
        failed = AgentRecord(
            agent_id="feat-dev-issue-7",
            role="feat-dev",
            issue_number=7,
            session_id="squadron-feat-dev-issue-7",
            status=AgentStatus.FAILED,
        )
        await registry.create_agent(failed)

        # Mention should respawn cleanly
        event = _comment_event(
            body="@feat-dev try again please",
            issue_number=7,
        )
        await router._route_event(event)

        agents = await registry.get_agents_for_issue(7)
        feat_agents = [a for a in agents if a.role == "feat-dev"]
        assert len(feat_agents) == 1
        assert feat_agents[0].status == AgentStatus.ACTIVE

        await manager.stop()


# ── EventRouter mention parsing tests ────────────────────────────────────────


@pytest.mark.asyncio
class TestEventRouterMentionParsing:
    """Tests that EventRouter populates mentioned_roles on SquadronEvent."""

    async def test_comment_event_populates_mentioned_roles(self, registry):
        config = _mention_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        event = _comment_event("@pm please look at this @feat-dev", issue_number=10)
        internal_event = router._to_squadron_event(event, SquadronEventType.ISSUE_COMMENT)

        assert internal_event.mentioned_roles == ["pm", "feat-dev"]

    async def test_non_comment_event_has_empty_mentions(self, registry):
        config = _mention_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        event = GitHubEvent(
            delivery_id="d-1",
            event_type="issues",
            action="opened",
            payload={"issue": {"number": 10}},
        )
        internal_event = router._to_squadron_event(event, SquadronEventType.ISSUE_OPENED)

        assert internal_event.mentioned_roles == []

    async def test_comment_with_unknown_roles_ignored(self, registry):
        config = _mention_config()
        event_queue = asyncio.Queue()
        router = EventRouter(event_queue, registry, config)

        event = _comment_event("@unknown-agent please help", issue_number=10)
        internal_event = router._to_squadron_event(event, SquadronEventType.ISSUE_COMMENT)

        assert internal_event.mentioned_roles == []
