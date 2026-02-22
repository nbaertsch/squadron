"""Tests for the PipelineEngine — reactive pipeline orchestration.

Covers:
- Pipeline trigger evaluation
- Stage execution (agent, gate, delay, action)
- Gate re-evaluation on subscribed events
- Agent lifecycle hooks (complete, blocked, error)
- Transition logic (on_complete, on_pass, on_fail)
- Pipeline termination (complete, fail, escalate)
"""

from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from squadron.pipeline.engine import PipelineEngine
from squadron.pipeline.registry import (
    PipelineRegistry,
    PipelineRunStatus,
    PipelineStageStatus,
)
from squadron.config import (
    PipelineConfig,
    PipelineGateCheck,
    PipelineStageConfig,
    PipelineEventSubscription,
    WorkflowTrigger,
    StageType,
)
from squadron.models import SquadronEvent, SquadronEventType


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "test.db")) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


@pytest_asyncio.fixture
async def pl_registry(db):
    reg = PipelineRegistry(db)
    await reg.initialize()
    return reg


@pytest.fixture
def spawn_mock():
    calls = []

    async def mock_spawn(role, issue_number, **kwargs):
        agent_id = f"agent-{role}-{issue_number}"
        calls.append({"role": role, "issue_number": issue_number, "agent_id": agent_id, **kwargs})
        return agent_id

    mock_spawn.calls = calls
    return mock_spawn


@pytest.fixture
def passing_gate_registry():
    """A gate registry where all checks pass."""
    from squadron.pipeline.gates import GateCheckRegistry
    from squadron.config import GateCheckResult

    reg = GateCheckRegistry.__new__(GateCheckRegistry)
    reg._checks = {}

    async def always_pass(ctx):
        return GateCheckResult(check_type=ctx.params.get("check", "test"), passed=True)

    reg.register_fn("command", always_pass)
    reg.register_fn("file_exists", always_pass)
    reg.register_fn("pr_approvals_met", always_pass)
    reg.register_fn("no_changes_requested", always_pass)
    reg.register_fn("human_approved", always_pass)
    return reg


@pytest.fixture
def failing_gate_registry():
    """A gate registry where all checks fail."""
    from squadron.pipeline.gates import GateCheckRegistry
    from squadron.config import GateCheckResult

    reg = GateCheckRegistry.__new__(GateCheckRegistry)
    reg._checks = {}

    async def always_fail(ctx):
        return GateCheckResult(
            check_type=ctx.params.get("check", "test"),
            passed=False,
            error_message="check failed",
        )

    for name in ["command", "pr_approvals_met", "no_changes_requested"]:
        reg.register_fn(name, always_fail)
    return reg


def make_engine(pl_registry, spawn_mock=None, gate_registry=None):
    engine = PipelineEngine(
        registry=pl_registry,
        gate_registry=gate_registry,
        owner="test-owner",
        repo="test-repo",
    )
    if spawn_mock:
        engine.set_spawn_callback(spawn_mock)
    return engine


def make_event(event_type=SquadronEventType.ISSUE_LABELED, issue_number=1, pr_number=None):
    return SquadronEvent(
        event_type=event_type,
        issue_number=issue_number,
        pr_number=pr_number,
        data={"payload": {}},
    )


# ── Simple Two-Stage Pipeline ──────────────────────────────────────────────────


def make_two_stage_pipeline():
    return PipelineConfig(
        trigger=WorkflowTrigger(
            event="issues.labeled",
            conditions={"label": "feature"},
        ),
        stages=[
            PipelineStageConfig(
                id="develop",
                type=StageType.AGENT,
                agent="feat-dev",
                on_complete="review",
            ),
            PipelineStageConfig(
                id="review",
                type=StageType.AGENT,
                agent="pr-review",
            ),
        ],
    )


class TestTriggerEvaluation:
    async def test_trigger_match_creates_run(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)
        engine.add_pipeline("two-stage", make_two_stage_pipeline())

        payload = {"label": {"name": "feature"}, "issue": {"number": 10}}
        event = make_event(issue_number=10)

        run = await engine.evaluate_event("issues.labeled", payload, event)
        assert run is not None
        assert run.pipeline_name == "two-stage"
        assert run.issue_number == 10
        assert run.status == PipelineRunStatus.RUNNING

    async def test_trigger_no_match_returns_none(self, pl_registry):
        engine = make_engine(pl_registry)
        engine.add_pipeline("two-stage", make_two_stage_pipeline())

        payload = {"label": {"name": "bug"}}  # wrong label
        event = make_event(issue_number=5)

        run = await engine.evaluate_event("issues.labeled", payload, event)
        assert run is None

    async def test_duplicate_run_not_created(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)
        engine.add_pipeline("two-stage", make_two_stage_pipeline())

        payload = {"label": {"name": "feature"}, "issue": {"number": 20}}
        event = make_event(issue_number=20)

        run1 = await engine.evaluate_event("issues.labeled", payload, event)
        run2 = await engine.evaluate_event("issues.labeled", payload, event)
        assert run1 is not None
        assert run2 is None  # Duplicate prevented

    async def test_first_stage_agent_spawned(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)
        engine.add_pipeline("two-stage", make_two_stage_pipeline())

        payload = {"label": {"name": "feature"}, "issue": {"number": 15}}
        event = make_event(issue_number=15)

        await engine.evaluate_event("issues.labeled", payload, event)

        assert len(spawn_mock.calls) == 1
        assert spawn_mock.calls[0]["role"] == "feat-dev"
        assert spawn_mock.calls[0]["issue_number"] == 15


# ── Agent Stage ────────────────────────────────────────────────────────────────


class TestAgentStage:
    async def test_on_agent_complete_advances_pipeline(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)
        engine.add_pipeline("two-stage", make_two_stage_pipeline())

        payload = {"label": {"name": "feature"}, "issue": {"number": 30}}
        event = make_event(issue_number=30)
        run = await engine.evaluate_event("issues.labeled", payload, event)

        # First stage spawned
        assert len(spawn_mock.calls) == 1
        agent_id = spawn_mock.calls[0]["agent_id"]

        # Agent completes
        await engine.on_agent_complete(agent_id, {"output": "done"})

        # Second stage should have been started
        assert len(spawn_mock.calls) == 2
        assert spawn_mock.calls[1]["role"] == "pr-review"

    async def test_on_agent_complete_no_stage_run(self, pl_registry):
        """on_agent_complete with unknown agent_id does nothing gracefully."""
        engine = make_engine(pl_registry)
        await engine.on_agent_complete("nonexistent-agent-id")

    async def test_on_agent_error_fails_pipeline(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)
        engine.add_pipeline("two-stage", make_two_stage_pipeline())

        payload = {"label": {"name": "feature"}, "issue": {"number": 40}}
        event = make_event(issue_number=40)
        run = await engine.evaluate_event("issues.labeled", payload, event)
        agent_id = spawn_mock.calls[0]["agent_id"]

        await engine.on_agent_error(agent_id, "some error occurred")

        refreshed = await pl_registry.get_run(run.run_id)
        assert refreshed.status == PipelineRunStatus.FAILED
        assert refreshed.error_message is not None  # error message set

    async def test_on_agent_blocked_does_not_fail(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)
        engine.add_pipeline("two-stage", make_two_stage_pipeline())

        payload = {"label": {"name": "feature"}, "issue": {"number": 50}}
        event = make_event(issue_number=50)
        run = await engine.evaluate_event("issues.labeled", payload, event)
        agent_id = spawn_mock.calls[0]["agent_id"]

        await engine.on_agent_blocked(agent_id, "waiting for blocker")

        # Pipeline should still be RUNNING
        refreshed = await pl_registry.get_run(run.run_id)
        assert refreshed.status == PipelineRunStatus.RUNNING

    async def test_last_stage_completes_pipeline(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)

        # Single stage pipeline
        pipeline = PipelineConfig(
            trigger=WorkflowTrigger(event="issues.labeled"),
            stages=[
                PipelineStageConfig(
                    id="only-stage",
                    type=StageType.AGENT,
                    agent="feat-dev",
                ),
            ],
        )
        engine.add_pipeline("single", pipeline)

        payload = {"issue": {"number": 60}}
        event = make_event(issue_number=60)
        run = await engine.evaluate_event("issues.labeled", payload, event)
        agent_id = spawn_mock.calls[0]["agent_id"]

        await engine.on_agent_complete(agent_id)

        refreshed = await pl_registry.get_run(run.run_id)
        assert refreshed.status == PipelineRunStatus.COMPLETED


# ── Gate Stage ─────────────────────────────────────────────────────────────────


class TestGateStage:
    def make_gate_pipeline(self, gate_on_fail="develop"):
        return PipelineConfig(
            trigger=WorkflowTrigger(event="issues.labeled"),
            stages=[
                PipelineStageConfig(
                    id="develop",
                    type=StageType.AGENT,
                    agent="feat-dev",
                    on_complete="quality-gate",
                ),
                PipelineStageConfig(
                    id="quality-gate",
                    type=StageType.GATE,
                    gate_checks=[
                        PipelineGateCheck(check="pr_approvals_met", params={"count": 1}),
                    ],
                    on_pass="deploy",
                    on_fail=gate_on_fail,
                ),
                PipelineStageConfig(
                    id="deploy",
                    type=StageType.AGENT,
                    agent="deployer",
                ),
            ],
        )

    async def test_gate_pass_advances_to_next_stage(
        self, pl_registry, spawn_mock, passing_gate_registry
    ):
        engine = make_engine(pl_registry, spawn_mock, gate_registry=passing_gate_registry)
        engine.add_pipeline("gate-flow", self.make_gate_pipeline())

        payload = {"issue": {"number": 70}}
        event = make_event(issue_number=70)
        run = await engine.evaluate_event("issues.labeled", payload, event)
        dev_agent = spawn_mock.calls[0]["agent_id"]

        # Developer completes → gate should evaluate and pass
        await engine.on_agent_complete(dev_agent)

        # Gate passed → should spawn deployer
        assert len(spawn_mock.calls) == 2
        assert spawn_mock.calls[1]["role"] == "deployer"

    async def test_gate_fail_routes_to_on_fail(
        self, pl_registry, spawn_mock, failing_gate_registry
    ):
        engine = make_engine(pl_registry, spawn_mock, gate_registry=failing_gate_registry)
        engine.add_pipeline("gate-flow", self.make_gate_pipeline(gate_on_fail="develop"))

        payload = {"issue": {"number": 80}}
        event = make_event(issue_number=80)
        run = await engine.evaluate_event("issues.labeled", payload, event)
        dev_agent = spawn_mock.calls[0]["agent_id"]

        # Developer completes → gate should fail → routes back to develop
        await engine.on_agent_complete(dev_agent)

        # develop spawned again (iteration 2)
        assert len(spawn_mock.calls) == 2
        assert spawn_mock.calls[1]["role"] == "feat-dev"

    async def test_gate_pass_stores_check_results(
        self, pl_registry, spawn_mock, passing_gate_registry
    ):
        engine = make_engine(pl_registry, spawn_mock, gate_registry=passing_gate_registry)
        engine.add_pipeline("gate-flow", self.make_gate_pipeline())

        payload = {"issue": {"number": 90}}
        event = make_event(issue_number=90)
        run = await engine.evaluate_event("issues.labeled", payload, event)
        agent_id = spawn_mock.calls[0]["agent_id"]
        await engine.on_agent_complete(agent_id)

        # Fetch stage runs and verify gate checks were recorded
        stage_runs = await pl_registry.get_stage_runs_for_run(run.run_id)
        gate_stages = [s for s in stage_runs if s.stage_id == "quality-gate"]
        assert len(gate_stages) >= 1

        gate_checks = await pl_registry.get_gate_checks_for_stage(gate_stages[0].id)
        assert len(gate_checks) >= 1
        assert all(c["passed"] for c in gate_checks)


# ── Reactive Gate Re-evaluation ────────────────────────────────────────────────


class TestReactiveGate:
    async def test_on_event_reevaluates_waiting_gate(self, pl_registry, spawn_mock):
        """A waiting pipeline gate is re-evaluated when a subscribed event arrives."""
        fail_count = {"n": 0}

        from squadron.pipeline.gates import GateCheckRegistry
        from squadron.config import GateCheckResult

        gate_reg = GateCheckRegistry.__new__(GateCheckRegistry)
        gate_reg._checks = {}

        async def conditional_check(ctx):
            fail_count["n"] += 1
            if fail_count["n"] >= 2:
                return GateCheckResult(check_type="test", passed=True)
            return GateCheckResult(
                check_type="test", passed=False, error_message="not ready"
            )

        gate_reg.register_fn("pr_approvals_met", conditional_check)

        pipeline = PipelineConfig(
            trigger=WorkflowTrigger(event="issues.labeled"),
            stages=[
                PipelineStageConfig(
                    id="gate",
                    type=StageType.GATE,
                    gate_checks=[
                        PipelineGateCheck(check="pr_approvals_met", params={}),
                    ],
                    on_pass="done",
                    on_fail="gate",  # self-referential — will wait
                    event_subscriptions=[
                        PipelineEventSubscription(
                            event="pull_request_review.submitted"
                        ),
                    ],
                ),
                PipelineStageConfig(id="done", type=StageType.AGENT, agent="closer"),
            ],
        )

        engine = make_engine(pl_registry, spawn_mock, gate_registry=gate_reg)
        engine.add_pipeline("reactive", pipeline)

        # Trigger pipeline
        payload = {"issue": {"number": 100}}
        event = make_event(issue_number=100)
        run = await engine.evaluate_event("issues.labeled", payload, event)

        # First gate evaluation fails, run goes to WAITING
        refreshed = await pl_registry.get_run(run.run_id)
        assert refreshed.status == PipelineRunStatus.WAITING

        # Simulate a PR review event arriving
        review_event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=run.pr_number,
            issue_number=100,
            data={},
        )
        await engine.on_event("pull_request_review.submitted", review_event)

        # Allow the re-evaluation task to complete
        await asyncio.sleep(0.05)

        # Gate should have passed, advancing to 'done' stage
        refreshed = await pl_registry.get_run(run.run_id)
        assert refreshed.status in (
            PipelineRunStatus.RUNNING,
            PipelineRunStatus.COMPLETED,
        )
        assert spawn_mock.calls, "No agent spawned after gate pass"
        assert spawn_mock.calls[-1]["role"] == "closer"

    async def test_on_event_ignored_for_completed_run(self, pl_registry):
        """Events are not routed to completed pipelines."""
        engine = make_engine(pl_registry)

        # No running/waiting pipelines subscribed
        event = make_event()
        await engine.on_event("pull_request_review.submitted", event)
        # No error should be raised


# ── Pipeline Completion Paths ──────────────────────────────────────────────────


class TestPipelineTermination:
    async def test_empty_pipeline_completes_immediately(self, pl_registry):
        engine = make_engine(pl_registry)
        engine.add_pipeline(
            "empty",
            PipelineConfig(
                trigger=WorkflowTrigger(event="issues.labeled"),
                stages=[],
            ),
        )

        payload = {"issue": {"number": 200}}
        event = make_event(issue_number=200)
        run = await engine.evaluate_event("issues.labeled", payload, event)
        assert run is not None

        refreshed = await pl_registry.get_run(run.run_id)
        assert refreshed.status == PipelineRunStatus.COMPLETED

    async def test_explicit_complete_transition(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)

        pipeline = PipelineConfig(
            trigger=WorkflowTrigger(event="issues.labeled"),
            stages=[
                PipelineStageConfig(
                    id="work",
                    type=StageType.AGENT,
                    agent="worker",
                    on_complete="complete",  # explicit completion token
                ),
            ],
        )
        engine.add_pipeline("explicit-complete", pipeline)

        payload = {"issue": {"number": 210}}
        event = make_event(issue_number=210)
        run = await engine.evaluate_event("issues.labeled", payload, event)
        agent_id = spawn_mock.calls[0]["agent_id"]
        await engine.on_agent_complete(agent_id)

        refreshed = await pl_registry.get_run(run.run_id)
        assert refreshed.status == PipelineRunStatus.COMPLETED

    async def test_max_iterations_causes_escalation(self, pl_registry, spawn_mock):
        engine = make_engine(pl_registry, spawn_mock)

        pipeline = PipelineConfig(
            trigger=WorkflowTrigger(event="issues.labeled"),
            stages=[
                PipelineStageConfig(
                    id="work",
                    type=StageType.AGENT,
                    agent="worker",
                    on_complete="work",  # loop back to itself
                    max_iterations=2,
                ),
            ],
        )
        engine.add_pipeline("loop", pipeline)

        payload = {"issue": {"number": 220}}
        event = make_event(issue_number=220)
        run = await engine.evaluate_event("issues.labeled", payload, event)

        # Complete twice (reaches max_iterations on third attempt)
        for _ in range(2):
            agent_id = spawn_mock.calls[-1]["agent_id"]
            await engine.on_agent_complete(agent_id)

        refreshed = await pl_registry.get_run(run.run_id)
        assert refreshed.status == PipelineRunStatus.ESCALATED


# ── Config ─────────────────────────────────────────────────────────────────────


class TestPipelineConfig:
    def test_pipeline_config_collect_events(self):
        pipeline = PipelineConfig(
            trigger=WorkflowTrigger(event="issues.labeled"),
            event_subscriptions=[
                PipelineEventSubscription(event="check_suite.completed"),
            ],
            stages=[
                PipelineStageConfig(
                    id="gate",
                    type=StageType.GATE,
                    event_subscriptions=[
                        PipelineEventSubscription(
                            event="pull_request_review.submitted"
                        ),
                    ],
                ),
            ],
        )
        events = pipeline.collect_subscribed_events()
        assert "check_suite.completed" in events
        assert "pull_request_review.submitted" in events

    def test_pipeline_config_get_stage(self):
        pipeline = make_two_stage_pipeline()
        stage = pipeline.get_stage("develop")
        assert stage is not None
        assert stage.agent == "feat-dev"
        assert pipeline.get_stage("nonexistent") is None

    def test_pipeline_config_get_next_stage_id(self):
        pipeline = make_two_stage_pipeline()
        assert pipeline.get_next_stage_id("develop") == "review"
        assert pipeline.get_next_stage_id("review") is None

    def test_pipeline_config_from_yaml(self):
        """PipelineConfig is loadable via SquadronConfig YAML integration."""
        from squadron.config import SquadronConfig
        import yaml

        yaml_text = """
project:
  name: test
pipelines:
  review-flow:
    trigger:
      event: pull_request.opened
    stages:
      - id: review
        type: agent
        agent: pr-review
      - id: merge-gate
        type: gate
        gate_checks:
          - check: pr_approvals_met
            params:
              count: 1
          - check: ci_status
        on_pass: merge
        on_fail: review
      - id: merge
        type: action
        action: merge_pr
"""
        raw = yaml.safe_load(yaml_text)
        config = SquadronConfig(**raw)
        assert "review-flow" in config.pipelines
        pl = config.pipelines["review-flow"]
        assert len(pl.stages) == 3
        gate_stage = pl.get_stage("merge-gate")
        assert gate_stage is not None
        assert len(gate_stage.gate_checks) == 2
        assert gate_stage.gate_checks[0].check == "pr_approvals_met"
        assert gate_stage.gate_checks[0].params["count"] == 1


# ── Workflow Engine pr_approval Gate ──────────────────────────────────────────


class TestWorkflowEnginePrApprovalGate:
    """Tests for the pr_approval gate fix (AD-019 gap #2)."""

    async def test_pr_approval_gate_passes(self):
        """pr_approval gate passes when there are sufficient approvals."""
        from squadron.workflow.engine import WorkflowEngine
        from squadron.workflow.registry import WorkflowRegistryV2
        from squadron.config import (
            GateCondition,
            WorkflowConfig,
            WorkflowRun,
            WorkflowRunStatus,
            WorkflowTrigger,
        )
        import aiosqlite
        import tempfile, os

        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [{"agent_role": "pr-review", "agent_id": "agent-1"}]

        # Create a minimal WorkflowRun with pr_number set
        run = WorkflowRun(
            run_id="wf-test001",
            workflow_name="test",
            pr_number=42,
            status=WorkflowRunStatus.RUNNING,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            async with aiosqlite.connect(os.path.join(tmpdir, "test.db")) as db:
                db.row_factory = aiosqlite.Row
                wf_registry = WorkflowRegistryV2(db)
                await wf_registry.initialize()

                engine = WorkflowEngine(wf_registry)
                engine.set_agent_registry(MockRegistry())

                condition = GateCondition(check="pr_approval", count=1)
                result = await engine._check_pr_approval(condition, run)

                assert result.passed is True
                assert result.result_data["actual"] == 1

    async def test_pr_approval_gate_fails_insufficient(self):
        """pr_approval gate fails when count is not met."""
        from squadron.workflow.engine import WorkflowEngine
        from squadron.workflow.registry import WorkflowRegistryV2
        from squadron.config import GateCondition, WorkflowRun, WorkflowRunStatus
        import aiosqlite
        import tempfile, os

        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return []

        run = WorkflowRun(
            run_id="wf-test002",
            workflow_name="test",
            pr_number=42,
            status=WorkflowRunStatus.RUNNING,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            async with aiosqlite.connect(os.path.join(tmpdir, "test.db")) as db:
                db.row_factory = aiosqlite.Row
                wf_registry = WorkflowRegistryV2(db)
                await wf_registry.initialize()

                engine = WorkflowEngine(wf_registry)
                engine.set_agent_registry(MockRegistry())

                condition = GateCondition(check="pr_approval", count=1)
                result = await engine._check_pr_approval(condition, run)

                assert result.passed is False
                assert "0/1" in result.error_message

    async def test_pr_approval_gate_no_registry(self):
        """pr_approval gate fails gracefully when no registry is set."""
        from squadron.workflow.engine import WorkflowEngine
        from squadron.workflow.registry import WorkflowRegistryV2
        from squadron.config import GateCondition, WorkflowRun, WorkflowRunStatus
        import aiosqlite
        import tempfile, os

        run = WorkflowRun(
            run_id="wf-test003",
            workflow_name="test",
            pr_number=42,
            status=WorkflowRunStatus.RUNNING,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            async with aiosqlite.connect(os.path.join(tmpdir, "test.db")) as db:
                db.row_factory = aiosqlite.Row
                wf_registry = WorkflowRegistryV2(db)
                await wf_registry.initialize()

                engine = WorkflowEngine(wf_registry)
                # No registry set

                condition = GateCondition(check="pr_approval", count=1)
                result = await engine._check_pr_approval(condition, run)

                assert result.passed is False
                assert "registry" in result.error_message.lower()

    async def test_evaluate_condition_pr_approval_delegates_to_check(self):
        """_evaluate_condition correctly routes pr_approval to _check_pr_approval."""
        from squadron.workflow.engine import WorkflowEngine
        from squadron.workflow.registry import WorkflowRegistryV2
        from squadron.config import (
            GateCondition,
            WorkflowConfig,
            WorkflowRun,
            WorkflowRunStatus,
            WorkflowTrigger,
            StageDefinition,
            StageType,
        )
        import aiosqlite
        import tempfile, os

        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [{"agent_role": "human", "agent_id": "alice"}]

        run = WorkflowRun(
            run_id="wf-eval001",
            workflow_name="test",
            pr_number=5,
            status=WorkflowRunStatus.RUNNING,
        )

        workflow = WorkflowConfig(
            trigger=WorkflowTrigger(event="issues.labeled"),
            stages=[
                StageDefinition(id="gate", type=StageType.GATE, conditions=[
                    GateCondition(check="pr_approval", count=1)
                ])
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            async with aiosqlite.connect(os.path.join(tmpdir, "test.db")) as db:
                db.row_factory = aiosqlite.Row
                wf_registry = WorkflowRegistryV2(db)
                await wf_registry.initialize()

                engine = WorkflowEngine(wf_registry)
                engine.set_agent_registry(MockRegistry())

                condition = GateCondition(check="pr_approval", count=1)
                result = await engine._evaluate_condition(condition, run, workflow)

                assert result.passed is True
