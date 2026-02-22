"""Unified pipeline system — AD-019.

Replaces all legacy orchestration: triggers, review_policy, workflows,
and Workflow Engine v2. See docs/design/unified-pipeline-system.md.

Key exports:
    PipelineEngine — Core execution engine
    PipelineRegistry — SQLite persistence
    GateCheckRegistry — Pluggable gate condition checks
    PipelineDefinition — Pipeline config model
    PipelineRun, StageRun — Runtime state models
"""

from squadron.pipeline.engine import (
    ActionCallback,
    NotifyCallback,
    PipelineEngine,
    SpawnAgentCallback,
)
from squadron.pipeline.gates import (
    BranchUpToDateCheck,
    CiStatusCheck,
    CommandCheck,
    FileExistsCheck,
    GateCheck,
    GateCheckRegistry,
    GateCheckResult,
    HumanApprovedCheck,
    LabelPresentCheck,
    NoChangesRequestedCheck,
    PipelineContext,
    PrApprovalsMetCheck,
)
from squadron.pipeline.models import (
    ErrorConfig,
    GateCheckRecord,
    GateConditionConfig,
    GateTimeoutConfig,
    HumanNotifyConfig,
    HumanStageConfig,
    HumanStageState,
    HumanWaitType,
    JoinStrategy,
    ParallelBranch,
    PipelineDefinition,
    PipelineRun,
    PipelineRunStatus,
    PipelineScope,
    ReactiveAction,
    ReactiveEventConfig,
    StageDefinition,
    StageRun,
    StageRunStatus,
    StageType,
    TriggerDefinition,
    WebhookRequestConfig,
)
from squadron.pipeline.registry import PipelineRegistry

__all__ = [
    # Engine
    "PipelineEngine",
    "SpawnAgentCallback",
    "ActionCallback",
    "NotifyCallback",
    # Registry
    "PipelineRegistry",
    # Gates
    "GateCheck",
    "GateCheckRegistry",
    "GateCheckResult",
    "PipelineContext",
    "BranchUpToDateCheck",
    "CiStatusCheck",
    "CommandCheck",
    "FileExistsCheck",
    "HumanApprovedCheck",
    "LabelPresentCheck",
    "NoChangesRequestedCheck",
    "PrApprovalsMetCheck",
    # Definition models
    "PipelineDefinition",
    "StageDefinition",
    "TriggerDefinition",
    "ReactiveEventConfig",
    "GateConditionConfig",
    "GateTimeoutConfig",
    "ErrorConfig",
    "HumanStageConfig",
    "HumanNotifyConfig",
    "ParallelBranch",
    "WebhookRequestConfig",
    # Runtime state models
    "PipelineRun",
    "PipelineRunStatus",
    "PipelineScope",
    "StageRun",
    "StageRunStatus",
    "GateCheckRecord",
    "HumanStageState",
    # Enums
    "StageType",
    "ReactiveAction",
    "JoinStrategy",
    "HumanWaitType",
]
