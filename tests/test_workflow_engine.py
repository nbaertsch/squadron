"""Tests for workflow engine — state machine and stage execution."""

import pytest
import pytest_asyncio
from datetime import datetime, timezone

import aiosqlite

from squadron.models import SquadronEvent, SquadronEventType
from squadron.config import (
    GateCondition,
    StageDefinition,
    StageType,
    WorkflowConfig,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTrigger,
)
from squadron.workflow.registry import WorkflowRegistryV2
from squadron.workflow.engine import WorkflowEngine


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_connection(tmp_path):
    """Create an in-memory database connection."""
    db_path = tmp_path / "test_workflow.db"
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        yield db


@pytest_asyncio.fixture
async def registry(db_connection):
    """Create and initialize a workflow registry."""
    reg = WorkflowRegistryV2(db_connection)
    await reg.initialize()
    return reg


@pytest.fixture
def simple_workflow() -> dict[str, WorkflowConfig]:
    """A simple two-stage workflow for testing."""
    return {
        "simple-flow": WorkflowConfig(
            trigger=WorkflowTrigger(
                event="issues.labeled",
                conditions={"label": "feature"},
            ),
            stages=[
                StageDefinition(
                    id="plan",
                    type=StageType.AGENT,
                    agent="architect",
                    on_complete="implement",
                ),
                StageDefinition(
                    id="implement",
                    type=StageType.AGENT,
                    agent="developer",
                    on_complete="complete",
                ),
            ],
        )
    }


@pytest.fixture
def gate_workflow() -> dict[str, WorkflowConfig]:
    """A workflow with a gate stage."""
    return {
        "gate-flow": WorkflowConfig(
            trigger=WorkflowTrigger(event="issues.opened"),
            stages=[
                StageDefinition(
                    id="develop",
                    type=StageType.AGENT,
                    agent="developer",
                ),
                StageDefinition(
                    id="quality-gate",
                    type=StageType.GATE,
                    conditions=[
                        GateCondition(check="command", run="pytest"),
                    ],
                    on_pass="deploy",
                    on_fail="develop",
                ),
                StageDefinition(
                    id="deploy",
                    type=StageType.AGENT,
                    agent="deployer",
                ),
            ],
        )
    }


@pytest.fixture
def loop_workflow() -> dict[str, WorkflowConfig]:
    """A workflow with iteration limits."""
    return {
        "loop-flow": WorkflowConfig(
            trigger=WorkflowTrigger(event="issues.opened"),
            stages=[
                StageDefinition(
                    id="attempt",
                    type=StageType.AGENT,
                    agent="worker",
                    on_complete={
                        "goto": "check",
                    },
                ),
                StageDefinition(
                    id="check",
                    type=StageType.GATE,
                    conditions=[
                        GateCondition(check="command", run="test"),
                    ],
                    on_pass="complete",
                    on_fail={
                        "goto": "attempt",
                        "max_iterations": 3,
                        "then": "escalate",
                    },
                ),
            ],
        )
    }


def make_issue_event(issue_number: int, label: str = "feature") -> SquadronEvent:
    """Create a SquadronEvent for an issue labeled event."""
    return SquadronEvent(
        event_type=SquadronEventType.ISSUE_LABELED,
        issue_number=issue_number,
        data={
            "action": "labeled",
            "sender": "user",
            "payload": {
                "label": {"name": label},
                "issue": {
                    "number": issue_number,
                    "title": "Test Issue",
                    "body": "Test body",
                    "labels": [{"name": label}],
                },
            },
        },
    )


# ── Registry Tests ───────────────────────────────────────────────────────────


class TestWorkflowRegistryV2:
    @pytest.mark.asyncio
    async def test_create_and_get_workflow_run(self, registry):
        run = WorkflowRun(
            run_id="wfv2-test-001",
            workflow_name="test-workflow",
            issue_number=42,
            status=WorkflowRunStatus.PENDING,
            current_stage_id="stage-1",
        )
        await registry.create_workflow_run(run)

        retrieved = await registry.get_workflow_run("wfv2-test-001")
        assert retrieved is not None
        assert retrieved.run_id == "wfv2-test-001"
        assert retrieved.workflow_name == "test-workflow"
        assert retrieved.issue_number == 42
        assert retrieved.status == WorkflowRunStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_workflow_runs_by_issue(self, registry):
        for i in range(3):
            run = WorkflowRun(
                run_id=f"wfv2-issue-{i}",
                workflow_name="test",
                issue_number=100,
            )
            await registry.create_workflow_run(run)

        runs = await registry.get_workflow_runs_by_issue(100)
        assert len(runs) == 3

    @pytest.mark.asyncio
    async def test_get_active_workflow_runs(self, registry):
        # Create active runs
        for i in range(2):
            run = WorkflowRun(
                run_id=f"wfv2-active-{i}",
                workflow_name="test",
                status=WorkflowRunStatus.RUNNING,
            )
            await registry.create_workflow_run(run)

        # Create completed run
        completed = WorkflowRun(
            run_id="wfv2-done",
            workflow_name="test",
            status=WorkflowRunStatus.COMPLETED,
        )
        await registry.create_workflow_run(completed)

        active = await registry.get_active_workflow_runs()
        assert len(active) == 2

    @pytest.mark.asyncio
    async def test_update_workflow_run(self, registry):
        run = WorkflowRun(
            run_id="wfv2-update",
            workflow_name="test",
            status=WorkflowRunStatus.PENDING,
        )
        await registry.create_workflow_run(run)

        run.status = WorkflowRunStatus.RUNNING
        run.current_stage_id = "stage-2"
        run.started_at = datetime.now(timezone.utc)
        await registry.update_workflow_run(run)

        retrieved = await registry.get_workflow_run("wfv2-update")
        assert retrieved.status == WorkflowRunStatus.RUNNING
        assert retrieved.current_stage_id == "stage-2"
        assert retrieved.started_at is not None

    @pytest.mark.asyncio
    async def test_get_workflow_run_by_name_and_issue(self, registry):
        run = WorkflowRun(
            run_id="wfv2-unique",
            workflow_name="feature-flow",
            issue_number=50,
            status=WorkflowRunStatus.RUNNING,
        )
        await registry.create_workflow_run(run)

        found = await registry.get_workflow_run_by_name_and_issue("feature-flow", 50)
        assert found is not None
        assert found.run_id == "wfv2-unique"

        not_found = await registry.get_workflow_run_by_name_and_issue("other-flow", 50)
        assert not_found is None

    @pytest.mark.asyncio
    async def test_delete_workflow_run(self, registry):
        run = WorkflowRun(
            run_id="wfv2-delete",
            workflow_name="test",
        )
        await registry.create_workflow_run(run)

        await registry.delete_workflow_run("wfv2-delete")

        deleted = await registry.get_workflow_run("wfv2-delete")
        assert deleted is None


# ── Engine Core Tests ────────────────────────────────────────────────────────


class TestWorkflowEngineCore:
    @pytest_asyncio.fixture
    async def engine(self, registry, simple_workflow):
        eng = WorkflowEngine(registry=registry, workflows=simple_workflow)
        self._spawned_agents = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            trigger_event=None,
            workflow_run_id=None,
            stage_id=None,
            action=None,
        ):
            agent_id = f"{role}-{stage_id}-{issue_number}"
            self._spawned_agents.append(
                {
                    "agent_id": agent_id,
                    "role": role,
                    "issue_number": issue_number,
                    "workflow_run_id": workflow_run_id,
                    "stage_id": stage_id,
                    "action": action,
                }
            )
            return agent_id

        eng.set_spawn_callback(mock_spawn)
        return eng

    @pytest.mark.asyncio
    async def test_add_workflow(self, registry):
        engine = WorkflowEngine(registry=registry)
        assert len(engine.workflows) == 0

        workflow = WorkflowConfig(
            trigger=WorkflowTrigger(event="issues.opened"),
            stages=[StageDefinition(id="s1", type=StageType.AGENT, agent="a")],
        )
        engine.add_workflow("test", workflow)
        assert len(engine.workflows) == 1
        assert "test" in engine.workflows

    @pytest.mark.asyncio
    async def test_get_workflow(self, engine):
        workflow = engine.get_workflow("simple-flow")
        assert workflow is not None
        assert workflow.trigger.event == "issues.labeled"

        missing = engine.get_workflow("nonexistent")
        assert missing is None

    @pytest.mark.asyncio
    async def test_evaluate_event_triggers_workflow(self, engine, registry):
        event = make_issue_event(issue_number=10)
        payload = event.data["payload"]

        run = await engine.evaluate_event("issues.labeled", payload, event)
        assert run is not None
        assert run.workflow_name == "simple-flow"
        assert run.issue_number == 10
        assert run.status == WorkflowRunStatus.RUNNING

        # Verify agent was spawned
        assert len(self._spawned_agents) == 1
        assert self._spawned_agents[0]["role"] == "architect"
        assert self._spawned_agents[0]["stage_id"] == "plan"

    @pytest.mark.asyncio
    async def test_evaluate_event_no_match(self, engine):
        event = make_issue_event(issue_number=11, label="bug")  # wrong label
        payload = event.data["payload"]

        run = await engine.evaluate_event("issues.labeled", payload, event)
        assert run is None

    @pytest.mark.asyncio
    async def test_no_duplicate_workflow_runs(self, engine, registry):
        event = make_issue_event(issue_number=20)
        payload = event.data["payload"]

        # First trigger
        run1 = await engine.evaluate_event("issues.labeled", payload, event)
        assert run1 is not None

        # Second trigger for same issue — should be skipped
        run2 = await engine.evaluate_event("issues.labeled", payload, event)
        assert run2 is None

        runs = await registry.get_workflow_runs_by_issue(20)
        assert len(runs) == 1


# ── State Machine Tests ──────────────────────────────────────────────────────


class TestStateMachineTransitions:
    @pytest_asyncio.fixture
    async def engine(self, registry, simple_workflow):
        eng = WorkflowEngine(registry=registry, workflows=simple_workflow)
        self._spawned_agents = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            trigger_event=None,
            workflow_run_id=None,
            stage_id=None,
            action=None,
        ):
            agent_id = f"{role}-{stage_id}-{issue_number}"
            self._spawned_agents.append(
                {
                    "agent_id": agent_id,
                    "role": role,
                    "stage_id": stage_id,
                }
            )
            return agent_id

        eng.set_spawn_callback(mock_spawn)
        return eng

    @pytest.mark.asyncio
    async def test_advance_to_next_stage(self, engine, registry):
        event = make_issue_event(issue_number=30)
        run = await engine.evaluate_event("issues.labeled", event.data["payload"], event)

        # First stage spawned (plan)
        assert run.current_stage_id == "plan"
        assert len(self._spawned_agents) == 1

        # Simulate agent completion
        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_complete(agent_id, {"result": "success"})

        # Should have advanced to second stage
        updated_run = await registry.get_workflow_run(run.run_id)
        assert updated_run.current_stage_id == "implement"
        assert len(self._spawned_agents) == 2
        assert self._spawned_agents[1]["role"] == "developer"

    @pytest.mark.asyncio
    async def test_complete_workflow(self, engine, registry):
        event = make_issue_event(issue_number=31)
        run = await engine.evaluate_event("issues.labeled", event.data["payload"], event)

        # Complete first stage
        await engine.on_agent_complete(self._spawned_agents[0]["agent_id"])

        # Complete second stage
        await engine.on_agent_complete(self._spawned_agents[1]["agent_id"])

        # Workflow should be completed
        final_run = await registry.get_workflow_run(run.run_id)
        assert final_run.status == WorkflowRunStatus.COMPLETED
        assert final_run.completed_at is not None

    @pytest.mark.asyncio
    async def test_agent_error_transitions_to_error_handler(self, engine, registry):
        event = make_issue_event(issue_number=32)
        run = await engine.evaluate_event("issues.labeled", event.data["payload"], event)

        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_error(agent_id, "Something went wrong")

        # Without an on_error handler, workflow should fail
        final_run = await registry.get_workflow_run(run.run_id)
        assert final_run.status == WorkflowRunStatus.FAILED
        assert "no handler" in final_run.error_message
        # Original error stored in outputs
        assert final_run.outputs["plan"]["error"] == "Something went wrong"


# ── Gate Stage Tests ─────────────────────────────────────────────────────────


class TestGateStageExecution:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_workflow):
        eng = WorkflowEngine(registry=registry, workflows=gate_workflow)
        self._spawned_agents = []
        self._command_results = {}  # command -> (exit_code, stdout, stderr)

        async def mock_spawn(
            role,
            issue_number,
            *,
            trigger_event=None,
            workflow_run_id=None,
            stage_id=None,
            action=None,
        ):
            agent_id = f"{role}-{stage_id}-{issue_number}"
            self._spawned_agents.append(
                {
                    "agent_id": agent_id,
                    "role": role,
                    "stage_id": stage_id,
                }
            )
            return agent_id

        async def mock_run_command(command, *, cwd=None, timeout=300):
            if command in self._command_results:
                return self._command_results[command]
            return (0, "", "")

        eng.set_spawn_callback(mock_spawn)
        eng.set_command_callback(mock_run_command)
        return eng

    @pytest.mark.asyncio
    async def test_gate_passes_advances_workflow(self, engine, registry):
        event = make_issue_event(issue_number=40)
        run = await engine.evaluate_event("issues.opened", event.data["payload"], event)

        # First stage: develop
        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_complete(agent_id)

        # Gate should pass (command returns 0) and advance to deploy
        updated_run = await registry.get_workflow_run(run.run_id)
        assert updated_run.current_stage_id == "deploy"
        assert len(self._spawned_agents) == 2
        assert self._spawned_agents[1]["role"] == "deployer"

    @pytest.mark.asyncio
    async def test_gate_fails_loops_back(self, engine, registry):
        # Make pytest command fail
        self._command_results["pytest"] = (1, "", "test failed")

        event = make_issue_event(issue_number=41)
        run = await engine.evaluate_event("issues.opened", event.data["payload"], event)

        # First stage: develop
        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_complete(agent_id)

        # Gate should fail and loop back to develop
        updated_run = await registry.get_workflow_run(run.run_id)
        assert updated_run.current_stage_id == "develop"
        assert len(self._spawned_agents) == 2
        assert self._spawned_agents[1]["role"] == "developer"


# ── Iteration Limit Tests ────────────────────────────────────────────────────


class TestIterationLimits:
    @pytest_asyncio.fixture
    async def engine(self, registry, loop_workflow):
        eng = WorkflowEngine(registry=registry, workflows=loop_workflow)
        self._spawned_agents = []
        self._command_pass = False  # Controls gate result

        async def mock_spawn(
            role,
            issue_number,
            *,
            trigger_event=None,
            workflow_run_id=None,
            stage_id=None,
            action=None,
        ):
            agent_id = f"{role}-{stage_id}-{issue_number}-{len(self._spawned_agents)}"
            self._spawned_agents.append(
                {
                    "agent_id": agent_id,
                    "role": role,
                    "stage_id": stage_id,
                }
            )
            return agent_id

        async def mock_run_command(command, *, cwd=None, timeout=300):
            if self._command_pass:
                return (0, "ok", "")
            return (1, "", "fail")

        eng.set_spawn_callback(mock_spawn)
        eng.set_command_callback(mock_run_command)
        return eng

    @pytest.mark.asyncio
    async def test_escalates_after_max_iterations(self, engine, registry):
        event = make_issue_event(issue_number=50)
        run = await engine.evaluate_event("issues.opened", event.data["payload"], event)

        # Loop 3 times, then escalate
        for i in range(3):
            agent_id = self._spawned_agents[-1]["agent_id"]
            await engine.on_agent_complete(agent_id)
            # Gate fails, loops back to attempt

        # After 3rd iteration, should escalate
        final_run = await registry.get_workflow_run(run.run_id)
        assert final_run.status == WorkflowRunStatus.ESCALATED

    @pytest.mark.asyncio
    async def test_success_before_max_iterations(self, engine, registry):
        event = make_issue_event(issue_number=51)
        run = await engine.evaluate_event("issues.opened", event.data["payload"], event)

        # First attempt
        await engine.on_agent_complete(self._spawned_agents[0]["agent_id"])
        # Gate fails

        # Second attempt
        await engine.on_agent_complete(self._spawned_agents[1]["agent_id"])
        # Gate fails

        # Third attempt — make it pass this time
        self._command_pass = True
        await engine.on_agent_complete(self._spawned_agents[2]["agent_id"])

        # Should complete successfully
        final_run = await registry.get_workflow_run(run.run_id)
        assert final_run.status == WorkflowRunStatus.COMPLETED


# ── Agent Integration Tests ──────────────────────────────────────────────────


class TestAgentIntegration:
    @pytest_asyncio.fixture
    async def engine(self, registry, simple_workflow):
        eng = WorkflowEngine(registry=registry, workflows=simple_workflow)
        self._spawned_agents = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            trigger_event=None,
            workflow_run_id=None,
            stage_id=None,
            action=None,
        ):
            agent_id = f"{role}-{stage_id}-{issue_number}"
            self._spawned_agents.append(
                {
                    "agent_id": agent_id,
                    "role": role,
                }
            )
            return agent_id

        eng.set_spawn_callback(mock_spawn)
        return eng

    @pytest.mark.asyncio
    async def test_on_agent_complete_stores_outputs(self, engine, registry):
        event = make_issue_event(issue_number=60)
        run = await engine.evaluate_event("issues.labeled", event.data["payload"], event)

        agent_id = self._spawned_agents[0]["agent_id"]
        outputs = {"files_created": ["src/main.py"], "lines_added": 100}
        await engine.on_agent_complete(agent_id, outputs)

        updated_run = await registry.get_workflow_run(run.run_id)
        assert "plan" in updated_run.outputs
        assert updated_run.outputs["plan"]["files_created"] == ["src/main.py"]

    @pytest.mark.asyncio
    async def test_on_agent_blocked_does_not_fail(self, engine, registry):
        event = make_issue_event(issue_number=61)
        run = await engine.evaluate_event("issues.labeled", event.data["payload"], event)

        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_blocked(agent_id, "Waiting for user input")

        # Workflow should still be running
        updated_run = await registry.get_workflow_run(run.run_id)
        assert updated_run.status == WorkflowRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_on_agent_error_fails_workflow(self, engine, registry):
        event = make_issue_event(issue_number=62)
        run = await engine.evaluate_event("issues.labeled", event.data["payload"], event)

        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_error(agent_id, "API rate limit exceeded")

        updated_run = await registry.get_workflow_run(run.run_id)
        assert updated_run.status == WorkflowRunStatus.FAILED
        assert "no handler" in updated_run.error_message
        # Original error stored in outputs
        assert updated_run.outputs["plan"]["error"] == "API rate limit exceeded"


# ── Context Propagation Tests ────────────────────────────────────────────────


class TestContextPropagation:
    @pytest_asyncio.fixture
    async def engine(self, registry):
        workflows = {
            "context-flow": WorkflowConfig(
                trigger=WorkflowTrigger(event="issues.opened"),
                context={"env": "production", "version": "1.0"},
                stages=[
                    StageDefinition(
                        id="stage-1",
                        type=StageType.AGENT,
                        agent="worker",
                        on_complete={
                            "goto": "stage-2",
                            "context": {"stage_1_done": True},
                        },
                    ),
                    StageDefinition(
                        id="stage-2",
                        type=StageType.AGENT,
                        agent="worker",
                    ),
                ],
            )
        }
        eng = WorkflowEngine(registry=registry, workflows=workflows)
        self._spawned_agents = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            trigger_event=None,
            workflow_run_id=None,
            stage_id=None,
            action=None,
        ):
            agent_id = f"{role}-{stage_id}-{issue_number}"
            self._spawned_agents.append({"agent_id": agent_id})
            return agent_id

        eng.set_spawn_callback(mock_spawn)
        return eng

    @pytest.mark.asyncio
    async def test_initial_context_includes_workflow_context(self, engine, registry):
        event = make_issue_event(issue_number=70)
        run = await engine.evaluate_event("issues.opened", event.data["payload"], event)

        assert run.context["env"] == "production"
        assert run.context["version"] == "1.0"

    @pytest.mark.asyncio
    async def test_context_includes_event_data(self, engine, registry):
        event = make_issue_event(issue_number=71)
        run = await engine.evaluate_event("issues.opened", event.data["payload"], event)

        assert run.context["issue_number"] == 71
        assert run.context["issue_title"] == "Test Issue"

    @pytest.mark.asyncio
    async def test_transition_context_merged(self, engine, registry):
        event = make_issue_event(issue_number=72)
        run = await engine.evaluate_event("issues.opened", event.data["payload"], event)

        # Complete first stage
        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_complete(agent_id)

        updated_run = await registry.get_workflow_run(run.run_id)
        assert updated_run.context.get("stage_1_done") is True
        # Original context preserved
        assert updated_run.context["env"] == "production"
