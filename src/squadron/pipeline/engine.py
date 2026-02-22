"""Pipeline engine — core execution of pipeline runs and stage transitions.

AD-019: Unified pipeline engine replacing WorkflowEngine, trigger dispatch,
and review policy orchestration.

Key exports:
    PipelineEngine — Main engine class with start_pipeline(), execute_stage(),
        handle_reactive_event(), on_agent_complete/error, complete_human_stage().
    SpawnAgentCallback — Protocol for the AgentManager integration point.
    NotifyCallback — Protocol for notification delivery (PR comments, labels, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from squadron.pipeline.gates import GateCheckRegistry, GateCheckResult, PipelineContext
from squadron.pipeline.models import (
    GateCheckRecord,
    GateTimeoutConfig,
    HumanStageState,
    HumanWaitType,
    JoinStrategy,
    ParallelBranch,
    PipelineDefinition,
    PipelineRun,
    PipelineRunStatus,
    ReactiveAction,
    StageDefinition,
    StageRun,
    StageRunStatus,
    StageType,
    _parse_duration_seconds,
)
from squadron.pipeline.registry import PipelineRegistry

if TYPE_CHECKING:
    from squadron.github_client import GitHubClient
    from squadron.models import SquadronEvent

logger = logging.getLogger("squadron.pipeline.engine")

# Maximum sub-pipeline nesting depth
MAX_NESTING_DEPTH = 3

# Mapping from HumanWaitType to the set of valid completion action strings
_HUMAN_WAIT_ACTIONS: dict[HumanWaitType, set[str]] = {
    HumanWaitType.APPROVAL: {"approved", "approval"},
    HumanWaitType.COMMENT: {"commented", "comment"},
    HumanWaitType.LABEL: {"labeled", "label"},
    HumanWaitType.DISMISS: {"dismissed", "dismiss"},
}

# Mapping from HumanWaitType to the GitHub event that satisfies it
_HUMAN_WAIT_EVENT_MAP: dict[HumanWaitType, str] = {
    HumanWaitType.APPROVAL: "pull_request_review.submitted",
    HumanWaitType.COMMENT: "issue_comment.created",
    HumanWaitType.LABEL: "pull_request.labeled",
    HumanWaitType.DISMISS: "pull_request_review.dismissed",
}


# ── Callback Protocols ───────────────────────────────────────────────────────


class SpawnAgentCallback(Protocol):
    """Called by the engine to spawn or wake an agent for an agent stage."""

    async def __call__(
        self,
        role: str,
        issue_number: int | None,
        *,
        pr_number: int | None = None,
        pipeline_run_id: str | None = None,
        stage_id: str | None = None,
        action: str | None = None,
        continue_session: bool = False,
        context: dict[str, Any] | None = None,
    ) -> str | None:
        """Spawn/wake an agent. Returns agent_id or None on failure."""
        ...


class ActionCallback(Protocol):
    """Called by the engine to execute a built-in action (e.g. merge_pr)."""

    async def __call__(
        self,
        action: str,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> dict[str, Any]:
        """Execute an action. Returns result dict with at least {"success": bool}."""
        ...


class NotifyCallback(Protocol):
    """Called by the engine to deliver notifications (PR comments, labels, assignments).

    The engine is transport-agnostic — all notification delivery is routed through
    this callback. The ``target`` parameter selects the delivery mechanism:

        - "pr_comment" — post a comment on the PR
        - "label" — add a label to the PR/issue
        - "assign" — request review / assign users
        - "remove_label" — remove a label
    """

    async def __call__(
        self,
        target: str,
        context: PipelineContext,
        *,
        message: str | None = None,
        label: str | None = None,
        users: list[str] | None = None,
    ) -> None:
        """Deliver a notification. Failures should be logged, not raised."""
        ...


# ── Pipeline Engine ──────────────────────────────────────────────────────────


class PipelineEngine:
    """Core pipeline execution engine.

    Responsibilities:
        - Evaluate trigger conditions to start new pipelines
        - Execute stages in sequence (agent, gate, action, delay, etc.)
        - Handle reactive events on running pipelines
        - Manage stage transitions and pipeline completion/failure
        - Coordinate with AgentManager via callbacks

    Usage:
        engine = PipelineEngine(registry, gate_registry)
        engine.set_spawn_callback(agent_manager.spawn_for_pipeline)
        engine.set_action_callback(action_executor)
        engine.add_pipeline("review-flow", pipeline_def)
        await engine.evaluate_event(event_type, payload, squadron_event)
    """

    def __init__(
        self,
        registry: PipelineRegistry,
        gate_registry: GateCheckRegistry,
        *,
        github_client: GitHubClient | None = None,
        owner: str = "",
        repo: str = "",
    ):
        self._registry = registry
        self._gate_registry = gate_registry
        self._github_client = github_client
        self._owner = owner
        self._repo = repo

        # Pipeline definitions (name → definition)
        self._pipelines: dict[str, PipelineDefinition] = {}

        # Callbacks (set by AgentManager)
        self._spawn_agent: SpawnAgentCallback | None = None
        self._action_callback: ActionCallback | None = None
        self._notify_callback: NotifyCallback | None = None

        # Track running async tasks for delay stages
        self._delay_tasks: dict[str, asyncio.Task] = {}

        # Track timeout enforcement tasks (stage_run_id → Task)
        self._timeout_tasks: dict[int, asyncio.Task] = {}

        # Track reminder tasks for human stages (stage_run_id → Task)
        self._reminder_tasks: dict[int, asyncio.Task] = {}

    # ── Configuration ────────────────────────────────────────────────────────

    def add_pipeline(self, name: str, definition: PipelineDefinition) -> None:
        """Register a pipeline definition."""
        self._pipelines[name] = definition

    def get_pipeline(self, name: str) -> PipelineDefinition | None:
        """Look up a pipeline definition by name."""
        return self._pipelines.get(name)

    def list_pipelines(self) -> list[str]:
        """Return the names of all registered pipeline definitions."""
        return list(self._pipelines.keys())

    def set_spawn_callback(self, callback: SpawnAgentCallback) -> None:
        """Set the callback for spawning agents."""
        self._spawn_agent = callback

    def set_action_callback(self, callback: ActionCallback) -> None:
        """Set the callback for executing built-in actions."""
        self._action_callback = callback

    def set_notify_callback(self, callback: NotifyCallback) -> None:
        """Set the callback for delivering notifications (PR comments, labels, etc.)."""
        self._notify_callback = callback

    def validate_all_pipelines(self) -> list[str]:
        """Validate all registered pipelines. Returns list of error messages."""
        errors: list[str] = []
        for name, defn in self._pipelines.items():
            # Validate internal stage references
            ref_errors = defn.validate_stage_references()
            for err in ref_errors:
                errors.append(f"Pipeline '{name}': {err}")

            # Validate sub-pipeline references exist
            for ref in defn.get_sub_pipeline_refs():
                if ref not in self._pipelines:
                    errors.append(f"Pipeline '{name}' references unknown sub-pipeline '{ref}'")

            # Validate gate check names exist
            for stage in defn.stages:
                if stage.type == StageType.GATE:
                    for cond in stage.conditions + (stage.any_of or []):
                        if not self._gate_registry.has(cond.check):
                            errors.append(
                                f"Pipeline '{name}', stage '{stage.id}': "
                                f"unknown gate check '{cond.check}'"
                            )

        # Check for sub-pipeline cycles (BFS)
        cycle_errors = self._detect_cycles()
        errors.extend(cycle_errors)

        return errors

    def _detect_cycles(self) -> list[str]:
        """Detect cycles in sub-pipeline references via BFS."""
        errors: list[str] = []
        for name in self._pipelines:
            visited: set[str] = set()
            queue = [name]
            while queue:
                current = queue.pop(0)
                if current in visited:
                    errors.append(f"Cycle detected in sub-pipeline references involving '{name}'")
                    break
                visited.add(current)
                defn = self._pipelines.get(current)
                if defn:
                    queue.extend(defn.get_sub_pipeline_refs())
        return errors

    # ── Event Evaluation (trigger matching) ──────────────────────────────────

    async def evaluate_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        squadron_event: SquadronEvent | None = None,
    ) -> PipelineRun | None:
        """Check if an incoming event matches any pipeline trigger.

        If matched, starts a new pipeline run and returns it.
        Also routes reactive events to running pipelines.
        """
        started_run: PipelineRun | None = None

        # 1. Check trigger-based activation (new pipelines)
        for name, defn in self._pipelines.items():
            if defn.trigger and defn.trigger.matches(event_type, payload):
                # Extract context from event
                issue_number = _extract_issue_number(payload)
                pr_number = _extract_pr_number(payload)

                # Dedup: don't start a duplicate pipeline for the same trigger
                if pr_number:
                    existing = await self._registry.get_running_pipelines_for_pr(pr_number)
                    if any(r.pipeline_name == name for r in existing):
                        logger.info(
                            "Pipeline '%s' already running for PR #%s, skipping",
                            name,
                            pr_number,
                        )
                        continue

                run = await self._start_pipeline(
                    name,
                    defn,
                    event_type=event_type,
                    payload=payload,
                    issue_number=issue_number,
                    pr_number=pr_number,
                    delivery_id=(squadron_event.delivery_id if squadron_event else None),
                )
                started_run = run

        # 2. Route reactive events to running pipelines
        await self._route_reactive_event(event_type, payload)

        return started_run

    # ── Pipeline Lifecycle ───────────────────────────────────────────────────

    async def _start_pipeline(
        self,
        name: str,
        definition: PipelineDefinition,
        *,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
        delivery_id: str | None = None,
        parent_run_id: str | None = None,
        parent_stage_id: str | None = None,
        nesting_depth: int = 0,
        extra_context: dict[str, Any] | None = None,
    ) -> PipelineRun:
        """Create and start a new pipeline run."""
        run_id = f"pl-{uuid.uuid4().hex[:12]}"

        # Build initial context
        context = dict(definition.context)
        if extra_context:
            context.update(extra_context)
        if pr_number:
            context["pr_number"] = pr_number
        if issue_number:
            context["issue_number"] = issue_number

        # Snapshot the definition for versioning
        snapshot = definition.model_dump_json()

        run = PipelineRun(
            run_id=run_id,
            pipeline_name=name,
            definition_snapshot=snapshot,
            trigger_event=event_type,
            trigger_delivery_id=delivery_id,
            issue_number=issue_number,
            pr_number=pr_number,
            scope=definition.scope,
            parent_run_id=parent_run_id,
            parent_stage_id=parent_stage_id,
            nesting_depth=nesting_depth,
            status=PipelineRunStatus.RUNNING,
            current_stage_id=definition.stages[0].id if definition.stages else None,
            context=context,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )

        await self._registry.create_pipeline_run(run)
        logger.info(
            "Started pipeline '%s' run %s (PR #%s, issue #%s)",
            name,
            run_id,
            pr_number,
            issue_number,
        )

        # Auto-associate PR with pipeline (for cross-PR event routing)
        if pr_number:
            await self._registry.add_pr_association(run_id, pr_number, self._repo, role="primary")

        # Execute the first stage
        if definition.stages:
            await self._execute_stage(run, definition, definition.stages[0])

        return run

    async def start_pipeline(
        self,
        name: str,
        *,
        issue_number: int | None = None,
        pr_number: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> PipelineRun | None:
        """Public API: start a named pipeline manually.

        Returns the PipelineRun or None if the pipeline name is unknown.
        """
        defn = self._pipelines.get(name)
        if not defn:
            logger.error("Unknown pipeline: '%s'", name)
            return None

        return await self._start_pipeline(
            name,
            defn,
            issue_number=issue_number,
            pr_number=pr_number,
            extra_context=context,
        )

    async def _complete_pipeline(self, run: PipelineRun) -> None:
        """Mark a pipeline run as completed."""
        run.status = PipelineRunStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        await self._registry.update_pipeline_run(run)
        logger.info("Pipeline '%s' run %s completed", run.pipeline_name, run.run_id)

        # Execute on_complete hooks
        await self._execute_pipeline_hooks(run, "on_complete")

        # If this is a sub-pipeline, notify the parent
        if run.parent_run_id and run.parent_stage_id:
            await self._on_sub_pipeline_complete(run)

    async def _fail_pipeline(
        self,
        run: PipelineRun,
        error_message: str,
        *,
        error_stage_id: str | None = None,
    ) -> None:
        """Mark a pipeline run as failed."""
        run.status = PipelineRunStatus.FAILED
        run.completed_at = datetime.now(timezone.utc)
        run.error_message = error_message
        run.error_stage_id = error_stage_id
        await self._registry.update_pipeline_run(run)
        logger.error(
            "Pipeline '%s' run %s failed at stage '%s': %s",
            run.pipeline_name,
            run.run_id,
            error_stage_id,
            error_message,
        )

        # Execute on_error hooks
        await self._execute_pipeline_hooks(run, "on_error")

        # If this is a sub-pipeline, notify the parent
        if run.parent_run_id and run.parent_stage_id:
            await self._on_sub_pipeline_complete(run)

    async def _escalate_pipeline(self, run: PipelineRun, reason: str) -> None:
        """Mark a pipeline run as escalated."""
        run.status = PipelineRunStatus.ESCALATED
        run.completed_at = datetime.now(timezone.utc)
        run.error_message = reason
        await self._registry.update_pipeline_run(run)
        logger.warning(
            "Pipeline '%s' run %s escalated: %s",
            run.pipeline_name,
            run.run_id,
            reason,
        )

        # If this is a sub-pipeline, notify the parent
        if run.parent_run_id and run.parent_stage_id:
            await self._on_sub_pipeline_complete(run)

    async def cancel_pipeline(self, run_id: str) -> bool:
        """Cancel a running pipeline and all child pipelines. Returns True if cancelled."""
        run = await self._registry.get_pipeline_run(run_id)
        if not run or run.status not in (
            PipelineRunStatus.PENDING,
            PipelineRunStatus.RUNNING,
        ):
            return False

        run.status = PipelineRunStatus.CANCELLED
        run.completed_at = datetime.now(timezone.utc)
        await self._registry.update_pipeline_run(run)

        # Cancel any running delay tasks
        task = self._delay_tasks.pop(run_id, None)
        if task and not task.done():
            task.cancel()

        # Cascade cancellation to child pipelines
        children = await self._registry.get_child_pipelines(run_id)
        for child in children:
            if child.status in (PipelineRunStatus.PENDING, PipelineRunStatus.RUNNING):
                await self.cancel_pipeline(child.run_id)

        logger.info("Pipeline '%s' run %s cancelled", run.pipeline_name, run_id)
        return True

    async def complete_human_stage(
        self,
        run_id: str,
        stage_id: str,
        *,
        completed_by: str,
        action: str = "approved",
    ) -> bool:
        """Signal that a human has completed their action on a human stage.

        Args:
            run_id: The pipeline run ID.
            stage_id: The stage ID of the human stage.
            completed_by: GitHub username of the person who acted.
            action: The action taken (e.g. "approved", "commented", "labeled", "dismissed").

        Returns:
            True if the stage was successfully completed/advanced, False otherwise.
        """
        run = await self._registry.get_pipeline_run(run_id)
        if not run or run.status != PipelineRunStatus.RUNNING:
            logger.warning(
                "complete_human_stage: pipeline %s not running (status=%s)",
                run_id,
                run.status if run else "not found",
            )
            return False

        try:
            defn = PipelineDefinition.model_validate_json(run.definition_snapshot)
        except Exception:
            logger.error("Failed to parse definition for pipeline %s", run_id)
            return False

        stage = defn.get_stage(stage_id)
        if not stage or stage.type != StageType.HUMAN:
            logger.warning(
                "complete_human_stage: stage '%s' not found or not a human stage", stage_id
            )
            return False

        latest = await self._registry.get_latest_stage_run(run_id, stage_id)
        if not latest or latest.status != StageRunStatus.WAITING:
            logger.warning(
                "complete_human_stage: stage '%s' not in WAITING state (status=%s)",
                stage_id,
                latest.status if latest else "not found",
            )
            return False

        # Validate action matches wait_for type
        human_config = stage.human
        if human_config:
            expected_actions = _HUMAN_WAIT_ACTIONS.get(human_config.wait_for, set())
            if expected_actions and action not in expected_actions:
                logger.info(
                    "complete_human_stage: action '%s' does not match wait_for '%s' "
                    "(expected one of %s)",
                    action,
                    human_config.wait_for.value,
                    expected_actions,
                )
                return False

            # Validate from_group constraint
            if human_config.from_group:
                # from_group is a team/org name — the integration layer should validate
                # membership. Here we just record the actor.
                pass

        # Track completion in HumanStageState
        human_state = await self._registry.get_human_stage_state(
            latest.id  # type: ignore[arg-type]
        )
        if human_state:
            # Multi-approval support: check if count threshold is met
            required_count = human_config.count if human_config else 1
            current_approvers = human_state.assigned_users or []

            if completed_by not in current_approvers:
                current_approvers.append(completed_by)
                human_state.assigned_users = current_approvers

            # Count unique completions — we track actors in assigned_users
            completion_count = len(current_approvers)

            if completion_count < required_count:
                # Not enough approvals yet — stay in WAITING
                human_state.completed_by = None
                human_state.completed_action = action
                await self._registry.update_human_stage_state(human_state)
                logger.info(
                    "Human stage '%s' has %d/%d approvals (pipeline %s)",
                    stage_id,
                    completion_count,
                    required_count,
                    run_id,
                )
                return True

            # Enough completions — mark done
            human_state.completed_by = completed_by
            human_state.completed_action = action
            await self._registry.update_human_stage_state(human_state)

        # Cancel reminder task if running
        if latest.id is not None:
            reminder_task = self._reminder_tasks.pop(latest.id, None)
            if reminder_task and not reminder_task.done():
                reminder_task.cancel()

        # Cancel timeout task if running
        if latest.id is not None:
            timeout_task = self._timeout_tasks.pop(latest.id, None)
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()

        # Complete the stage
        latest.status = StageRunStatus.COMPLETED
        latest.completed_at = datetime.now(timezone.utc)
        latest.outputs = {"completed_by": completed_by, "action": action}
        await self._registry.update_stage_run(latest)

        logger.info(
            "Human stage '%s' completed by %s (action=%s, pipeline %s)",
            stage_id,
            completed_by,
            action,
            run_id,
        )

        # Advance pipeline
        await self._advance_after_stage(run, defn, stage, "complete")
        return True

    # ── Stage Execution ──────────────────────────────────────────────────────

    async def _execute_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
    ) -> None:
        """Execute a single pipeline stage. Dispatches to type-specific handler."""
        # Update pipeline's current stage
        run.current_stage_id = stage.id
        await self._registry.update_pipeline_run(run)

        # Check conditional execution
        if stage.condition and not self._evaluate_stage_condition(stage, run):
            logger.info(
                "Stage '%s' condition not met, skipping (pipeline %s)",
                stage.id,
                run.run_id,
            )
            skip_to = stage.skip_to
            if skip_to:
                await self._transition_to(run, definition, skip_to)
            else:
                # Skip to next stage in sequence
                next_stage = definition.get_next_stage(stage.id)
                if next_stage:
                    await self._execute_stage(run, definition, next_stage)
                else:
                    await self._complete_pipeline(run)
            return

        # Create stage run record
        retry_count = 0
        if isinstance(stage.on_error, dict):
            retry_count = stage.on_error.get("retry", 0)
        elif hasattr(stage.on_error, "retry"):
            retry_count = stage.on_error.retry  # type: ignore[union-attr]

        stage_run = StageRun(
            run_id=run.run_id,
            stage_id=stage.id,
            status=StageRunStatus.RUNNING,
            max_attempts=1 + retry_count,
            started_at=datetime.now(timezone.utc),
        )
        stage_run_id = await self._registry.create_stage_run(stage_run)
        stage_run.id = stage_run_id

        try:
            match stage.type:
                case StageType.AGENT:
                    await self._execute_agent_stage(run, definition, stage, stage_run)
                case StageType.GATE:
                    await self._execute_gate_stage(run, definition, stage, stage_run)
                case StageType.ACTION:
                    await self._execute_action_stage(run, definition, stage, stage_run)
                case StageType.DELAY:
                    await self._execute_delay_stage(run, definition, stage, stage_run)
                case StageType.HUMAN:
                    await self._execute_human_stage(run, definition, stage, stage_run)
                case StageType.PARALLEL:
                    await self._execute_parallel_stage(run, definition, stage, stage_run)
                case StageType.PIPELINE:
                    await self._execute_pipeline_stage(run, definition, stage, stage_run)
                case StageType.WEBHOOK:
                    # Webhook stages are Phase 5
                    logger.warning("Webhook stages not yet implemented (stage '%s')", stage.id)
                    stage_run.status = StageRunStatus.SKIPPED
                    stage_run.completed_at = datetime.now(timezone.utc)
                    await self._registry.update_stage_run(stage_run)
                    await self._advance_after_stage(run, definition, stage, "complete")

        except Exception as exc:
            logger.exception(
                "Stage '%s' failed with exception (pipeline %s)",
                stage.id,
                run.run_id,
            )
            stage_run.status = StageRunStatus.FAILED
            stage_run.error_message = str(exc)
            stage_run.completed_at = datetime.now(timezone.utc)
            await self._registry.update_stage_run(stage_run)
            await self._handle_stage_error(run, definition, stage, str(exc))

    async def _execute_agent_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute an agent stage — spawn/wake an agent and wait for completion."""
        if not self._spawn_agent:
            msg = "No spawn agent callback configured"
            raise RuntimeError(msg)

        agent_id = await self._spawn_agent(
            stage.agent,  # type: ignore[arg-type]
            run.issue_number,
            pr_number=run.pr_number,
            pipeline_run_id=run.run_id,
            stage_id=stage.id,
            action=stage.action,
            continue_session=stage.continue_session,
            context=run.context,
        )

        if agent_id:
            stage_run.agent_id = agent_id
            stage_run.status = StageRunStatus.WAITING
            await self._registry.update_stage_run(stage_run)
            logger.info(
                "Agent stage '%s' spawned agent %s (pipeline %s)",
                stage.id,
                agent_id,
                run.run_id,
            )
        else:
            stage_run.status = StageRunStatus.FAILED
            stage_run.error_message = "Failed to spawn agent"
            stage_run.completed_at = datetime.now(timezone.utc)
            await self._registry.update_stage_run(stage_run)
            await self._handle_stage_error(run, definition, stage, "Failed to spawn agent")

    async def _execute_gate_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a gate stage — evaluate all conditions."""
        ctx = self._build_context(run)
        conditions = stage.conditions
        use_any = bool(stage.any_of)
        if use_any:
            conditions = stage.any_of or []

        all_results: list[GateCheckResult] = []
        for cond in conditions:
            check = self._gate_registry.get(cond.check)

            # Cross-PR gate targeting: override context pr_number if condition has `pr`
            eval_ctx = ctx
            if cond.pr is not None:
                target_pr = self._resolve_pr_target(cond.pr, run)
                if target_pr is not None:
                    eval_ctx = PipelineContext(
                        pr_number=target_pr,
                        issue_number=ctx.issue_number,
                        owner=ctx.owner,
                        repo=ctx.repo,
                        pipeline_run_id=ctx.pipeline_run_id,
                        context=ctx.context,
                        github_client=ctx.github_client,
                    )

            config = cond.get_config()
            config.pop("pr", None)  # Don't pass `pr` to the check itself
            result = await check.evaluate(config, eval_ctx)
            all_results.append(result)

            # Record each check
            await self._registry.create_gate_check(
                GateCheckRecord(
                    stage_run_id=stage_run.id,  # type: ignore[arg-type]
                    check_type=cond.check,
                    check_config=json.dumps(cond.get_config()),
                    passed=result.passed,
                    message=result.message,
                    result_data=result.data,
                )
            )

        # Evaluate overall result
        if use_any:
            passed = any(r.passed for r in all_results)
        else:
            passed = all(r.passed for r in all_results)

        if passed:
            stage_run.status = StageRunStatus.COMPLETED
            stage_run.completed_at = datetime.now(timezone.utc)
            await self._registry.update_stage_run(stage_run)
            # Cancel timeout if one was scheduled
            if stage_run.id is not None:
                timeout_task = self._timeout_tasks.pop(stage_run.id, None)
                if timeout_task and not timeout_task.done():
                    timeout_task.cancel()
            logger.info("Gate '%s' passed (pipeline %s)", stage.id, run.run_id)
            await self._advance_after_stage(run, definition, stage, "pass")
        else:
            # Gate not yet passing — enter WAITING state for reactive re-eval
            stage_run.status = StageRunStatus.WAITING
            await self._registry.update_stage_run(stage_run)
            # Schedule timeout if configured
            self._schedule_stage_timeout(run, definition, stage, stage_run, ctx)
            logger.info(
                "Gate '%s' waiting — conditions not met (pipeline %s)",
                stage.id,
                run.run_id,
            )

    async def _execute_action_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a built-in action stage (e.g. merge_pr)."""
        action_name = stage.action
        if not action_name:
            msg = f"Action stage '{stage.id}' has no action specified"
            raise RuntimeError(msg)

        ctx = self._build_context(run)
        if self._action_callback:
            result = await self._action_callback(action_name, stage.config, ctx)
            success = result.get("success", False)
        else:
            # No action callback — log and skip
            logger.warning(
                "No action callback configured for action '%s' (stage '%s')",
                action_name,
                stage.id,
            )
            success = True
            result = {}

        stage_run.outputs = result
        if success:
            stage_run.status = StageRunStatus.COMPLETED
            stage_run.completed_at = datetime.now(timezone.utc)
            await self._registry.update_stage_run(stage_run)
            result_key = "success" if stage.on_success else "complete"
            await self._advance_after_stage(run, definition, stage, result_key)
        else:
            stage_run.status = StageRunStatus.FAILED
            stage_run.error_message = result.get("error", "Action failed")
            stage_run.completed_at = datetime.now(timezone.utc)
            await self._registry.update_stage_run(stage_run)

            if stage.on_conflict:
                await self._transition_to(run, definition, stage.on_conflict)
            else:
                await self._handle_stage_error(
                    run, definition, stage, result.get("error", "Action failed")
                )

    async def _execute_delay_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a delay stage — wait for the specified duration."""
        duration_str = stage.duration
        if not duration_str:
            msg = f"Delay stage '{stage.id}' has no duration"
            raise RuntimeError(msg)

        seconds = _parse_duration_seconds(duration_str)

        async def _delay_coroutine() -> None:
            try:
                await asyncio.sleep(seconds)
                stage_run.status = StageRunStatus.COMPLETED
                stage_run.completed_at = datetime.now(timezone.utc)
                await self._registry.update_stage_run(stage_run)
                await self._advance_after_stage(run, definition, stage, "complete")
            except asyncio.CancelledError:
                stage_run.status = StageRunStatus.CANCELLED
                stage_run.completed_at = datetime.now(timezone.utc)
                await self._registry.update_stage_run(stage_run)

        # Mark as waiting and launch background task
        stage_run.status = StageRunStatus.WAITING
        await self._registry.update_stage_run(stage_run)

        task = asyncio.create_task(_delay_coroutine())
        self._delay_tasks[run.run_id] = task
        logger.info(
            "Delay stage '%s' waiting %ds (pipeline %s)",
            stage.id,
            seconds,
            run.run_id,
        )

    async def _execute_human_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a human stage — enter WAITING state for human action.

        Full lifecycle:
            1. Enter WAITING state and create HumanStageState record
            2. Auto-assign reviewers from ``human.from_group`` (via notify callback)
            3. Send entry notification (via notify callback)
            4. Schedule reminder task if ``human.notify.reminder`` is configured
        """
        human_config = stage.human
        ctx = self._build_context(run)

        stage_run.status = StageRunStatus.WAITING
        await self._registry.update_stage_run(stage_run)

        # Create tracking record
        human_state = HumanStageState(
            stage_run_id=stage_run.id,  # type: ignore[arg-type]
        )

        # Auto-assign reviewers
        assigned_users: list[str] = []
        if human_config and human_config.auto_assign and human_config.from_group:
            assigned_users = [human_config.from_group]
            human_state.assigned_users = assigned_users
            if self._notify_callback:
                try:
                    await self._notify_callback(
                        "assign",
                        ctx,
                        users=assigned_users,
                        message=human_config.description
                        or f"Review requested for stage '{stage.id}'",
                    )
                except Exception:
                    logger.exception(
                        "Failed to auto-assign reviewers for human stage '%s' (pipeline %s)",
                        stage.id,
                        run.run_id,
                    )

        await self._registry.create_human_stage_state(human_state)

        # Send entry notification
        if human_config and human_config.notify and human_config.notify.on_enter:
            if self._notify_callback:
                try:
                    await self._notify_callback(
                        "pr_comment",
                        ctx,
                        message=human_config.notify.on_enter,
                    )
                    # Update notification timestamp
                    human_state.entry_notified_at = datetime.now(timezone.utc)
                    await self._registry.update_human_stage_state(human_state)
                except Exception:
                    logger.exception(
                        "Failed to send entry notification for human stage '%s' (pipeline %s)",
                        stage.id,
                        run.run_id,
                    )

        # Schedule reminder task
        if (
            human_config
            and human_config.notify
            and human_config.notify.reminder
            and stage_run.id is not None
        ):
            reminder_cfg = human_config.notify.reminder
            interval_str = reminder_cfg.get("interval", "24h")
            interval_secs = _parse_duration_seconds(interval_str)
            max_reminders = reminder_cfg.get("max_reminders", 3)
            reminder_message = reminder_cfg.get(
                "message",
                f"Reminder: human action required on stage '{stage.id}'",
            )

            async def _reminder_loop(
                sr_id: int,
                interval: int,
                max_count: int,
                msg: str,
            ) -> None:
                count = 0
                try:
                    while count < max_count:
                        await asyncio.sleep(interval)
                        count += 1
                        if self._notify_callback:
                            try:
                                await self._notify_callback(
                                    "pr_comment",
                                    ctx,
                                    message=f"[Reminder {count}/{max_count}] {msg}",
                                )
                            except Exception:
                                logger.exception("Failed to send reminder %d", count)
                        # Update reminder tracking
                        state = await self._registry.get_human_stage_state(sr_id)
                        if state:
                            state.reminder_count = count
                            state.last_reminder_at = datetime.now(timezone.utc)
                            await self._registry.update_human_stage_state(state)
                except asyncio.CancelledError:
                    pass

            task = asyncio.create_task(
                _reminder_loop(stage_run.id, interval_secs, max_reminders, reminder_message)
            )
            self._reminder_tasks[stage_run.id] = task

        # Schedule timeout if configured
        self._schedule_stage_timeout(run, definition, stage, stage_run, ctx)

        logger.info(
            "Human stage '%s' waiting for %s action (pipeline %s, assigned=%s)",
            stage.id,
            human_config.wait_for.value if human_config else "approval",
            run.run_id,
            assigned_users or "none",
        )

    async def _execute_parallel_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a parallel stage — spawn branches of various types.

        Supported branch types:
        - ``agent``: Spawn an agent (requires spawn callback).
        - ``pipeline``: Start a sub-pipeline as a branch.
        - ``action``: Execute a built-in action as a branch.
        """
        stage_run.status = StageRunStatus.WAITING
        await self._registry.update_stage_run(stage_run)

        for branch in stage.branches:
            # Check branch condition (simplified — full conditions in Phase 5)
            if branch.condition:
                logger.info(
                    "Skipping conditional branch '%s' (conditions not yet evaluated)",
                    branch.id,
                )
                continue

            branch_stage_id = f"{stage.id}/{branch.id}"

            if branch.type == StageType.AGENT and branch.agent:
                await self._execute_parallel_agent_branch(run, stage, branch, branch_stage_id)
            elif branch.type == StageType.PIPELINE and branch.pipeline:
                await self._execute_parallel_pipeline_branch(run, stage, branch, branch_stage_id)
            elif branch.type == StageType.ACTION and branch.action:
                await self._execute_parallel_action_branch(
                    run, definition, stage, branch, branch_stage_id
                )
            else:
                logger.warning(
                    "Unsupported or misconfigured parallel branch '%s' (type=%s)",
                    branch.id,
                    branch.type.value,
                )

        logger.info(
            "Parallel stage '%s' launched branches (pipeline %s)",
            stage.id,
            run.run_id,
        )

    async def _execute_parallel_agent_branch(
        self,
        run: PipelineRun,
        stage: StageDefinition,
        branch: ParallelBranch,
        branch_stage_id: str,
    ) -> None:
        """Spawn an agent for a parallel branch."""
        if not self._spawn_agent:
            msg = "No spawn agent callback configured"
            raise RuntimeError(msg)

        branch_stage_run = StageRun(
            run_id=run.run_id,
            stage_id=branch_stage_id,
            status=StageRunStatus.RUNNING,
            branch_id=branch.id,
            parent_stage_id=stage.id,
            started_at=datetime.now(timezone.utc),
        )
        branch_id = await self._registry.create_stage_run(branch_stage_run)
        branch_stage_run.id = branch_id

        agent_id = await self._spawn_agent(
            branch.agent,  # type: ignore[arg-type]
            run.issue_number,
            pr_number=run.pr_number,
            pipeline_run_id=run.run_id,
            stage_id=branch_stage_id,
            action=branch.action,
            context=run.context,
        )

        if agent_id:
            branch_stage_run.agent_id = agent_id
            branch_stage_run.status = StageRunStatus.WAITING
        else:
            branch_stage_run.status = StageRunStatus.FAILED
            branch_stage_run.error_message = "Failed to spawn agent"
            branch_stage_run.completed_at = datetime.now(timezone.utc)
        await self._registry.update_stage_run(branch_stage_run)

    async def _execute_parallel_pipeline_branch(
        self,
        run: PipelineRun,
        stage: StageDefinition,
        branch: ParallelBranch,
        branch_stage_id: str,
    ) -> None:
        """Start a sub-pipeline for a parallel branch."""
        pipeline_name = branch.pipeline
        if not pipeline_name:
            return

        child_def = self._pipelines.get(pipeline_name)
        if not child_def:
            logger.error(
                "Unknown sub-pipeline '%s' in parallel branch '%s'", pipeline_name, branch.id
            )
            return

        if run.nesting_depth >= MAX_NESTING_DEPTH:
            logger.error("Sub-pipeline nesting depth exceeded in parallel branch '%s'", branch.id)
            return

        branch_stage_run = StageRun(
            run_id=run.run_id,
            stage_id=branch_stage_id,
            status=StageRunStatus.RUNNING,
            branch_id=branch.id,
            parent_stage_id=stage.id,
            started_at=datetime.now(timezone.utc),
        )
        sr_id = await self._registry.create_stage_run(branch_stage_run)
        branch_stage_run.id = sr_id

        child_run = await self._start_pipeline(
            pipeline_name,
            child_def,
            issue_number=run.issue_number,
            pr_number=run.pr_number,
            parent_run_id=run.run_id,
            parent_stage_id=branch_stage_id,
            nesting_depth=run.nesting_depth + 1,
            extra_context=branch.context,
        )

        branch_stage_run.child_pipeline_run_id = child_run.run_id
        branch_stage_run.status = StageRunStatus.WAITING
        await self._registry.update_stage_run(branch_stage_run)

    async def _execute_parallel_action_branch(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        branch: ParallelBranch,
        branch_stage_id: str,
    ) -> None:
        """Execute an action for a parallel branch."""
        branch_stage_run = StageRun(
            run_id=run.run_id,
            stage_id=branch_stage_id,
            status=StageRunStatus.RUNNING,
            branch_id=branch.id,
            parent_stage_id=stage.id,
            started_at=datetime.now(timezone.utc),
        )
        sr_id = await self._registry.create_stage_run(branch_stage_run)
        branch_stage_run.id = sr_id

        ctx = self._build_context(run)
        if self._action_callback:
            try:
                result = await self._action_callback(branch.action, branch.config, ctx)  # type: ignore[arg-type]
                success = result.get("success", False)
            except Exception as exc:
                result = {"error": str(exc)}
                success = False
        else:
            logger.warning("No action callback for parallel branch '%s'", branch.id)
            result = {}
            success = True

        branch_stage_run.outputs = result
        if success:
            branch_stage_run.status = StageRunStatus.COMPLETED
        else:
            branch_stage_run.status = StageRunStatus.FAILED
            branch_stage_run.error_message = result.get("error", "Action failed")
        branch_stage_run.completed_at = datetime.now(timezone.utc)
        await self._registry.update_stage_run(branch_stage_run)

        # Immediately check parallel completion since action branches complete synchronously
        await self._check_parallel_completion(branch_stage_run)

    async def _execute_pipeline_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a sub-pipeline stage — start a child pipeline."""
        pipeline_name = stage.pipeline
        if not pipeline_name:
            msg = f"Pipeline stage '{stage.id}' has no pipeline name"
            raise RuntimeError(msg)

        child_def = self._pipelines.get(pipeline_name)
        if not child_def:
            msg = f"Unknown sub-pipeline: '{pipeline_name}'"
            raise RuntimeError(msg)

        # Check nesting depth
        if run.nesting_depth >= MAX_NESTING_DEPTH:
            msg = f"Sub-pipeline nesting depth exceeded (max {MAX_NESTING_DEPTH})"
            raise RuntimeError(msg)

        # Start child pipeline
        child_run = await self._start_pipeline(
            pipeline_name,
            child_def,
            issue_number=run.issue_number,
            pr_number=run.pr_number,
            parent_run_id=run.run_id,
            parent_stage_id=stage.id,
            nesting_depth=run.nesting_depth + 1,
            extra_context=stage.context,
        )

        stage_run.child_pipeline_run_id = child_run.run_id
        stage_run.status = StageRunStatus.WAITING
        await self._registry.update_stage_run(stage_run)

        logger.info(
            "Pipeline stage '%s' started sub-pipeline '%s' run %s (pipeline %s)",
            stage.id,
            pipeline_name,
            child_run.run_id,
            run.run_id,
        )

    # ── Stage Transition Helpers ─────────────────────────────────────────────

    async def _advance_after_stage(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        result: str,
    ) -> None:
        """Determine the next stage after a stage completes and execute it."""
        # Check explicit transition first
        target = stage.get_next_stage_id(result)

        if target == "__complete__":
            await self._complete_pipeline(run)
            return
        if target == "__escalate__":
            await self._escalate_pipeline(run, f"Escalated from stage '{stage.id}'")
            return

        # If target is a stage ID, go to it
        if target and target != "__next__":
            next_stage = definition.get_stage(target)
            if next_stage:
                await self._execute_stage(run, definition, next_stage)
            else:
                await self._fail_pipeline(
                    run,
                    f"Stage '{stage.id}' references unknown target '{target}'",
                    error_stage_id=stage.id,
                )
            return

        # Default: advance to next stage in sequence
        next_stage = definition.get_next_stage(stage.id)
        if next_stage:
            await self._execute_stage(run, definition, next_stage)
        else:
            # No more stages — pipeline complete
            await self._complete_pipeline(run)

    async def _transition_to(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        target: str,
    ) -> None:
        """Transition to a specific stage by ID or special target."""
        if target == "__complete__":
            await self._complete_pipeline(run)
        elif target == "__escalate__" or target == "escalate":
            await self._escalate_pipeline(run, "Escalated via transition")
        elif target == "fail":
            await self._fail_pipeline(run, "Failed via transition")
        else:
            stage = definition.get_stage(target)
            if stage:
                await self._execute_stage(run, definition, stage)
            else:
                await self._fail_pipeline(run, f"Unknown transition target: '{target}'")

    async def _handle_stage_error(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        error_message: str,
    ) -> None:
        """Handle a stage error — check retry config and on_error transitions."""
        on_error = stage.on_error
        if isinstance(on_error, dict):
            retry = on_error.get("retry", 0)
            then = on_error.get("then")
        elif hasattr(on_error, "retry"):
            retry = on_error.retry  # type: ignore[union-attr]
            then = on_error.then  # type: ignore[union-attr]
        else:
            retry = 0
            then = None

        # Check if we can retry
        if retry > 0:
            latest = await self._registry.get_latest_stage_run(run.run_id, stage.id)
            if latest and latest.attempt_number < latest.max_attempts:
                logger.info(
                    "Retrying stage '%s' (attempt %d/%d, pipeline %s)",
                    stage.id,
                    latest.attempt_number + 1,
                    latest.max_attempts,
                    run.run_id,
                )
                # Create a new stage run for the retry
                retry_run = StageRun(
                    run_id=run.run_id,
                    stage_id=stage.id,
                    status=StageRunStatus.RUNNING,
                    attempt_number=latest.attempt_number + 1,
                    max_attempts=latest.max_attempts,
                    started_at=datetime.now(timezone.utc),
                )
                retry_id = await self._registry.create_stage_run(retry_run)
                retry_run.id = retry_id

                # Re-dispatch the stage
                try:
                    match stage.type:
                        case StageType.AGENT:
                            await self._execute_agent_stage(run, definition, stage, retry_run)
                        case StageType.ACTION:
                            await self._execute_action_stage(run, definition, stage, retry_run)
                        case _:
                            # Other stage types don't typically retry
                            pass
                    return
                except Exception:
                    logger.exception(
                        "Retry of stage '%s' also failed (pipeline %s)",
                        stage.id,
                        run.run_id,
                    )

        # Follow on_error transition
        if then:
            await self._transition_to(run, definition, then)
        else:
            # No error handler — fail the pipeline
            await self._fail_pipeline(run, error_message, error_stage_id=stage.id)

    def _evaluate_stage_condition(self, stage: StageDefinition, run: PipelineRun) -> bool:
        """Evaluate a stage's conditional execution config (simplified).

        Full template expression evaluation is Phase 5.
        For now, supports:
            - labels_include: check if label is in context["labels"]
        """
        if not stage.condition:
            return True

        condition = stage.condition
        ctx = run.context
        labels = ctx.get("labels", [])

        # any: at least one condition must match
        if "any" in condition:
            for sub in condition["any"]:
                if "labels_include" in sub:
                    if sub["labels_include"] in labels:
                        return True
            return False

        # all: every condition must match
        if "all" in condition:
            for sub in condition["all"]:
                if "labels_include" in sub:
                    if sub["labels_include"] not in labels:
                        return False
            return True

        # Direct condition
        if "labels_include" in condition:
            return condition["labels_include"] in labels

        # Unknown condition format — default to True
        return True

    # ── Reactive Event Handling ──────────────────────────────────────────────

    async def _route_reactive_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Route a reactive event to all running pipelines that care about it."""
        pr_number = _extract_pr_number(payload)
        issue_number = _extract_issue_number(payload)

        if not pr_number and not issue_number:
            return

        # Find running pipelines for this PR/issue
        running: list[PipelineRun] = []
        if pr_number:
            running.extend(await self._registry.get_running_pipelines_for_pr(pr_number))
        if issue_number:
            running.extend(
                await self._registry.get_pipeline_runs_by_issue(
                    issue_number, status=PipelineRunStatus.RUNNING
                )
            )

        # Dedup
        seen: set[str] = set()
        unique_runs: list[PipelineRun] = []
        for r in running:
            if r.run_id not in seen:
                seen.add(r.run_id)
                unique_runs.append(r)

        for run in unique_runs:
            # Load the definition from snapshot
            try:
                defn = PipelineDefinition.model_validate_json(run.definition_snapshot)
            except Exception:
                logger.warning(
                    "Failed to parse definition snapshot for pipeline %s",
                    run.run_id,
                )
                continue

            # Check on_events config
            reactive_config = defn.on_events.get(event_type)
            if reactive_config:
                await self._handle_reactive_action(run, defn, reactive_config)

            # Always re-evaluate gates/human stages on relevant events
            await self._reevaluate_waiting_stages(run, defn, event_type, payload)

    async def _handle_reactive_action(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        config: Any,  # ReactiveEventConfig
    ) -> None:
        """Handle a configured reactive event action."""
        action = config.action

        if action == ReactiveAction.CANCEL:
            await self.cancel_pipeline(run.run_id)
        elif action == ReactiveAction.REEVALUATE_GATES:
            # Already handled by _reevaluate_waiting_stages
            pass
        elif action == ReactiveAction.INVALIDATE_AND_RESTART:
            await self._invalidate_and_restart(run, definition, config)
        elif action == ReactiveAction.NOTIFY:
            await self._handle_reactive_notify(run, config)
        elif action == ReactiveAction.WAKE_AGENT:
            await self._handle_reactive_wake_agent(run, definition)

    async def _handle_reactive_notify(
        self,
        run: PipelineRun,
        config: Any,  # ReactiveEventConfig
    ) -> None:
        """Handle NOTIFY reactive action — send a notification via the notify callback."""
        if not self._notify_callback:
            logger.info(
                "Reactive NOTIFY on pipeline %s — no notify callback configured",
                run.run_id,
            )
            return

        ctx = self._build_context(run)
        notify_cfg = config.notify or {}
        message = notify_cfg.get(
            "message", f"Reactive event notification for pipeline {run.run_id}"
        )
        label = notify_cfg.get("label")
        target = notify_cfg.get("target", "pr_comment")

        try:
            await self._notify_callback(target, ctx, message=message, label=label)
            logger.info("Reactive NOTIFY sent for pipeline %s", run.run_id)
        except Exception:
            logger.exception("Failed to send reactive NOTIFY for pipeline %s", run.run_id)

    async def _handle_reactive_wake_agent(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
    ) -> None:
        """Handle WAKE_AGENT reactive action — wake the agent for the current stage."""
        if not run.current_stage_id:
            return

        current_stage = definition.get_stage(run.current_stage_id)
        if not current_stage:
            return

        latest = await self._registry.get_latest_stage_run(run.run_id, current_stage.id)
        if not latest or not latest.agent_id:
            logger.info(
                "WAKE_AGENT: no agent found for stage '%s' (pipeline %s)",
                run.current_stage_id,
                run.run_id,
            )
            return

        if not self._spawn_agent:
            logger.warning("WAKE_AGENT: no spawn callback configured")
            return

        try:
            await self._spawn_agent(
                current_stage.agent or "",
                run.issue_number,
                pr_number=run.pr_number,
                pipeline_run_id=run.run_id,
                stage_id=current_stage.id,
                continue_session=True,
                context=run.context,
            )
            logger.info(
                "WAKE_AGENT: woke agent for stage '%s' (pipeline %s)",
                current_stage.id,
                run.run_id,
            )
        except Exception:
            logger.exception(
                "WAKE_AGENT: failed to wake agent for stage '%s' (pipeline %s)",
                current_stage.id,
                run.run_id,
            )

    async def _invalidate_and_restart(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        config: Any,  # ReactiveEventConfig
    ) -> None:
        """Handle invalidate_and_restart reactive action.

        Marks specified stages as stale and restarts from a given stage.
        """
        stages_to_invalidate = config.invalidate
        restart_from = config.restart_from

        # Invalidate specified stage runs
        for stage_id in stages_to_invalidate:
            latest = await self._registry.get_latest_stage_run(run.run_id, stage_id)
            if latest and latest.status in (
                StageRunStatus.COMPLETED,
                StageRunStatus.WAITING,
            ):
                latest.status = StageRunStatus.CANCELLED
                latest.completed_at = datetime.now(timezone.utc)
                await self._registry.update_stage_run(latest)
                logger.info(
                    "Invalidated stage '%s' in pipeline %s",
                    stage_id,
                    run.run_id,
                )

        # Restart from the specified stage
        if restart_from == "current":
            stage_id = run.current_stage_id
        else:
            stage_id = restart_from

        if stage_id:
            stage = definition.get_stage(stage_id)
            if stage:
                await self._execute_stage(run, definition, stage)

    async def _reevaluate_waiting_stages(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Re-evaluate any gate/human stages that are currently WAITING."""
        if not run.current_stage_id:
            return

        current_stage = definition.get_stage(run.current_stage_id)
        if not current_stage:
            return

        if current_stage.type == StageType.GATE:
            # Check if any conditions react to this event
            reactive_events = self._gate_registry.get_reactive_events()
            reactive_checks = reactive_events.get(event_type, set())

            # Check if the current gate has any conditions that react
            conditions = current_stage.conditions + (current_stage.any_of or [])
            relevant = any(c.check in reactive_checks for c in conditions)

            if relevant:
                latest = await self._registry.get_latest_stage_run(run.run_id, current_stage.id)
                if latest and latest.status == StageRunStatus.WAITING:
                    logger.info(
                        "Re-evaluating gate '%s' due to event '%s' (pipeline %s)",
                        current_stage.id,
                        event_type,
                        run.run_id,
                    )
                    # Re-run gate evaluation
                    await self._execute_gate_stage(run, definition, current_stage, latest)

        elif current_stage.type == StageType.HUMAN:
            # Check if this event is relevant for the human stage's wait_for type
            human_config = current_stage.human
            if not human_config:
                return

            expected_event = _HUMAN_WAIT_EVENT_MAP.get(human_config.wait_for)
            if expected_event != event_type:
                return

            latest = await self._registry.get_latest_stage_run(run.run_id, current_stage.id)
            if not latest or latest.status != StageRunStatus.WAITING:
                return

            # Extract the actor and action from the payload
            actor, action = _extract_human_action(event_type, payload or {})
            if not actor:
                return

            logger.info(
                "Human stage '%s' received matching event '%s' from '%s' (pipeline %s)",
                current_stage.id,
                event_type,
                actor,
                run.run_id,
            )

            # For approvals, check that the review state is "approved"
            if human_config.wait_for == HumanWaitType.APPROVAL:
                review = (payload or {}).get("review", {})
                if review.get("state", "").lower() != "approved":
                    return

            # Delegate to complete_human_stage for validation + count tracking
            await self.complete_human_stage(
                run.run_id,
                current_stage.id,
                completed_by=actor,
                action=action,
            )

    # ── Agent Completion Callbacks ───────────────────────────────────────────

    async def on_agent_complete(
        self,
        agent_id: str,
        *,
        outputs: dict[str, Any] | None = None,
    ) -> None:
        """Called when an agent finishes (via report_complete or submit_pr_review)."""
        stage_run = await self._registry.get_stage_run_by_agent(agent_id)
        if not stage_run:
            logger.debug("No pipeline stage found for agent %s", agent_id)
            return

        stage_run.status = StageRunStatus.COMPLETED
        stage_run.completed_at = datetime.now(timezone.utc)
        if outputs:
            stage_run.outputs = outputs
        await self._registry.update_stage_run(stage_run)

        # Track PR associations for multi-PR pipelines
        await self._track_pr_from_outputs(stage_run, outputs)

        # Check if this is a parallel branch
        if stage_run.parent_stage_id:
            await self._check_parallel_completion(stage_run)
            return

        # Load pipeline run and definition
        run = await self._registry.get_pipeline_run(stage_run.run_id)
        if not run or run.status != PipelineRunStatus.RUNNING:
            return

        try:
            defn = PipelineDefinition.model_validate_json(run.definition_snapshot)
        except Exception:
            logger.error("Failed to parse definition for pipeline %s", run.run_id)
            return

        stage = defn.get_stage(stage_run.stage_id)
        if stage:
            await self._advance_after_stage(run, defn, stage, "complete")

    async def on_agent_error(
        self,
        agent_id: str,
        error: str,
    ) -> None:
        """Called when an agent fails."""
        stage_run = await self._registry.get_stage_run_by_agent(agent_id)
        if not stage_run:
            return

        stage_run.status = StageRunStatus.FAILED
        stage_run.error_message = error
        stage_run.completed_at = datetime.now(timezone.utc)
        await self._registry.update_stage_run(stage_run)

        # Check if this is a parallel branch
        if stage_run.parent_stage_id:
            await self._check_parallel_completion(stage_run)
            return

        run = await self._registry.get_pipeline_run(stage_run.run_id)
        if not run or run.status != PipelineRunStatus.RUNNING:
            return

        try:
            defn = PipelineDefinition.model_validate_json(run.definition_snapshot)
        except Exception:
            return

        stage = defn.get_stage(stage_run.stage_id)
        if stage:
            await self._handle_stage_error(run, defn, stage, error)

    async def _check_parallel_completion(self, completed_branch: StageRun) -> None:
        """Check if a parallel stage should advance based on its join strategy.

        Join strategies:
        - ``all`` (default): Wait for all branches to finish.
        - ``any``: Advance as soon as any branch succeeds.
        """
        if not completed_branch.parent_stage_id:
            return

        # Get all branch stage runs for this parallel stage
        all_stages = await self._registry.get_stage_runs_for_pipeline(completed_branch.run_id)
        branches = [s for s in all_stages if s.parent_stage_id == completed_branch.parent_stage_id]

        # Load definition to determine join strategy
        run = await self._registry.get_pipeline_run(completed_branch.run_id)
        if not run or run.status != PipelineRunStatus.RUNNING:
            return

        try:
            defn = PipelineDefinition.model_validate_json(run.definition_snapshot)
        except Exception:
            return

        stage = defn.get_stage(completed_branch.parent_stage_id)
        if not stage:
            return

        join = stage.join or JoinStrategy.ALL

        # Find the parent stage run
        parent_runs = [
            s
            for s in all_stages
            if s.stage_id == completed_branch.parent_stage_id and s.parent_stage_id is None
        ]
        if not parent_runs:
            return
        parent_stage_run = parent_runs[-1]

        if join == JoinStrategy.ANY:
            # Advance as soon as any branch completes successfully
            any_succeeded = any(s.status == StageRunStatus.COMPLETED for s in branches)
            if any_succeeded:
                parent_stage_run.status = StageRunStatus.COMPLETED
                parent_stage_run.completed_at = datetime.now(timezone.utc)
                await self._registry.update_stage_run(parent_stage_run)
                await self._advance_after_stage(run, defn, stage, "complete")
                return

            # If all branches are done and none succeeded, fail
            all_done = all(
                s.status
                in (StageRunStatus.COMPLETED, StageRunStatus.FAILED, StageRunStatus.SKIPPED)
                for s in branches
            )
            if all_done:
                parent_stage_run.status = StageRunStatus.FAILED
                parent_stage_run.error_message = "All parallel branches failed (join: any)"
                parent_stage_run.completed_at = datetime.now(timezone.utc)
                await self._registry.update_stage_run(parent_stage_run)
                if stage.on_any_reject:
                    target = stage.on_any_reject.get("goto")
                    if target:
                        await self._transition_to(run, defn, target)
                        return
                await self._handle_stage_error(
                    run, defn, stage, "All parallel branches failed (join: any)"
                )
        else:
            # join: all — wait for every branch to finish
            all_done = all(
                s.status
                in (StageRunStatus.COMPLETED, StageRunStatus.FAILED, StageRunStatus.SKIPPED)
                for s in branches
            )
            if not all_done:
                return

            any_failed = any(s.status == StageRunStatus.FAILED for s in branches)

            parent_stage_run.completed_at = datetime.now(timezone.utc)
            if any_failed:
                parent_stage_run.status = StageRunStatus.FAILED
                parent_stage_run.error_message = "One or more parallel branches failed"
            else:
                parent_stage_run.status = StageRunStatus.COMPLETED

            await self._registry.update_stage_run(parent_stage_run)

            if any_failed and stage.on_any_reject:
                target = stage.on_any_reject.get("goto")
                if target:
                    await self._transition_to(run, defn, target)
                    return
            await self._advance_after_stage(run, defn, stage, "complete")

    async def _on_sub_pipeline_complete(self, child_run: PipelineRun) -> None:
        """Handle completion of a sub-pipeline — advance the parent.

        Propagates child pipeline outputs into the parent run context under
        ``stages.<parent_stage_id>.outputs`` so subsequent stages can reference them.
        Also handles failure / escalation propagation.
        """
        if not child_run.parent_run_id or not child_run.parent_stage_id:
            return

        parent_run = await self._registry.get_pipeline_run(child_run.parent_run_id)
        if not parent_run or parent_run.status != PipelineRunStatus.RUNNING:
            return

        # Propagate child outputs into parent context
        if child_run.context:
            stages_data = parent_run.context.setdefault("stages", {})
            stage_data = stages_data.setdefault(child_run.parent_stage_id, {})
            # Collect outputs from child's completed stage runs
            child_stages = await self._registry.get_stage_runs_for_pipeline(child_run.run_id)
            child_outputs: dict[str, Any] = {}
            for cs in child_stages:
                if cs.outputs:
                    child_outputs[cs.stage_id] = cs.outputs
            stage_data["outputs"] = child_outputs
            stage_data["child_run_id"] = child_run.run_id
            stage_data["child_status"] = child_run.status.value
            await self._registry.update_pipeline_run(parent_run)

        # Update the parent's stage run
        latest = await self._registry.get_latest_stage_run(
            parent_run.run_id, child_run.parent_stage_id
        )
        if latest:
            if child_run.status == PipelineRunStatus.COMPLETED:
                latest.status = StageRunStatus.COMPLETED
            else:
                latest.status = StageRunStatus.FAILED
                latest.error_message = child_run.error_message
            latest.completed_at = datetime.now(timezone.utc)
            await self._registry.update_stage_run(latest)

        try:
            defn = PipelineDefinition.model_validate_json(parent_run.definition_snapshot)
        except Exception:
            return

        stage = defn.get_stage(child_run.parent_stage_id)
        if stage:
            if child_run.status == PipelineRunStatus.COMPLETED:
                await self._advance_after_stage(parent_run, defn, stage, "complete")
            else:
                await self._handle_stage_error(
                    parent_run,
                    defn,
                    stage,
                    child_run.error_message or "Sub-pipeline failed",
                )

    # ── Pipeline Hooks ────────────────────────────────────────────────────────

    async def _execute_pipeline_hooks(
        self,
        run: PipelineRun,
        hook_name: str,
    ) -> None:
        """Execute pipeline-level hooks (on_complete or on_error).

        Hooks are a list of action dicts, e.g.:
            on_complete:
              - notify: "Pipeline completed successfully"
              - label: "pipeline-done"
        """
        try:
            defn = PipelineDefinition.model_validate_json(run.definition_snapshot)
        except Exception:
            return

        hooks: list[dict[str, Any]] = []
        if hook_name == "on_complete":
            hooks = defn.on_complete
        elif hook_name == "on_error":
            hooks = defn.on_error

        if not hooks:
            return

        ctx = self._build_context(run)

        for hook_action in hooks:
            # notify: send a notification
            if "notify" in hook_action and self._notify_callback:
                try:
                    await self._notify_callback(
                        "pr_comment",
                        ctx,
                        message=hook_action["notify"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to execute %s notify hook for pipeline %s",
                        hook_name,
                        run.run_id,
                    )

            # label: add a label
            if "label" in hook_action and self._notify_callback:
                try:
                    await self._notify_callback(
                        "label",
                        ctx,
                        label=hook_action["label"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to execute %s label hook for pipeline %s",
                        hook_name,
                        run.run_id,
                    )

            # action: execute a built-in action
            if "action" in hook_action and self._action_callback:
                try:
                    await self._action_callback(
                        hook_action["action"],
                        hook_action.get("config", {}),
                        ctx,
                    )
                except Exception:
                    logger.exception(
                        "Failed to execute %s action hook for pipeline %s",
                        hook_name,
                        run.run_id,
                    )

    # ── Timeout Enforcement ────────────────────────────────────────────────────

    def _schedule_stage_timeout(
        self,
        run: PipelineRun,
        definition: PipelineDefinition,
        stage: StageDefinition,
        stage_run: StageRun,
        ctx: PipelineContext,
    ) -> None:
        """Schedule a timeout timer for a stage if ``stage.timeout`` is configured.

        When the timeout fires, the engine executes ``stage.on_timeout`` config:
            - notify: send a notification
            - label: add a label to the PR
            - then: "fail" | "escalate" | "cancel"
            - extend: reset the timer with a new duration (up to max_extensions)
        """
        timeout_secs = stage.parse_timeout_seconds()
        if timeout_secs is None or stage_run.id is None:
            return

        on_timeout = stage.on_timeout
        if isinstance(on_timeout, dict):
            timeout_cfg = GateTimeoutConfig(**on_timeout)
        elif isinstance(on_timeout, GateTimeoutConfig):
            timeout_cfg = on_timeout
        else:
            # No on_timeout config — default to fail
            timeout_cfg = GateTimeoutConfig(then="fail")

        async def _timeout_handler(
            sr_id: int,
            secs: int,
            cfg: GateTimeoutConfig,
            extension_count: int = 0,
        ) -> None:
            try:
                await asyncio.sleep(secs)
            except asyncio.CancelledError:
                return

            logger.warning(
                "Stage '%s' timed out after %ds (pipeline %s, extension=%d)",
                stage.id,
                secs,
                run.run_id,
                extension_count,
            )

            # Send timeout notification
            if cfg.notify and self._notify_callback:
                notify_msg = cfg.notify.get("message", f"Stage '{stage.id}' has timed out")
                try:
                    await self._notify_callback("pr_comment", ctx, message=notify_msg)
                except Exception:
                    logger.exception("Failed to send timeout notification")

                # Add label if specified
                timeout_label = cfg.notify.get("label")
                if timeout_label:
                    try:
                        await self._notify_callback("label", ctx, label=timeout_label)
                    except Exception:
                        logger.exception("Failed to add timeout label")

            # Handle extend
            if cfg.extend and extension_count < cfg.max_extensions:
                extend_secs = _parse_duration_seconds(cfg.extend)
                logger.info(
                    "Extending timeout for stage '%s' by %ds (extension %d/%d)",
                    stage.id,
                    extend_secs,
                    extension_count + 1,
                    cfg.max_extensions,
                )
                new_task = asyncio.create_task(
                    _timeout_handler(sr_id, extend_secs, cfg, extension_count + 1)
                )
                self._timeout_tasks[sr_id] = new_task
                return

            # Execute terminal action
            then_action = cfg.then or "fail"
            if then_action == "fail":
                # Fail the stage
                sr = await self._registry.get_stage_run(sr_id)
                if sr and sr.status == StageRunStatus.WAITING:
                    sr.status = StageRunStatus.FAILED
                    sr.error_message = f"Timed out after {secs}s"
                    sr.completed_at = datetime.now(timezone.utc)
                    await self._registry.update_stage_run(sr)
                    await self._handle_stage_error(
                        run, definition, stage, f"Timed out after {secs}s"
                    )
            elif then_action == "escalate":
                await self._escalate_pipeline(run, f"Stage '{stage.id}' timed out after {secs}s")
            elif then_action == "cancel":
                await self.cancel_pipeline(run.run_id)

            # Clean up
            self._timeout_tasks.pop(sr_id, None)

        task = asyncio.create_task(_timeout_handler(stage_run.id, timeout_secs, timeout_cfg))
        self._timeout_tasks[stage_run.id] = task
        logger.info(
            "Scheduled %ds timeout for stage '%s' (pipeline %s)",
            timeout_secs,
            stage.id,
            run.run_id,
        )

    # ── Context Builders ─────────────────────────────────────────────────────

    async def _track_pr_from_outputs(
        self,
        stage_run: StageRun,
        outputs: dict[str, Any] | None,
    ) -> None:
        """Track PR association if agent outputs contain a pr_number.

        For multi-PR pipelines, new PRs created by agents are added to the
        ``pipeline_pr_associations`` table and the run's ``context["prs"]`` list
        so subsequent stages can reference them.
        """
        if not outputs:
            return
        pr_number = outputs.get("pr_number")
        if not pr_number:
            return
        try:
            pr_number = int(pr_number)
        except (TypeError, ValueError):
            return

        run = await self._registry.get_pipeline_run(stage_run.run_id)
        if not run:
            return

        # Add association
        await self._registry.add_pr_association(
            run.run_id,
            pr_number,
            self._repo,
            stage_id=stage_run.stage_id,
            role="created",
        )

        # Update context prs list
        prs: list[int] = run.context.get("prs", [])
        if pr_number not in prs:
            prs.append(pr_number)
            run.context["prs"] = prs
            await self._registry.update_pipeline_run(run)

        logger.info(
            "Tracked PR #%d from agent outputs (pipeline %s, stage '%s')",
            pr_number,
            run.run_id,
            stage_run.stage_id,
        )

    def _build_context(self, run: PipelineRun) -> PipelineContext:
        """Build a PipelineContext from a pipeline run for gate evaluation."""
        return PipelineContext(
            pr_number=run.pr_number,
            issue_number=run.issue_number,
            owner=self._owner,
            repo=self._repo,
            pipeline_run_id=run.run_id,
            context=run.context,
            github_client=self._github_client,
        )

    @staticmethod
    def _resolve_pr_target(pr_value: int | str, run: PipelineRun) -> int | None:
        """Resolve a gate condition's ``pr`` field to an actual PR number.

        Supports:
        - Integer PR numbers directly
        - Simple context references like ``context.prs[0]``
        """
        if isinstance(pr_value, int):
            return pr_value
        # Try parsing as int string
        try:
            return int(pr_value)
        except (ValueError, TypeError):
            pass
        # Simple context.prs[N] resolution
        pr_str = str(pr_value).strip()
        if pr_str.startswith("context.prs[") and pr_str.endswith("]"):
            idx_str = pr_str[len("context.prs[") : -1]
            try:
                idx = int(idx_str)
                prs = run.context.get("prs", [])
                if 0 <= idx < len(prs):
                    return int(prs[idx])
            except (ValueError, IndexError):
                pass
        logger.warning("Cannot resolve PR target '%s' for pipeline %s", pr_value, run.run_id)
        return None

    # ── Recovery ─────────────────────────────────────────────────────────────

    async def recover_active_pipelines(self) -> int:
        """Recover pipelines that were running when the server restarted.

        Returns the number of pipelines recovered.
        """
        active = await self._registry.get_active_pipeline_runs()
        recovered = 0

        for run in active:
            if run.status != PipelineRunStatus.RUNNING:
                continue

            try:
                PipelineDefinition.model_validate_json(run.definition_snapshot)
            except Exception:
                logger.warning(
                    "Cannot recover pipeline %s — invalid definition snapshot",
                    run.run_id,
                )
                continue

            # For stages in WAITING state (agent, gate, human), they'll
            # resume via their respective callbacks (on_agent_complete,
            # reactive events, etc.)
            #
            # For stages in RUNNING state that weren't waiting on external
            # input, we may need to re-execute them. But this is tricky —
            # for now, just log and leave them.
            if run.current_stage_id:
                latest = await self._registry.get_latest_stage_run(run.run_id, run.current_stage_id)
                if latest and latest.status == StageRunStatus.RUNNING:
                    logger.warning(
                        "Pipeline %s stage '%s' was mid-execution at restart; "
                        "leaving in RUNNING state for manual recovery",
                        run.run_id,
                        run.current_stage_id,
                    )

            recovered += 1
            logger.info(
                "Recovered pipeline %s (name='%s', stage='%s')",
                run.run_id,
                run.pipeline_name,
                run.current_stage_id,
            )

        return recovered


# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_pr_number(payload: dict[str, Any]) -> int | None:
    """Extract PR number from a GitHub webhook payload."""
    pr = payload.get("pull_request")
    if pr:
        return pr.get("number")
    # Some events (e.g. pull_request_target) carry the number at top level
    return payload.get("number")


def _extract_issue_number(payload: dict[str, Any]) -> int | None:
    """Extract issue number from a GitHub webhook payload."""
    issue = payload.get("issue")
    if issue:
        return issue.get("number")
    return None


def _extract_human_action(event_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Extract the actor username and action string from a GitHub event payload.

    Returns:
        (username, action) tuple. Returns ("", "") if the event can't be parsed.
    """
    sender = (payload.get("sender") or {}).get("login", "")

    if event_type == "pull_request_review.submitted":
        review = payload.get("review", {})
        state = review.get("state", "").lower()
        actor = (review.get("user") or {}).get("login", "") or sender
        return (actor, state)  # e.g. ("octocat", "approved")

    if event_type == "issue_comment.created":
        comment = payload.get("comment", {})
        actor = (comment.get("user") or {}).get("login", "") or sender
        return (actor, "commented")

    if event_type == "pull_request.labeled":
        label = (payload.get("label") or {}).get("name", "")
        return (sender, f"labeled:{label}")

    if event_type == "pull_request_review.dismissed":
        review = payload.get("review", {})
        actor = (review.get("user") or {}).get("login", "") or sender
        return (actor, "dismissed")

    return ("", "")
