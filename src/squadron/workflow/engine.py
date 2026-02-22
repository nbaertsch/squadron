"""Workflow Engine — Orchestrates workflow execution.

The engine is responsible for:
- Evaluating triggers and starting workflows
- Executing stages sequentially
- Managing state transitions
- Handling retries and errors
- Coordinating with the agent manager for agent stages
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

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
from squadron.workflow.registry import WorkflowRegistryV2

if TYPE_CHECKING:
    from squadron.models import SquadronEvent

logger = logging.getLogger(__name__)


# ── Callback Protocols ────────────────────────────────────────────────────────


class SpawnAgentCallback(Protocol):
    """Protocol for spawning an agent."""

    async def __call__(
        self,
        role: str,
        issue_number: int,
        *,
        trigger_event: "SquadronEvent | None" = None,
        workflow_run_id: str | None = None,
        stage_id: str | None = None,
        action: str | None = None,
    ) -> str | None:
        """Spawn an agent and return its ID."""
        ...


class RunCommandCallback(Protocol):
    """Protocol for running a shell command."""

    async def __call__(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int = 300,
    ) -> tuple[int, str, str]:
        """Run command, return (exit_code, stdout, stderr)."""
        ...


# ── Workflow Engine ───────────────────────────────────────────────────────────


class WorkflowEngine:
    """Orchestrates workflow execution.

    The engine maintains a collection of workflow definitions and
    evaluates incoming events against their triggers. When a trigger
    matches, it creates a workflow run and begins executing stages.

    Stage execution is event-driven — the engine advances the workflow
    when agents complete, gates pass, or timers expire.
    """

    def __init__(
        self,
        registry: WorkflowRegistryV2,
        workflows: dict[str, WorkflowConfig] | None = None,
    ):
        self.registry = registry
        self.workflows: dict[str, WorkflowConfig] = workflows or {}

        # Callbacks (set by AgentManager)
        self._spawn_agent: SpawnAgentCallback | None = None
        self._run_command: RunCommandCallback | None = None

        # Agent registry for approval-based gate checks (AD-019 gap #2 fix)
        self._agent_registry: Any | None = None

        # Active workflow tasks (run_id -> task)
        self._workflow_tasks: dict[str, asyncio.Task] = {}

    def set_spawn_callback(self, callback: SpawnAgentCallback) -> None:
        """Register callback for spawning agents."""
        self._spawn_agent = callback

    def set_command_callback(self, callback: RunCommandCallback) -> None:
        """Register callback for running commands."""
        self._run_command = callback

    def add_workflow(self, name: str, workflow: WorkflowConfig) -> None:
        """Add a workflow definition."""
        self.workflows[name] = workflow
        logger.info("Registered workflow: %s (%d stages)", name, len(workflow.stages))

    def get_workflow(self, name: str) -> WorkflowConfig | None:
        """Get a workflow by name."""
        return self.workflows.get(name)

    # ── Trigger Evaluation ────────────────────────────────────────────────────

    async def evaluate_event(
        self,
        event_type: str,
        payload: dict,
        squadron_event: "SquadronEvent",
    ) -> WorkflowRun | None:
        """Evaluate an event against all workflow triggers.

        Args:
            event_type: GitHub event type (e.g., "issues.labeled")
            payload: Full webhook payload
            squadron_event: Internal event representation

        Returns:
            The created WorkflowRun if a workflow was triggered, None otherwise.
        """
        for name, workflow in self.workflows.items():
            if not workflow.trigger.matches(event_type, payload):
                continue

            issue_number = squadron_event.issue_number
            pr_number = squadron_event.pr_number

            # Check for existing active run (prevent duplicates)
            if issue_number:
                existing = await self.registry.get_workflow_run_by_name_and_issue(
                    name, issue_number
                )
                if existing:
                    logger.info(
                        "Workflow '%s' already active for issue #%d (run=%s)",
                        name,
                        issue_number,
                        existing.run_id,
                    )
                    return None

            # Create new workflow run
            run = await self._create_workflow_run(
                name=name,
                workflow=workflow,
                event_type=event_type,
                delivery_id=squadron_event.source_delivery_id,
                issue_number=issue_number,
                pr_number=pr_number,
                payload=payload,
            )

            logger.info(
                "WORKFLOW TRIGGERED — %s (run=%s, issue=#%s, pr=#%s)",
                name,
                run.run_id,
                issue_number,
                pr_number,
            )

            # Start workflow execution
            await self.start_workflow(run, squadron_event)
            return run

        return None

    async def _create_workflow_run(
        self,
        name: str,
        workflow: WorkflowConfig,
        event_type: str,
        delivery_id: str | None,
        issue_number: int | None,
        pr_number: int | None,
        payload: dict,
    ) -> WorkflowRun:
        """Create a new workflow run."""
        run_id = f"wf-{uuid.uuid4().hex[:12]}"

        # Build initial context from workflow definition and event
        context = dict(workflow.context)
        context["issue_number"] = issue_number
        context["pr_number"] = pr_number

        # Extract useful fields from payload
        issue_data = payload.get("issue", {})
        if issue_data:
            context["issue_title"] = issue_data.get("title", "")
            context["issue_body"] = issue_data.get("body", "")
            context["labels"] = [lbl.get("name", "") for lbl in issue_data.get("labels", [])]

        run = WorkflowRun(
            run_id=run_id,
            workflow_name=name,
            trigger_event=event_type,
            trigger_delivery_id=delivery_id,
            issue_number=issue_number,
            pr_number=pr_number,
            status=WorkflowRunStatus.PENDING,
            current_stage_id=workflow.stages[0].id,
            current_stage_index=0,
            context=context,
            created_at=datetime.now(timezone.utc),
        )

        await self.registry.create_workflow_run(run)
        return run

    # ── Workflow Execution ────────────────────────────────────────────────────

    async def start_workflow(
        self,
        run: WorkflowRun,
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Start executing a workflow from its first stage."""
        workflow = self.get_workflow(run.workflow_name)
        if not workflow:
            logger.error("Workflow '%s' not found for run %s", run.workflow_name, run.run_id)
            await self._fail_workflow(run, f"Workflow '{run.workflow_name}' not found")
            return

        # Update status to running
        run.status = WorkflowRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        await self.registry.update_workflow_run(run)

        # Execute first stage
        first_stage = workflow.stages[0]
        await self._execute_stage(run, workflow, first_stage, trigger_event)

    async def resume_workflow(
        self,
        run_id: str,
        result: str = "complete",
        outputs: dict[str, Any] | None = None,
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Resume a workflow after a stage completes.

        Called by the agent manager when an agent finishes, or by
        the gate executor when a gate passes/fails.

        Args:
            run_id: Workflow run ID
            result: Stage result ("complete", "pass", "fail", "error", "timeout")
            outputs: Outputs from the completed stage
            trigger_event: Event that triggered the resume (if any)
        """
        run = await self.registry.get_workflow_run(run_id)
        if not run:
            logger.error("Cannot resume unknown workflow run: %s", run_id)
            return

        if run.status != WorkflowRunStatus.RUNNING:
            logger.warning(
                "Cannot resume workflow %s — status is %s",
                run_id,
                run.status,
            )
            return

        workflow = self.get_workflow(run.workflow_name)
        if not workflow:
            logger.error("Workflow '%s' not found for run %s", run.workflow_name, run_id)
            await self._fail_workflow(run, f"Workflow '{run.workflow_name}' not found")
            return

        current_stage = workflow.get_stage(run.current_stage_id)
        if not current_stage:
            logger.error("Current stage '%s' not found in workflow", run.current_stage_id)
            await self._fail_workflow(run, f"Stage '{run.current_stage_id}' not found")
            return

        # Store outputs
        if outputs:
            run.outputs[current_stage.id] = outputs
            await self.registry.update_workflow_run(run)

        # Determine next action based on result
        transition = current_stage.get_next_stage(result)
        await self._handle_transition(
            run, workflow, current_stage, transition, trigger_event, result
        )

    async def _execute_stage(
        self,
        run: WorkflowRun,
        workflow: WorkflowConfig,
        stage: StageDefinition,
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Execute a single stage."""
        logger.info(
            "STAGE START — %s/%s (run=%s, type=%s)",
            run.workflow_name,
            stage.id,
            run.run_id,
            stage.type.value,
        )

        # Track iteration count
        run.iteration_counts[stage.id] = run.iteration_counts.get(stage.id, 0) + 1
        run.current_stage_id = stage.id
        run.current_stage_index = workflow.get_stage_index(stage.id) or 0
        await self.registry.update_workflow_run(run)

        # Create stage run record
        stage_run = StageRun(
            run_id=run.run_id,
            stage_id=stage.id,
            stage_index=run.current_stage_index,
            status=StageRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        stage_run_id = await self.registry.create_stage_run(stage_run)
        stage_run.id = stage_run_id

        # Execute based on stage type
        try:
            if stage.type == StageType.AGENT:
                await self._execute_agent_stage(run, workflow, stage, stage_run, trigger_event)
            elif stage.type == StageType.GATE:
                await self._execute_gate_stage(run, workflow, stage, stage_run)
            elif stage.type == StageType.DELAY:
                await self._execute_delay_stage(run, workflow, stage, stage_run)
            elif stage.type == StageType.ACTION:
                await self._execute_action_stage(run, workflow, stage, stage_run)
            else:
                raise NotImplementedError(f"Stage type '{stage.type}' not implemented")

        except Exception as e:
            logger.exception("Stage execution error: %s/%s", run.workflow_name, stage.id)
            stage_run.status = StageRunStatus.FAILED
            stage_run.error_message = str(e)
            stage_run.completed_at = datetime.now(timezone.utc)
            await self.registry.update_stage_run(stage_run)

            # Handle error transition
            transition = stage.get_next_stage("error")
            if transition:
                await self._handle_transition(run, workflow, stage, transition, trigger_event)
            else:
                await self._fail_workflow(run, str(e), stage.id)

    async def _execute_agent_stage(
        self,
        run: WorkflowRun,
        workflow: WorkflowConfig,
        stage: StageDefinition,
        stage_run: StageRun,
        trigger_event: "SquadronEvent | None" = None,
    ) -> None:
        """Execute an agent stage."""
        if not self._spawn_agent:
            raise RuntimeError("No spawn agent callback registered")

        if not stage.agent:
            raise ValueError(f"Stage '{stage.id}' has no agent defined")

        issue_number = run.issue_number
        if not issue_number:
            raise ValueError("Cannot spawn agent without issue_number")

        # Spawn the agent
        agent_id = await self._spawn_agent(
            role=stage.agent,
            issue_number=issue_number,
            trigger_event=trigger_event,
            workflow_run_id=run.run_id,
            stage_id=stage.id,
            action=stage.action,
        )

        if not agent_id:
            raise RuntimeError(f"Failed to spawn agent '{stage.agent}'")

        # Update stage run with agent ID
        stage_run.agent_id = agent_id
        await self.registry.update_stage_run(stage_run)

        logger.info(
            "AGENT SPAWNED — %s for stage %s/%s (run=%s)",
            agent_id,
            run.workflow_name,
            stage.id,
            run.run_id,
        )

        # Agent stage completes asynchronously — workflow will resume
        # when agent calls report_complete or report_blocked

    async def _execute_gate_stage(
        self,
        run: WorkflowRun,
        workflow: WorkflowConfig,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a gate stage."""
        logger.info("Evaluating gate: %s/%s", run.workflow_name, stage.id)

        all_passed = True
        failed_checks: list[str] = []

        for condition in stage.conditions:
            result = await self._evaluate_condition(condition, run, workflow)

            # Record the check result
            await self.registry.create_gate_check(stage_run.id, result)

            if not result.passed:
                all_passed = False
                failed_checks.append(f"{condition.check}: {result.error_message or 'failed'}")

        # Update stage run
        stage_run.completed_at = datetime.now(timezone.utc)

        if all_passed:
            stage_run.status = StageRunStatus.COMPLETED
            stage_run.outputs = {"passed": True}
            await self.registry.update_stage_run(stage_run)

            logger.info("GATE PASSED — %s/%s", run.workflow_name, stage.id)
            await self.resume_workflow(run.run_id, "pass", {"passed": True})
        else:
            stage_run.status = StageRunStatus.FAILED
            stage_run.outputs = {"passed": False, "failed_checks": failed_checks}
            stage_run.error_message = "; ".join(failed_checks)
            await self.registry.update_stage_run(stage_run)

            logger.info("GATE FAILED — %s/%s: %s", run.workflow_name, stage.id, failed_checks)
            await self.resume_workflow(
                run.run_id, "fail", {"passed": False, "failed_checks": failed_checks}
            )

    def set_agent_registry(self, registry: Any) -> None:
        """Register the agent registry for approval-based gate checks."""
        self._agent_registry = registry

    async def _evaluate_condition(
        self,
        condition: GateCondition,
        run: WorkflowRun,
        workflow: WorkflowConfig,
    ) -> GateCheckResult:
        """Evaluate a single gate condition.

        Supported check types:
            - ``command``     — run a shell command
            - ``file_exists`` — check file paths exist
            - ``pr_approval`` — check PR has required approvals (AD-019 gap #2 fix)
        """
        check_type = condition.check

        if check_type == "command":
            return await self._check_command(condition, run)
        elif check_type == "file_exists":
            return await self._check_file_exists(condition, run)
        elif check_type == "pr_approval":
            return await self._check_pr_approval(condition, run)
        else:
            # Delegate to pipeline gate registry for pluggable checks (AD-019)
            try:
                from squadron.pipeline.gates import GateCheckContext, default_gate_registry
                ctx = GateCheckContext(
                    params={},
                    pr_number=run.pr_number,
                    issue_number=run.issue_number,
                    run_context=run.context,
                    registry=getattr(self, "_agent_registry", None),
                    run_command=self._run_command,
                )
                # Build params from legacy condition fields
                if condition.run:
                    ctx.params["run"] = condition.run
                if condition.expect:
                    ctx.params["expect"] = condition.expect
                if condition.paths:
                    ctx.params["paths"] = condition.paths
                if condition.count is not None:
                    ctx.params["count"] = condition.count
                check_fn = default_gate_registry.get(check_type)
                if check_fn:
                    return await default_gate_registry.evaluate(check_type, ctx)
            except ImportError:
                pass
            return GateCheckResult(
                check_type=check_type,
                passed=False,
                error_message=f"Unknown check type: '{check_type}'. "
                "Available built-in checks: command, file_exists, pr_approval, "
                "pr_approvals_met, no_changes_requested, human_approved, "
                "label_present, ci_status, branch_up_to_date",
            )

    async def _check_command(
        self,
        condition: GateCondition,
        run: WorkflowRun,
    ) -> GateCheckResult:
        """Evaluate a command check."""
        if not self._run_command:
            return GateCheckResult(
                check_type="command",
                passed=False,
                error_message="No command runner registered",
            )

        if not condition.run:
            return GateCheckResult(
                check_type="command",
                passed=False,
                error_message="No command specified",
            )

        try:
            exit_code, stdout, stderr = await self._run_command(condition.run)
            passed = condition.evaluate_command_result(exit_code, stdout, stderr)

            return GateCheckResult(
                check_type="command",
                passed=passed,
                result_data={
                    "exit_code": exit_code,
                    "stdout_lines": len(stdout.split("\n")),
                    "stderr_lines": len(stderr.split("\n")),
                },
                error_message=None if passed else f"Exit code {exit_code}",
            )
        except Exception as e:
            return GateCheckResult(
                check_type="command",
                passed=False,
                error_message=str(e),
            )

    async def _check_file_exists(
        self,
        condition: GateCondition,
        run: WorkflowRun,
    ) -> GateCheckResult:
        """Check if required files exist."""
        if not condition.paths:
            return GateCheckResult(
                check_type="file_exists",
                passed=False,
                error_message="No paths specified",
            )

        missing = []
        for path in condition.paths:
            if not Path(path).exists():
                missing.append(path)

        passed = len(missing) == 0
        return GateCheckResult(
            check_type="file_exists",
            passed=passed,
            result_data={"missing": missing},
            error_message=f"Missing files: {missing}" if missing else None,
        )

    async def _check_pr_approval(
        self,
        condition: GateCondition,
        run: WorkflowRun,
    ) -> GateCheckResult:
        """Check that a PR has the required number of approvals (AD-019 gap #2 fix).

        This implements the ``pr_approval`` check type that was declared in the
        schema (``GateCondition.check``) but was never wired up.

        Args:
            condition.count: Required number of approvals (default: 1).

        Requires the agent registry to be set via ``set_agent_registry()``.
        Human approvals (recorded by the framework's ``_handle_pr_review_submitted``
        fix) are counted alongside agent approvals.
        """
        if not self._agent_registry:
            return GateCheckResult(
                check_type="pr_approval",
                passed=False,
                error_message="No agent registry registered for pr_approval check",
            )

        pr_number = run.pr_number
        if not pr_number:
            return GateCheckResult(
                check_type="pr_approval",
                passed=False,
                error_message="No PR number in workflow run context",
            )

        required_count = condition.count if condition.count is not None else 1

        try:
            approvals = await self._agent_registry.get_pr_approvals(
                pr_number, state="approved"
            )
            count = len(approvals)
            passed = count >= required_count
            return GateCheckResult(
                check_type="pr_approval",
                passed=passed,
                result_data={
                    "required": required_count,
                    "actual": count,
                    "approvers": [
                        {"role": a.get("agent_role"), "id": a.get("agent_id")}
                        for a in approvals
                    ],
                },
                error_message=(
                    None if passed
                    else f"PR #{pr_number} has {count}/{required_count} approvals"
                ),
            )
        except Exception as exc:
            return GateCheckResult(
                check_type="pr_approval",
                passed=False,
                error_message=f"Approval check error: {exc}",
            )

    async def _execute_delay_stage(
        self,
        run: WorkflowRun,
        workflow: WorkflowConfig,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a delay stage."""
        if not stage.duration:
            raise ValueError(f"Stage '{stage.id}' has no duration")

        # Parse duration
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

        logger.info("DELAY — %s/%s: waiting %ds", run.workflow_name, stage.id, seconds)

        await asyncio.sleep(seconds)

        # Complete the stage
        stage_run.status = StageRunStatus.COMPLETED
        stage_run.completed_at = datetime.now(timezone.utc)
        await self.registry.update_stage_run(stage_run)

        await self.resume_workflow(run.run_id, "complete")

    async def _execute_action_stage(
        self,
        run: WorkflowRun,
        workflow: WorkflowConfig,
        stage: StageDefinition,
        stage_run: StageRun,
    ) -> None:
        """Execute a built-in action stage."""
        action = stage.action
        if not action:
            raise ValueError(f"Stage '{stage.id}' has no action")

        logger.info("ACTION — %s/%s: %s", run.workflow_name, stage.id, action)

        # For now, just mark as complete
        # Full action implementation would integrate with GitHub client
        stage_run.status = StageRunStatus.COMPLETED
        stage_run.completed_at = datetime.now(timezone.utc)
        await self.registry.update_stage_run(stage_run)

        await self.resume_workflow(run.run_id, "complete")

    # ── Transitions ───────────────────────────────────────────────────────────

    async def _handle_transition(
        self,
        run: WorkflowRun,
        workflow: WorkflowConfig,
        current_stage: StageDefinition,
        transition: StageTransition | None,
        trigger_event: "SquadronEvent | None" = None,
        result: str = "complete",
    ) -> None:
        """Handle a stage transition."""
        if transition is None:
            # No transition defined
            if result in ("error", "fail"):
                # Error/fail with no handler → fail the workflow
                await self._fail_workflow(
                    run,
                    f"Stage '{current_stage.id}' {result}ed with no handler",
                    current_stage.id,
                )
            else:
                # Success with no transition → complete the workflow
                await self._complete_workflow(run)
            return

        # Handle delay
        if transition.delay:
            duration = transition.delay.strip().lower()
            seconds = 0
            if duration.endswith("s"):
                seconds = int(duration[:-1])
            elif duration.endswith("m"):
                seconds = int(duration[:-1]) * 60

            if seconds > 0:
                logger.info("Transition delay: %ds", seconds)
                await asyncio.sleep(seconds)

        # Determine next stage
        next_stage_id = transition.goto

        if next_stage_id == "__complete__":
            await self._complete_workflow(run)
            return

        if next_stage_id == "__escalate__":
            await self._escalate_workflow(run, "Stage escalated")
            return

        if next_stage_id == "__next__":
            next_stage_id = workflow.get_next_stage_id(current_stage.id)
            if not next_stage_id:
                await self._complete_workflow(run)
                return

        # Check iteration limit
        if transition.max_iterations:
            current_iterations = run.iteration_counts.get(next_stage_id, 0)
            if current_iterations >= transition.max_iterations:
                logger.warning(
                    "Max iterations reached for stage %s (%d/%d)",
                    next_stage_id,
                    current_iterations,
                    transition.max_iterations,
                )
                if transition.then == "escalate":
                    await self._escalate_workflow(
                        run,
                        f"Max iterations ({transition.max_iterations}) for stage {next_stage_id}",
                    )
                elif transition.then:
                    # Jump to fallback stage
                    next_stage_id = transition.then
                else:
                    await self._fail_workflow(
                        run, f"Max iterations reached for stage {next_stage_id}"
                    )
                return

        # Get next stage
        next_stage = workflow.get_stage(next_stage_id)
        if not next_stage:
            await self._fail_workflow(run, f"Stage '{next_stage_id}' not found")
            return

        # Merge transition context
        if transition.context:
            run.context.update(transition.context)
            await self.registry.update_workflow_run(run)

        # Execute next stage
        await self._execute_stage(run, workflow, next_stage, trigger_event)

    # ── Workflow Completion ───────────────────────────────────────────────────

    async def _complete_workflow(self, run: WorkflowRun) -> None:
        """Mark workflow as completed."""
        run.status = WorkflowRunStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        await self.registry.update_workflow_run(run)

        logger.info("WORKFLOW COMPLETE — %s (run=%s)", run.workflow_name, run.run_id)

    async def _fail_workflow(
        self,
        run: WorkflowRun,
        error_message: str,
        error_stage: str | None = None,
    ) -> None:
        """Mark workflow as failed."""
        run.status = WorkflowRunStatus.FAILED
        run.completed_at = datetime.now(timezone.utc)
        run.error_message = error_message
        run.error_stage = error_stage or run.current_stage_id
        await self.registry.update_workflow_run(run)

        logger.error(
            "WORKFLOW FAILED — %s (run=%s, stage=%s): %s",
            run.workflow_name,
            run.run_id,
            run.error_stage,
            error_message,
        )

    async def _escalate_workflow(self, run: WorkflowRun, reason: str) -> None:
        """Escalate workflow to human."""
        run.status = WorkflowRunStatus.ESCALATED
        run.completed_at = datetime.now(timezone.utc)
        run.error_message = reason
        await self.registry.update_workflow_run(run)

        logger.warning(
            "WORKFLOW ESCALATED — %s (run=%s): %s",
            run.workflow_name,
            run.run_id,
            reason,
        )

    # ── Agent Integration ─────────────────────────────────────────────────────

    async def on_agent_complete(
        self,
        agent_id: str,
        outputs: dict[str, Any] | None = None,
    ) -> None:
        """Called when an agent completes its task.

        This method is called by the agent manager when an agent
        calls report_complete.
        """
        # Find the stage run for this agent
        stage_run = await self.registry.get_stage_run_by_agent(agent_id)
        if not stage_run:
            logger.debug("No workflow stage found for agent %s", agent_id)
            return

        # Update stage run
        stage_run.status = StageRunStatus.COMPLETED
        stage_run.completed_at = datetime.now(timezone.utc)
        if outputs:
            stage_run.outputs = outputs
        await self.registry.update_stage_run(stage_run)

        logger.info(
            "Agent %s completed workflow stage %s (run=%s)",
            agent_id,
            stage_run.stage_id,
            stage_run.run_id,
        )

        # Resume workflow
        await self.resume_workflow(stage_run.run_id, "complete", outputs)

    async def on_agent_blocked(
        self,
        agent_id: str,
        reason: str,
    ) -> None:
        """Called when an agent reports blocked."""
        stage_run = await self.registry.get_stage_run_by_agent(agent_id)
        if not stage_run:
            return

        # Agent blocked doesn't fail the stage immediately
        # The workflow continues waiting for the agent
        logger.info(
            "Agent %s blocked on stage %s: %s",
            agent_id,
            stage_run.stage_id,
            reason,
        )

    async def on_agent_error(
        self,
        agent_id: str,
        error: str,
    ) -> None:
        """Called when an agent encounters an error."""
        stage_run = await self.registry.get_stage_run_by_agent(agent_id)
        if not stage_run:
            return

        stage_run.status = StageRunStatus.FAILED
        stage_run.completed_at = datetime.now(timezone.utc)
        stage_run.error_message = error
        await self.registry.update_stage_run(stage_run)

        logger.error(
            "Agent %s error on stage %s: %s",
            agent_id,
            stage_run.stage_id,
            error,
        )

        # Resume workflow with error
        await self.resume_workflow(stage_run.run_id, "error", {"error": error})
