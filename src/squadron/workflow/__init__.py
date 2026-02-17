"""Workflow System — Deterministic multi-agent orchestration.

This module provides an extended workflow system that enables:
- Sequential and parallel stage execution
- Quality gates with pass/fail conditions
- Context propagation between stages
- Retry logic with iteration limits

Workflows are optional — agents can still operate autonomously via
triggers and prompts. Workflows provide stricter control for users
who want deterministic, auditable execution flows.
"""

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
)
from squadron.workflow.engine import WorkflowEngine
from squadron.workflow.registry import WorkflowRegistryV2 as WorkflowRegistry

__all__ = [
    "GateCheckResult",
    "GateCondition",
    "StageDefinition",
    "StageRun",
    "StageRunStatus",
    "StageTransition",
    "StageType",
    "WorkflowConfig",
    "WorkflowEngine",
    "WorkflowRegistry",
    "WorkflowRun",
    "WorkflowRunStatus",
]
