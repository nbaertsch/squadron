"""Pipeline Engine — reactive orchestration for the unified pipeline system.

The engine is the runtime counterpart to ``PipelineConfig``.  It:

1. **Evaluates triggers** — incoming events are matched against pipeline
   definitions; matching pipelines are instantiated as ``PipelineRun``s.

2. **Executes stages** — agent stages, gate stages, delay stages, and
   built-in action stages.

3. **Reacts to events** — running pipelines subscribe to events and
   re-evaluate waiting gate stages when relevant events arrive.  This
   resolves the "Workflow engine can't react to PR lifecycle events" gap.

4. **Tracks human reviews** — calls the registry to record human PR
   reviews alongside agent reviews (resolves gap #1: human PR reviews
   not tracked).

5. **Evaluates ``pr_approval`` gates** — uses the pluggable
   ``GateCheckRegistry`` with built-in ``pr_approvals_met`` and
   ``human_approved`` checks (resolves gap #2).

Callbacks (registered by ``AgentManager``):
- ``_spawn_agent`` — create or wake an agent
- ``_run_command`` — run a shell command for ``command`` gate checks

Event routing:
- ``AgentManager`` registers ``on_event()`` with the ``EventRouter`` for
  all event types that any active pipeline subscribes to.
- When an event arrives, ``on_event()`` is called; the engine fetches all
  waiting runs subscribed to that event type and re-evaluates their gates.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from squadron.pipeline.gates import GateCheckContext, GateCheckRegistry, default_gate_registry
from squadron.pipeline.registry import (
    PipelineRegistry,
    PipelineRun,
    PipelineRunStatus,
    PipelineStageRun,
    PipelineStageStatus,
)

if TYPE_CHECKING:
    from squadron.config import PipelineConfig, PipelineStageConfig
    from squadron.models import SquadronEvent

logger = logging.getLogger(__name__)


# ── Callback Protocols ────────────────────────────────────────────────────────


class SpawnAgentCallback(Protocol):
    """Protocol for spawning or waking an agent."""

    async def __call__(
        self,
        role: str,
        issue_number: int,
        *,
        trigger_event: "SquadronEvent | None" = None,
        pipeline_run_id: str | None = None,
        stage_id: str | None = None,
        action: str | None = None,
    ) -> str | None:
        """Spawn or wake an agent; return its ID."""
        ...


class RunCommandCallback(Protocol):
    """Protocol for running a shell command."""

    async def __call__(
        self, command: str, *, cwd: str | None = None, timeout: int = 300
    ) -> tuple[int, str, str]:
        """Run ``command``; return ``(exit_code, stdout, stderr)``."""
        ...


# ── Pipeline Engine ───────────────────────────────────────────────────────────


class PipelineEngine:
    """Reactive multi-stage pipeline orchestrator.

    Instantiate with a ``PipelineRegistry`` and optionally a custom
    ``GateCheckRegistry``.  Attach to ``AgentManager`` via
    ``set_spawn_callback`` and ``set_command_callback``.
    """

    def __init__(
        self,
        registry: PipelineRegistry,
        pipelines: dict[str, "PipelineConfig"] | None = None,
        gate_registry: GateCheckRegistry | None = None,
        owner: str = "",
        repo: str = "",
    ) -> None:
        self.registry = registry
        self.pipelines: dict[str, "PipelineConfig"] = pipelines or {}
        self.gate_registry: GateCheckRegistry = gate_registry or default_gate_registry
        self.owner = owner
        self.repo = repo

        # Runtime callbacks
        self._spawn_agent: SpawnAgentCallback | None = None
        self._run_command: RunCommandCallback | None = None
        self._agent_registry: Any = None  # AgentRegistry for approval lookups
        self._github: Any = None  # GitHubClient for API calls

        # Active pipeline run tasks
        self._pipeline_tasks: dict[str, asyncio.Task] = {}

    # ── Configuration ──────────────────────────────────────────────────────────

    def set_spawn_callback(self, callback: SpawnAgentCallback) -> None:
        """Register the agent spawn/wake callback."""
        self._spawn_agent = callback

    def set_command_callback(self, callback: RunCommandCallback) -> None:
        """Register the shell command runner callback."""
        self._run_command = callback

    def set_agent_registry(self, registry: Any) -> None:
        """Register the agent registry for approval gate lookups."""
        self._agent_registry = registry

    def set_github_client(self, github: Any) -> None:
        """Register the GitHub client for API-based gate checks."""
        self._github = github

    def add_pipeline(self, name: str, pipeline: "PipelineConfig") -> None:
        """Add or replace a pipeline definition."""
        self.pipelines[name] = pipeline
        # Load any gate plugins defined on the pipeline
        for plugin_module in pipeline.gate_plugins:
            try:
                self.gate_registry.load_plugin(plugin_module)
            except Exception:
                logger.exception("Failed to load gate plugin: %s", plugin_module)
        logger.info(
            "Registered pipeline: %s (%d stages)", name, len(pipeline.stages)
        )

    def get_pipeline(self, name: str) -> "PipelineConfig | None":
        """Retrieve a pipeline definition by name."""
        return self.pipelines.get(name)

    # ── Trigger Evaluation ─────────────────────────────────────────────────────

    async def evaluate_event(
        self,
        event_type: str,
        payload: dict,
        squadron_event: "SquadronEvent",
    ) -> PipelineRun | None:
        """Evaluate an event against all pipeline triggers.

        Returns the created ``PipelineRun`` if a pipeline was triggered, or
        ``None`` if no pipeline matched.
        """
        for name, pipeline in self.pipelines.items():
            if not pipeline.trigger.matches(event_type, payload):
                continue

            issue_number = squadron_event.issue_number
            pr_number = squadron_event.pr_number

            # Prevent duplicate runs for the same pipeline + issue
            if issue_number:
                existing = await self.registry.get_run_by_name_and_issue(
                    name, issue_number
                )
                if existing:
                    logger.info(
                        "Pipeline '%s' already active for issue #%d (run=%s)",
                        name, issue_number, existing.run_id,
                    )
                    continue

            run = await self._create_run(
                name=name,
                pipeline=pipeline,
                event_type=event_type,
                delivery_id=squadron_event.source_delivery_id,
                issue_number=issue_number,
                pr_number=pr_number,
                payload=payload,
            )

            logger.info(
                "PIPELINE TRIGGERED — %s (run=%s, issue=#%s, pr=#%s)",
                name, run.run_id, issue_number, pr_number,
            )

            await self.start_pipeline(run, squadron_event)
            return run

        return None

    async def on_event(
        self, event_type: str, squadron_event: "SquadronEvent"
    ) -> None:
        """Deliver an event to all waiting pipelines subscribed to it.

        Called by ``AgentManager`` for every event that has been dispatched.
        Pipelines in WAITING status with a matching ``subscribed_events``
        entry will have their current gate stage re-evaluated.
        """
        subscribed_runs = await self.registry.get_runs_subscribed_to(event_type)
        if not subscribed_runs:
            return

        logger.debug(
            "Event '%s' dispatched to %d waiting pipeline(s)",
            event_type, len(subscribed_runs),
        )

        for run in subscribed_runs:
            pipeline = self.get_pipeline(run.pipeline_name)
            if not pipeline:
                continue

            current_stage = pipeline.get_stage(run.current_stage_id or "")
            if not current_stage:
                continue

            # Only re-evaluate gate stages
            from squadron.config import StageType

            if current_stage.type != StageType.GATE and str(current_stage.type) != "gate":
                continue

            # Check if this stage is subscribed to this event
            stage_events = {sub.event for sub in current_stage.event_subscriptions}
            pipeline_events = {sub.event for sub in pipeline.event_subscriptions}
            all_subscribed = stage_events | pipeline_events

            if event_type not in all_subscribed:
                continue

            logger.info(
                "Re-evaluating gate '%s' for pipeline '%s' run %s (event=%s)",
                current_stage.id, run.pipeline_name, run.run_id, event_type,
            )

            # Re-evaluate the gate
            task_key = f"{run.run_id}:gate-reeval"
            if task_key not in self._pipeline_tasks or self._pipeline_tasks[task_key].done():
                task = asyncio.create_task(
                    self._reevaluate_gate(run, pipeline, current_stage, squadron_event),
                    name=f"gate-reeval-{run.run_id}",
                )
                self._pipeline_tasks[task_key] = task

    # ── Run Creation ───────────────────────────────────────────────────────────

    async def _create_run(
        self,
        name: str,
        pipeline: "PipelineConfig",
        event_type: str,
        delivery_id: str | None,
        issue_number: int | None,
        pr_number: int | None,
        payload: dict,
    ) -> PipelineRun:
        run_id = PipelineRegistry.new_run_id()

        # Build initial context
        context: dict[str, Any] = dict(pipeline.context)
        context["issue_number"] = issue_number
        context["pr_number"] = pr_number

        issue_data = payload.get("issue", {})
        if issue_data:
            context["issue_title"] = issue_data.get("title", "")
            context["labels"] = [
                lbl.get("name", "") for lbl in issue_data.get("labels", [])
            ]

        # Determine subscribed events from the pipeline definition
        subscribed = pipeline.collect_subscribed_events()

        run = PipelineRun(
            run_id=run_id,
            pipeline_name=name,
            trigger_event=event_type,
            trigger_delivery_id=delivery_id,
            issue_number=issue_number,
            pr_number=pr_number,
            status=PipelineRunStatus.PENDING,
            current_stage_id=pipeline.stages[0].id if pipeline.stages else None,
            current_stage_index=0,
            subscribed_events=subscribed,
            context=context,
        )

        await self.registry.create_run(run)
        return run

    # ── Pipeline Execution ─────────────────────────────────────────────────────

    async def start_pipeline(
        self,
        run: PipelineRun,
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Begin executing a pipeline from its first stage."""
        pipeline = self.get_pipeline(run.pipeline_name)
        if not pipeline:
            await self._fail_run(run, f"Pipeline '{run.pipeline_name}' not found")
            return

        if not pipeline.stages:
            await self._complete_run(run)
            return

        run.status = PipelineRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        await self.registry.update_run(run)

        await self._execute_stage(run, pipeline, pipeline.stages[0], trigger_event)

    async def resume_pipeline(
        self,
        run_id: str,
        result: str = "complete",
        outputs: dict[str, Any] | None = None,
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Resume a pipeline after the current stage completes.

        Called by ``on_agent_complete`` or after a gate passes/fails.

        Args:
            run_id: Pipeline run ID.
            result: Stage outcome — ``"complete"``, ``"pass"``, ``"fail"``,
                    ``"error"``, or ``"timeout"``.
            outputs: Key/value outputs from the completed stage.
            trigger_event: Event that triggered the resume (optional).
        """
        run = await self.registry.get_run(run_id)
        if not run:
            logger.error("Cannot resume unknown pipeline run: %s", run_id)
            return

        if run.status not in (PipelineRunStatus.RUNNING, PipelineRunStatus.WAITING):
            logger.warning(
                "Cannot resume pipeline %s — status is %s", run_id, run.status
            )
            return

        pipeline = self.get_pipeline(run.pipeline_name)
        if not pipeline:
            await self._fail_run(run, f"Pipeline '{run.pipeline_name}' not found")
            return

        current_stage = pipeline.get_stage(run.current_stage_id or "")
        if not current_stage:
            await self._fail_run(
                run, f"Stage '{run.current_stage_id}' not found in pipeline"
            )
            return

        # Persist stage outputs
        if outputs:
            run.outputs[current_stage.id] = outputs
            await self.registry.update_run(run)

        await self._handle_transition(
            run, pipeline, current_stage, result, trigger_event
        )

    async def _execute_stage(
        self,
        run: PipelineRun,
        pipeline: "PipelineConfig",
        stage: "PipelineStageConfig",
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Dispatch a single stage for execution."""
        from squadron.config import StageType

        logger.info(
            "PIPELINE STAGE START — %s/%s (run=%s, type=%s)",
            run.pipeline_name, stage.id, run.run_id, stage.type,
        )

        # Update iteration count
        run.iteration_counts[stage.id] = run.iteration_counts.get(stage.id, 0) + 1
        run.current_stage_id = stage.id
        run.current_stage_index = pipeline.get_stage_index(stage.id) or 0
        run.status = PipelineRunStatus.RUNNING
        await self.registry.update_run(run)

        # Create stage run record
        stage_run = PipelineStageRun(
            run_id=run.run_id,
            stage_id=stage.id,
            stage_index=run.current_stage_index,
            status=PipelineStageStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        stage_run_id = await self.registry.create_stage_run(stage_run)
        stage_run.id = stage_run_id

        stage_type = stage.type
        # Normalise string vs enum
        if isinstance(stage_type, str):
            stage_type = StageType(stage_type)

        try:
            if stage_type == StageType.AGENT:
                await self._execute_agent_stage(run, stage, stage_run, trigger_event)
            elif stage_type == StageType.GATE:
                await self._execute_gate_stage(run, pipeline, stage, stage_run)
            elif stage_type == StageType.DELAY:
                await self._execute_delay_stage(run, pipeline, stage, stage_run)
            elif stage_type == StageType.ACTION:
                await self._execute_action_stage(run, pipeline, stage, stage_run)
            else:
                raise NotImplementedError(f"Stage type '{stage_type}' not implemented")

        except Exception as exc:
            logger.exception(
                "Pipeline stage error: %s/%s", run.pipeline_name, stage.id
            )
            stage_run.status = PipelineStageStatus.FAILED
            stage_run.error_message = str(exc)
            stage_run.completed_at = datetime.now(timezone.utc)
            await self.registry.update_stage_run(stage_run)

            if stage.on_error:
                next_id = stage.on_error
                next_stage = pipeline.get_stage(next_id)
                if next_stage:
                    await self._execute_stage(run, pipeline, next_stage, trigger_event)
                    return
            await self._fail_run(run, str(exc), stage.id)

    async def _execute_agent_stage(
        self,
        run: PipelineRun,
        stage: "PipelineStageConfig",
        stage_run: PipelineStageRun,
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        if not self._spawn_agent:
            raise RuntimeError("No spawn agent callback registered")
        if not stage.agent:
            raise ValueError(f"Stage '{stage.id}' has no agent defined")
        if not run.issue_number:
            raise ValueError("Cannot spawn agent without issue_number")

        agent_id = await self._spawn_agent(
            role=stage.agent,
            issue_number=run.issue_number,
            trigger_event=trigger_event,
            pipeline_run_id=run.run_id,
            stage_id=stage.id,
            action=stage.action,
        )
        if not agent_id:
            raise RuntimeError(f"Failed to spawn agent '{stage.agent}'")

        stage_run.agent_id = agent_id
        await self.registry.update_stage_run(stage_run)

        logger.info(
            "AGENT SPAWNED — %s for pipeline stage %s/%s (run=%s)",
            agent_id, run.pipeline_name, stage.id, run.run_id,
        )
        # Stage completes asynchronously via on_agent_complete

    async def _execute_gate_stage(
        self,
        run: PipelineRun,
        pipeline: "PipelineConfig",
        stage: "PipelineStageConfig",
        stage_run: PipelineStageRun,
    ) -> None:
        """Evaluate all gate checks for a gate stage."""
        logger.info("Evaluating gate: %s/%s", run.pipeline_name, stage.id)

        passed, failed_checks = await self._evaluate_gate_checks(run, stage, stage_run)

        stage_run.completed_at = datetime.now(timezone.utc)

        if passed:
            stage_run.status = PipelineStageStatus.COMPLETED
            stage_run.outputs = {"passed": True}
            await self.registry.update_stage_run(stage_run)
            logger.info("GATE PASSED — %s/%s", run.pipeline_name, stage.id)
            await self.resume_pipeline(run.run_id, "pass", {"passed": True})
        else:
            # If there are event subscriptions, put the run in WAITING status
            # rather than immediately transitioning on fail.  The run will
            # re-evaluate when a subscribed event arrives.
            all_subs = (
                {s.event for s in stage.event_subscriptions}
                | {s.event for s in pipeline.event_subscriptions}
            )
            if all_subs and stage.on_fail == stage.id:
                # Gate is self-referential and reactive — wait for an event
                stage_run.status = PipelineStageStatus.WAITING
                await self.registry.update_stage_run(stage_run)
                run.status = PipelineRunStatus.WAITING
                await self.registry.update_run(run)
                logger.info(
                    "GATE WAITING — %s/%s (subscribed to %s)",
                    run.pipeline_name, stage.id, sorted(all_subs),
                )
            else:
                stage_run.status = PipelineStageStatus.FAILED
                stage_run.outputs = {
                    "passed": False, "failed_checks": failed_checks
                }
                stage_run.error_message = "; ".join(failed_checks)
                await self.registry.update_stage_run(stage_run)
                logger.info(
                    "GATE FAILED — %s/%s: %s",
                    run.pipeline_name, stage.id, failed_checks,
                )
                await self.resume_pipeline(
                    run.run_id, "fail",
                    {"passed": False, "failed_checks": failed_checks},
                )

    async def _reevaluate_gate(
        self,
        run: PipelineRun,
        pipeline: "PipelineConfig",
        stage: "PipelineStageConfig",
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Re-evaluate a waiting gate stage after a subscribed event arrives."""
        # Fetch the most recent stage run for this stage
        stage_run = await self.registry.get_latest_stage_run(run.run_id, stage.id)
        if not stage_run:
            logger.warning(
                "No stage run found for gate re-evaluation: %s/%s",
                run.run_id, stage.id,
            )
            return

        # Create a new evaluation attempt
        new_stage_run = PipelineStageRun(
            run_id=run.run_id,
            stage_id=stage.id,
            stage_index=run.current_stage_index,
            status=PipelineStageStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            attempt_number=(stage_run.attempt_number or 1) + 1,
        )
        new_stage_run.id = await self.registry.create_stage_run(new_stage_run)

        passed, failed_checks = await self._evaluate_gate_checks(
            run, stage, new_stage_run
        )

        new_stage_run.completed_at = datetime.now(timezone.utc)

        if passed:
            new_stage_run.status = PipelineStageStatus.COMPLETED
            new_stage_run.outputs = {"passed": True}
            await self.registry.update_stage_run(new_stage_run)

            run.status = PipelineRunStatus.RUNNING
            await self.registry.update_run(run)

            logger.info(
                "GATE PASSED (re-eval) — %s/%s", run.pipeline_name, stage.id
            )
            await self.resume_pipeline(run.run_id, "pass", {"passed": True})
        else:
            new_stage_run.status = PipelineStageStatus.FAILED
            new_stage_run.outputs = {
                "passed": False, "failed_checks": failed_checks
            }
            await self.registry.update_stage_run(new_stage_run)
            logger.debug(
                "GATE STILL FAILING — %s/%s (waiting for next event)",
                run.pipeline_name, stage.id,
            )
            # Remain in WAITING — will be re-evaluated on next event

    async def _evaluate_gate_checks(
        self,
        run: PipelineRun,
        stage: "PipelineStageConfig",
        stage_run: PipelineStageRun,
    ) -> tuple[bool, list[str]]:
        """Evaluate all gate check conditions for a stage.

        Returns ``(all_passed, list_of_failure_messages)``.
        """
        all_passed = True
        failed_checks: list[str] = []

        # Support both new-style gate_checks and legacy conditions
        checks_to_run = list(stage.gate_checks)

        # Also handle legacy GateCondition-style conditions
        if stage.conditions and not checks_to_run:
            # Convert legacy conditions to PipelineGateCheck format
            from squadron.config import PipelineGateCheck
            for cond in stage.conditions:
                if isinstance(cond, dict):
                    check_type = cond.get("check", "")
                    params: dict = {}
                    if "run" in cond:
                        params["run"] = cond["run"]
                    if "expect" in cond:
                        params["expect"] = cond["expect"]
                    if "paths" in cond:
                        params["paths"] = cond["paths"]
                    if "count" in cond:
                        params["count"] = cond["count"]
                    checks_to_run.append(PipelineGateCheck(check=check_type, params=params))

        # Build gate context
        ctx = GateCheckContext(
            pr_number=run.pr_number,
            issue_number=run.issue_number,
            owner=self.owner,
            repo=self.repo,
            run_context=run.context,
            registry=self._agent_registry,
            github=self._github,
            run_command=self._run_command,
        )

        for gate_check in checks_to_run:
            ctx.params = gate_check.params
            result = await self.gate_registry.evaluate(gate_check.check, ctx)

            # Persist check result
            if stage_run.id is not None:
                await self.registry.create_gate_check(
                    stage_run_id=stage_run.id,
                    check_type=result.check_type,
                    passed=result.passed,
                    result_data=result.result_data,
                    error_message=result.error_message,
                )

            if not result.passed:
                all_passed = False
                failed_checks.append(
                    f"{result.check_type}: {result.error_message or 'failed'}"
                )

        return all_passed, failed_checks

    async def _execute_delay_stage(
        self,
        run: PipelineRun,
        pipeline: "PipelineConfig",
        stage: "PipelineStageConfig",
        stage_run: PipelineStageRun,
    ) -> None:
        if not stage.duration:
            raise ValueError(f"Delay stage '{stage.id}' has no duration")

        duration = stage.duration.strip().lower()
        seconds = 0
        if duration.endswith("s"):
            seconds = int(duration[:-1])
        elif duration.endswith("m"):
            seconds = int(duration[:-1]) * 60
        elif duration.endswith("h"):
            seconds = int(duration[:-1]) * 3600
        else:
            seconds = int(duration)

        logger.info(
            "DELAY — %s/%s: waiting %ds", run.pipeline_name, stage.id, seconds
        )
        await asyncio.sleep(seconds)

        stage_run.status = PipelineStageStatus.COMPLETED
        stage_run.completed_at = datetime.now(timezone.utc)
        await self.registry.update_stage_run(stage_run)
        await self.resume_pipeline(run.run_id, "complete")

    async def _execute_action_stage(
        self,
        run: PipelineRun,
        pipeline: "PipelineConfig",
        stage: "PipelineStageConfig",
        stage_run: PipelineStageRun,
    ) -> None:
        action = stage.action
        if not action:
            raise ValueError(f"Action stage '{stage.id}' has no action")

        logger.info(
            "ACTION — %s/%s: %s", run.pipeline_name, stage.id, action
        )

        # Built-in actions
        result_outputs: dict[str, Any] = {"action": action}

        if action == "merge_pr" and run.pr_number and self._github:
            try:
                await self._github.merge_pull_request(
                    self.owner, self.repo, run.pr_number
                )
                result_outputs["merged"] = True
                logger.info("Auto-merged PR #%d via pipeline", run.pr_number)
            except Exception as exc:
                result_outputs["merged"] = False
                result_outputs["error"] = str(exc)
                logger.error("Failed to merge PR #%d: %s", run.pr_number, exc)

        stage_run.status = PipelineStageStatus.COMPLETED
        stage_run.outputs = result_outputs
        stage_run.completed_at = datetime.now(timezone.utc)
        await self.registry.update_stage_run(stage_run)
        await self.resume_pipeline(run.run_id, "complete", result_outputs)

    # ── Transitions ────────────────────────────────────────────────────────────

    async def _handle_transition(
        self,
        run: PipelineRun,
        pipeline: "PipelineConfig",
        current_stage: "PipelineStageConfig",
        result: str,
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Determine and execute the next pipeline step after a stage completes."""
        # Determine the ``on_*`` transition target
        next_stage_id: str | None = None

        if result in ("complete",):
            next_stage_id = current_stage.on_complete
        elif result == "pass":
            next_stage_id = current_stage.on_pass or current_stage.on_complete
        elif result == "fail":
            next_stage_id = current_stage.on_fail
        elif result == "error":
            next_stage_id = current_stage.on_error

        # Resolve special tokens
        if next_stage_id == "complete" or next_stage_id == "__complete__":
            await self._complete_run(run)
            return
        if next_stage_id == "escalate" or next_stage_id == "__escalate__":
            await self._escalate_run(run, f"Stage '{current_stage.id}' escalated")
            return
        if next_stage_id is None:
            if result == "error":
                # Error with no explicit handler → fail the pipeline
                await self._fail_run(
                    run,
                    f"Stage '{current_stage.id}' errored with no on_error handler",
                    current_stage.id,
                )
                return
            if result == "fail":
                # Gate fail with no on_fail handler → fail the pipeline
                await self._fail_run(
                    run,
                    f"Gate '{current_stage.id}' failed with no on_fail handler",
                    current_stage.id,
                )
                return
            # No explicit transition — advance to next stage in sequence
            next_stage_id = pipeline.get_next_stage_id(current_stage.id)
            if next_stage_id is None:
                # End of pipeline
                await self._complete_run(run)
                return

        # Check iteration limit
        if current_stage.max_iterations is not None:
            iterations = run.iteration_counts.get(next_stage_id, 0)
            if iterations >= current_stage.max_iterations:
                logger.warning(
                    "Max iterations reached for stage %s in pipeline %s (%d/%d)",
                    next_stage_id, run.pipeline_name, iterations,
                    current_stage.max_iterations,
                )
                await self._escalate_run(
                    run,
                    f"Max iterations ({current_stage.max_iterations}) "
                    f"reached for stage '{next_stage_id}'",
                )
                return

        next_stage = pipeline.get_stage(next_stage_id)
        if not next_stage:
            await self._fail_run(
                run, f"Stage '{next_stage_id}' not found in pipeline"
            )
            return

        await self._execute_stage(run, pipeline, next_stage, trigger_event)

    # ── Run Termination ────────────────────────────────────────────────────────

    async def _complete_run(self, run: PipelineRun) -> None:
        run.status = PipelineRunStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        await self.registry.update_run(run)
        logger.info(
            "PIPELINE COMPLETE — %s (run=%s)", run.pipeline_name, run.run_id
        )

    async def _fail_run(
        self, run: PipelineRun, error: str, error_stage: str | None = None
    ) -> None:
        run.status = PipelineRunStatus.FAILED
        run.completed_at = datetime.now(timezone.utc)
        run.error_message = error
        run.error_stage = error_stage or run.current_stage_id
        await self.registry.update_run(run)
        logger.error(
            "PIPELINE FAILED — %s (run=%s, stage=%s): %s",
            run.pipeline_name, run.run_id, run.error_stage, error,
        )

    async def _escalate_run(self, run: PipelineRun, reason: str) -> None:
        run.status = PipelineRunStatus.ESCALATED
        run.completed_at = datetime.now(timezone.utc)
        run.error_message = reason
        await self.registry.update_run(run)
        logger.warning(
            "PIPELINE ESCALATED — %s (run=%s): %s",
            run.pipeline_name, run.run_id, reason,
        )

    # ── Agent Lifecycle Hooks ──────────────────────────────────────────────────

    async def on_agent_complete(
        self, agent_id: str, outputs: dict[str, Any] | None = None
    ) -> None:
        """Called when an agent completes its task.

        Finds the pipeline stage run associated with ``agent_id`` and
        resumes the pipeline.
        """
        stage_run = await self.registry.get_stage_run_by_agent(agent_id)
        if not stage_run:
            logger.debug("No pipeline stage run found for agent %s", agent_id)
            return

        stage_run.status = PipelineStageStatus.COMPLETED
        stage_run.completed_at = datetime.now(timezone.utc)
        if outputs:
            stage_run.outputs = outputs
        await self.registry.update_stage_run(stage_run)

        logger.info(
            "Agent %s completed pipeline stage %s (run=%s)",
            agent_id, stage_run.stage_id, stage_run.run_id,
        )
        await self.resume_pipeline(stage_run.run_id, "complete", outputs)

    async def on_agent_blocked(self, agent_id: str, reason: str) -> None:
        """Called when an agent reports blocked — pipeline continues waiting."""
        stage_run = await self.registry.get_stage_run_by_agent(agent_id)
        if stage_run:
            logger.info(
                "Agent %s blocked on pipeline stage %s: %s",
                agent_id, stage_run.stage_id, reason,
            )

    async def on_agent_error(self, agent_id: str, error: str) -> None:
        """Called when an agent encounters an error — pipeline stage fails."""
        stage_run = await self.registry.get_stage_run_by_agent(agent_id)
        if not stage_run:
            return

        stage_run.status = PipelineStageStatus.FAILED
        stage_run.completed_at = datetime.now(timezone.utc)
        stage_run.error_message = error
        await self.registry.update_stage_run(stage_run)

        logger.error(
            "Agent %s error on pipeline stage %s: %s",
            agent_id, stage_run.stage_id, error,
        )
        await self.resume_pipeline(stage_run.run_id, "error", {"error": error})
