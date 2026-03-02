"""Integration tests for the event-driven lifecycle pipeline.

These tests wire up real EventRouter + PipelineEngine + AgentManager with a
mocked CopilotAgent to verify the full event-driven pipeline chain:
    webhook → router → pipeline trigger → agent spawn → run → cleanup.

The only mocks are the Copilot SDK subprocess, git worktree creation, and
the GitHub API client.  Everything else (SQLite registries, pipeline engine,
event routing) is real.

Previously in tests/e2e/test_lifecycle_e2e.py — moved here because these are
integration tests (mocked SDK), not E2E tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio

from squadron.config import (
    AgentDefinition,
    AgentRoleConfig,
    CircuitBreakerConfig,
    LabelsConfig,
    ProjectConfig,
    ProviderConfig,
    RuntimeConfig,
    SkillsConfig,
    SquadronConfig,
)
from squadron.event_router import EventRouter
from squadron.models import (
    AgentStatus,
    GitHubEvent,
)
from squadron.pipeline.engine import PipelineEngine
from squadron.pipeline.gates import GateCheckRegistry
from squadron.pipeline.models import (
    PipelineDefinition,
    StageDefinition,
    TriggerDefinition,
)
from squadron.pipeline.registry import PipelineRegistry
from squadron.registry import AgentRegistry


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def agent_registry(tmp_path):
    """Real SQLite-backed agent registry."""
    db_path = str(tmp_path / "lifecycle_integration.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
    """Real SQLite connection for pipeline registry."""
    db_path = tmp_path / "pipeline_integration.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


@pytest_asyncio.fixture
async def pipeline_registry(pipeline_db):
    """Real pipeline registry."""
    reg = PipelineRegistry(pipeline_db)
    await reg.initialize()
    return reg


@pytest.fixture
def gate_registry():
    return GateCheckRegistry()


@pytest.fixture
def squadron_config():
    return SquadronConfig(
        project=ProjectConfig(name="test-project", owner="testowner", repo="testrepo"),
        runtime=RuntimeConfig(
            provider=ProviderConfig(type="copilot"),
            max_concurrent_agents=5,
        ),
        circuit_breakers=CircuitBreakerConfig(),
        labels=LabelsConfig(),
        skills=SkillsConfig(),
        agent_roles={
            "feat-dev": AgentRoleConfig(agent_definition="agents/feat-dev.md"),
            "reviewer": AgentRoleConfig(agent_definition="agents/reviewer.md"),
        },
        sandbox={"enabled": False},
    )


@pytest.fixture
def agent_definitions():
    return {
        "feat-dev": AgentDefinition(
            role="feat-dev",
            raw_content="---\nname: feat-dev\n---\nYou are a feature dev agent.",
            prompt="You are a feature dev agent.",
            name="feat-dev",
            description="Feature development",
            tools=["read_file", "write_file", "report_complete", "report_blocked"],
        ),
        "reviewer": AgentDefinition(
            role="reviewer",
            raw_content="---\nname: reviewer\n---\nYou are a code review agent.",
            prompt="You are a code review agent.",
            name="reviewer",
            description="Code review",
            tools=["read_file", "report_complete"],
        ),
    }


@pytest.fixture
def github_mock():
    github = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.create_issue = AsyncMock(return_value={"number": 99})
    github.get_open_prs_for_branch = AsyncMock(return_value=[])
    github.search_prs = AsyncMock(return_value=[])
    github.get_issue = AsyncMock(
        return_value={
            "number": 42,
            "title": "Add new feature",
            "body": "Please add the foo feature",
            "labels": [],
            "state": "open",
            "user": {"login": "testuser"},
        }
    )
    return github


def _make_issue_opened_event(issue_number: int = 42) -> GitHubEvent:
    return GitHubEvent(
        delivery_id=f"delivery-issue-{issue_number}",
        event_type="issues",
        action="opened",
        payload={
            "action": "opened",
            "issue": {
                "number": issue_number,
                "title": "Add new feature",
                "body": "Please implement the foo feature",
                "labels": [{"name": "feat-dev"}],
                "state": "open",
                "user": {"login": "testuser"},
            },
            "sender": {"login": "testuser", "type": "User"},
            "repository": {"full_name": "testowner/testrepo"},
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# Integration tests — no credentials required, mocked SDK
# ═════════════════════════════════════════════════════════════════════════════


class TestEventToAgentSpawn:
    """Webhook event → EventRouter → PipelineEngine → AgentManager.spawn.

    Wires up real EventRouter, PipelineEngine, and AgentManager with a
    mocked CopilotAgent.  The only mocks are the Copilot SDK subprocess,
    git worktree creation, and the GitHub API client.
    """

    async def test_issue_opened_triggers_pipeline_and_spawns_agent(
        self,
        agent_registry: AgentRegistry,
        pipeline_registry: PipelineRegistry,
        gate_registry: GateCheckRegistry,
        squadron_config: SquadronConfig,
        agent_definitions: dict,
        github_mock,
        tmp_path: Path,
    ):
        """issues.opened event triggers pipeline, spawns feat-dev agent."""
        event_queue: asyncio.Queue[GitHubEvent] = asyncio.Queue()
        router = EventRouter(event_queue, agent_registry, squadron_config)

        engine = PipelineEngine(
            registry=pipeline_registry,
            gate_registry=gate_registry,
            owner="testowner",
            repo="testrepo",
        )
        engine.add_pipeline(
            "auto-dev",
            PipelineDefinition(
                description="Auto-assign feat-dev on issue open",
                trigger=TriggerDefinition(event="issues.opened"),
                stages=[StageDefinition(id="develop", type="agent", agent="feat-dev")],
            ),
        )

        from squadron.agent_manager import AgentManager

        manager = AgentManager(
            config=squadron_config,
            registry=agent_registry,
            github=github_mock,
            router=router,
            agent_definitions=agent_definitions,
            repo_root=tmp_path,
        )
        manager.set_pipeline_engine(engine)

        spawned_agents: list[str] = []
        original_spawn = manager.spawn_pipeline_agent

        async def tracking_spawn(*args, **kwargs):
            result = await original_spawn(*args, **kwargs)
            if result:
                spawned_agents.append(result)
            return result

        engine.set_spawn_callback(tracking_spawn)

        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"
        mock_session.send_and_wait = AsyncMock(return_value=mock_result)
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        mock_copilot.start = AsyncMock()
        mock_copilot.stop = AsyncMock()

        with (
            patch("squadron.agent_manager.CopilotAgent", return_value=mock_copilot),
            patch.object(manager, "_create_worktree", new_callable=AsyncMock) as mock_wt,
            patch.object(manager, "_find_existing_pr_for_issue", new_callable=AsyncMock) as mock_pr,
        ):
            mock_wt.return_value = tmp_path / "worktrees" / "feat-dev-issue-42"
            mock_pr.return_value = None

            await manager.start()
            await router.start()
            await event_queue.put(_make_issue_opened_event(42))
            await asyncio.sleep(0.5)
            await router.stop()

        assert len(spawned_agents) == 1, f"Expected 1 spawned agent, got {spawned_agents}"
        agent_id = spawned_agents[0]
        assert "feat-dev" in agent_id

        agent = await agent_registry.get_agent(agent_id)
        assert agent is not None
        assert agent.role == "feat-dev"
        assert agent.issue_number == 42
        mock_copilot.start.assert_called_once()
        mock_copilot.create_session.assert_called_once()

    async def test_unmatched_event_does_not_spawn_agent(
        self,
        agent_registry: AgentRegistry,
        pipeline_registry: PipelineRegistry,
        gate_registry: GateCheckRegistry,
        squadron_config: SquadronConfig,
        agent_definitions: dict,
        github_mock,
        tmp_path: Path,
    ):
        """A push event should NOT trigger a pipeline configured for issues.opened."""
        event_queue: asyncio.Queue[GitHubEvent] = asyncio.Queue()
        router = EventRouter(event_queue, agent_registry, squadron_config)

        engine = PipelineEngine(
            registry=pipeline_registry,
            gate_registry=gate_registry,
            owner="testowner",
            repo="testrepo",
        )
        engine.add_pipeline(
            "auto-dev",
            PipelineDefinition(
                description="issues.opened only",
                trigger=TriggerDefinition(event="issues.opened"),
                stages=[StageDefinition(id="develop", type="agent", agent="feat-dev")],
            ),
        )

        from squadron.agent_manager import AgentManager

        manager = AgentManager(
            config=squadron_config,
            registry=agent_registry,
            github=github_mock,
            router=router,
            agent_definitions=agent_definitions,
            repo_root=tmp_path,
        )
        manager.set_pipeline_engine(engine)

        spawned: list[str] = []

        async def tracking_spawn(*args, **kwargs):
            spawned.append("spawned")
            return None

        engine.set_spawn_callback(tracking_spawn)

        with patch("squadron.agent_manager.CopilotAgent"):
            await manager.start()
            await router.start()

            await event_queue.put(
                GitHubEvent(
                    delivery_id="delivery-push-1",
                    event_type="push",
                    action=None,
                    payload={
                        "ref": "refs/heads/main",
                        "sender": {"login": "testuser", "type": "User"},
                        "repository": {"full_name": "testowner/testrepo"},
                    },
                )
            )
            await asyncio.sleep(0.3)
            await router.stop()

        assert len(spawned) == 0


class TestFullLifecycleRoundTrip:
    """Complete round-trip: webhook → pipeline → spawn → run → complete → cleanup."""

    async def test_issue_opened_to_agent_completion(
        self,
        agent_registry: AgentRegistry,
        pipeline_registry: PipelineRegistry,
        gate_registry: GateCheckRegistry,
        squadron_config: SquadronConfig,
        agent_definitions: dict,
        github_mock,
        tmp_path: Path,
    ):
        """Full chain: issue.opened → pipeline → spawn → _run_agent → report_complete → cleanup."""
        event_queue: asyncio.Queue[GitHubEvent] = asyncio.Queue()
        router = EventRouter(event_queue, agent_registry, squadron_config)

        engine = PipelineEngine(
            registry=pipeline_registry,
            gate_registry=gate_registry,
            owner="testowner",
            repo="testrepo",
        )
        engine.add_pipeline(
            "auto-dev",
            PipelineDefinition(
                description="Auto-assign feat-dev",
                trigger=TriggerDefinition(event="issues.opened"),
                stages=[StageDefinition(id="develop", type="agent", agent="feat-dev")],
            ),
        )

        from squadron.agent_manager import AgentManager

        manager = AgentManager(
            config=squadron_config,
            registry=agent_registry,
            github=github_mock,
            router=router,
            agent_definitions=agent_definitions,
            repo_root=tmp_path,
        )
        manager.set_pipeline_engine(engine)
        engine.set_spawn_callback(manager.spawn_pipeline_agent)

        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"

        async def complete_during_turn(*args, **kwargs):
            agents = await agent_registry.get_all_active_agents()
            for a in agents:
                if a.role == "feat-dev" and a.status == AgentStatus.ACTIVE:
                    a.status = AgentStatus.COMPLETED
                    a.active_since = None
                    await agent_registry.update_agent(a)
                    break
            return mock_result

        mock_session.send_and_wait = AsyncMock(side_effect=complete_during_turn)
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        mock_copilot.start = AsyncMock()
        mock_copilot.stop = AsyncMock()
        mock_copilot.delete_session = AsyncMock()

        with (
            patch("squadron.agent_manager.CopilotAgent", return_value=mock_copilot),
            patch.object(manager, "_create_worktree", new_callable=AsyncMock) as mock_wt,
            patch.object(manager, "_find_existing_pr_for_issue", new_callable=AsyncMock) as mock_pr,
        ):
            mock_wt.return_value = tmp_path / "worktrees" / "feat-dev-issue-42"
            mock_pr.return_value = None

            await manager.start()
            await router.start()
            await event_queue.put(_make_issue_opened_event(42))

            # Wait for the full chain to complete
            for _ in range(20):
                await asyncio.sleep(0.1)
                active = await agent_registry.get_all_active_agents()
                if not any(a.role == "feat-dev" for a in active):
                    break

            await router.stop()

        # Agent should be COMPLETED in registry
        all_agents = await agent_registry.get_all_agents_for_issue(42)
        feat_dev = [a for a in all_agents if a.role == "feat-dev"]
        assert len(feat_dev) >= 1, "Agent should exist in registry"

        completed = [a for a in feat_dev if a.status == AgentStatus.COMPLETED]
        assert len(completed) == 1, (
            f"Expected 1 COMPLETED agent, got: {[a.status for a in feat_dev]}"
        )

        mock_copilot.start.assert_called_once()
        mock_copilot.create_session.assert_called_once()
        mock_copilot.delete_session.assert_called_once()
