"""Tests for pipeline system Pydantic models (AD-019)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from squadron.pipeline.models import (
    GateCheckRecord,
    GateConditionConfig,
    HumanStageState,
    HumanWaitType,
    JoinStrategy,
    ParallelBranch,
    PipelineDefinition,
    PipelineRun,
    PipelineRunStatus,
    PipelineScope,
    ReactiveAction,
    StageDefinition,
    StageRun,
    StageRunStatus,
    StageType,
    TriggerDefinition,
    _parse_duration_seconds,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _agent_stage(id: str = "s1", **overrides) -> dict:
    return {"id": id, "type": "agent", "agent": "coder", **overrides}


def _gate_stage(id: str = "g1", **overrides) -> dict:
    return {
        "id": id,
        "type": "gate",
        "conditions": [{"check": "ci_status"}],
        **overrides,
    }


def _pipeline_def(**overrides) -> PipelineDefinition:
    defaults: dict = {"stages": [_agent_stage()]}
    defaults.update(overrides)
    return PipelineDefinition(**defaults)


# ── Enum value tests ─────────────────────────────────────────────────────────


class TestEnums:
    def test_stage_type_values(self):
        assert StageType.AGENT == "agent"
        assert StageType.GATE == "gate"
        assert StageType.HUMAN == "human"
        assert StageType.PARALLEL == "parallel"
        assert StageType.DELAY == "delay"
        assert StageType.ACTION == "action"
        assert StageType.WEBHOOK == "webhook"
        assert StageType.PIPELINE == "pipeline"

    def test_reactive_action_values(self):
        assert ReactiveAction.REEVALUATE_GATES == "reevaluate_gates"
        assert ReactiveAction.INVALIDATE_AND_RESTART == "invalidate_and_restart"
        assert ReactiveAction.CANCEL == "cancel"
        assert ReactiveAction.NOTIFY == "notify"
        assert ReactiveAction.WAKE_AGENT == "wake_agent"

    def test_join_strategy_values(self):
        assert JoinStrategy.ALL == "all"
        assert JoinStrategy.ANY == "any"

    def test_human_wait_type_values(self):
        assert HumanWaitType.APPROVAL == "approval"
        assert HumanWaitType.COMMENT == "comment"
        assert HumanWaitType.LABEL == "label"
        assert HumanWaitType.DISMISS == "dismiss"

    def test_pipeline_scope_values(self):
        assert PipelineScope.SINGLE_PR == "single-pr"
        assert PipelineScope.MULTI_PR == "multi-pr"
        assert PipelineScope.ISSUE == "issue"

    def test_pipeline_run_status_values(self):
        assert PipelineRunStatus.PENDING == "pending"
        assert PipelineRunStatus.RUNNING == "running"
        assert PipelineRunStatus.COMPLETED == "completed"
        assert PipelineRunStatus.FAILED == "failed"
        assert PipelineRunStatus.CANCELLED == "cancelled"
        assert PipelineRunStatus.ESCALATED == "escalated"

    def test_stage_run_status_values(self):
        assert StageRunStatus.PENDING == "pending"
        assert StageRunStatus.RUNNING == "running"
        assert StageRunStatus.WAITING == "waiting"
        assert StageRunStatus.COMPLETED == "completed"
        assert StageRunStatus.FAILED == "failed"
        assert StageRunStatus.SKIPPED == "skipped"
        assert StageRunStatus.CANCELLED == "cancelled"


# ── _parse_duration_seconds ──────────────────────────────────────────────────


class TestParseDurationSeconds:
    def test_seconds(self):
        assert _parse_duration_seconds("30s") == 30

    def test_minutes(self):
        assert _parse_duration_seconds("5m") == 300

    def test_hours(self):
        assert _parse_duration_seconds("2h") == 7200

    def test_days(self):
        assert _parse_duration_seconds("1d") == 86400

    def test_whitespace_stripped(self):
        assert _parse_duration_seconds("  10m  ") == 600

    def test_invalid_unit_raises(self):
        with pytest.raises(ValueError, match="Invalid duration format"):
            _parse_duration_seconds("30w")

    def test_bare_number_raises(self):
        with pytest.raises(ValueError, match="Invalid duration format"):
            _parse_duration_seconds("30")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Invalid duration format"):
            _parse_duration_seconds("")

    def test_negative_not_matched_raises(self):
        with pytest.raises(ValueError, match="Invalid duration format"):
            _parse_duration_seconds("-5m")


# ── Definition Models ────────────────────────────────────────────────────────


class TestTriggerDefinition:
    def test_matches_event_type(self):
        trigger = TriggerDefinition(event="pull_request.opened")
        assert trigger.matches("pull_request.opened", {})

    def test_no_match_different_event(self):
        trigger = TriggerDefinition(event="pull_request.opened")
        assert not trigger.matches("issues.opened", {})

    def test_matches_with_label_condition(self):
        trigger = TriggerDefinition(
            event="issues.labeled",
            conditions={"label": "bug"},
        )
        assert trigger.matches("issues.labeled", {"label": {"name": "bug"}})
        assert not trigger.matches("issues.labeled", {"label": {"name": "feature"}})

    def test_matches_with_base_branch_condition(self):
        trigger = TriggerDefinition(
            event="pull_request.opened",
            conditions={"base_branch": "main"},
        )
        payload = {"pull_request": {"base": {"ref": "main"}}}
        assert trigger.matches("pull_request.opened", payload)

        payload_dev = {"pull_request": {"base": {"ref": "develop"}}}
        assert not trigger.matches("pull_request.opened", payload_dev)

    def test_base_branch_missing_pr_key(self):
        trigger = TriggerDefinition(
            event="pull_request.opened",
            conditions={"base_branch": "main"},
        )
        assert not trigger.matches("pull_request.opened", {})

    def test_matches_generic_payload_key(self):
        trigger = TriggerDefinition(
            event="custom.event",
            conditions={"action": "created"},
        )
        assert trigger.matches("custom.event", {"action": "created"})
        assert not trigger.matches("custom.event", {"action": "deleted"})

    def test_no_conditions_matches_any_payload(self):
        trigger = TriggerDefinition(event="push")
        assert trigger.matches("push", {"ref": "refs/heads/main", "extra": 42})


class TestGateConditionConfig:
    def test_get_config_empty_when_all_none(self):
        gate = GateConditionConfig(check="ci_status")
        assert gate.get_config() == {}

    def test_get_config_command_run(self):
        gate = GateConditionConfig(check="command", run="make test")
        assert gate.get_config() == {"run": "make test"}

    def test_get_config_pr_approvals(self):
        gate = GateConditionConfig(check="pr_approvals_met", count=2)
        assert gate.get_config() == {"count": 2}

    def test_get_config_label_present(self):
        gate = GateConditionConfig(check="label_present", label="approved")
        assert gate.get_config() == {"label": "approved"}

    def test_get_config_multiple_fields(self):
        gate = GateConditionConfig(
            check="ci_status",
            scope="required",
            workflows=["build", "test"],
            expect="success",
        )
        cfg = gate.get_config()
        assert cfg == {
            "scope": "required",
            "workflows": ["build", "test"],
            "expect": "success",
        }

    def test_get_config_file_exists_with_paths(self):
        gate = GateConditionConfig(check="file_exists", paths=["README.md"])
        assert gate.get_config() == {"paths": ["README.md"]}


class TestStageDefinition:
    # ── Valid constructions ───────────────────────────────────────────────

    def test_valid_agent_stage(self):
        stage = StageDefinition(id="build", type="agent", agent="coder")
        assert stage.type == StageType.AGENT
        assert stage.agent == "coder"

    def test_valid_gate_stage(self):
        stage = StageDefinition(
            id="checks",
            type="gate",
            conditions=[{"check": "ci_status"}],
        )
        assert stage.type == StageType.GATE
        assert len(stage.conditions) == 1

    def test_valid_gate_stage_with_any_of(self):
        stage = StageDefinition(
            id="checks",
            type="gate",
            any_of=[{"check": "ci_status"}],
        )
        assert stage.any_of is not None
        assert len(stage.any_of) == 1

    def test_valid_human_stage(self):
        stage = StageDefinition(
            id="review",
            type="human",
            human={"wait_for": "approval"},
        )
        assert stage.type == StageType.HUMAN
        assert stage.human is not None
        assert stage.human.wait_for == HumanWaitType.APPROVAL

    def test_valid_parallel_stage(self):
        stage = StageDefinition(
            id="fanout",
            type="parallel",
            branches=[
                {"id": "b1", "agent": "coder"},
                {"id": "b2", "agent": "tester"},
            ],
        )
        assert stage.type == StageType.PARALLEL
        assert len(stage.branches) == 2

    def test_valid_delay_stage(self):
        stage = StageDefinition(id="wait", type="delay", duration="30m")
        assert stage.type == StageType.DELAY
        assert stage.duration == "30m"

    def test_valid_action_stage(self):
        stage = StageDefinition(id="deploy", type="action", action="deploy-prod")
        assert stage.type == StageType.ACTION
        assert stage.action == "deploy-prod"

    def test_valid_webhook_stage(self):
        stage = StageDefinition(
            id="notify",
            type="webhook",
            request={"url": "https://example.com/hook"},
        )
        assert stage.type == StageType.WEBHOOK
        assert stage.request is not None

    def test_valid_pipeline_stage(self):
        stage = StageDefinition(id="sub", type="pipeline", pipeline="deploy-pipeline")
        assert stage.type == StageType.PIPELINE
        assert stage.pipeline == "deploy-pipeline"

    # ── Invalid constructions ────────────────────────────────────────────

    def test_agent_stage_without_agent_raises(self):
        with pytest.raises(ValidationError, match="agent stages require 'agent' field"):
            StageDefinition(id="bad", type="agent")

    def test_gate_stage_without_conditions_raises(self):
        with pytest.raises(
            ValidationError,
            match="gate stages require 'conditions' or 'any_of'",
        ):
            StageDefinition(id="bad", type="gate")

    def test_human_stage_without_human_raises(self):
        with pytest.raises(ValidationError, match="human stages require 'human' config"):
            StageDefinition(id="bad", type="human")

    def test_parallel_stage_without_branches_raises(self):
        with pytest.raises(ValidationError, match="parallel stages require 'branches'"):
            StageDefinition(id="bad", type="parallel")

    def test_delay_stage_without_duration_raises(self):
        with pytest.raises(ValidationError, match="delay stages require 'duration'"):
            StageDefinition(id="bad", type="delay")

    def test_action_stage_without_action_raises(self):
        with pytest.raises(ValidationError, match="action stages require 'action' field"):
            StageDefinition(id="bad", type="action")

    def test_stage_id_with_spaces_raises(self):
        with pytest.raises(ValidationError, match="must match pattern"):
            StageDefinition(id="bad stage", type="agent", agent="coder")

    def test_stage_id_starting_with_digit_raises(self):
        with pytest.raises(ValidationError, match="must match pattern"):
            StageDefinition(id="1bad", type="agent", agent="coder")

    # ── Transition helpers ───────────────────────────────────────────────

    def test_get_next_stage_id_on_pass_string(self):
        stage = StageDefinition(
            id="g1", type="gate", conditions=[{"check": "ci_status"}], on_pass="deploy"
        )
        assert stage.get_next_stage_id("pass") == "deploy"

    def test_get_next_stage_id_on_fail_string(self):
        stage = StageDefinition(
            id="g1", type="gate", conditions=[{"check": "ci_status"}], on_fail="rollback"
        )
        assert stage.get_next_stage_id("fail") == "rollback"

    def test_get_next_stage_id_on_complete_dict_goto(self):
        stage = StageDefinition(id="s1", type="agent", agent="coder", on_complete={"goto": "s2"})
        assert stage.get_next_stage_id("complete") == "s2"

    def test_get_next_stage_id_returns_none_when_unset(self):
        stage = StageDefinition(id="s1", type="agent", agent="coder")
        assert stage.get_next_stage_id("pass") is None
        assert stage.get_next_stage_id("fail") is None
        assert stage.get_next_stage_id("complete") is None

    def test_get_next_stage_id_unknown_result(self):
        stage = StageDefinition(id="s1", type="agent", agent="coder")
        assert stage.get_next_stage_id("unknown_result") is None

    # ── Timeout parsing ──────────────────────────────────────────────────

    def test_parse_timeout_seconds(self):
        stage = StageDefinition(id="s1", type="agent", agent="coder", timeout="30m")
        assert stage.parse_timeout_seconds() == 1800

    def test_parse_timeout_seconds_none_when_no_timeout(self):
        stage = StageDefinition(id="s1", type="agent", agent="coder")
        assert stage.parse_timeout_seconds() is None


class TestParallelBranch:
    def test_valid_branch_with_agent(self):
        branch = ParallelBranch(id="b1", agent="coder")
        assert branch.agent == "coder"
        assert branch.action is None

    def test_valid_branch_with_action(self):
        branch = ParallelBranch(id="b1", action="deploy")
        assert branch.action == "deploy"

    def test_invalid_branch_id_raises(self):
        with pytest.raises(ValidationError, match="must match pattern"):
            ParallelBranch(id="bad branch", agent="coder")

    def test_branch_id_with_hyphen_and_underscore(self):
        branch = ParallelBranch(id="my-branch_1", agent="coder")
        assert branch.id == "my-branch_1"


# ── PipelineDefinition ───────────────────────────────────────────────────────


class TestPipelineDefinition:
    def test_valid_single_stage(self):
        pd = _pipeline_def()
        assert len(pd.stages) == 1
        assert pd.stages[0].id == "s1"

    def test_valid_multi_stage(self):
        pd = PipelineDefinition(
            stages=[
                _agent_stage("s1"),
                _gate_stage("g1"),
                _agent_stage("s2", agent="tester"),
            ]
        )
        assert len(pd.stages) == 3

    def test_zero_stages_raises(self):
        with pytest.raises(ValidationError):
            PipelineDefinition(stages=[])

    def test_duplicate_stage_ids_raises(self):
        with pytest.raises(ValidationError, match="Duplicate stage IDs"):
            PipelineDefinition(stages=[_agent_stage("dup"), _agent_stage("dup", agent="other")])

    def test_get_stage_found(self):
        pd = PipelineDefinition(stages=[_agent_stage("s1"), _gate_stage("g1")])
        stage = pd.get_stage("g1")
        assert stage is not None
        assert stage.type == StageType.GATE

    def test_get_stage_not_found(self):
        pd = _pipeline_def()
        assert pd.get_stage("nonexistent") is None

    def test_get_stage_index_found(self):
        pd = PipelineDefinition(
            stages=[_agent_stage("s1"), _gate_stage("g1"), _agent_stage("s2", agent="x")]
        )
        assert pd.get_stage_index("s1") == 0
        assert pd.get_stage_index("g1") == 1
        assert pd.get_stage_index("s2") == 2

    def test_get_stage_index_not_found(self):
        pd = _pipeline_def()
        assert pd.get_stage_index("nope") is None

    def test_get_next_stage(self):
        pd = PipelineDefinition(stages=[_agent_stage("s1"), _gate_stage("g1")])
        nxt = pd.get_next_stage("s1")
        assert nxt is not None
        assert nxt.id == "g1"

    def test_get_next_stage_last_returns_none(self):
        pd = PipelineDefinition(stages=[_agent_stage("s1"), _gate_stage("g1")])
        assert pd.get_next_stage("g1") is None

    def test_get_next_stage_unknown_id_returns_none(self):
        pd = _pipeline_def()
        assert pd.get_next_stage("nonexistent") is None

    def test_validate_stage_references_valid(self):
        pd = PipelineDefinition(
            stages=[
                _agent_stage("s1", on_complete="g1"),
                _gate_stage("g1", on_pass="s2", on_fail="__escalate__"),
                _agent_stage("s2", agent="tester"),
            ]
        )
        assert pd.validate_stage_references() == []

    def test_validate_stage_references_invalid(self):
        pd = PipelineDefinition(
            stages=[
                _agent_stage("s1", on_complete="missing_stage"),
                _gate_stage("g1"),
            ]
        )
        errors = pd.validate_stage_references()
        assert len(errors) == 1
        assert "missing_stage" in errors[0]

    def test_validate_stage_references_special_targets_ok(self):
        pd = PipelineDefinition(
            stages=[
                _agent_stage("s1", on_complete="__complete__"),
                _gate_stage("g1", on_pass="__next__"),
            ]
        )
        assert pd.validate_stage_references() == []

    def test_get_sub_pipeline_refs_empty(self):
        pd = _pipeline_def()
        assert pd.get_sub_pipeline_refs() == set()

    def test_get_sub_pipeline_refs(self):
        pd = PipelineDefinition(
            stages=[
                _agent_stage("s1"),
                {"id": "sub1", "type": "pipeline", "pipeline": "deploy-flow"},
                {"id": "sub2", "type": "pipeline", "pipeline": "test-flow"},
            ]
        )
        refs = pd.get_sub_pipeline_refs()
        assert refs == {"deploy-flow", "test-flow"}

    def test_default_scope(self):
        pd = _pipeline_def()
        assert pd.scope == PipelineScope.SINGLE_PR


# ── Runtime State Models ─────────────────────────────────────────────────────


class TestRuntimeModels:
    # ── PipelineRun ──────────────────────────────────────────────────────

    def test_pipeline_run_defaults(self):
        run = PipelineRun(run_id="r1", pipeline_name="ci")
        assert run.status == PipelineRunStatus.PENDING
        assert run.scope == PipelineScope.SINGLE_PR
        assert run.nesting_depth == 0
        assert run.context == {}
        assert run.current_stage_id is None
        assert run.parent_run_id is None
        assert run.created_at is None
        assert run.error_message is None

    def test_pipeline_run_with_all_fields(self):
        now = datetime.now(timezone.utc)
        run = PipelineRun(
            run_id="r1",
            pipeline_name="deploy",
            trigger_event="pull_request.merged",
            pr_number=42,
            status=PipelineRunStatus.RUNNING,
            current_stage_id="s1",
            created_at=now,
            started_at=now,
        )
        assert run.pr_number == 42
        assert run.status == PipelineRunStatus.RUNNING
        assert run.started_at == now

    # ── StageRun ─────────────────────────────────────────────────────────

    def test_stage_run_defaults(self):
        sr = StageRun(run_id="r1", stage_id="s1")
        assert sr.status == StageRunStatus.PENDING
        assert sr.attempt_number == 1
        assert sr.max_attempts == 1
        assert sr.outputs == {}
        assert sr.duration_seconds is None

    def test_stage_run_duration_seconds(self):
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 12, 5, 30, tzinfo=timezone.utc)
        sr = StageRun(
            run_id="r1",
            stage_id="s1",
            started_at=start,
            completed_at=end,
        )
        assert sr.duration_seconds == 330.0

    def test_stage_run_duration_none_without_completed(self):
        sr = StageRun(
            run_id="r1",
            stage_id="s1",
            started_at=datetime.now(timezone.utc),
        )
        assert sr.duration_seconds is None

    def test_stage_run_duration_none_without_started(self):
        sr = StageRun(run_id="r1", stage_id="s1")
        assert sr.duration_seconds is None

    # ── GateCheckRecord ──────────────────────────────────────────────────

    def test_gate_check_record_construction(self):
        rec = GateCheckRecord(
            stage_run_id=1,
            check_type="ci_status",
            passed=True,
            message="All checks passed",
            result_data={"workflows": ["build"]},
        )
        assert rec.stage_run_id == 1
        assert rec.passed is True
        assert rec.message == "All checks passed"
        assert rec.result_data == {"workflows": ["build"]}
        assert rec.checked_at is not None

    def test_gate_check_record_defaults(self):
        rec = GateCheckRecord(stage_run_id=1, check_type="label_present")
        assert rec.passed is None
        assert rec.message == ""
        assert rec.result_data == {}

    # ── HumanStageState ──────────────────────────────────────────────────

    def test_human_stage_state_construction(self):
        now = datetime.now(timezone.utc)
        state = HumanStageState(
            stage_run_id=5,
            entry_notified_at=now,
            assigned_users=["alice", "bob"],
            reminder_count=2,
            completed_by="alice",
            completed_action="approved",
        )
        assert state.stage_run_id == 5
        assert state.assigned_users == ["alice", "bob"]
        assert state.completed_by == "alice"
        assert state.reminder_count == 2

    def test_human_stage_state_defaults(self):
        state = HumanStageState(stage_run_id=1)
        assert state.assigned_users == []
        assert state.reminder_count == 0
        assert state.completed_by is None
        assert state.entry_notified_at is None
