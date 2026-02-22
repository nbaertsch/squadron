"""Pipeline System — unified orchestration primitive for Squadron (AD-019).

This package provides the ``PipelineEngine`` and associated components that
replace Squadron's three fragmented orchestration mechanisms:

1. Config-driven triggers (``agent_roles.<role>.triggers``)
2. Workflow Engine v2 (``src/squadron/workflow/``)
3. Review Policy (``review_policy`` in config.yaml)

Key components:

- :class:`~squadron.pipeline.engine.PipelineEngine` — reactive orchestrator
- :class:`~squadron.pipeline.registry.PipelineRegistry` — unified SQLite state
- :class:`~squadron.pipeline.gates.GateCheckRegistry` — pluggable gate checks
- :data:`~squadron.pipeline.gates.default_gate_registry` — pre-populated registry

Config classes (in ``squadron.config``):

- :class:`~squadron.config.PipelineConfig` — pipeline definition
- :class:`~squadron.config.PipelineStageConfig` — stage definition
- :class:`~squadron.config.PipelineGateCheck` — gate check condition
- :class:`~squadron.config.PipelineEventSubscription` — reactive event subscription
"""

from squadron.pipeline.engine import PipelineEngine
from squadron.pipeline.gates import (
    GateCheckContext,
    GateCheckRegistry,
    default_gate_registry,
)
from squadron.pipeline.registry import (
    PipelineRegistry,
    PipelineRun,
    PipelineRunStatus,
    PipelineStageRun,
    PipelineStageStatus,
)

__all__ = [
    # Engine
    "PipelineEngine",
    # Gates
    "GateCheckContext",
    "GateCheckRegistry",
    "default_gate_registry",
    # Registry
    "PipelineRegistry",
    "PipelineRun",
    "PipelineRunStatus",
    "PipelineStageRun",
    "PipelineStageStatus",
]
