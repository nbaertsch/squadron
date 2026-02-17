"""Tests for workflow schema models."""

import pytest
from datetime import datetime, timezone

from pydantic import ValidationError

from squadron.config import (
    GateCheckResult,
    GateCondition,
    StageDefinition,
    StageRun,
    StageRunStatus,
    StageTransition,
    StageType,
    WorkflowConfig,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTrigger,
)


# ── StageType Tests ──────────────────────────────────────────────────────────


class TestStageType:
    def test_all_values(self):
        assert StageType.AGENT == "agent"
        assert StageType.GATE == "gate"
        assert StageType.PARALLEL == "parallel"
        assert StageType.DELAY == "delay"
        assert StageType.ACTION == "action"
        assert StageType.WEBHOOK == "webhook"


# ── WorkflowTrigger Tests ──────────────────────────────────────────────────


class TestWorkflowTrigger:
    def test_event_type_matching(self):
        trigger = WorkflowTrigger(event="issues.labeled")
        assert trigger.matches("issues.labeled", {})
        assert not trigger.matches("issues.opened", {})

    def test_label_condition(self):
        trigger = WorkflowTrigger(
            event="issues.labeled",
            conditions={"label": "feature"},
        )
        payload = {"label": {"name": "feature"}}
        assert trigger.matches("issues.labeled", payload)

        wrong_label = {"label": {"name": "bug"}}
        assert not trigger.matches("issues.labeled", wrong_label)

    def test_labels_any_match(self):
        trigger = WorkflowTrigger(
            event="issues.opened",
            conditions={"labels": ["urgent", "critical"]},
        )
        has_urgent = {"issue": {"labels": [{"name": "urgent"}]}}
        assert trigger.matches("issues.opened", has_urgent)

        has_critical = {"issue": {"labels": [{"name": "critical"}]}}
        assert trigger.matches("issues.opened", has_critical)

        has_neither = {"issue": {"labels": [{"name": "docs"}]}}
        assert not trigger.matches("issues.opened", has_neither)

    def test_base_branch_condition(self):
        trigger = WorkflowTrigger(
            event="pull_request.opened",
            conditions={"base_branch": "main"},
        )
        payload = {"pull_request": {"base": {"ref": "main"}}}
        assert trigger.matches("pull_request.opened", payload)

        wrong_branch = {"pull_request": {"base": {"ref": "develop"}}}
        assert not trigger.matches("pull_request.opened", wrong_branch)

    def test_no_conditions_matches_all(self):
        trigger = WorkflowTrigger(event="issues.opened")
        assert trigger.matches("issues.opened", {})
        assert trigger.matches("issues.opened", {"any": "payload"})


# ── StageTransition Tests ─────────────────────────────────────────────────────


class TestStageTransition:
    def test_from_string_goto(self):
        transition = StageTransition.from_value("review-stage")
        assert transition.goto == "review-stage"
        assert transition.delay is None

    def test_from_string_complete(self):
        transition = StageTransition.from_value("complete")
        assert transition.goto == "__complete__"

    def test_from_string_escalate(self):
        transition = StageTransition.from_value("escalate")
        assert transition.goto == "__escalate__"

    def test_from_dict(self):
        transition = StageTransition.from_value(
            {
                "goto": "next-stage",
                "delay": "30s",
                "max_iterations": 3,
                "then": "escalate",
            }
        )
        assert transition.goto == "next-stage"
        assert transition.delay == "30s"
        assert transition.max_iterations == 3
        assert transition.then == "escalate"

    def test_from_none(self):
        assert StageTransition.from_value(None) is None


# ── GateCondition Tests ───────────────────────────────────────────────────────


class TestGateCondition:
    def test_command_check(self):
        condition = GateCondition(
            check="command",
            run="pytest tests/",
            expect="exit_code == 0",
        )
        assert condition.check == "command"
        assert condition.run == "pytest tests/"

    def test_evaluate_command_exit_0(self):
        condition = GateCondition(check="command", run="echo hello")
        assert condition.evaluate_command_result(0, "hello", "")
        assert not condition.evaluate_command_result(1, "", "error")

    def test_evaluate_command_custom_exit_code(self):
        condition = GateCondition(
            check="command",
            run="some_cmd",
            expect="exit_code == 2",
        )
        assert condition.evaluate_command_result(2, "", "")
        assert not condition.evaluate_command_result(0, "", "")

    def test_evaluate_command_not_equal(self):
        condition = GateCondition(
            check="command",
            run="some_cmd",
            expect="exit_code != 1",
        )
        assert condition.evaluate_command_result(0, "", "")
        assert condition.evaluate_command_result(2, "", "")
        assert not condition.evaluate_command_result(1, "", "")

    def test_file_exists_check(self):
        condition = GateCondition(
            check="file_exists",
            paths=["README.md", "pyproject.toml"],
        )
        assert condition.check == "file_exists"
        assert len(condition.paths) == 2

    def test_pr_approval_check(self):
        condition = GateCondition(
            check="pr_approval",
            count=2,
        )
        assert condition.check == "pr_approval"
        assert condition.count == 2


# ── StageDefinition Tests ─────────────────────────────────────────────────────


class TestStageDefinition:
    def test_valid_agent_stage(self):
        stage = StageDefinition(
            id="implement",
            name="Implementation",
            type=StageType.AGENT,
            agent="feat-dev",
            action="implement",
        )
        assert stage.id == "implement"
        assert stage.agent == "feat-dev"
        assert stage.type == StageType.AGENT

    def test_agent_stage_requires_agent(self):
        with pytest.raises(ValidationError):
            StageDefinition(
                id="missing-agent",
                type=StageType.AGENT,
            )

    def test_valid_gate_stage(self):
        stage = StageDefinition(
            id="quality-check",
            type=StageType.GATE,
            conditions=[
                GateCondition(check="command", run="pytest"),
            ],
        )
        assert stage.type == StageType.GATE
        assert len(stage.conditions) == 1

    def test_gate_stage_requires_conditions(self):
        with pytest.raises(ValidationError):
            StageDefinition(
                id="empty-gate",
                type=StageType.GATE,
            )

    def test_valid_delay_stage(self):
        stage = StageDefinition(
            id="wait",
            type=StageType.DELAY,
            duration="30s",
        )
        assert stage.type == StageType.DELAY
        assert stage.duration == "30s"

    def test_delay_stage_requires_duration(self):
        with pytest.raises(ValidationError):
            StageDefinition(
                id="no-duration",
                type=StageType.DELAY,
            )

    def test_invalid_stage_id(self):
        with pytest.raises(ValidationError):
            StageDefinition(
                id="invalid@id!",
                type=StageType.AGENT,
                agent="test",
            )

    def test_valid_stage_id_with_dashes_underscores(self):
        stage = StageDefinition(
            id="my-stage_01",
            type=StageType.AGENT,
            agent="test",
        )
        assert stage.id == "my-stage_01"

    def test_get_next_stage_complete(self):
        stage = StageDefinition(
            id="test",
            type=StageType.AGENT,
            agent="dev",
            on_complete="next-stage",
        )
        transition = stage.get_next_stage("complete")
        assert transition.goto == "next-stage"

    def test_get_next_stage_pass(self):
        stage = StageDefinition(
            id="gate",
            type=StageType.GATE,
            conditions=[GateCondition(check="command", run="echo")],
            on_pass="continue",
            on_fail="retry",
        )
        pass_transition = stage.get_next_stage("pass")
        assert pass_transition.goto == "continue"

        fail_transition = stage.get_next_stage("fail")
        assert fail_transition.goto == "retry"

    def test_get_next_stage_default(self):
        stage = StageDefinition(
            id="test",
            type=StageType.AGENT,
            agent="dev",
        )
        transition = stage.get_next_stage("complete")
        assert transition.goto == "__next__"

    def test_parse_timeout_seconds(self):
        stage = StageDefinition(
            id="test",
            type=StageType.AGENT,
            agent="dev",
            timeout="30s",
        )
        assert stage.parse_timeout_seconds() == 30

    def test_parse_timeout_minutes(self):
        stage = StageDefinition(
            id="test",
            type=StageType.AGENT,
            agent="dev",
            timeout="5m",
        )
        assert stage.parse_timeout_seconds() == 300

    def test_parse_timeout_hours(self):
        stage = StageDefinition(
            id="test",
            type=StageType.AGENT,
            agent="dev",
            timeout="2h",
        )
        assert stage.parse_timeout_seconds() == 7200

    def test_parse_timeout_none(self):
        stage = StageDefinition(
            id="test",
            type=StageType.AGENT,
            agent="dev",
        )
        assert stage.parse_timeout_seconds() is None


# ── WorkflowConfig Tests ──────────────────────────────────────────────────────────


class TestWorkflowConfig:
    def test_valid_workflow(self):
        workflow = WorkflowConfig(
            description="Feature development workflow",
            trigger=WorkflowTrigger(
                event="issues.labeled",
                conditions={"label": "feature"},
            ),
            stages=[
                StageDefinition(
                    id="plan",
                    type=StageType.AGENT,
                    agent="architect",
                ),
                StageDefinition(
                    id="implement",
                    type=StageType.AGENT,
                    agent="developer",
                ),
            ],
        )
        assert workflow.description == "Feature development workflow"
        assert len(workflow.stages) == 2

    def test_requires_at_least_one_stage(self):
        with pytest.raises(ValidationError):
            WorkflowConfig(
                trigger=WorkflowTrigger(event="issues.opened"),
                stages=[],
            )

    def test_duplicate_stage_ids_rejected(self):
        with pytest.raises(ValidationError):
            WorkflowConfig(
                trigger=WorkflowTrigger(event="issues.opened"),
                stages=[
                    StageDefinition(id="same", type=StageType.AGENT, agent="a"),
                    StageDefinition(id="same", type=StageType.AGENT, agent="b"),
                ],
            )

    def test_get_stage(self):
        workflow = WorkflowConfig(
            trigger=WorkflowTrigger(event="issues.opened"),
            stages=[
                StageDefinition(id="first", type=StageType.AGENT, agent="a"),
                StageDefinition(id="second", type=StageType.AGENT, agent="b"),
            ],
        )
        assert workflow.get_stage("first").id == "first"
        assert workflow.get_stage("second").agent == "b"
        assert workflow.get_stage("missing") is None

    def test_get_stage_index(self):
        workflow = WorkflowConfig(
            trigger=WorkflowTrigger(event="issues.opened"),
            stages=[
                StageDefinition(id="first", type=StageType.AGENT, agent="a"),
                StageDefinition(id="second", type=StageType.AGENT, agent="b"),
            ],
        )
        assert workflow.get_stage_index("first") == 0
        assert workflow.get_stage_index("second") == 1
        assert workflow.get_stage_index("missing") is None

    def test_get_next_stage_id(self):
        workflow = WorkflowConfig(
            trigger=WorkflowTrigger(event="issues.opened"),
            stages=[
                StageDefinition(id="first", type=StageType.AGENT, agent="a"),
                StageDefinition(id="second", type=StageType.AGENT, agent="b"),
                StageDefinition(id="third", type=StageType.AGENT, agent="c"),
            ],
        )
        assert workflow.get_next_stage_id("first") == "second"
        assert workflow.get_next_stage_id("second") == "third"
        assert workflow.get_next_stage_id("third") is None

    def test_context_initialization(self):
        workflow = WorkflowConfig(
            trigger=WorkflowTrigger(event="issues.opened"),
            context={"project": "squadron", "version": "1.0"},
            stages=[
                StageDefinition(id="s1", type=StageType.AGENT, agent="a"),
            ],
        )
        assert workflow.context["project"] == "squadron"
        assert workflow.context["version"] == "1.0"


# ── WorkflowRun Tests ─────────────────────────────────────────────────────────


class TestWorkflowRun:
    def test_create_workflow_run(self):
        run = WorkflowRun(
            run_id="wfv2-abc123",
            workflow_name="feature-dev",
            issue_number=42,
        )
        assert run.run_id == "wfv2-abc123"
        assert run.workflow_name == "feature-dev"
        assert run.status == WorkflowRunStatus.PENDING
        assert run.current_stage_index == 0

    def test_status_transitions(self):
        run = WorkflowRun(
            run_id="wfv2-001",
            workflow_name="test",
        )
        assert run.status == WorkflowRunStatus.PENDING

        run.status = WorkflowRunStatus.RUNNING
        assert run.status == WorkflowRunStatus.RUNNING

        run.status = WorkflowRunStatus.COMPLETED
        assert run.status == WorkflowRunStatus.COMPLETED

    def test_iteration_tracking(self):
        run = WorkflowRun(
            run_id="wfv2-iter",
            workflow_name="test",
        )
        assert run.iteration_counts == {}

        run.iteration_counts["stage-a"] = 1
        run.iteration_counts["stage-a"] += 1
        assert run.iteration_counts["stage-a"] == 2


# ── StageRun Tests ────────────────────────────────────────────────────────────


class TestStageRun:
    def test_create_stage_run(self):
        stage_run = StageRun(
            run_id="wfv2-001",
            stage_id="implement",
            stage_index=0,
        )
        assert stage_run.run_id == "wfv2-001"
        assert stage_run.stage_id == "implement"
        assert stage_run.status == StageRunStatus.PENDING

    def test_duration_calculation(self):
        stage_run = StageRun(
            run_id="wfv2-001",
            stage_id="implement",
            stage_index=0,
            started_at=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            completed_at=datetime(2024, 1, 1, 10, 5, 30, tzinfo=timezone.utc),
        )
        assert stage_run.duration_seconds == 330.0  # 5 min 30 sec

    def test_duration_none_without_completion(self):
        stage_run = StageRun(
            run_id="wfv2-001",
            stage_id="implement",
            stage_index=0,
            started_at=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert stage_run.duration_seconds is None


# ── GateCheckResult Tests ─────────────────────────────────────────────────────


class TestGateCheckResult:
    def test_passed_result(self):
        result = GateCheckResult(
            check_type="command",
            passed=True,
            result_data={"exit_code": 0},
        )
        assert result.passed
        assert result.check_type == "command"

    def test_failed_result(self):
        result = GateCheckResult(
            check_type="file_exists",
            passed=False,
            error_message="File not found",
        )
        assert not result.passed
        assert result.error_message == "File not found"

    def test_timestamp_default(self):
        result = GateCheckResult(
            check_type="test",
            passed=True,
        )
        assert result.checked_at is not None
