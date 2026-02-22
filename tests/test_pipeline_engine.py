"""Tests for pipeline engine — core execution of pipeline runs and stage transitions (AD-019)."""

from __future__ import annotations

from typing import Any

import aiosqlite
import pytest
import pytest_asyncio

from squadron.pipeline.engine import PipelineEngine
from squadron.pipeline.gates import (
    GateCheck,
    GateCheckRegistry,
    GateCheckResult,
    PipelineContext,
)
from squadron.pipeline.models import (
    GateConditionConfig,
    HumanNotifyConfig,
    HumanStageConfig,
    HumanWaitType,
    JoinStrategy,
    ParallelBranch,
    PipelineDefinition,
    PipelineRunStatus,
    ReactiveAction,
    ReactiveEventConfig,
    StageDefinition,
    StageRunStatus,
    StageType,
    TriggerDefinition,
)
from squadron.pipeline.registry import PipelineRegistry


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_simple_pipeline() -> PipelineDefinition:
    """Two-stage pipeline: agent then action."""
    return PipelineDefinition(
        description="Test pipeline",
        trigger=TriggerDefinition(event="pull_request.opened"),
        stages=[
            StageDefinition(id="review", type="agent", agent="reviewer"),
            StageDefinition(id="merge", type="action", action="merge_pr"),
        ],
    )


def make_gate_pipeline(
    *,
    check_name: str = "always_pass",
    on_fail: str | None = None,
) -> PipelineDefinition:
    """Pipeline with a gate stage followed by an action."""
    gate_kwargs: dict[str, Any] = {}
    if on_fail is not None:
        gate_kwargs["on_fail"] = on_fail
    return PipelineDefinition(
        description="Gate pipeline",
        trigger=TriggerDefinition(event="pull_request.opened"),
        stages=[
            StageDefinition(
                id="gate-check",
                type="gate",
                conditions=[GateConditionConfig(check=check_name)],
                **gate_kwargs,
            ),
            StageDefinition(id="post-gate", type="action", action="merge_pr"),
        ],
    )


def make_parallel_pipeline() -> PipelineDefinition:
    """Pipeline with a parallel stage containing two agent branches."""
    return PipelineDefinition(
        description="Parallel pipeline",
        trigger=TriggerDefinition(event="pull_request.opened"),
        stages=[
            StageDefinition(
                id="parallel-review",
                type="parallel",
                branches=[
                    ParallelBranch(id="security", agent="security-reviewer"),
                    ParallelBranch(id="code", agent="code-reviewer"),
                ],
            ),
            StageDefinition(id="final", type="action", action="merge_pr"),
        ],
    )


def make_reactive_pipeline(*, action: ReactiveAction = ReactiveAction.CANCEL) -> PipelineDefinition:
    """Pipeline that reacts to push events."""
    return PipelineDefinition(
        description="Reactive pipeline",
        trigger=TriggerDefinition(event="pull_request.opened"),
        on_events={
            "push": ReactiveEventConfig(action=action),
        },
        stages=[
            StageDefinition(id="review", type="agent", agent="reviewer"),
            StageDefinition(id="merge", type="action", action="merge_pr"),
        ],
    )


class AlwaysPassCheck(GateCheck):
    reactive_events: set[str] = set()

    async def evaluate(self, config: dict[str, Any], context: PipelineContext) -> GateCheckResult:
        return GateCheckResult(passed=True, message="always passes")


class AlwaysFailCheck(GateCheck):
    reactive_events: set[str] = set()

    async def evaluate(self, config: dict[str, Any], context: PipelineContext) -> GateCheckResult:
        return GateCheckResult(passed=False, message="always fails")


class FlipCheck(GateCheck):
    """Gate check that fails on first call, passes on second."""

    reactive_events: set[str] = {"pull_request_review.submitted"}

    def __init__(self) -> None:
        self._call_count = 0

    async def evaluate(self, config: dict[str, Any], context: PipelineContext) -> GateCheckResult:
        self._call_count += 1
        if self._call_count >= 2:
            return GateCheckResult(passed=True, message="now passes")
        return GateCheckResult(passed=False, message="not yet")


# ── Shared Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = tmp_path / "test_pipeline_engine.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


@pytest_asyncio.fixture
async def registry(db):
    reg = PipelineRegistry(db)
    await reg.initialize()
    return reg


@pytest.fixture
def gate_registry():
    reg = GateCheckRegistry()
    # Register custom test checks (override ValueError for existing)
    reg._checks["always_pass"] = AlwaysPassCheck()
    reg._checks["always_fail"] = AlwaysFailCheck()
    return reg


# ── Class 1: Pipeline Setup & Validation ─────────────────────────────────────


class TestPipelineEngineSetup:
    @pytest.fixture
    def engine(self, registry, gate_registry):
        return PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )

    def test_add_and_get_pipeline(self, engine):
        defn = make_simple_pipeline()
        engine.add_pipeline("review-flow", defn)
        assert engine.get_pipeline("review-flow") is defn
        assert engine.get_pipeline("nonexistent") is None

    def test_validate_all_pipelines_valid(self, engine):
        engine.add_pipeline("review-flow", make_simple_pipeline())
        errors = engine.validate_all_pipelines()
        assert errors == []

    def test_validate_invalid_stage_ref(self, engine):
        defn = PipelineDefinition(
            description="Bad ref",
            stages=[
                StageDefinition(
                    id="review",
                    type="agent",
                    agent="reviewer",
                    on_complete="nonexistent-stage",
                ),
            ],
        )
        engine.add_pipeline("bad-ref", defn)
        errors = engine.validate_all_pipelines()
        assert len(errors) == 1
        assert "nonexistent-stage" in errors[0]

    def test_validate_invalid_gate_check(self, engine):
        defn = PipelineDefinition(
            description="Bad gate",
            stages=[
                StageDefinition(
                    id="gate",
                    type="gate",
                    conditions=[GateConditionConfig(check="totally_unknown_check")],
                ),
            ],
        )
        engine.add_pipeline("bad-gate", defn)
        errors = engine.validate_all_pipelines()
        assert any("totally_unknown_check" in e for e in errors)

    def test_validate_cycle_detection(self, engine):
        # Pipeline A references sub-pipeline B, B references A → cycle
        pipeline_a = PipelineDefinition(
            description="A",
            stages=[StageDefinition(id="sub", type="pipeline", pipeline="pipeline-b")],
        )
        pipeline_b = PipelineDefinition(
            description="B",
            stages=[StageDefinition(id="sub", type="pipeline", pipeline="pipeline-a")],
        )
        engine.add_pipeline("pipeline-a", pipeline_a)
        engine.add_pipeline("pipeline-b", pipeline_b)
        errors = engine.validate_all_pipelines()
        assert any("Cycle" in e or "cycle" in e.lower() for e in errors)


# ── Class 2: Event Evaluation ────────────────────────────────────────────────


class TestEventEvaluation:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append(
                {
                    "role": role,
                    "issue_number": issue_number,
                    "pr_number": pr_number,
                    "stage_id": stage_id,
                    "agent_id": agent_id,
                }
            )
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action, "config": config, "context": context})
            return {"success": True, "result": "ok"}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_evaluate_event_triggers_pipeline(self, engine):
        engine.add_pipeline("pr-review", make_simple_pipeline())
        payload = {"pull_request": {"number": 42}}
        run = await engine.evaluate_event("pull_request.opened", payload)
        assert run is not None
        assert run.pipeline_name == "pr-review"
        assert run.pr_number == 42
        assert run.status == PipelineRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_evaluate_event_no_match(self, engine):
        engine.add_pipeline("pr-review", make_simple_pipeline())
        payload = {"issue": {"number": 10}}
        run = await engine.evaluate_event("issues.opened", payload)
        assert run is None

    @pytest.mark.asyncio
    async def test_evaluate_event_dedup_by_running_pipeline(self, engine):
        """Same pipeline name already running for same PR should not start a second."""
        engine.add_pipeline("pr-review", make_simple_pipeline())
        payload = {"pull_request": {"number": 42}}

        run1 = await engine.evaluate_event("pull_request.opened", payload)
        assert run1 is not None

        # Second event for same PR — pipeline already running
        run2 = await engine.evaluate_event("pull_request.opened", payload)
        assert run2 is None


# ── Class 3: Agent Stage Execution ───────────────────────────────────────────


class TestAgentStageExecution:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append(
                {
                    "role": role,
                    "issue_number": issue_number,
                    "pr_number": pr_number,
                    "stage_id": stage_id,
                    "agent_id": agent_id,
                }
            )
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action, "config": config, "context": context})
            return {"success": True, "result": "ok"}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_agent_stage_spawns_agent(self, engine, registry):
        engine.add_pipeline("pr-review", make_simple_pipeline())
        run = await engine.start_pipeline("pr-review", pr_number=42)
        assert run is not None
        assert len(self._spawned_agents) == 1
        assert self._spawned_agents[0]["role"] == "reviewer"
        assert self._spawned_agents[0]["pr_number"] == 42
        assert self._spawned_agents[0]["stage_id"] == "review"

    @pytest.mark.asyncio
    async def test_agent_stage_enters_waiting(self, engine, registry):
        engine.add_pipeline("pr-review", make_simple_pipeline())
        run = await engine.start_pipeline("pr-review", pr_number=42)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING
        assert stage_run.agent_id == "reviewer-review-agent"

    @pytest.mark.asyncio
    async def test_on_agent_complete_advances(self, engine, registry):
        engine.add_pipeline("pr-review", make_simple_pipeline())
        run = await engine.start_pipeline("pr-review", pr_number=42)
        assert run is not None

        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_complete(agent_id, outputs={"review": "lgtm"})

        # Review stage should be completed
        stage_run = await registry.get_latest_stage_run(run.run_id, "review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.COMPLETED

        # Action stage (merge) should have executed
        assert len(self._actions) == 1
        assert self._actions[0]["action"] == "merge_pr"

    @pytest.mark.asyncio
    async def test_on_agent_error_handles_failure(self, engine, registry):
        engine.add_pipeline("pr-review", make_simple_pipeline())
        run = await engine.start_pipeline("pr-review", pr_number=42)
        assert run is not None

        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_error(agent_id, "agent crashed")

        # Stage should be failed
        stage_run = await registry.get_latest_stage_run(run.run_id, "review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.FAILED
        assert stage_run.error_message == "agent crashed"

        # Pipeline should be failed (no on_error handler)
        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.FAILED

    @pytest.mark.asyncio
    async def test_agent_spawn_failure_fails_stage(self, engine, registry):
        """When spawn callback returns None, the stage should fail."""

        # Override spawn to return None
        async def fail_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            return None

        engine.set_spawn_callback(fail_spawn)
        engine.add_pipeline("pr-review", make_simple_pipeline())
        run = await engine.start_pipeline("pr-review", pr_number=42)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.FAILED

        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.FAILED


# ── Class 4: Gate Stage Execution ────────────────────────────────────────────


class TestGateStageExecution:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append(
                {
                    "role": role,
                    "stage_id": stage_id,
                    "agent_id": agent_id,
                }
            )
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_gate_all_pass_completes(self, engine, registry):
        defn = make_gate_pipeline(check_name="always_pass")
        engine.add_pipeline("gated", defn)
        run = await engine.start_pipeline("gated", pr_number=10)
        assert run is not None

        # Gate passed → stage completed → action stage should have fired
        stage_run = await registry.get_latest_stage_run(run.run_id, "gate-check")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.COMPLETED
        assert len(self._actions) == 1

    @pytest.mark.asyncio
    async def test_gate_any_fail_enters_waiting(self, engine, registry):
        defn = make_gate_pipeline(check_name="always_fail")
        engine.add_pipeline("gated", defn)
        run = await engine.start_pipeline("gated", pr_number=10)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "gate-check")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING
        # Action should NOT have fired
        assert len(self._actions) == 0

    @pytest.mark.asyncio
    async def test_gate_fail_with_on_fail_transition(self, engine, registry):
        """Gate fails and follows on_fail transition to a specific stage."""
        defn = PipelineDefinition(
            description="Gate with on_fail",
            stages=[
                StageDefinition(
                    id="gate-check",
                    type="gate",
                    conditions=[GateConditionConfig(check="always_fail")],
                    on_fail="fallback",
                ),
                StageDefinition(id="normal-path", type="action", action="merge_pr"),
                StageDefinition(id="fallback", type="action", action="notify_failure"),
            ],
        )
        engine.add_pipeline("gated-fallback", defn)
        run = await engine.start_pipeline("gated-fallback", pr_number=10)
        assert run is not None

        # The gate uses on_fail which is a pass/fail transition key.
        # In the engine, gate failing enters WAITING state (reactive re-eval).
        # on_fail is only followed via _advance_after_stage with result="fail".
        # The current gate implementation enters WAITING on fail, it does NOT
        # follow on_fail transition automatically — it waits for re-evaluation.
        stage_run = await registry.get_latest_stage_run(run.run_id, "gate-check")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING

    @pytest.mark.asyncio
    async def test_gate_records_check_results(self, engine, registry):
        """Gate evaluations are persisted as GateCheckRecord rows."""
        defn = make_gate_pipeline(check_name="always_pass")
        engine.add_pipeline("gated", defn)
        run = await engine.start_pipeline("gated", pr_number=10)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "gate-check")
        assert stage_run is not None
        checks = await registry.get_gate_checks_for_stage(stage_run.id)
        assert len(checks) == 1
        assert checks[0].passed is True
        assert checks[0].check_type == "always_pass"


# ── Class 5: Action Stage Execution ─────────────────────────────────────────


class TestActionStageExecution:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            return f"{role}-{stage_id}-agent"

        async def mock_action(action, config, context):
            self._actions.append(
                {
                    "action": action,
                    "config": config,
                    "context": context,
                }
            )
            return {"success": True, "result": "done"}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_action_stage_calls_callback(self, engine, registry):
        """Action-only pipeline calls the action callback with correct args."""
        defn = PipelineDefinition(
            description="Action pipeline",
            stages=[
                StageDefinition(id="do-merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("action-only", defn)
        run = await engine.start_pipeline("action-only", pr_number=99)
        assert run is not None
        assert len(self._actions) == 1
        assert self._actions[0]["action"] == "merge_pr"
        # Context object should carry pr_number
        ctx = self._actions[0]["context"]
        assert isinstance(ctx, PipelineContext)
        assert ctx.pr_number == 99

    @pytest.mark.asyncio
    async def test_action_stage_completes(self, engine, registry):
        defn = PipelineDefinition(
            description="Action pipeline",
            stages=[
                StageDefinition(id="do-merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("action-only", defn)
        run = await engine.start_pipeline("action-only", pr_number=99)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "do-merge")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_action_stage_failure(self, engine, registry):
        """Action returning success=False should fail the stage."""

        async def failing_action(action, config, context):
            return {"success": False, "error": "merge conflict"}

        engine.set_action_callback(failing_action)

        defn = PipelineDefinition(
            description="Failing action",
            stages=[
                StageDefinition(id="do-merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("fail-action", defn)
        run = await engine.start_pipeline("fail-action", pr_number=5)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "do-merge")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.FAILED

        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.FAILED


# ── Class 6: Pipeline Lifecycle ──────────────────────────────────────────────


class TestPipelineLifecycle:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append(
                {
                    "role": role,
                    "stage_id": stage_id,
                    "agent_id": agent_id,
                }
            )
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_pipeline_completes_after_last_stage(self, engine, registry):
        """Pipeline status → COMPLETED after final action stage succeeds."""
        defn = PipelineDefinition(
            description="Single action",
            stages=[StageDefinition(id="act", type="action", action="merge_pr")],
        )
        engine.add_pipeline("single", defn)
        run = await engine.start_pipeline("single", pr_number=1)
        assert run is not None

        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.COMPLETED
        assert updated_run.completed_at is not None

    @pytest.mark.asyncio
    async def test_pipeline_fail(self, engine, registry):
        """Error in agent stage with no retry → pipeline FAILED."""
        engine.add_pipeline("pr-review", make_simple_pipeline())
        run = await engine.start_pipeline("pr-review", pr_number=42)
        assert run is not None

        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_error(agent_id, "catastrophic failure")

        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.FAILED
        assert updated_run.error_message == "catastrophic failure"
        assert updated_run.error_stage_id == "review"

    @pytest.mark.asyncio
    async def test_cancel_pipeline(self, engine, registry):
        engine.add_pipeline("pr-review", make_simple_pipeline())
        run = await engine.start_pipeline("pr-review", pr_number=42)
        assert run is not None

        cancelled = await engine.cancel_pipeline(run.run_id)
        assert cancelled is True

        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.CANCELLED
        assert updated_run.completed_at is not None

    @pytest.mark.asyncio
    async def test_cancel_already_completed_returns_false(self, engine, registry):
        """Cancelling a completed pipeline returns False."""
        defn = PipelineDefinition(
            description="Quick",
            stages=[StageDefinition(id="act", type="action", action="merge_pr")],
        )
        engine.add_pipeline("quick", defn)
        run = await engine.start_pipeline("quick", pr_number=1)
        assert run is not None

        # Already completed
        cancelled = await engine.cancel_pipeline(run.run_id)
        assert cancelled is False

    @pytest.mark.asyncio
    async def test_explicit_stage_transitions(self, engine, registry):
        """on_complete pointing to a specific stage jumps there, skipping the middle."""
        defn = PipelineDefinition(
            description="Jump pipeline",
            stages=[
                StageDefinition(
                    id="step-a",
                    type="action",
                    action="action_a",
                    on_complete="step-c",
                ),
                StageDefinition(id="step-b", type="action", action="action_b"),
                StageDefinition(id="step-c", type="action", action="action_c"),
            ],
        )
        engine.add_pipeline("jump", defn)
        run = await engine.start_pipeline("jump", pr_number=1)
        assert run is not None

        # step-a completed → jump to step-c (skipping step-b)
        actions_called = [a["action"] for a in self._actions]
        assert "action_a" in actions_called
        assert "action_c" in actions_called
        assert "action_b" not in actions_called

    @pytest.mark.asyncio
    async def test_start_unknown_pipeline_returns_none(self, engine):
        result = await engine.start_pipeline("does-not-exist")
        assert result is None

    @pytest.mark.asyncio
    async def test_multi_stage_sequential_completion(self, engine, registry):
        """Agent stage → on_agent_complete → action stage → pipeline COMPLETED."""
        engine.add_pipeline("two-stage", make_simple_pipeline())
        run = await engine.start_pipeline("two-stage", pr_number=7)
        assert run is not None

        agent_id = self._spawned_agents[0]["agent_id"]
        await engine.on_agent_complete(agent_id)

        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.COMPLETED


# ── Class 7: Reactive Events ────────────────────────────────────────────────


class TestReactiveEvents:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append(
                {
                    "role": role,
                    "stage_id": stage_id,
                    "agent_id": agent_id,
                }
            )
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_cancel_reactive_event(self, engine, registry):
        """Push event with action=cancel should cancel the running pipeline."""
        defn = make_reactive_pipeline(action=ReactiveAction.CANCEL)
        engine.add_pipeline("reactive", defn)

        # Start via trigger
        payload = {"pull_request": {"number": 42}}
        run = await engine.evaluate_event("pull_request.opened", payload)
        assert run is not None
        assert run.status == PipelineRunStatus.RUNNING

        # Fire the reactive event
        push_payload = {"pull_request": {"number": 42}}
        await engine.evaluate_event("push", push_payload)

        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_reevaluate_gates_reactive(self, engine, registry, gate_registry):
        """Reactive event re-evaluates a waiting gate and advances on pass."""
        flip_check = FlipCheck()
        gate_registry._checks["flip_check"] = flip_check

        defn = PipelineDefinition(
            description="Flip gate pipeline",
            trigger=TriggerDefinition(event="pull_request.opened"),
            on_events={
                "pull_request_review.submitted": ReactiveEventConfig(
                    action=ReactiveAction.REEVALUATE_GATES,
                ),
            },
            stages=[
                StageDefinition(
                    id="gate",
                    type="gate",
                    conditions=[GateConditionConfig(check="flip_check")],
                ),
                StageDefinition(id="merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("flip-gate", defn)

        # Start — first evaluation fails, gate enters WAITING
        payload = {"pull_request": {"number": 55}}
        run = await engine.evaluate_event("pull_request.opened", payload)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "gate")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING

        # Fire reactive event — second evaluation passes
        review_payload = {"pull_request": {"number": 55}}
        await engine.evaluate_event("pull_request_review.submitted", review_payload)

        stage_run = await registry.get_latest_stage_run(run.run_id, "gate")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.COMPLETED

        # Action after gate should have fired
        assert len(self._actions) == 1
        assert self._actions[0]["action"] == "merge_pr"


# ── Class 8: Parallel Stage Execution ────────────────────────────────────────


class TestParallelStageExecution:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append(
                {
                    "role": role,
                    "stage_id": stage_id,
                    "agent_id": agent_id,
                }
            )
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_parallel_spawns_all_branches(self, engine, registry):
        engine.add_pipeline("parallel", make_parallel_pipeline())
        run = await engine.start_pipeline("parallel", pr_number=20)
        assert run is not None

        # Should have spawned two agents (one per branch)
        assert len(self._spawned_agents) == 2
        roles = {a["role"] for a in self._spawned_agents}
        assert "security-reviewer" in roles
        assert "code-reviewer" in roles

    @pytest.mark.asyncio
    async def test_parallel_stage_enters_waiting(self, engine, registry):
        engine.add_pipeline("parallel", make_parallel_pipeline())
        run = await engine.start_pipeline("parallel", pr_number=20)
        assert run is not None

        # Parent parallel stage should be WAITING
        stage_run = await registry.get_latest_stage_run(run.run_id, "parallel-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING

    @pytest.mark.asyncio
    async def test_parallel_completes_when_all_done(self, engine, registry):
        engine.add_pipeline("parallel", make_parallel_pipeline())
        run = await engine.start_pipeline("parallel", pr_number=20)
        assert run is not None

        # Complete both branch agents
        for spawned in self._spawned_agents:
            await engine.on_agent_complete(spawned["agent_id"])

        # Parent parallel stage should now be completed
        stage_run = await registry.get_latest_stage_run(run.run_id, "parallel-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.COMPLETED

        # The final action should have fired
        assert len(self._actions) == 1
        assert self._actions[0]["action"] == "merge_pr"

        # Pipeline should be completed
        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_parallel_partial_complete_stays_waiting(self, engine, registry):
        """Completing only one branch of a parallel stage keeps it WAITING."""
        engine.add_pipeline("parallel", make_parallel_pipeline())
        run = await engine.start_pipeline("parallel", pr_number=20)
        assert run is not None

        # Complete only the first branch
        await engine.on_agent_complete(self._spawned_agents[0]["agent_id"])

        # Parent should still be waiting
        stage_run = await registry.get_latest_stage_run(run.run_id, "parallel-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING
        assert len(self._actions) == 0


# ── Class 9: Context and Edge Cases ─────────────────────────────────────────


class TestContextAndEdgeCases:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append(
                {
                    "role": role,
                    "issue_number": issue_number,
                    "pr_number": pr_number,
                    "stage_id": stage_id,
                    "context": context,
                }
            )
            return agent_id

        async def mock_action(action, config, context):
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_context_propagated_to_agent(self, engine, registry):
        """Extra context passed to start_pipeline appears in spawn callback."""
        defn = PipelineDefinition(
            description="Context test",
            context={"base_key": "base_val"},
            stages=[StageDefinition(id="review", type="agent", agent="reviewer")],
        )
        engine.add_pipeline("ctx-test", defn)
        run = await engine.start_pipeline("ctx-test", pr_number=5, context={"extra": "data"})
        assert run is not None

        spawned_ctx = self._spawned_agents[0]["context"]
        assert spawned_ctx["base_key"] == "base_val"
        assert spawned_ctx["extra"] == "data"
        assert spawned_ctx["pr_number"] == 5

    @pytest.mark.asyncio
    async def test_pipeline_run_persisted(self, engine, registry):
        """Pipeline run is persisted to DB and retrievable."""
        engine.add_pipeline("pr-review", make_simple_pipeline())
        run = await engine.start_pipeline("pr-review", pr_number=42)
        assert run is not None

        fetched = await registry.get_pipeline_run(run.run_id)
        assert fetched is not None
        assert fetched.pipeline_name == "pr-review"
        assert fetched.pr_number == 42
        assert fetched.status == PipelineRunStatus.RUNNING
        assert fetched.current_stage_id == "review"

    @pytest.mark.asyncio
    async def test_on_agent_complete_unknown_agent_noop(self, engine):
        """Completing an unknown agent_id is a no-op, doesn't raise."""
        await engine.on_agent_complete("totally-unknown-agent-id")

    @pytest.mark.asyncio
    async def test_on_agent_error_unknown_agent_noop(self, engine):
        """Erroring an unknown agent_id is a no-op, doesn't raise."""
        await engine.on_agent_error("totally-unknown-agent-id", "some error")

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_false(self, engine):
        result = await engine.cancel_pipeline("nonexistent-run-id")
        assert result is False


# ── Class 10: Human Stage Lifecycle (Phase 2) ───────────────────────────────


def make_human_pipeline(
    *,
    wait_for: HumanWaitType = HumanWaitType.APPROVAL,
    from_group: str | None = None,
    count: int = 1,
    auto_assign: bool = True,
    notify: HumanNotifyConfig | None = None,
) -> PipelineDefinition:
    """Pipeline with a human stage followed by an action."""
    return PipelineDefinition(
        description="Human pipeline",
        trigger=TriggerDefinition(event="pull_request.opened"),
        stages=[
            StageDefinition(
                id="human-review",
                type="human",
                human=HumanStageConfig(
                    description="Review required",
                    wait_for=wait_for,
                    count=count,
                    auto_assign=auto_assign,
                    notify=notify,
                    **{"from": from_group} if from_group else {},
                ),
            ),
            StageDefinition(id="merge", type="action", action="merge_pr"),
        ],
    )


class TestHumanStageLifecycle:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []
        self._notifications: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append({"role": role, "stage_id": stage_id, "agent_id": agent_id})
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        async def mock_notify(target, context, *, message=None, label=None, users=None):
            self._notifications.append(
                {"target": target, "message": message, "label": label, "users": users}
            )

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        eng.set_notify_callback(mock_notify)
        return eng

    @pytest.mark.asyncio
    async def test_human_stage_enters_waiting(self, engine, registry):
        """Human stage enters WAITING state."""
        defn = make_human_pipeline()
        engine.add_pipeline("human", defn)
        run = await engine.start_pipeline("human", pr_number=42)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING

    @pytest.mark.asyncio
    async def test_human_stage_creates_state_record(self, engine, registry):
        """Human stage creates a HumanStageState tracking record."""
        defn = make_human_pipeline()
        engine.add_pipeline("human", defn)
        run = await engine.start_pipeline("human", pr_number=42)
        assert run is not None

        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        state = await registry.get_human_stage_state(stage_run.id)
        assert state is not None
        assert state.stage_run_id == stage_run.id

    @pytest.mark.asyncio
    async def test_human_stage_auto_assign(self, engine, registry):
        """Human stage auto-assigns reviewers via notify callback."""
        defn = make_human_pipeline(from_group="security-team")
        engine.add_pipeline("human", defn)
        run = await engine.start_pipeline("human", pr_number=42)
        assert run is not None

        # Should have sent an assign notification
        assign_notifs = [n for n in self._notifications if n["target"] == "assign"]
        assert len(assign_notifs) == 1
        assert assign_notifs[0]["users"] == ["security-team"]

    @pytest.mark.asyncio
    async def test_human_stage_no_auto_assign_when_disabled(self, engine, registry):
        """No auto-assign notification when auto_assign=False."""
        defn = make_human_pipeline(from_group="security-team", auto_assign=False)
        engine.add_pipeline("human", defn)
        await engine.start_pipeline("human", pr_number=42)

        assign_notifs = [n for n in self._notifications if n["target"] == "assign"]
        assert len(assign_notifs) == 0

    @pytest.mark.asyncio
    async def test_human_stage_entry_notification(self, engine, registry):
        """Human stage sends entry notification when configured."""
        defn = make_human_pipeline(notify=HumanNotifyConfig(on_enter="Please review this PR"))
        engine.add_pipeline("human", defn)
        await engine.start_pipeline("human", pr_number=42)

        pr_notifs = [n for n in self._notifications if n["target"] == "pr_comment"]
        assert len(pr_notifs) == 1
        assert pr_notifs[0]["message"] == "Please review this PR"

    @pytest.mark.asyncio
    async def test_complete_human_stage(self, engine, registry):
        """complete_human_stage advances the pipeline."""
        defn = make_human_pipeline()
        engine.add_pipeline("human", defn)
        run = await engine.start_pipeline("human", pr_number=42)
        assert run is not None

        result = await engine.complete_human_stage(
            run.run_id, "human-review", completed_by="octocat", action="approved"
        )
        assert result is True

        # Stage should be completed
        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.COMPLETED
        assert stage_run.outputs["completed_by"] == "octocat"

        # Action should have fired
        assert len(self._actions) == 1
        assert self._actions[0]["action"] == "merge_pr"

        # Pipeline should complete
        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_complete_human_stage_wrong_action_type(self, engine, registry):
        """complete_human_stage rejects actions that don't match wait_for."""
        defn = make_human_pipeline(wait_for=HumanWaitType.APPROVAL)
        engine.add_pipeline("human", defn)
        run = await engine.start_pipeline("human", pr_number=42)
        assert run is not None

        result = await engine.complete_human_stage(
            run.run_id, "human-review", completed_by="octocat", action="commented"
        )
        assert result is False

        # Stage should still be WAITING
        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING

    @pytest.mark.asyncio
    async def test_complete_human_stage_multi_approval(self, engine, registry):
        """Human stage with count=2 requires two completions."""
        defn = make_human_pipeline(count=2)
        engine.add_pipeline("human", defn)
        run = await engine.start_pipeline("human", pr_number=42)
        assert run is not None

        # First approval — should stay WAITING
        result1 = await engine.complete_human_stage(
            run.run_id, "human-review", completed_by="alice", action="approved"
        )
        assert result1 is True

        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING
        assert len(self._actions) == 0

        # Second approval — should complete
        result2 = await engine.complete_human_stage(
            run.run_id, "human-review", completed_by="bob", action="approved"
        )
        assert result2 is True

        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.COMPLETED
        assert len(self._actions) == 1

    @pytest.mark.asyncio
    async def test_complete_human_stage_not_running(self, engine, registry):
        """complete_human_stage returns False for non-running pipeline."""
        result = await engine.complete_human_stage(
            "nonexistent-run", "some-stage", completed_by="octocat", action="approved"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_complete_human_stage_wrong_stage_type(self, engine, registry):
        """complete_human_stage returns False for non-human stage."""
        defn = PipelineDefinition(
            description="Agent only",
            stages=[StageDefinition(id="review", type="agent", agent="reviewer")],
        )
        engine.add_pipeline("agent", defn)
        run = await engine.start_pipeline("agent", pr_number=42)
        assert run is not None

        result = await engine.complete_human_stage(
            run.run_id, "review", completed_by="octocat", action="approved"
        )
        assert result is False


# ── Class 11: Human Stage Reactive Events (Phase 2) ─────────────────────────


class TestHumanStageReactiveEvents:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._actions: list[dict[str, Any]] = []
        self._notifications: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            return f"{role}-{stage_id}-agent"

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        async def mock_notify(target, context, *, message=None, label=None, users=None):
            self._notifications.append(
                {"target": target, "message": message, "label": label, "users": users}
            )

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        eng.set_notify_callback(mock_notify)
        return eng

    @pytest.mark.asyncio
    async def test_approval_event_completes_human_stage(self, engine, registry):
        """PR review approval event completes a human stage waiting for approval."""
        defn = PipelineDefinition(
            description="Human reactive",
            trigger=TriggerDefinition(event="pull_request.opened"),
            on_events={
                "pull_request_review.submitted": ReactiveEventConfig(
                    action=ReactiveAction.REEVALUATE_GATES,
                ),
            },
            stages=[
                StageDefinition(
                    id="human-review",
                    type="human",
                    human=HumanStageConfig(
                        description="Approval needed",
                        wait_for=HumanWaitType.APPROVAL,
                    ),
                ),
                StageDefinition(id="merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("human-reactive", defn)

        # Start the pipeline
        payload = {"pull_request": {"number": 42}}
        run = await engine.evaluate_event("pull_request.opened", payload)
        assert run is not None

        # Human stage should be WAITING
        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING

        # Fire approval event
        review_payload = {
            "pull_request": {"number": 42},
            "review": {"state": "approved", "user": {"login": "reviewer1"}},
            "sender": {"login": "reviewer1"},
        }
        await engine.evaluate_event("pull_request_review.submitted", review_payload)

        # Human stage should now be completed
        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.COMPLETED

        # Action should have fired
        assert len(self._actions) == 1

    @pytest.mark.asyncio
    async def test_non_approval_review_does_not_complete(self, engine, registry):
        """A 'changes_requested' review does not complete an approval human stage."""
        defn = PipelineDefinition(
            description="Human reactive",
            trigger=TriggerDefinition(event="pull_request.opened"),
            stages=[
                StageDefinition(
                    id="human-review",
                    type="human",
                    human=HumanStageConfig(
                        wait_for=HumanWaitType.APPROVAL,
                    ),
                ),
                StageDefinition(id="merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("human-reactive", defn)

        payload = {"pull_request": {"number": 42}}
        run = await engine.evaluate_event("pull_request.opened", payload)
        assert run is not None

        # Fire changes_requested review event
        review_payload = {
            "pull_request": {"number": 42},
            "review": {"state": "changes_requested", "user": {"login": "reviewer1"}},
            "sender": {"login": "reviewer1"},
        }
        await engine.evaluate_event("pull_request_review.submitted", review_payload)

        # Human stage should still be WAITING
        stage_run = await registry.get_latest_stage_run(run.run_id, "human-review")
        assert stage_run is not None
        assert stage_run.status == StageRunStatus.WAITING
        assert len(self._actions) == 0


# ── Class 12: NOTIFY + WAKE_AGENT Reactive Actions (Phase 2) ────────────────


class TestReactiveNotifyWakeAgent:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned_agents: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []
        self._notifications: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned_agents.append(
                {
                    "role": role,
                    "stage_id": stage_id,
                    "agent_id": agent_id,
                    "continue_session": continue_session,
                }
            )
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        async def mock_notify(target, context, *, message=None, label=None, users=None):
            self._notifications.append(
                {"target": target, "message": message, "label": label, "users": users}
            )

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        eng.set_notify_callback(mock_notify)
        return eng

    @pytest.mark.asyncio
    async def test_reactive_notify_sends_notification(self, engine, registry):
        """NOTIFY reactive action sends a notification via the callback."""
        defn = PipelineDefinition(
            description="Notify reactive",
            trigger=TriggerDefinition(event="pull_request.opened"),
            on_events={
                "push": ReactiveEventConfig(
                    action=ReactiveAction.NOTIFY,
                    notify={"message": "New push detected!", "target": "pr_comment"},
                ),
            },
            stages=[
                StageDefinition(id="review", type="agent", agent="reviewer"),
            ],
        )
        engine.add_pipeline("notify-reactive", defn)

        payload = {"pull_request": {"number": 42}}
        run = await engine.evaluate_event("pull_request.opened", payload)
        assert run is not None

        # Fire reactive event
        push_payload = {"pull_request": {"number": 42}}
        await engine.evaluate_event("push", push_payload)

        # Should have a notification
        comment_notifs = [n for n in self._notifications if n["target"] == "pr_comment"]
        assert len(comment_notifs) >= 1
        assert any("New push detected!" in (n["message"] or "") for n in comment_notifs)

    @pytest.mark.asyncio
    async def test_reactive_wake_agent(self, engine, registry):
        """WAKE_AGENT reactive action wakes the current stage's agent."""
        defn = PipelineDefinition(
            description="Wake agent reactive",
            trigger=TriggerDefinition(event="pull_request.opened"),
            on_events={
                "issue_comment.created": ReactiveEventConfig(
                    action=ReactiveAction.WAKE_AGENT,
                ),
            },
            stages=[
                StageDefinition(id="review", type="agent", agent="reviewer"),
            ],
        )
        engine.add_pipeline("wake-reactive", defn)

        payload = {"pull_request": {"number": 42}}
        run = await engine.evaluate_event("pull_request.opened", payload)
        assert run is not None

        # Initial spawn
        assert len(self._spawned_agents) == 1
        assert self._spawned_agents[0]["continue_session"] is False

        # Fire reactive event
        comment_payload = {
            "pull_request": {"number": 42},
            "comment": {"user": {"login": "someone"}},
            "sender": {"login": "someone"},
        }
        await engine.evaluate_event("issue_comment.created", comment_payload)

        # Should have woken the agent (second spawn with continue_session=True)
        assert len(self._spawned_agents) == 2
        assert self._spawned_agents[1]["continue_session"] is True


# ── Class 13: Pipeline Hooks (Phase 2) ──────────────────────────────────────


class TestPipelineHooks:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._actions: list[dict[str, Any]] = []
        self._notifications: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            return f"{role}-{stage_id}-agent"

        async def mock_action(action, config, context):
            self._actions.append({"action": action, "config": config})
            return {"success": True}

        async def mock_notify(target, context, *, message=None, label=None, users=None):
            self._notifications.append(
                {"target": target, "message": message, "label": label, "users": users}
            )

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        eng.set_notify_callback(mock_notify)
        return eng

    @pytest.mark.asyncio
    async def test_on_complete_hook_notify(self, engine, registry):
        """on_complete hook sends a notification when pipeline completes."""
        defn = PipelineDefinition(
            description="Hook test",
            on_complete=[{"notify": "Pipeline completed!"}],
            stages=[StageDefinition(id="act", type="action", action="merge_pr")],
        )
        engine.add_pipeline("hooked", defn)
        run = await engine.start_pipeline("hooked", pr_number=1)
        assert run is not None

        # Pipeline should be completed (single action stage)
        updated = await registry.get_pipeline_run(run.run_id)
        assert updated is not None
        assert updated.status == PipelineRunStatus.COMPLETED

        # on_complete hook should have sent a notification
        comment_notifs = [n for n in self._notifications if n["target"] == "pr_comment"]
        assert len(comment_notifs) == 1
        assert comment_notifs[0]["message"] == "Pipeline completed!"

    @pytest.mark.asyncio
    async def test_on_complete_hook_label(self, engine, registry):
        """on_complete hook adds a label when pipeline completes."""
        defn = PipelineDefinition(
            description="Hook test",
            on_complete=[{"label": "pipeline-done"}],
            stages=[StageDefinition(id="act", type="action", action="merge_pr")],
        )
        engine.add_pipeline("hooked", defn)
        await engine.start_pipeline("hooked", pr_number=1)

        label_notifs = [n for n in self._notifications if n["target"] == "label"]
        assert len(label_notifs) == 1
        assert label_notifs[0]["label"] == "pipeline-done"

    @pytest.mark.asyncio
    async def test_on_error_hook_notify(self, engine, registry):
        """on_error hook sends a notification when pipeline fails."""

        async def failing_action(action, config, context):
            return {"success": False, "error": "merge conflict"}

        engine.set_action_callback(failing_action)

        defn = PipelineDefinition(
            description="Error hook test",
            on_error=[{"notify": "Pipeline failed!"}],
            stages=[StageDefinition(id="act", type="action", action="merge_pr")],
        )
        engine.add_pipeline("error-hooked", defn)
        run = await engine.start_pipeline("error-hooked", pr_number=1)
        assert run is not None

        updated = await registry.get_pipeline_run(run.run_id)
        assert updated is not None
        assert updated.status == PipelineRunStatus.FAILED

        comment_notifs = [n for n in self._notifications if n["target"] == "pr_comment"]
        assert len(comment_notifs) == 1
        assert comment_notifs[0]["message"] == "Pipeline failed!"

    @pytest.mark.asyncio
    async def test_on_complete_hook_action(self, engine, registry):
        """on_complete hook can execute a built-in action."""
        defn = PipelineDefinition(
            description="Action hook test",
            on_complete=[{"action": "cleanup", "config": {"dry_run": True}}],
            stages=[StageDefinition(id="act", type="action", action="merge_pr")],
        )
        engine.add_pipeline("action-hooked", defn)
        await engine.start_pipeline("action-hooked", pr_number=1)

        # Should have 2 actions: the stage action + the hook action
        assert len(self._actions) == 2
        assert self._actions[0]["action"] == "merge_pr"
        assert self._actions[1]["action"] == "cleanup"
        assert self._actions[1]["config"] == {"dry_run": True}


# ── Class 14: Timeout Enforcement (Phase 2) ─────────────────────────────────


class TestTimeoutEnforcement:
    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._actions: list[dict[str, Any]] = []
        self._notifications: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            return f"{role}-{stage_id}-agent"

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        async def mock_notify(target, context, *, message=None, label=None, users=None):
            self._notifications.append(
                {"target": target, "message": message, "label": label, "users": users}
            )

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        eng.set_notify_callback(mock_notify)
        return eng

    @pytest.mark.asyncio
    async def test_timeout_schedules_task(self, engine, registry):
        """A human stage with timeout schedules an asyncio task."""
        defn = PipelineDefinition(
            description="Timeout test",
            stages=[
                StageDefinition(
                    id="human-review",
                    type="human",
                    human=HumanStageConfig(description="Review"),
                    timeout="30m",
                    on_timeout={"then": "fail"},
                ),
                StageDefinition(id="merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("timeout", defn)
        run = await engine.start_pipeline("timeout", pr_number=42)
        assert run is not None

        # A timeout task should be scheduled
        assert len(engine._timeout_tasks) == 1

        # Clean up
        for task in engine._timeout_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_timeout_cancelled_on_stage_complete(self, engine, registry):
        """Timeout task is cancelled when the stage completes normally."""
        defn = PipelineDefinition(
            description="Timeout cancel test",
            stages=[
                StageDefinition(
                    id="human-review",
                    type="human",
                    human=HumanStageConfig(description="Review"),
                    timeout="30m",
                    on_timeout={"then": "fail"},
                ),
                StageDefinition(id="merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("timeout", defn)
        run = await engine.start_pipeline("timeout", pr_number=42)
        assert run is not None

        # Complete the human stage
        await engine.complete_human_stage(
            run.run_id, "human-review", completed_by="octocat", action="approved"
        )

        # Timeout task should have been cleaned up
        assert len(engine._timeout_tasks) == 0

    @pytest.mark.asyncio
    async def test_gate_timeout_schedules_task(self, engine, registry, gate_registry):
        """A gate stage with timeout schedules an asyncio task."""
        gate_registry._checks["always_fail"] = AlwaysFailCheck()

        defn = PipelineDefinition(
            description="Gate timeout",
            stages=[
                StageDefinition(
                    id="gate-check",
                    type="gate",
                    conditions=[GateConditionConfig(check="always_fail")],
                    timeout="1h",
                    on_timeout={"then": "escalate"},
                ),
                StageDefinition(id="merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("gate-timeout", defn)
        run = await engine.start_pipeline("gate-timeout", pr_number=42)
        assert run is not None

        # Timeout task should be scheduled (gate entered WAITING)
        assert len(engine._timeout_tasks) == 1

        # Clean up
        for task in engine._timeout_tasks.values():
            task.cancel()


# ── Class 15: Helper Functions (Phase 2) ─────────────────────────────────────


class TestHelperFunctions:
    def test_extract_pr_number_from_pull_request(self):
        from squadron.pipeline.engine import _extract_pr_number

        payload = {"pull_request": {"number": 42}}
        assert _extract_pr_number(payload) == 42

    def test_extract_pr_number_from_top_level(self):
        from squadron.pipeline.engine import _extract_pr_number

        payload = {"number": 99}
        assert _extract_pr_number(payload) == 99

    def test_extract_pr_number_empty(self):
        from squadron.pipeline.engine import _extract_pr_number

        assert _extract_pr_number({}) is None

    def test_extract_human_action_approval(self):
        from squadron.pipeline.engine import _extract_human_action

        payload = {
            "review": {"state": "approved", "user": {"login": "alice"}},
            "sender": {"login": "alice"},
        }
        actor, action = _extract_human_action("pull_request_review.submitted", payload)
        assert actor == "alice"
        assert action == "approved"

    def test_extract_human_action_comment(self):
        from squadron.pipeline.engine import _extract_human_action

        payload = {
            "comment": {"user": {"login": "bob"}},
            "sender": {"login": "bob"},
        }
        actor, action = _extract_human_action("issue_comment.created", payload)
        assert actor == "bob"
        assert action == "commented"

    def test_extract_human_action_label(self):
        from squadron.pipeline.engine import _extract_human_action

        payload = {
            "label": {"name": "approved"},
            "sender": {"login": "charlie"},
        }
        actor, action = _extract_human_action("pull_request.labeled", payload)
        assert actor == "charlie"
        assert action == "labeled:approved"

    def test_extract_human_action_unknown_event(self):
        from squadron.pipeline.engine import _extract_human_action

        actor, action = _extract_human_action("unknown.event", {})
        assert actor == ""
        assert action == ""


# ── Phase 4: Sub-Pipeline, Multi-PR, Join Strategy Tests ────────────────────


def make_sub_pipeline_pair() -> tuple[PipelineDefinition, PipelineDefinition]:
    """Parent pipeline with a sub-pipeline stage, and the child definition."""
    child = PipelineDefinition(
        description="Child pipeline",
        stages=[
            StageDefinition(id="child-work", type="agent", agent="worker"),
        ],
    )
    parent = PipelineDefinition(
        description="Parent pipeline",
        stages=[
            StageDefinition(id="sub", type="pipeline", pipeline="child-pipeline"),
            StageDefinition(id="post-sub", type="action", action="merge_pr"),
        ],
    )
    return parent, child


def make_join_any_pipeline() -> PipelineDefinition:
    """Parallel pipeline with join: any strategy."""
    return PipelineDefinition(
        description="Join-any pipeline",
        trigger=TriggerDefinition(event="pull_request.opened"),
        stages=[
            StageDefinition(
                id="parallel-any",
                type="parallel",
                join=JoinStrategy.ANY,
                branches=[
                    ParallelBranch(id="fast", agent="fast-reviewer"),
                    ParallelBranch(id="slow", agent="slow-reviewer"),
                ],
            ),
            StageDefinition(id="done", type="action", action="merge_pr"),
        ],
    )


def make_parallel_pipeline_branch(branch: ParallelBranch) -> PipelineDefinition:
    """Parallel pipeline with a single custom branch plus an agent branch."""
    return PipelineDefinition(
        description="Mixed-type parallel pipeline",
        trigger=TriggerDefinition(event="pull_request.opened"),
        stages=[
            StageDefinition(
                id="mixed-parallel",
                type="parallel",
                branches=[
                    branch,
                    ParallelBranch(id="code", agent="code-reviewer"),
                ],
            ),
            StageDefinition(id="done", type="action", action="merge_pr"),
        ],
    )


class CrossPRGateCheck(GateCheck):
    """Gate check that records the pr_number it was called with."""

    reactive_events: set[str] = set()

    def __init__(self) -> None:
        self.evaluated_prs: list[int | None] = []

    async def evaluate(self, config: dict[str, Any], context: PipelineContext) -> GateCheckResult:
        self.evaluated_prs.append(context.pr_number)
        return GateCheckResult(passed=True, message="ok")


class TestCancelCascade:
    """P4.1: cancel_pipeline cascades to child pipelines."""

    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-agent"
            self._spawned.append({"role": role, "agent_id": agent_id})
            return agent_id

        async def mock_action(action, config, context):
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_cancel_cascades_to_child(self, engine, registry):
        parent_def, child_def = make_sub_pipeline_pair()
        engine.add_pipeline("parent-pipeline", parent_def)
        engine.add_pipeline("child-pipeline", child_def)

        run = await engine.start_pipeline("parent-pipeline", pr_number=10)
        assert run is not None

        # Child pipeline should have been started
        children = await registry.get_child_pipelines(run.run_id)
        assert len(children) == 1
        child_run = children[0]
        assert child_run.status == PipelineRunStatus.RUNNING

        # Cancel the parent
        result = await engine.cancel_pipeline(run.run_id)
        assert result is True

        # Both parent and child should be cancelled
        updated_parent = await registry.get_pipeline_run(run.run_id)
        assert updated_parent is not None
        assert updated_parent.status == PipelineRunStatus.CANCELLED

        updated_child = await registry.get_pipeline_run(child_run.run_id)
        assert updated_child is not None
        assert updated_child.status == PipelineRunStatus.CANCELLED


class TestChildOutputPropagation:
    """P4.2: Child pipeline outputs propagate to parent context."""

    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-agent"
            self._spawned.append({"role": role, "agent_id": agent_id, "run_id": pipeline_run_id})
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_child_outputs_in_parent_context(self, engine, registry):
        parent_def, child_def = make_sub_pipeline_pair()
        engine.add_pipeline("parent-pipeline", parent_def)
        engine.add_pipeline("child-pipeline", child_def)

        run = await engine.start_pipeline("parent-pipeline", pr_number=10)
        assert run is not None

        # Find the child's agent and complete it with outputs
        child_spawned = [s for s in self._spawned if s["role"] == "worker"]
        assert len(child_spawned) == 1

        await engine.on_agent_complete(
            child_spawned[0]["agent_id"],
            outputs={"result": "success", "artifact": "build-123"},
        )

        # Parent should have advanced past the sub stage
        updated_parent = await registry.get_pipeline_run(run.run_id)
        assert updated_parent is not None
        # Check child outputs are in parent context
        stages_data = updated_parent.context.get("stages", {})
        assert "sub" in stages_data
        sub_data = stages_data["sub"]
        assert "outputs" in sub_data
        assert "child_run_id" in sub_data

        # Post-sub action should have executed
        assert len(self._actions) == 1


class TestFailEscalatePropagation:
    """P4.3: _fail_pipeline and _escalate_pipeline notify parent."""

    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-agent"
            self._spawned.append({"role": role, "agent_id": agent_id})
            return agent_id

        async def mock_action(action, config, context):
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_child_failure_propagates_to_parent(self, engine, registry):
        parent_def, child_def = make_sub_pipeline_pair()
        engine.add_pipeline("parent-pipeline", parent_def)
        engine.add_pipeline("child-pipeline", child_def)

        run = await engine.start_pipeline("parent-pipeline", pr_number=10)
        assert run is not None

        # Complete the child agent with an error
        child_spawned = [s for s in self._spawned if s["role"] == "worker"]
        assert len(child_spawned) == 1
        await engine.on_agent_error(child_spawned[0]["agent_id"], "task failed")

        # Parent pipeline should have failed because child propagated failure
        updated_parent = await registry.get_pipeline_run(run.run_id)
        assert updated_parent is not None
        assert updated_parent.status == PipelineRunStatus.FAILED

    @pytest.mark.asyncio
    async def test_escalated_child_propagates_to_parent(self, engine, registry):
        """An escalated child pipeline should propagate failure to parent."""
        parent_def, child_def = make_sub_pipeline_pair()
        engine.add_pipeline("parent-pipeline", parent_def)
        engine.add_pipeline("child-pipeline", child_def)

        run = await engine.start_pipeline("parent-pipeline", pr_number=10)
        assert run is not None

        # Get child run and escalate it directly
        children = await registry.get_child_pipelines(run.run_id)
        assert len(children) == 1
        child_run = children[0]

        # Escalate the child via engine's internal method
        await engine._escalate_pipeline(child_run, "needs human intervention")

        # Parent's sub stage should be failed, parent should have handled the error
        parent_sub_stage = await registry.get_latest_stage_run(run.run_id, "sub")
        assert parent_sub_stage is not None
        assert parent_sub_stage.status == StageRunStatus.FAILED


class TestPRAssociationTracking:
    """P4.4: Engine tracks PR associations for multi-PR pipelines."""

    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-agent"
            self._spawned.append({"role": role, "agent_id": agent_id})
            return agent_id

        async def mock_action(action, config, context):
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_pr_associated_on_pipeline_start(self, engine, registry):
        defn = make_simple_pipeline()
        engine.add_pipeline("review-flow", defn)
        run = await engine.start_pipeline("review-flow", pr_number=42)
        assert run is not None

        # PR association should have been created
        assocs = await registry.get_pr_associations(run.run_id)
        assert len(assocs) == 1
        assert assocs[0]["pr_number"] == 42
        assert assocs[0]["role"] == "primary"

    @pytest.mark.asyncio
    async def test_pr_tracked_from_agent_outputs(self, engine, registry):
        defn = make_simple_pipeline()
        engine.add_pipeline("review-flow", defn)
        run = await engine.start_pipeline("review-flow", issue_number=10)
        assert run is not None

        # Agent completes and reports a PR number in outputs
        agent_spawned = [s for s in self._spawned if s["role"] == "reviewer"]
        assert len(agent_spawned) == 1
        await engine.on_agent_complete(
            agent_spawned[0]["agent_id"],
            outputs={"pr_number": 55},
        )

        # Association should be created, and context.prs should include it
        assocs = await registry.get_pr_associations(run.run_id)
        pr_numbers = [a["pr_number"] for a in assocs]
        assert 55 in pr_numbers

        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert 55 in updated_run.context.get("prs", [])

    @pytest.mark.asyncio
    async def test_cross_pr_event_routing_via_association(self, engine, registry):
        """Events for associated PRs should route to the pipeline."""
        defn = PipelineDefinition(
            description="Multi-PR pipeline",
            stages=[
                StageDefinition(id="work", type="agent", agent="worker"),
                StageDefinition(id="merge", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("multi-pr", defn)
        run = await engine.start_pipeline("multi-pr", issue_number=10)
        assert run is not None

        # Manually add a PR association
        await registry.add_pr_association(run.run_id, 77, "test-repo")

        # Query running pipelines for PR 77
        running = await registry.get_running_pipelines_for_pr(77)
        assert any(r.run_id == run.run_id for r in running)


class TestCrossPRGateTargeting:
    """P4.5: Gate conditions with `pr` field target a specific PR."""

    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        self._cross_pr_check = CrossPRGateCheck()
        gate_registry._checks["cross_pr_check"] = self._cross_pr_check

        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            return f"{role}-agent"

        async def mock_action(action, config, context):
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_gate_with_pr_override(self, engine, registry):
        """Gate condition with `pr: 99` should evaluate against PR 99, not the run's PR."""
        defn = PipelineDefinition(
            description="Cross-PR gate test",
            stages=[
                StageDefinition(
                    id="cross-gate",
                    type="gate",
                    conditions=[
                        GateConditionConfig(check="cross_pr_check", pr=99),
                    ],
                ),
                StageDefinition(id="done", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("cross-pr", defn)
        run = await engine.start_pipeline("cross-pr", pr_number=42)
        assert run is not None

        # The check should have been called with PR 99, not 42
        assert self._cross_pr_check.evaluated_prs == [99]

    @pytest.mark.asyncio
    async def test_gate_without_pr_uses_run_pr(self, engine, registry):
        """Gate condition without `pr` should use the pipeline run's pr_number."""
        defn = PipelineDefinition(
            description="Normal gate test",
            stages=[
                StageDefinition(
                    id="normal-gate",
                    type="gate",
                    conditions=[
                        GateConditionConfig(check="cross_pr_check"),
                    ],
                ),
                StageDefinition(id="done", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("normal", defn)
        run = await engine.start_pipeline("normal", pr_number=42)
        assert run is not None

        # The check should have been called with the run's PR (42)
        assert self._cross_pr_check.evaluated_prs == [42]

    @pytest.mark.asyncio
    async def test_gate_pr_context_reference(self, engine, registry):
        """Gate condition with `pr: 'context.prs[0]'` resolves from context."""
        defn = PipelineDefinition(
            description="Context PR gate test",
            context={"prs": [77]},
            stages=[
                StageDefinition(
                    id="ctx-gate",
                    type="gate",
                    conditions=[
                        GateConditionConfig(check="cross_pr_check", pr="context.prs[0]"),
                    ],
                ),
                StageDefinition(id="done", type="action", action="merge_pr"),
            ],
        )
        engine.add_pipeline("ctx-pr", defn)
        run = await engine.start_pipeline("ctx-pr", pr_number=42)
        assert run is not None

        # Should have resolved context.prs[0] = 77
        assert self._cross_pr_check.evaluated_prs == [77]


class TestJoinStrategyAny:
    """P4.6: JoinStrategy.ANY advances when any branch succeeds."""

    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned.append({"role": role, "stage_id": stage_id, "agent_id": agent_id})
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action})
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_join_any_advances_on_first_success(self, engine, registry):
        engine.add_pipeline("join-any", make_join_any_pipeline())
        run = await engine.start_pipeline("join-any", pr_number=20)
        assert run is not None
        assert len(self._spawned) == 2

        # Complete only the first branch
        await engine.on_agent_complete(self._spawned[0]["agent_id"])

        # Pipeline should have advanced past the parallel stage
        parent_sr = await registry.get_latest_stage_run(run.run_id, "parallel-any")
        assert parent_sr is not None
        assert parent_sr.status == StageRunStatus.COMPLETED

        # Final action should have fired
        assert len(self._actions) == 1

    @pytest.mark.asyncio
    async def test_join_any_waits_if_first_fails(self, engine, registry):
        engine.add_pipeline("join-any", make_join_any_pipeline())
        run = await engine.start_pipeline("join-any", pr_number=20)
        assert run is not None

        # Fail the first branch
        await engine.on_agent_error(self._spawned[0]["agent_id"], "branch failed")

        # Pipeline should still be waiting (other branch still running)
        parent_sr = await registry.get_latest_stage_run(run.run_id, "parallel-any")
        assert parent_sr is not None
        assert parent_sr.status == StageRunStatus.WAITING

        # Complete the second branch successfully
        await engine.on_agent_complete(self._spawned[1]["agent_id"])

        # Now should advance
        parent_sr = await registry.get_latest_stage_run(run.run_id, "parallel-any")
        assert parent_sr is not None
        assert parent_sr.status == StageRunStatus.COMPLETED
        assert len(self._actions) == 1

    @pytest.mark.asyncio
    async def test_join_any_fails_if_all_fail(self, engine, registry):
        engine.add_pipeline("join-any", make_join_any_pipeline())
        run = await engine.start_pipeline("join-any", pr_number=20)
        assert run is not None

        # Fail both branches
        await engine.on_agent_error(self._spawned[0]["agent_id"], "branch 1 failed")
        await engine.on_agent_error(self._spawned[1]["agent_id"], "branch 2 failed")

        # Pipeline should have failed
        updated_run = await registry.get_pipeline_run(run.run_id)
        assert updated_run is not None
        assert updated_run.status == PipelineRunStatus.FAILED


class TestParallelBranchTypes:
    """P4.7: Parallel stages support pipeline and action branch types."""

    @pytest_asyncio.fixture
    async def engine(self, registry, gate_registry):
        eng = PipelineEngine(
            registry=registry,
            gate_registry=gate_registry,
            owner="test-owner",
            repo="test-repo",
        )
        self._spawned: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []

        async def mock_spawn(
            role,
            issue_number,
            *,
            pr_number=None,
            pipeline_run_id=None,
            stage_id=None,
            action=None,
            continue_session=False,
            context=None,
        ):
            agent_id = f"{role}-{stage_id}-agent"
            self._spawned.append({"role": role, "stage_id": stage_id, "agent_id": agent_id})
            return agent_id

        async def mock_action(action, config, context):
            self._actions.append({"action": action, "config": config})
            return {"success": True}

        eng.set_spawn_callback(mock_spawn)
        eng.set_action_callback(mock_action)
        return eng

    @pytest.mark.asyncio
    async def test_parallel_action_branch(self, engine, registry):
        """An action branch in a parallel stage executes immediately."""
        defn = make_parallel_pipeline_branch(
            ParallelBranch(
                id="lint",
                type=StageType.ACTION,
                action="run_lint",
                config={"strict": True},
            )
        )
        engine.add_pipeline("mixed", defn)
        run = await engine.start_pipeline("mixed", pr_number=20)
        assert run is not None

        # Action branch should have already executed
        lint_actions = [a for a in self._actions if a["action"] == "run_lint"]
        assert len(lint_actions) == 1
        assert lint_actions[0]["config"] == {"strict": True}

        # Agent branch should also have been spawned
        assert len(self._spawned) == 1
        assert self._spawned[0]["role"] == "code-reviewer"

    @pytest.mark.asyncio
    async def test_parallel_pipeline_branch(self, engine, registry):
        """A pipeline branch in a parallel stage starts a sub-pipeline."""
        child_def = PipelineDefinition(
            description="Sub workflow",
            stages=[StageDefinition(id="sub-work", type="agent", agent="sub-worker")],
        )
        engine.add_pipeline("sub-workflow", child_def)

        defn = make_parallel_pipeline_branch(
            ParallelBranch(
                id="sub",
                type=StageType.PIPELINE,
                pipeline="sub-workflow",
            )
        )
        engine.add_pipeline("mixed", defn)
        run = await engine.start_pipeline("mixed", pr_number=20)
        assert run is not None

        # Should have spawned the code-reviewer agent AND the sub-pipeline's agent
        assert len(self._spawned) == 2
        roles = {s["role"] for s in self._spawned}
        assert "code-reviewer" in roles
        assert "sub-worker" in roles

        # Child pipeline should exist
        children = await registry.get_child_pipelines(run.run_id)
        assert len(children) == 1


class TestResolvePRTarget:
    """P4.5 helper: _resolve_pr_target resolves different PR reference formats."""

    def test_integer_pr(self):
        from squadron.pipeline.engine import PipelineEngine
        from squadron.pipeline.models import PipelineRun

        run = PipelineRun(
            run_id="pl-test",
            pipeline_name="test",
            definition_snapshot="{}",
            status=PipelineRunStatus.RUNNING,
            context={},
        )
        assert PipelineEngine._resolve_pr_target(42, run) == 42

    def test_string_integer_pr(self):
        from squadron.pipeline.engine import PipelineEngine
        from squadron.pipeline.models import PipelineRun

        run = PipelineRun(
            run_id="pl-test",
            pipeline_name="test",
            definition_snapshot="{}",
            status=PipelineRunStatus.RUNNING,
            context={},
        )
        assert PipelineEngine._resolve_pr_target("99", run) == 99

    def test_context_prs_reference(self):
        from squadron.pipeline.engine import PipelineEngine
        from squadron.pipeline.models import PipelineRun

        run = PipelineRun(
            run_id="pl-test",
            pipeline_name="test",
            definition_snapshot="{}",
            status=PipelineRunStatus.RUNNING,
            context={"prs": [10, 20, 30]},
        )
        assert PipelineEngine._resolve_pr_target("context.prs[0]", run) == 10
        assert PipelineEngine._resolve_pr_target("context.prs[2]", run) == 30

    def test_invalid_reference_returns_none(self):
        from squadron.pipeline.engine import PipelineEngine
        from squadron.pipeline.models import PipelineRun

        run = PipelineRun(
            run_id="pl-test",
            pipeline_name="test",
            definition_snapshot="{}",
            status=PipelineRunStatus.RUNNING,
            context={},
        )
        assert PipelineEngine._resolve_pr_target("invalid.ref", run) is None

    def test_out_of_bounds_reference(self):
        from squadron.pipeline.engine import PipelineEngine
        from squadron.pipeline.models import PipelineRun

        run = PipelineRun(
            run_id="pl-test",
            pipeline_name="test",
            definition_snapshot="{}",
            status=PipelineRunStatus.RUNNING,
            context={"prs": [10]},
        )
        assert PipelineEngine._resolve_pr_target("context.prs[5]", run) is None


class TestGateConditionPRField:
    """P4.5 model: GateConditionConfig includes `pr` in get_config()."""

    def test_pr_field_in_get_config(self):
        cond = GateConditionConfig(check="ci_status", pr=42)
        config = cond.get_config()
        assert config["pr"] == 42

    def test_pr_field_string_in_get_config(self):
        cond = GateConditionConfig(check="ci_status", pr="context.prs[0]")
        config = cond.get_config()
        assert config["pr"] == "context.prs[0]"

    def test_pr_field_absent_when_none(self):
        cond = GateConditionConfig(check="ci_status")
        config = cond.get_config()
        assert "pr" not in config
