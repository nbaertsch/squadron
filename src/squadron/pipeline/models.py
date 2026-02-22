"""Pipeline system Pydantic models — definitions and runtime state.

AD-019: Unified pipeline system replacing triggers, review_policy, and workflows.

Key exports:
    Definition models: PipelineDefinition, StageDefinition, TriggerDefinition,
        ReactiveEventConfig, GateConditionConfig, HumanStageConfig, ParallelBranch
    Runtime state models: PipelineRun, PipelineRunStatus, StageRun, StageRunStatus,
        GateCheckRecord, HumanStageState
    Enums: StageType, ReactiveAction, JoinStrategy, HumanWaitType, PipelineScope
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# ── Enums ────────────────────────────────────────────────────────────────────


class StageType(str, Enum):
    """All supported pipeline stage types."""

    AGENT = "agent"
    GATE = "gate"
    HUMAN = "human"
    PARALLEL = "parallel"
    DELAY = "delay"
    ACTION = "action"
    WEBHOOK = "webhook"
    PIPELINE = "pipeline"


class ReactiveAction(str, Enum):
    """Actions that can be taken when a reactive event fires on a running pipeline."""

    REEVALUATE_GATES = "reevaluate_gates"
    INVALIDATE_AND_RESTART = "invalidate_and_restart"
    CANCEL = "cancel"
    NOTIFY = "notify"
    WAKE_AGENT = "wake_agent"


class JoinStrategy(str, Enum):
    """How a parallel stage waits for its branches."""

    ALL = "all"
    ANY = "any"


class HumanWaitType(str, Enum):
    """What human action completes a human stage."""

    APPROVAL = "approval"
    COMMENT = "comment"
    LABEL = "label"
    DISMISS = "dismiss"


class PipelineScope(str, Enum):
    """Scope of a pipeline run."""

    SINGLE_PR = "single-pr"
    MULTI_PR = "multi-pr"
    ISSUE = "issue"


class PipelineRunStatus(str, Enum):
    """Pipeline run lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ESCALATED = "escalated"


class StageRunStatus(str, Enum):
    """Stage run lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


# ── Stage ID validation ──────────────────────────────────────────────────────

STAGE_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


# ── Definition Models (parsed from YAML config) ─────────────────────────────


class TriggerDefinition(BaseModel):
    """When a pipeline should be activated by a GitHub event."""

    event: str
    conditions: dict[str, Any] = {}

    def matches(self, event_type: str, payload: dict[str, Any]) -> bool:
        """Check if an incoming event matches this trigger."""
        if self.event != event_type:
            return False
        for key, expected in self.conditions.items():
            if key == "label":
                label_name = (payload.get("label") or {}).get("name", "")
                if label_name != expected:
                    return False
            elif key == "base_branch":
                pr = payload.get("pull_request") or {}
                base = (pr.get("base") or {}).get("ref", "")
                if base != expected:
                    return False
            else:
                if payload.get(key) != expected:
                    return False
        return True


class ReactiveEventConfig(BaseModel):
    """Configuration for a reactive event on a running pipeline."""

    action: ReactiveAction
    invalidate: list[str] = []
    restart_from: str | None = None
    notify: dict[str, Any] = {}
    context: dict[str, Any] = {}


class GateConditionConfig(BaseModel):
    """A single gate condition within a gate stage."""

    check: str  # Registered check name (e.g. "pr_approvals_met", "ci_status")
    # All other fields are check-specific config, passed to the gate check evaluator
    scope: str | None = None
    workflows: list[str] | None = None
    expect: str | None = None
    label: str | None = None
    paths: list[str] | None = None
    count: int | None = None
    run: str | None = None  # Command to run (for "command" check)
    pr: int | str | None = None  # Target PR for cross-PR gate checks

    def get_config(self) -> dict[str, Any]:
        """Return all non-None check-specific fields as a config dict."""
        result: dict[str, Any] = {}
        for field_name in (
            "scope",
            "workflows",
            "expect",
            "label",
            "paths",
            "count",
            "run",
            "pr",
        ):
            val = getattr(self, field_name)
            if val is not None:
                result[field_name] = val
        return result


class GateTimeoutConfig(BaseModel):
    """Configurable timeout behavior for gates."""

    notify: dict[str, Any] | None = None
    then: Literal["fail", "escalate", "cancel"] | None = None
    extend: str | None = None  # Duration string, e.g. "24h"
    max_extensions: int = 0


class ErrorConfig(BaseModel):
    """On-error behavior for a stage."""

    retry: int = 0
    then: str | None = None  # Stage ID, "escalate", or "fail"


class HumanNotifyConfig(BaseModel):
    """Notification config for human stages."""

    on_enter: str | None = None
    reminder: dict[str, Any] | None = None  # interval, message, max_reminders


class HumanStageConfig(BaseModel):
    """Config specific to human stages."""

    description: str = ""
    wait_for: HumanWaitType = HumanWaitType.APPROVAL
    from_group: str | None = Field(None, alias="from")
    count: int = 1
    auto_assign: bool = True
    notify: HumanNotifyConfig | None = None

    model_config = {"populate_by_name": True}


class ParallelBranch(BaseModel):
    """A branch within a parallel stage."""

    id: str
    agent: str | None = None
    action: str | None = None
    type: StageType = StageType.AGENT
    condition: dict[str, Any] | None = None
    timeout: str | None = None
    pipeline: str | None = None  # For type: pipeline branches
    config: dict[str, Any] = {}  # For type: action branches
    context: dict[str, Any] = {}  # Extra context for sub-pipeline branches

    @model_validator(mode="after")
    def validate_branch(self) -> ParallelBranch:
        if not STAGE_ID_PATTERN.match(self.id):
            msg = f"Branch ID '{self.id}' must match pattern {STAGE_ID_PATTERN.pattern}"
            raise ValueError(msg)
        return self


class WebhookRequestConfig(BaseModel):
    """HTTP request config for webhook stages."""

    url: str
    method: str = "POST"
    headers: dict[str, str] = {}
    body: dict[str, Any] = {}


class StageDefinition(BaseModel):
    """A single stage in a pipeline definition.

    This is a union type — the valid fields depend on `type`.
    """

    id: str
    type: StageType

    # Agent stage fields
    agent: str | None = None
    action: str | None = None
    continue_session: bool = False

    # Gate stage fields
    conditions: list[GateConditionConfig] = []
    any_of: list[GateConditionConfig] | None = None  # Disjunction mode

    # Human stage fields
    human: HumanStageConfig | None = None

    # Parallel stage fields
    join: JoinStrategy | None = None
    branches: list[ParallelBranch] = []

    # Delay stage fields
    duration: str | None = None
    poll: dict[str, Any] | None = None

    # Action stage fields
    action_type: str | None = Field(None, alias="action_name")
    config: dict[str, Any] = {}

    # Webhook stage fields
    request: WebhookRequestConfig | None = None
    expect: dict[str, Any] | None = None

    # Sub-pipeline stage fields
    pipeline: str | None = None

    # Conditional execution (all stage types)
    condition: dict[str, Any] | None = None
    skip_to: str | None = None

    # Transition config (all stage types)
    on_complete: str | dict | None = None
    on_pass: str | dict | None = None
    on_fail: str | dict | None = None
    on_error: ErrorConfig | dict | None = None
    on_success: str | dict | None = None
    on_conflict: str | None = None
    on_any_reject: dict[str, Any] | None = None

    # Timeout
    timeout: str | None = None
    on_timeout: GateTimeoutConfig | dict | None = None

    # Context propagation
    context: dict[str, Any] = {}

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_stage(self) -> StageDefinition:
        if not STAGE_ID_PATTERN.match(self.id):
            msg = f"Stage ID '{self.id}' must match pattern {STAGE_ID_PATTERN.pattern}"
            raise ValueError(msg)

        match self.type:
            case StageType.AGENT:
                if not self.agent:
                    msg = f"Stage '{self.id}': agent stages require 'agent' field"
                    raise ValueError(msg)
            case StageType.GATE:
                if not self.conditions and not self.any_of:
                    msg = f"Stage '{self.id}': gate stages require 'conditions' or 'any_of'"
                    raise ValueError(msg)
            case StageType.HUMAN:
                if not self.human:
                    msg = f"Stage '{self.id}': human stages require 'human' config"
                    raise ValueError(msg)
            case StageType.PARALLEL:
                if not self.branches:
                    msg = f"Stage '{self.id}': parallel stages require 'branches'"
                    raise ValueError(msg)
            case StageType.DELAY:
                if not self.duration:
                    msg = f"Stage '{self.id}': delay stages require 'duration'"
                    raise ValueError(msg)
            case StageType.ACTION:
                if not self.action:
                    msg = f"Stage '{self.id}': action stages require 'action' field"
                    raise ValueError(msg)
            case StageType.WEBHOOK:
                if not self.request:
                    msg = f"Stage '{self.id}': webhook stages require 'request' config"
                    raise ValueError(msg)
            case StageType.PIPELINE:
                if not self.pipeline:
                    msg = f"Stage '{self.id}': pipeline stages require 'pipeline' name"
                    raise ValueError(msg)

        return self

    def get_next_stage_id(self, result: str) -> str | None:
        """Get the target stage ID for a given result ("complete", "pass", "fail", etc.)."""
        transition_map = {
            "complete": self.on_complete,
            "pass": self.on_pass,
            "fail": self.on_fail,
            "error": self.on_error,
            "success": self.on_success,
        }
        raw = transition_map.get(result)
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            return raw.get("goto")
        if isinstance(raw, ErrorConfig):
            return raw.then
        return None

    def parse_timeout_seconds(self) -> int | None:
        """Parse timeout string (e.g. '30m', '2h') to seconds."""
        if not self.timeout:
            return None
        return _parse_duration_seconds(self.timeout)


class PipelineDefinition(BaseModel):
    """Complete pipeline definition parsed from YAML config."""

    description: str = ""
    trigger: TriggerDefinition | None = None  # None = sub-pipeline only
    scope: PipelineScope = PipelineScope.SINGLE_PR
    on_events: dict[str, ReactiveEventConfig] = {}
    context: dict[str, Any] = {}
    stages: list[StageDefinition] = Field(min_length=1)
    on_complete: list[dict[str, Any]] = []
    on_error: list[dict[str, Any]] = []

    @model_validator(mode="after")
    def validate_unique_stage_ids(self) -> PipelineDefinition:
        ids = [s.id for s in self.stages]
        dupes = [sid for sid in ids if ids.count(sid) > 1]
        if dupes:
            msg = f"Duplicate stage IDs: {sorted(set(dupes))}"
            raise ValueError(msg)
        return self

    def get_stage(self, stage_id: str) -> StageDefinition | None:
        """Look up a stage by ID."""
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        return None

    def get_stage_index(self, stage_id: str) -> int | None:
        """Get the index of a stage by ID."""
        for i, stage in enumerate(self.stages):
            if stage.id == stage_id:
                return i
        return None

    def get_next_stage(self, current_id: str) -> StageDefinition | None:
        """Get the stage after the given one in sequence."""
        idx = self.get_stage_index(current_id)
        if idx is None or idx + 1 >= len(self.stages):
            return None
        return self.stages[idx + 1]

    def validate_stage_references(self) -> list[str]:
        """Validate that all stage ID references point to existing stages.

        Returns a list of error messages (empty = valid).
        """
        valid_ids = {s.id for s in self.stages}
        special_targets = {"__complete__", "__escalate__", "__next__"}
        errors: list[str] = []

        for stage in self.stages:
            for field_name in (
                "on_complete",
                "on_pass",
                "on_fail",
                "on_success",
                "skip_to",
            ):
                raw = getattr(stage, field_name, None)
                target = raw if isinstance(raw, str) else None
                if isinstance(raw, dict):
                    target = raw.get("goto")
                if target and target not in valid_ids and target not in special_targets:
                    errors.append(
                        f"Stage '{stage.id}' references unknown stage '{target}' in '{field_name}'"
                    )

        return errors

    def get_sub_pipeline_refs(self) -> set[str]:
        """Return the set of pipeline names referenced by pipeline-type stages."""
        refs: set[str] = set()
        for stage in self.stages:
            if stage.type == StageType.PIPELINE and stage.pipeline:
                refs.add(stage.pipeline)
        return refs


# ── Runtime State Models (persisted in SQLite) ───────────────────────────────


class PipelineRun(BaseModel):
    """Runtime state of a pipeline execution."""

    run_id: str
    pipeline_name: str
    definition_snapshot: str = "{}"  # JSON-serialized PipelineDefinition

    # Trigger context
    trigger_event: str | None = None
    trigger_delivery_id: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    scope: PipelineScope = PipelineScope.SINGLE_PR

    # Sub-pipeline support
    parent_run_id: str | None = None
    parent_stage_id: str | None = None
    nesting_depth: int = 0

    # Execution state
    status: PipelineRunStatus = PipelineRunStatus.PENDING
    current_stage_id: str | None = None

    # Context
    context: dict[str, Any] = {}

    # Timestamps
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Error tracking
    error_message: str | None = None
    error_stage_id: str | None = None


class StageRun(BaseModel):
    """Runtime state of a single stage execution."""

    id: int | None = None  # DB auto-increment
    run_id: str
    stage_id: str

    # Execution
    status: StageRunStatus = StageRunStatus.PENDING
    agent_id: str | None = None

    # Parallel stage support
    branch_id: str | None = None
    parent_stage_id: str | None = None

    # Sub-pipeline support
    child_pipeline_run_id: str | None = None

    # Results
    outputs: dict[str, Any] = {}
    error_message: str | None = None

    # Retry tracking
    attempt_number: int = 1
    max_attempts: int = 1

    # Timing
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None


class GateCheckRecord(BaseModel):
    """Record of a single gate check evaluation."""

    id: int | None = None  # DB auto-increment
    stage_run_id: int
    check_type: str
    check_config: str | None = None  # JSON

    passed: bool | None = None
    message: str = ""
    result_data: dict[str, Any] = {}

    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HumanStageState(BaseModel):
    """Tracking state for a human-in-the-loop stage."""

    id: int | None = None  # DB auto-increment
    stage_run_id: int

    # Notification tracking
    entry_notified_at: datetime | None = None
    last_reminder_at: datetime | None = None
    reminder_count: int = 0

    # Assignment tracking
    assigned_users: list[str] = []

    # Completion tracking
    completed_by: str | None = None
    completed_action: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_duration_seconds(duration: str) -> int:
    """Parse a duration string like '30s', '5m', '2h', '1d' to seconds.

    Raises ValueError on invalid format.
    """
    match = re.match(r"^(\d+)\s*(s|m|h|d)$", duration.strip())
    if not match:
        msg = f"Invalid duration format: '{duration}'. Expected <number><s|m|h|d>"
        raise ValueError(msg)
    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]
