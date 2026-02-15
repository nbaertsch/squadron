"""Workflow Engine — executes sequential agent pipelines.

Matches incoming GitHub events against workflow triggers defined in
``.squadron/workflows/*.yaml`` and orchestrates multi-stage agent
pipelines where each stage's completion (e.g. PR approval) triggers
the next stage.

Example pipeline: PR opened → test-coverage review → security review
→ final review & merge.

Design:
- Each workflow run is tracked in the ``workflow_runs`` SQLite table.
- Stage transitions are event-driven: a PR review approval from the
  current stage's agent advances the pipeline to the next stage.
- The engine is stateless between events — all state lives in the DB.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Protocol

from squadron.models import SquadronEvent

if TYPE_CHECKING:
    from squadron.config import SquadronConfig, WorkflowDefinition
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Matches events to workflow triggers and advances stage pipelines.

    Lifecycle:
    1. ``evaluate_event()`` — called by the event router for every event.
       Checks all workflow definitions for a trigger match.  If matched,
       creates a workflow run and spawns the first stage's agent.
    2. ``handle_stage_completed()`` — called when an agent belonging to an
       active workflow run completes its action (e.g. PR approval).
       Advances to the next stage or completes the workflow.
    """

    def __init__(
        self,
        config: SquadronConfig,
        registry: AgentRegistry,
        workflows: list[WorkflowDefinition],
    ):
        self.config = config
        self.registry = registry
        self.workflows = workflows

        # Callback set by AgentManager to spawn agents for stages
        self._spawn_review_agent: SpawnReviewCallback | None = None

    def set_spawn_callback(self, callback: SpawnReviewCallback) -> None:
        """Register a callback for spawning review agents.

        The callback signature is::

            async def spawn(role: str, pr_number: int, event: SquadronEvent) -> str | None

        Returns the ``agent_id`` of the spawned agent, or ``None`` on failure.
        """
        self._spawn_review_agent = callback

    # ── Event Evaluation ─────────────────────────────────────────────────

    async def evaluate_event(
        self,
        event_type: str,
        payload: dict,
        squadron_event: SquadronEvent,
    ) -> bool:
        """Check if any workflow should activate for this event.

        Args:
            event_type: GitHub event full type (e.g. "pull_request.opened").
            payload: Full webhook payload dict.
            squadron_event: The internal SquadronEvent for callbacks.

        Returns:
            True if a workflow was triggered.
        """
        for workflow in self.workflows:
            if not workflow.matches(event_type, payload):
                continue

            pr_number = squadron_event.pr_number
            issue_number = squadron_event.issue_number

            # Prevent duplicate runs for the same PR + workflow
            if pr_number is not None:
                existing = await self.registry.get_workflow_runs_for_pr(pr_number)
                if any(r["workflow_name"] == workflow.name for r in existing):
                    logger.info(
                        "Workflow '%s' already active for PR #%d — skipping",
                        workflow.name,
                        pr_number,
                    )
                    return False

            # Create the workflow run
            run_id = f"wf-{uuid.uuid4().hex[:12]}"
            first_stage = workflow.stages[0]

            logger.info(
                "WORKFLOW TRIGGERED — %s (run=%s, pr=#%s, first_stage=%s)",
                workflow.name,
                run_id,
                pr_number,
                first_stage.name,
            )

            await self.registry.create_workflow_run(
                run_id=run_id,
                workflow_name=workflow.name,
                current_stage=first_stage.name,
                pr_number=pr_number,
                issue_number=issue_number,
                stage_index=0,
            )

            # Spawn the first stage's agent
            agent_id = await self._spawn_stage_agent(
                run_id=run_id,
                stage=first_stage,
                pr_number=pr_number,
                event=squadron_event,
            )

            if agent_id:
                await self.registry.advance_workflow_run(
                    run_id=run_id,
                    next_stage=first_stage.name,
                    stage_index=0,
                    stage_agent_id=agent_id,
                )

            return True

        return False

    # ── Stage Advancement ────────────────────────────────────────────────

    async def handle_pr_review(
        self,
        pr_number: int,
        reviewer: str,
        review_state: str,
        payload: dict,
        squadron_event: SquadronEvent,
    ) -> bool:
        """Handle a PR review event — advance workflow if conditions are met.

        Called by the event router / agent manager when a
        ``pull_request_review.submitted`` event arrives.

        Args:
            pr_number: The PR that was reviewed.
            reviewer: GitHub login of the reviewer.
            review_state: Review state ("approved", "changes_requested", "commented").
            payload: Full webhook payload.
            squadron_event: The internal SquadronEvent.

        Returns:
            True if a workflow stage was advanced.
        """
        runs = await self.registry.get_workflow_runs_for_pr(pr_number)
        if not runs:
            return False

        for run in runs:
            workflow = self._find_workflow(run["workflow_name"])
            if not workflow:
                logger.warning(
                    "Workflow '%s' not found for run %s", run["workflow_name"], run["run_id"]
                )
                continue

            current_stage_idx = run["stage_index"]
            if current_stage_idx >= len(workflow.stages):
                continue

            current_stage = workflow.stages[current_stage_idx]

            # Verify the reviewer is the agent assigned to this stage
            stage_agent_id = run["stage_agent_id"]
            if stage_agent_id:
                # Check if the reviewer matches the bot username for this agent
                agent_record = await self.registry.get_agent(stage_agent_id)
                if agent_record:
                    # Bot reviews come from the App's bot user, so we check
                    # that the review is from our bot OR from the agent role
                    expected_bot = self.config.project.bot_username
                    if reviewer != expected_bot and reviewer != expected_bot.rstrip("[bot]"):
                        logger.debug(
                            "Review on PR #%d from %s — not from workflow agent (%s)",
                            pr_number,
                            reviewer,
                            expected_bot,
                        )
                        continue

            if review_state == "approved":
                return await self._handle_approval(
                    run=run,
                    workflow=workflow,
                    current_stage=current_stage,
                    current_stage_idx=current_stage_idx,
                    squadron_event=squadron_event,
                )

            elif review_state == "changes_requested":
                return await self._handle_rejection(
                    run=run,
                    workflow=workflow,
                    current_stage=current_stage,
                )

        return False

    async def _handle_approval(
        self,
        run: dict,
        workflow: WorkflowDefinition,
        current_stage,  # WorkflowStage
        current_stage_idx: int,
        squadron_event: SquadronEvent,
    ) -> bool:
        """Handle stage approval — advance to next stage or complete."""
        run_id = run["run_id"]
        pr_number = run["pr_number"]
        on_approve = current_stage.on_approve

        if on_approve == "complete":
            await self.registry.complete_workflow_run(run_id, "completed")
            logger.info(
                "WORKFLOW COMPLETE — %s (run=%s, stage=%s)",
                workflow.name,
                run_id,
                current_stage.name,
            )
            return True

        # Determine next stage
        if on_approve == "next":
            next_idx = current_stage_idx + 1
        else:
            # Named stage jump
            next_idx = self._find_stage_index(workflow, on_approve)
            if next_idx is None:
                logger.error(
                    "Workflow %s stage %s has on_approve='%s' but that stage doesn't exist",
                    workflow.name,
                    current_stage.name,
                    on_approve,
                )
                await self.registry.complete_workflow_run(run_id, "error")
                return False

        # Check if we've reached the end
        if next_idx >= len(workflow.stages):
            await self.registry.complete_workflow_run(run_id, "completed")
            logger.info(
                "WORKFLOW COMPLETE — %s (run=%s, all stages done)",
                workflow.name,
                run_id,
            )
            return True

        # Spawn next stage's agent
        next_stage = workflow.stages[next_idx]
        logger.info(
            "WORKFLOW ADVANCE — %s stage %s → %s (run=%s, pr=#%s)",
            workflow.name,
            current_stage.name,
            next_stage.name,
            run_id,
            pr_number,
        )

        agent_id = await self._spawn_stage_agent(
            run_id=run_id,
            stage=next_stage,
            pr_number=pr_number,
            event=squadron_event,
        )

        await self.registry.advance_workflow_run(
            run_id=run_id,
            next_stage=next_stage.name,
            stage_index=next_idx,
            stage_agent_id=agent_id,
        )

        return True

    async def _handle_rejection(
        self,
        run: dict,
        workflow: WorkflowDefinition,
        current_stage,  # WorkflowStage
    ) -> bool:
        """Handle stage rejection (changes_requested)."""
        run_id = run["run_id"]
        on_reject = current_stage.on_reject

        if on_reject == "stop":
            await self.registry.complete_workflow_run(run_id, "rejected")
            logger.info(
                "WORKFLOW REJECTED — %s (run=%s, stage=%s, action=stop)",
                workflow.name,
                run_id,
                current_stage.name,
            )
            return True

        elif on_reject == "restart":
            # Restart from the first stage
            first_stage = workflow.stages[0]
            await self.registry.advance_workflow_run(
                run_id=run_id,
                next_stage=first_stage.name,
                stage_index=0,
            )
            logger.info(
                "WORKFLOW RESTART — %s restarting from %s (run=%s)",
                workflow.name,
                first_stage.name,
                run_id,
            )
            return True

        else:
            # Named stage jump
            jump_idx = self._find_stage_index(workflow, on_reject)
            if jump_idx is not None:
                target_stage = workflow.stages[jump_idx]
                await self.registry.advance_workflow_run(
                    run_id=run_id,
                    next_stage=target_stage.name,
                    stage_index=jump_idx,
                )
                logger.info(
                    "WORKFLOW REJECT JUMP — %s → %s (run=%s)",
                    workflow.name,
                    target_stage.name,
                    run_id,
                )
                return True

            logger.error(
                "Workflow %s stage %s has on_reject='%s' — stage not found",
                workflow.name,
                current_stage.name,
                on_reject,
            )
            await self.registry.complete_workflow_run(run_id, "error")
            return False

    # ── Agent Spawning ───────────────────────────────────────────────────

    async def _spawn_stage_agent(
        self,
        run_id: str,
        stage,  # WorkflowStage
        pr_number: int | None,
        event: SquadronEvent,
    ) -> str | None:
        """Spawn an agent for a workflow stage.

        Uses the registered spawn callback (provided by AgentManager)
        to create a review agent with the stage's configured role.

        The agent_id follows the pattern: ``{role}-wf-{pr_number}``
        to distinguish workflow agents from regular approval flow agents.
        """
        if not self._spawn_review_agent:
            logger.error("No spawn callback registered — cannot spawn stage agent")
            return None

        try:
            agent_id = await self._spawn_review_agent(
                role=stage.agent,
                pr_number=pr_number or 0,
                event=event,
                workflow_run_id=run_id,
                stage_name=stage.name,
                action=stage.action,
            )
            logger.info(
                "Spawned workflow agent: %s (stage=%s, run=%s)",
                agent_id,
                stage.name,
                run_id,
            )
            return agent_id
        except Exception:
            logger.exception("Failed to spawn agent for stage %s (run=%s)", stage.name, run_id)
            return None

    # ── Lookup by Agent ──────────────────────────────────────────────────

    async def get_workflow_for_agent(self, agent_id: str) -> tuple[dict, WorkflowDefinition] | None:
        """Look up the workflow run and definition for a given agent.

        Returns (run_dict, WorkflowDefinition) or None.
        """
        run = await self.registry.get_workflow_run_by_agent(agent_id)
        if not run:
            return None
        workflow = self._find_workflow(run["workflow_name"])
        if not workflow:
            return None
        return run, workflow

    # ── Helpers ──────────────────────────────────────────────────────────

    def _find_workflow(self, name: str) -> WorkflowDefinition | None:
        """Find a workflow definition by name."""
        for wf in self.workflows:
            if wf.name == name:
                return wf
        return None

    def _find_stage_index(self, workflow: WorkflowDefinition, stage_name: str) -> int | None:
        """Find the index of a stage by name within a workflow."""
        for i, stage in enumerate(workflow.stages):
            if stage.name == stage_name:
                return i
        return None


# ── Type alias for spawn callback ────────────────────────────────────────────


class SpawnReviewCallback(Protocol):
    """Protocol for the agent spawning callback."""

    async def __call__(
        self,
        role: str,
        pr_number: int,
        event: SquadronEvent,
        *,
        workflow_run_id: str | None = None,
        stage_name: str | None = None,
        action: str | None = None,
    ) -> str | None: ...
