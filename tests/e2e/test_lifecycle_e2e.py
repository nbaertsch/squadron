"""E2E lifecycle tests: webhook → event router → pipeline → agent → state machine.

Exercises the full Squadron lifecycle with real components:
  - Real AgentRegistry (SQLite)
  - Real EventRouter (async consumer loop)
  - Real PipelineEngine (trigger matching, stage execution)
  - Real AgentManager (create_agent, _run_agent, post-turn state machine)
  - Mock CopilotAgent/Session (no live LLM)
  - Mock GitHubClient (no real API calls)
  - Mock git operations (no real worktrees)

These tests do NOT require any credentials.  They validate that the
entire event-driven pipeline works end-to-end, from webhook event
ingestion through agent completion and resource cleanup.

Run::

    pytest tests/e2e/test_lifecycle_e2e.py -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    AgentRecord,
    AgentStatus,
    GitHubEvent,
    SquadronEvent,
    SquadronEventType,
)
from squadron.pipeline.engine import PipelineEngine
from squadron.pipeline.models import (
    PipelineDefinition,
    StageDefinition,
    TriggerDefinition,
)
from squadron.pipeline.gates import GateCheckRegistry
from squadron.pipeline.registry import PipelineRegistry
from squadron.registry import AgentRegistry

import aiosqlite


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def agent_registry(tmp_path):
    """Real SQLite-backed agent registry."""
    db_path = str(tmp_path / "lifecycle_e2e.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
    """Real SQLite connection for pipeline registry."""
    db_path = tmp_path / "pipeline_e2e.db"
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
    """Real gate check registry (no custom checks needed for these tests)."""
    return GateCheckRegistry()


@pytest.fixture
def squadron_config():
    """Realistic SquadronConfig for lifecycle tests."""
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
            "feat-dev": AgentRoleConfig(
                agent_definition="agents/feat-dev.md",
            ),
            "reviewer": AgentRoleConfig(
                agent_definition="agents/reviewer.md",
            ),
        },
        sandbox={"enabled": False},  # sandbox disabled for lifecycle tests
    )


@pytest.fixture
def agent_definitions():
    """Agent definitions for the test agents."""
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
    """Mock GitHubClient."""
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
    """Create a realistic issues.opened webhook event."""
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


def _make_pr_opened_event(pr_number: int = 10, issue_number: int = 42) -> GitHubEvent:
    """Create a realistic pull_request.opened webhook event."""
    return GitHubEvent(
        delivery_id=f"delivery-pr-{pr_number}",
        event_type="pull_request",
        action="opened",
        payload={
            "action": "opened",
            "pull_request": {
                "number": pr_number,
                "title": "Fix issue #42",
                "body": f"Closes #{issue_number}",
                "head": {"ref": f"feat-dev-issue-{issue_number}"},
                "base": {"ref": "main"},
                "state": "open",
                "user": {"login": "squadron-dev[bot]"},
            },
            "sender": {"login": "squadron-dev[bot]", "type": "Bot"},
            "repository": {"full_name": "testowner/testrepo"},
        },
    )


# ── Test 1: Event → Pipeline → Agent Spawn ──────────────────────────────────


class TestEventToAgentSpawn:
    """Verify the full path from webhook event through pipeline to agent creation."""

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
        """An issues.opened event should trigger a pipeline that spawns a feat-dev agent."""
        event_queue: asyncio.Queue[GitHubEvent] = asyncio.Queue()
        router = EventRouter(event_queue, agent_registry, squadron_config)

        # Build pipeline engine with a simple pipeline: issues.opened → spawn feat-dev
        engine = PipelineEngine(
            registry=pipeline_registry,
            gate_registry=gate_registry,
            owner="testowner",
            repo="testrepo",
        )

        pipeline_def = PipelineDefinition(
            description="Auto-assign feat-dev on issue open",
            trigger=TriggerDefinition(event="issues.opened"),
            stages=[
                StageDefinition(id="develop", type="agent", agent="feat-dev"),
            ],
        )
        engine.add_pipeline("auto-dev", pipeline_def)

        # Build AgentManager with mocked externals
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

        # Track spawned agents via the spawn callback
        spawned_agents: list[str] = []
        original_spawn = manager.spawn_pipeline_agent

        async def tracking_spawn(*args, **kwargs):
            result = await original_spawn(*args, **kwargs)
            if result:
                spawned_agents.append(result)
            return result

        engine.set_spawn_callback(tracking_spawn)

        # Mock CopilotAgent creation and git operations
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

            # Start the system
            await manager.start()
            await router.start()

            # Enqueue the webhook event
            await event_queue.put(_make_issue_opened_event(42))

            # Wait for event processing and agent creation
            # The chain is: event_queue → router → pipeline engine → spawn_pipeline_agent → create_agent → _run_agent
            await asyncio.sleep(0.5)

            # Stop cleanly
            await router.stop()

        # Verify: pipeline triggered and agent was spawned
        assert len(spawned_agents) == 1, f"Expected 1 spawned agent, got {spawned_agents}"
        agent_id = spawned_agents[0]
        assert "feat-dev" in agent_id

        # Verify: agent exists in registry
        agent = await agent_registry.get_agent(agent_id)
        assert agent is not None, f"Agent {agent_id} not found in registry"
        assert agent.role == "feat-dev"
        assert agent.issue_number == 42

        # Verify: CopilotAgent was started and session was created
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

        # Only trigger on issues.opened
        pipeline_def = PipelineDefinition(
            description="Auto-assign feat-dev on issue open",
            trigger=TriggerDefinition(event="issues.opened"),
            stages=[
                StageDefinition(id="develop", type="agent", agent="feat-dev"),
            ],
        )
        engine.add_pipeline("auto-dev", pipeline_def)

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

        async def tracking_spawn(*args, **kwargs):
            spawned_agents.append("spawned")
            return None

        engine.set_spawn_callback(tracking_spawn)

        with patch("squadron.agent_manager.CopilotAgent"):
            await manager.start()
            await router.start()

            # Send a push event (not issues.opened)
            push_event = GitHubEvent(
                delivery_id="delivery-push-1",
                event_type="push",
                action=None,
                payload={
                    "ref": "refs/heads/main",
                    "sender": {"login": "testuser", "type": "User"},
                    "repository": {"full_name": "testowner/testrepo"},
                },
            )
            await event_queue.put(push_event)
            await asyncio.sleep(0.3)
            await router.stop()

        # No pipeline should have triggered
        assert len(spawned_agents) == 0


# ── Test 2: Agent State Machine (ACTIVE → SLEEPING → ACTIVE → COMPLETED) ───


class TestAgentStateMachine:
    """Verify agent state transitions through the full lifecycle."""

    def _build_manager(self, config, registry, github, router, agent_definitions, tmp_path):
        from squadron.agent_manager import AgentManager

        return AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions=agent_definitions,
            repo_root=tmp_path,
        )

    async def test_agent_completes_and_cleans_up(
        self,
        agent_registry: AgentRegistry,
        squadron_config: SquadronConfig,
        agent_definitions: dict,
        github_mock,
        tmp_path: Path,
    ):
        """Agent that calls report_complete transitions to COMPLETED and resources are freed."""
        router = MagicMock()
        manager = self._build_manager(
            squadron_config,
            agent_registry,
            github_mock,
            router,
            agent_definitions,
            tmp_path,
        )

        # Create agent in registry
        agent = AgentRecord(
            agent_id="feat-dev-issue-42",
            role="feat-dev",
            issue_number=42,
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
            session_id="squadron-feat-dev-issue-42",
        )
        await agent_registry.create_agent(agent)

        # Set up mock CopilotAgent
        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        mock_copilot.delete_session = AsyncMock()
        mock_copilot.stop = AsyncMock()
        manager._copilot_agents[agent.agent_id] = mock_copilot
        manager.agent_inboxes[agent.agent_id] = asyncio.Queue()

        # Simulate agent calling report_complete during its turn
        async def side_effect_complete(*args, **kwargs):
            a = await agent_registry.get_agent(agent.agent_id)
            a.status = AgentStatus.COMPLETED
            a.active_since = None
            await agent_registry.update_agent(a)
            return mock_result

        mock_session.send_and_wait = AsyncMock(side_effect=side_effect_complete)

        # Track the task
        manager._agent_tasks[agent.agent_id] = MagicMock()

        # Run the agent
        await manager._run_agent(agent, trigger_event=None, resume=False)

        # Verify: agent is COMPLETED in registry
        persisted = await agent_registry.get_agent(agent.agent_id)
        assert persisted.status == AgentStatus.COMPLETED

        # Verify: resources cleaned up
        assert agent.agent_id not in manager._copilot_agents
        assert agent.agent_id not in manager._agent_tasks
        assert agent.agent_id not in manager.agent_inboxes

    async def test_agent_blocks_and_sleeps(
        self,
        agent_registry: AgentRegistry,
        squadron_config: SquadronConfig,
        agent_definitions: dict,
        github_mock,
        tmp_path: Path,
    ):
        """Agent that calls report_blocked transitions to SLEEPING, CopilotClient is stopped."""
        router = MagicMock()
        manager = self._build_manager(
            squadron_config,
            agent_registry,
            github_mock,
            router,
            agent_definitions,
            tmp_path,
        )

        agent = AgentRecord(
            agent_id="feat-dev-issue-42",
            role="feat-dev",
            issue_number=42,
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await agent_registry.create_agent(agent)

        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        manager._copilot_agents[agent.agent_id] = mock_copilot
        manager._agent_tasks[agent.agent_id] = MagicMock()

        # Simulate agent calling report_blocked during its turn
        async def side_effect_block(*args, **kwargs):
            a = await agent_registry.get_agent(agent.agent_id)
            a.status = AgentStatus.SLEEPING
            a.sleeping_since = datetime.now(timezone.utc)
            a.active_since = None
            a.blocked_by = [99]
            await agent_registry.update_agent(a)
            return mock_result

        mock_session.send_and_wait = AsyncMock(side_effect=side_effect_block)

        await manager._run_agent(agent, trigger_event=None, resume=False)

        # Verify: agent is SLEEPING in registry
        persisted = await agent_registry.get_agent(agent.agent_id)
        assert persisted.status == AgentStatus.SLEEPING
        assert persisted.sleeping_since is not None
        assert persisted.active_since is None
        assert 99 in persisted.blocked_by

        # Verify: CopilotClient stopped and removed (issue #103)
        assert agent.agent_id not in manager._copilot_agents
        mock_copilot.stop.assert_called_once()

        # Verify: task removed
        assert agent.agent_id not in manager._agent_tasks

    async def test_exception_escalates_agent(
        self,
        agent_registry: AgentRegistry,
        squadron_config: SquadronConfig,
        agent_definitions: dict,
        github_mock,
        tmp_path: Path,
    ):
        """Unhandled exception during agent turn transitions to ESCALATED."""
        router = MagicMock()
        manager = self._build_manager(
            squadron_config,
            agent_registry,
            github_mock,
            router,
            agent_definitions,
            tmp_path,
        )

        agent = AgentRecord(
            agent_id="feat-dev-issue-42",
            role="feat-dev",
            issue_number=42,
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
            session_id="squadron-feat-dev-issue-42",
        )
        await agent_registry.create_agent(agent)

        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_session.send_and_wait = AsyncMock(side_effect=RuntimeError("SDK crash"))
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        mock_copilot.delete_session = AsyncMock()
        mock_copilot.stop = AsyncMock()
        manager._copilot_agents[agent.agent_id] = mock_copilot

        await manager._run_agent(agent, trigger_event=None, resume=False)

        # Verify: agent is ESCALATED
        persisted = await agent_registry.get_agent(agent.agent_id)
        assert persisted.status == AgentStatus.ESCALATED

        # Verify: cleanup happened
        assert agent.agent_id not in manager._copilot_agents

    async def test_turn_count_incremented_on_normal_completion(
        self,
        agent_registry: AgentRegistry,
        squadron_config: SquadronConfig,
        agent_definitions: dict,
        github_mock,
        tmp_path: Path,
    ):
        """turn_count should be incremented after a successful turn."""
        router = MagicMock()
        manager = self._build_manager(
            squadron_config,
            agent_registry,
            github_mock,
            router,
            agent_definitions,
            tmp_path,
        )

        agent = AgentRecord(
            agent_id="feat-dev-issue-42",
            role="feat-dev",
            issue_number=42,
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
            turn_count=0,
        )
        await agent_registry.create_agent(agent)

        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"
        mock_session.send_and_wait = AsyncMock(return_value=mock_result)
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        manager._copilot_agents[agent.agent_id] = mock_copilot

        await manager._run_agent(agent, trigger_event=None, resume=False)

        persisted = await agent_registry.get_agent(agent.agent_id)
        assert persisted.turn_count == 1


# ── Test 3: Event Router Integration ────────────────────────────────────────


class TestEventRouterIntegration:
    """Verify the event router correctly dispatches events to handlers."""

    async def test_event_routed_to_registered_handler(
        self,
        agent_registry: AgentRegistry,
        squadron_config: SquadronConfig,
    ):
        """Events matching registered handlers should be dispatched."""
        event_queue: asyncio.Queue[GitHubEvent] = asyncio.Queue()
        router = EventRouter(event_queue, agent_registry, squadron_config)

        received_events: list[SquadronEvent] = []

        async def handler(event: SquadronEvent):
            received_events.append(event)

        router.on(SquadronEventType.ISSUE_OPENED, handler)

        await router.start()
        await event_queue.put(_make_issue_opened_event(42))
        await asyncio.sleep(0.2)
        await router.stop()

        assert len(received_events) == 1
        assert received_events[0].event_type == SquadronEventType.ISSUE_OPENED
        assert received_events[0].issue_number == 42

    async def test_duplicate_event_filtered(
        self,
        agent_registry: AgentRegistry,
        squadron_config: SquadronConfig,
    ):
        """Duplicate events (same delivery_id) should be filtered."""
        event_queue: asyncio.Queue[GitHubEvent] = asyncio.Queue()
        router = EventRouter(event_queue, agent_registry, squadron_config)

        received_events: list[SquadronEvent] = []

        async def handler(event: SquadronEvent):
            received_events.append(event)

        router.on(SquadronEventType.ISSUE_OPENED, handler)

        await router.start()

        # Send the same event twice
        event = _make_issue_opened_event(42)
        await event_queue.put(event)
        await asyncio.sleep(0.2)
        await event_queue.put(event)  # duplicate
        await asyncio.sleep(0.2)

        await router.stop()

        # Only one should have been dispatched
        assert len(received_events) == 1

    async def test_bot_events_still_routed(
        self,
        agent_registry: AgentRegistry,
        squadron_config: SquadronConfig,
    ):
        """Bot-originated events should still be routed (self-loop is AgentManager's job)."""
        event_queue: asyncio.Queue[GitHubEvent] = asyncio.Queue()
        router = EventRouter(event_queue, agent_registry, squadron_config)

        received_events: list[SquadronEvent] = []

        async def handler(event: SquadronEvent):
            received_events.append(event)

        router.on(SquadronEventType.PR_OPENED, handler)

        await router.start()
        await event_queue.put(_make_pr_opened_event(10, 42))
        await asyncio.sleep(0.2)
        await router.stop()

        assert len(received_events) == 1
        assert received_events[0].pr_number == 10


# ── Test 4: Pipeline Engine Stage Execution ──────────────────────────────────


class TestPipelineStageExecution:
    """Verify pipeline stages execute in sequence and spawn agents."""

    async def test_two_stage_pipeline_executes_both_stages(
        self,
        pipeline_registry: PipelineRegistry,
        gate_registry: GateCheckRegistry,
    ):
        """A two-stage pipeline (agent → action) should execute both stages."""
        engine = PipelineEngine(
            registry=pipeline_registry,
            gate_registry=gate_registry,
            owner="testowner",
            repo="testrepo",
        )

        pipeline_def = PipelineDefinition(
            description="Two-stage pipeline",
            trigger=TriggerDefinition(event="issues.opened"),
            stages=[
                StageDefinition(id="develop", type="agent", agent="feat-dev"),
                StageDefinition(id="notify", type="action", action="comment_on_issue"),
            ],
        )
        engine.add_pipeline("two-stage", pipeline_def)

        # Track spawn and action calls
        spawned: list[dict] = []
        actions: list[dict] = []

        async def spawn_cb(role, issue_number, **kwargs):
            spawned.append({"role": role, "issue_number": issue_number, **kwargs})
            return f"{role}-issue-{issue_number}"

        async def action_cb(action: str, config: dict, context) -> dict:
            actions.append({"action": action, **config})
            return {"success": True}

        engine.set_spawn_callback(spawn_cb)
        engine.set_action_callback(action_cb)

        # Simulate issues.opened event
        payload = {
            "action": "opened",
            "issue": {"number": 42, "title": "Test", "labels": []},
            "sender": {"login": "testuser", "type": "User"},
            "repository": {"full_name": "testowner/testrepo"},
        }
        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_OPENED,
            source_delivery_id="delivery-42",
            issue_number=42,
            data={"action": "opened", "payload": payload, "sender": "testuser"},
        )

        run = await engine.evaluate_event("issues.opened", payload, event)
        assert run is not None, "Pipeline should have triggered"

        # First stage (agent) should have been executed
        assert len(spawned) == 1
        assert spawned[0]["role"] == "feat-dev"
        assert spawned[0]["issue_number"] == 42

        # Simulate agent completion to advance pipeline
        agent_id = "feat-dev-issue-42"
        await engine.on_agent_complete(agent_id)

        # Second stage (action) should have been executed
        assert len(actions) == 1
        assert actions[0]["action"] == "comment_on_issue"


# ── Test 5: Full Lifecycle Round-Trip ────────────────────────────────────────


class TestFullLifecycleRoundTrip:
    """End-to-end: webhook event → pipeline → agent spawn → agent runs → completes → cleanup."""

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
        """Full round-trip: issue.opened → pipeline → spawn → run → report_complete → cleanup."""
        event_queue: asyncio.Queue[GitHubEvent] = asyncio.Queue()
        router = EventRouter(event_queue, agent_registry, squadron_config)

        engine = PipelineEngine(
            registry=pipeline_registry,
            gate_registry=gate_registry,
            owner="testowner",
            repo="testrepo",
        )

        pipeline_def = PipelineDefinition(
            description="Auto-assign feat-dev",
            trigger=TriggerDefinition(event="issues.opened"),
            stages=[
                StageDefinition(id="develop", type="agent", agent="feat-dev"),
            ],
        )
        engine.add_pipeline("auto-dev", pipeline_def)

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

        # Mock Copilot: agent calls report_complete during its turn
        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"

        async def complete_during_turn(*args, **kwargs):
            # Find the agent that was just created
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

            # Inject the webhook event
            await event_queue.put(_make_issue_opened_event(42))

            # Wait for the full chain to complete
            # (event → router → pipeline → spawn → _run_agent → post-turn state machine)
            for _ in range(20):
                await asyncio.sleep(0.1)
                # Check if agent has reached terminal state
                agents = await agent_registry.get_all_active_agents()
                feat_dev_agents = [a for a in agents if a.role == "feat-dev"]
                if not feat_dev_agents:
                    # Agent completed and was cleaned up (or check terminal states)
                    break

            await router.stop()

        # Verify: agent was created, ran, completed, and cleaned up
        # The agent should still be in the registry as COMPLETED (not deleted)
        all_agents = []
        # get_all_agents_for_issue includes terminal states
        all_agents = await agent_registry.get_all_agents_for_issue(42)
        feat_dev_agents = [a for a in all_agents if a.role == "feat-dev"]

        assert len(feat_dev_agents) >= 1, "Agent should exist in registry"
        completed = [a for a in feat_dev_agents if a.status == AgentStatus.COMPLETED]
        assert len(completed) == 1, (
            f"Expected 1 COMPLETED agent, got statuses: {[a.status for a in feat_dev_agents]}"
        )

        # Verify: CopilotAgent was started and session was created
        mock_copilot.start.assert_called_once()
        mock_copilot.create_session.assert_called_once()

        # Verify: Cleanup happened (session destroyed, copilot stopped)
        mock_copilot.delete_session.assert_called_once()
