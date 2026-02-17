"""Agent Manager — manages agent lifecycle and LLM sessions.

Responsible for:
- Creating/resuming/destroying agent sessions
- Managing per-agent CopilotClient instances (or mock equivalents)
- Agent inbox management (asyncio.Queue per agent)
- Git worktree creation/cleanup
- PM invocation (fresh session per event batch)
- Dev/review agent lifecycle (persistent sessions with sleep/wake)

See runtime-architecture.md (AD-017) for full design.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from squadron.copilot import CopilotAgent, build_resume_config, build_session_config
from squadron.models import AgentRecord, AgentStatus, SquadronEvent, SquadronEventType
from squadron.tools.squadron_tools import SquadronTools

if TYPE_CHECKING:
    from squadron.config import AgentDefinition, CircuitBreakerDefaults, SquadronConfig
    from squadron.event_router import EventRouter
    from squadron.github_client import GitHubClient
    from squadron.registry import AgentRegistry
    from squadron.workflow_engine import WorkflowEngine

logger = logging.getLogger(__name__)


class AgentManager:
    """Manages the lifecycle of all agent instances."""

    def __init__(
        self,
        config: SquadronConfig,
        registry: AgentRegistry,
        github: GitHubClient,
        router: EventRouter,
        agent_definitions: dict[str, AgentDefinition],
        repo_root: Path,
    ):
        self.config = config
        self.registry = registry
        self.github = github
        self.router = router
        self.agent_definitions = agent_definitions
        self.repo_root = repo_root

        # Per-agent inboxes for event delivery
        self.agent_inboxes: dict[str, asyncio.Queue[SquadronEvent]] = {}

        # Unified tool registry (D-7: enforced tool boundaries)
        self._tools = SquadronTools(
            registry=registry,
            github=github,
            agent_inboxes=self.agent_inboxes,
            owner=config.project.owner,
            repo=config.project.repo,
            config=config,
            pre_sleep_hook=self._wip_commit_and_push,
        )

        # Per-agent CopilotAgent instances (one CLI subprocess each)
        self._copilot_agents: dict[str, CopilotAgent] = {}

        # Track active agent tasks
        self._agent_tasks: dict[str, asyncio.Task] = {}

        # Per-agent duration watchdog tasks (D-10: background timer enforcement)
        self._watchdog_tasks: dict[str, asyncio.Task] = {}

        self._running = False

        # Observability: last spawn timestamp (ISO string)
        self.last_spawn_time: str | None = None

        # Workflow engine (optional — set via set_workflow_engine)
        self._workflow_engine: WorkflowEngine | None = None

        # Agent concurrency limiter
        max_concurrent = config.runtime.max_concurrent_agents
        self._agent_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None
        )

        # Track which event types have config-driven handlers (for idempotent re-registration)
        self._config_trigger_types: set[SquadronEventType] = set()

    def set_workflow_engine(self, engine: WorkflowEngine) -> None:
        """Attach the workflow engine for event-driven pipeline triggers."""
        self._workflow_engine = engine

    async def start(self) -> None:
        """Start the agent manager — register config-driven event handlers."""
        self._running = True

        # Register config-driven trigger handler for all event types
        # that appear in agent_roles.triggers
        self._register_trigger_handlers()

        # Register mention-based routing for comment events (Layer 2)
        self.router.on(SquadronEventType.ISSUE_COMMENT, self._handle_mention_routing)

        # Register lifecycle handler for issue close (unblocking)
        self.router.on(SquadronEventType.ISSUE_CLOSED, self._handle_issue_closed)

        # Register handler for issue reassignment (D-12: abort on reassign)
        self.router.on(SquadronEventType.ISSUE_ASSIGNED, self._handle_issue_assigned)

        logger.info("Agent manager started")

    async def stop(self) -> None:
        """Stop all agents gracefully."""
        self._running = False

        # Stop all running agent tasks (snapshot to avoid dict-changed-during-iteration)
        for agent_id, task in list(self._agent_tasks.items()):
            logger.info("Stopping agent %s", agent_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Mark as sleeping for recovery
            agent = await self.registry.get_agent(agent_id)
            if agent and agent.status == AgentStatus.ACTIVE:
                agent.status = AgentStatus.SLEEPING
                agent.sleeping_since = datetime.now(timezone.utc)
                await self.registry.update_agent(agent)

        # Cancel all watchdog timers
        for agent_id, watchdog in list(self._watchdog_tasks.items()):
            watchdog.cancel()
        self._watchdog_tasks.clear()

        # Stop all CopilotAgent instances (CLI subprocesses)
        for agent_id, copilot in list(self._copilot_agents.items()):
            await copilot.stop()
        self._copilot_agents.clear()

        self._agent_tasks.clear()
        logger.info("Agent manager stopped")

    # ── Config-Driven Trigger Matching ───────────────────────────────────

    def _register_trigger_handlers(self) -> None:
        """Register event handlers based on config.yaml agent_roles.triggers.

        Scans all agent roles for trigger definitions and registers
        _handle_config_trigger for each unique event type that appears.
        Idempotent — clears previously registered config trigger handlers first.
        """
        from squadron.event_router import EVENT_MAP

        # Clear previously registered config-trigger event types
        for old_type in self._config_trigger_types:
            self.router.clear_handlers_for(old_type)

        # Collect all unique SquadronEventTypes referenced by triggers
        trigger_event_types: set[SquadronEventType] = set()
        for _role_name, role_config in self.config.agent_roles.items():
            for trigger in role_config.triggers:
                internal_type = EVENT_MAP.get(trigger.event)
                if internal_type:
                    trigger_event_types.add(internal_type)
                else:
                    logger.warning(
                        "Unknown trigger event type '%s' — not in EVENT_MAP", trigger.event
                    )

        # Register the universal trigger handler for each event type
        for event_type in trigger_event_types:
            self.router.on(event_type, self._handle_config_trigger)

        self._config_trigger_types = trigger_event_types

        logger.info(
            "Registered config triggers for %d event types: %s",
            len(trigger_event_types),
            ", ".join(t.value for t in trigger_event_types),
        )

    async def _handle_config_trigger(self, event: SquadronEvent) -> None:
        """Match an event against all config triggers and execute matching actions.

        This is the universal trigger handler — all event→agent behaviour is
        driven by config triggers and workflow definitions.  Supports four
        trigger actions:
          - spawn: create a new agent (default)
          - wake: wake a sleeping agent of this role
          - complete: complete an agent of this role
          - sleep: transition an active agent to SLEEPING

        After processing role triggers, also evaluates workflow triggers
        (sequential agent pipelines) and handles PR review stage advancement.
        """
        from squadron.event_router import REVERSE_EVENT_MAP

        github_event_type = REVERSE_EVENT_MAP.get(event.event_type)
        if not github_event_type:
            return

        payload = event.data.get("payload", {})

        for role_name, role_config in self.config.agent_roles.items():
            for trigger in role_config.triggers:
                if trigger.event != github_event_type:
                    continue

                # Label match (for issues.labeled triggers)
                if trigger.label:
                    event_label = payload.get("label", {}).get("name", "")
                    if event_label != trigger.label:
                        continue

                # Condition evaluation
                if trigger.condition and not self._evaluate_condition(
                    trigger.condition, event, role_name, payload
                ):
                    continue

                # Dispatch by action
                if trigger.action == "spawn":
                    await self._trigger_spawn(role_name, role_config, trigger, event)
                elif trigger.action == "wake":
                    await self._trigger_wake(role_name, event)
                elif trigger.action == "complete":
                    await self._trigger_complete(role_name, event)
                elif trigger.action == "sleep":
                    await self._trigger_sleep(role_name, event)

        # ── Workflow evaluation (sequential agent pipelines) ─────────
        if self._workflow_engine:
            await self._evaluate_workflows(github_event_type, payload, event)

    def _evaluate_condition(
        self,
        condition: dict,
        event: SquadronEvent,
        role_name: str,
        payload: dict,
    ) -> bool:
        """Evaluate a trigger condition dict. Returns True if all conditions pass."""
        # approval_flow: true — only spawn if this role is in the approval flow reviewers
        if condition.get("approval_flow"):
            if not self.config.approval_flows.enabled:
                return False
            pr_data = payload.get("pull_request", {})
            labels = [lbl.get("name", "") for lbl in pr_data.get("labels", [])]
            reviewer_roles = self.config.approval_flows.get_reviewers_for_pr(labels)
            if role_name not in reviewer_roles:
                return False

        # merged: true/false — check if PR was merged
        if "merged" in condition:
            pr_data = payload.get("pull_request", {})
            if pr_data.get("merged", False) != condition["merged"]:
                return False

        # review_state: "changes_requested" / "approved" / "commented" — filter by review action
        if "review_state" in condition:
            review = payload.get("review", {})
            if review.get("state", "").lower() != condition["review_state"].lower():
                return False

        return True

    async def _trigger_spawn(
        self,
        role_name: str,
        role_config: Any,
        trigger: Any,
        event: SquadronEvent,
    ) -> None:
        """Handle spawn action — create a new agent for this role."""
        # For PR-triggered spawns, use pr_number as issue fallback
        issue_number = event.issue_number
        if not issue_number and event.pr_number:
            # Try to extract source issue from PR body
            payload = event.data.get("payload", {})
            pr_data = payload.get("pull_request", {})
            body = pr_data.get("body", "") or ""
            issue_number = self._extract_issue_number(body) or event.pr_number

        if not issue_number:
            logger.warning(
                "Trigger %s/%s matched but no issue_number — skipping",
                role_name,
                trigger.event,
            )
            return

        # Singleton guard — only one agent of this role globally
        if role_config.singleton:
            all_active = await self.registry.get_all_active_agents()
            active_of_role = [a for a in all_active if a.role == role_name]
            if active_of_role:
                logger.info(
                    "Singleton role %s already has active agent %s — skipping",
                    role_name,
                    active_of_role[0].agent_id,
                )
                return

        # Duplicate guard (ephemeral agents skip)
        if not role_config.is_ephemeral:
            existing = await self.registry.get_agents_for_issue(issue_number)
            if any(a.role == role_name for a in existing):
                logger.info(
                    "Agent %s already exists for issue #%d — skipping",
                    role_name,
                    issue_number,
                )
                return

        logger.info(
            "Config trigger matched: %s/%s [%s] → spawning %s for issue #%d",
            trigger.event,
            trigger.label or "*",
            trigger.action,
            role_name,
            issue_number,
        )
        record = await self.create_agent(role_name, issue_number, trigger_event=event)
        if record:
            self.last_spawn_time = datetime.now(timezone.utc).isoformat()
        # For PR-spawned agents, associate with the PR
        if record and event.pr_number and not record.pr_number:
            record.pr_number = event.pr_number
            payload = event.data.get("payload", {})
            pr_data = payload.get("pull_request", {})
            # Use PR's head branch for reviewer agents
            if pr_data.get("head", {}).get("ref"):
                record.branch = pr_data["head"]["ref"]
            await self.registry.update_agent(record)

    async def _trigger_wake(self, role_name: str, event: SquadronEvent) -> None:
        """Handle wake action — wake sleeping agents of this role for the PR/issue."""
        agents = await self.registry.get_all_active_agents()
        target_pr = event.pr_number
        target_issue = event.issue_number

        for agent in agents:
            if agent.role != role_name:
                continue
            if agent.status != AgentStatus.SLEEPING:
                continue
            # Match by PR number or issue number
            if target_pr and agent.pr_number == target_pr:
                pass  # match
            elif target_issue and agent.issue_number == target_issue:
                pass  # match
            else:
                continue

            wake_event = SquadronEvent(
                event_type=SquadronEventType.WAKE_AGENT,
                pr_number=target_pr,
                issue_number=target_issue,
                agent_id=agent.agent_id,
                data=event.data,
            )
            logger.info(
                "Config trigger: wake %s (role=%s, pr=#%s)",
                agent.agent_id,
                role_name,
                target_pr,
            )
            await self.wake_agent(agent.agent_id, wake_event)

    async def _trigger_complete(self, role_name: str, event: SquadronEvent) -> None:
        """Handle complete action — complete agents of this role for the PR/issue."""
        agents = await self.registry.get_all_active_agents()
        target_pr = event.pr_number
        target_issue = event.issue_number

        for agent in agents:
            if agent.role != role_name:
                continue
            if agent.status in (AgentStatus.COMPLETED, AgentStatus.ESCALATED):
                continue
            # Match by PR number or issue number
            if target_pr and agent.pr_number == target_pr:
                pass  # match
            elif target_issue and agent.issue_number == target_issue:
                pass  # match
            else:
                continue

            logger.info(
                "Config trigger: completing %s (role=%s, pr=#%s)",
                agent.agent_id,
                role_name,
                target_pr,
            )
            agent.status = AgentStatus.COMPLETED
            agent.active_since = None
            await self.registry.update_agent(agent)

            copilot = self._copilot_agents.get(agent.agent_id)
            await self._cleanup_agent(
                agent.agent_id,
                destroy_session=True,
                copilot=copilot,
                session_id=agent.session_id,
            )

            # Post completion comment
            if agent.issue_number:
                try:
                    reason = ""
                    if target_pr:
                        payload = event.data.get("payload", {})
                        merged = payload.get("pull_request", {}).get("merged", False)
                        reason = f"PR #{target_pr} {'merged' if merged else 'closed'}."
                    await self.github.comment_on_issue(
                        self.config.project.owner,
                        self.config.project.repo,
                        agent.issue_number,
                        f"**[squadron:{agent.role}]** {reason} Task complete.",
                    )
                except Exception:
                    logger.debug("Failed to post completion comment for %s", agent.agent_id)

    async def _trigger_sleep(self, role_name: str, event: SquadronEvent) -> None:
        """Handle sleep action — transition active agents of this role to SLEEPING.

        Used to put a dev agent to sleep after it opens a PR, so it waits
        for review feedback before continuing.  Matches agents by PR number
        or issue number (extracted from the PR body).
        """
        agents = await self.registry.get_all_active_agents()
        target_pr = event.pr_number
        target_issue = event.issue_number

        # Also try to extract linked issue from PR body
        if not target_issue and target_pr:
            payload = event.data.get("payload", {})
            pr_data = payload.get("pull_request", {})
            body = pr_data.get("body", "") or ""
            target_issue = self._extract_issue_number(body)

        for agent in agents:
            if agent.role != role_name:
                continue
            if agent.status != AgentStatus.ACTIVE:
                continue
            # Match by PR number or issue number
            if target_pr and agent.pr_number == target_pr:
                pass  # match
            elif target_issue and agent.issue_number == target_issue:
                pass  # match
            else:
                continue

            logger.info(
                "Config trigger: sleeping %s (role=%s, pr=#%s)",
                agent.agent_id,
                role_name,
                target_pr,
            )

            # Associate the PR with this agent if not already set
            if target_pr and not agent.pr_number:
                agent.pr_number = target_pr

            # WIP commit before sleep (3.1)
            await self._wip_commit_and_push(agent)

            agent.status = AgentStatus.SLEEPING
            agent.sleeping_since = datetime.now(timezone.utc)
            agent.active_since = None
            await self.registry.update_agent(agent)

            # Cancel the running agent task (the session is preserved)
            task = self._agent_tasks.pop(agent.agent_id, None)
            if task and not task.done():
                task.cancel()

            # Cancel watchdog — sleeping agents don't have timers
            self._cancel_watchdog(agent.agent_id)
            # Release concurrency slot
            self._release_semaphore()

            if agent.issue_number:
                try:
                    await self.github.comment_on_issue(
                        self.config.project.owner,
                        self.config.project.repo,
                        agent.issue_number,
                        f"**[squadron:{agent.role}]** PR #{target_pr} opened. "
                        "Going to sleep while waiting for review feedback.",
                    )
                except Exception:
                    logger.debug("Failed to post sleep comment for %s", agent.agent_id)

    # ── WIP Commit (3.1 — save work before sleep) ──────────────────────

    async def _wip_commit_and_push(self, agent: AgentRecord) -> None:
        """Auto-save work-in-progress before an agent sleeps.

        Does ``git add -A && git commit && git push`` in the agent's
        worktree so that no local changes are lost when the container
        is recycled.  Failures are logged but never propagated — the
        sleep transition is more important than the push.
        """
        if not agent.worktree_path or not agent.branch:
            logger.debug("Skipping WIP commit for %s — no worktree/branch", agent.agent_id)
            return

        worktree = Path(agent.worktree_path)
        if not worktree.exists():
            logger.debug("Skipping WIP commit for %s — worktree missing", agent.agent_id)
            return

        try:
            # Stage everything
            rc, _, stderr = await self._run_git_in(worktree, "add", "-A")
            if rc != 0:
                logger.warning("git add failed for %s: %s", agent.agent_id, stderr)
                return

            # Check if there is anything to commit
            rc, status_out, _ = await self._run_git_in(worktree, "status", "--porcelain")
            if not status_out.strip():
                logger.debug("No WIP changes to commit for %s", agent.agent_id)
                return

            # Commit
            rc, _, stderr = await self._run_git_in(
                worktree,
                "commit",
                "-m",
                f"[squadron-wip] auto-save before sleep ({agent.agent_id})",
                "--allow-empty",
            )
            if rc != 0:
                logger.warning("git commit failed for %s: %s", agent.agent_id, stderr)
                return

            # Push
            rc, _, stderr = await self._run_git_in(
                worktree, "push", "origin", agent.branch, timeout=120
            )
            if rc != 0:
                logger.warning("git push failed for %s: %s", agent.agent_id, stderr)
            else:
                logger.info("WIP commit pushed for %s on %s", agent.agent_id, agent.branch)

        except asyncio.TimeoutError:
            logger.warning("WIP commit/push timed out for %s", agent.agent_id)
        except Exception:
            logger.exception("WIP commit/push failed for %s", agent.agent_id)

    # ── Workflow Evaluation (2.3d — unified dispatch) ────────────────────

    async def _evaluate_workflows(
        self,
        github_event_type: str,
        payload: dict,
        event: SquadronEvent,
    ) -> None:
        """Evaluate workflow triggers and PR review stage advancement.

        Called at the end of ``_handle_config_trigger()`` to consolidate all
        event dispatch in the agent manager.  Delegates to the workflow engine
        for trigger matching and stage advancement.
        """
        assert self._workflow_engine is not None

        # 1. Check if any workflow should activate for this event
        try:
            triggered = await self._workflow_engine.evaluate_event(
                github_event_type,
                payload,
                event,
            )
            if triggered:
                logger.info(
                    "Workflow triggered for %s — pipeline initiated",
                    github_event_type,
                )
        except Exception:
            logger.exception("Workflow engine error evaluating %s", github_event_type)

        # 2. For PR review events, check if this advances a workflow stage
        if event.event_type == SquadronEventType.PR_REVIEW_SUBMITTED and event.pr_number:
            review = payload.get("review", {})
            try:
                await self._workflow_engine.handle_pr_review(
                    pr_number=event.pr_number,
                    reviewer=review.get("user", {}).get("login", ""),
                    review_state=review.get("state", ""),
                    payload=payload,
                    squadron_event=event,
                )
            except Exception:
                logger.exception(
                    "Workflow engine error handling PR review for #%d",
                    event.pr_number,
                )

    # ── Agent Creation ───────────────────────────────────────────────────

    async def create_agent(
        self,
        role: str,
        issue_number: int,
        trigger_event: SquadronEvent | None = None,
    ) -> AgentRecord:
        """Create a new agent for an issue.

        Stateful agents (default):
        1. Create agent record in registry
        2. Create git worktree for branch isolation
        3. Start agent session

        Ephemeral agents (config: lifecycle: ephemeral):
        1. Create agent record with unique ID (timestamp suffix)
        2. No worktree — uses repo root
        3. Start session, run to completion, destroy
        """
        import time

        role_config = self.config.agent_roles.get(role)
        is_ephemeral = role_config.is_ephemeral if role_config else False

        # Ephemeral agents get unique IDs (multiple can run for same issue)
        if is_ephemeral:
            agent_id = f"{role}-issue-{issue_number}-{int(time.time())}"
        else:
            agent_id = f"{role}-issue-{issue_number}"

        # Duplicate guard for persistent agents — check by role + issue
        if not is_ephemeral:
            existing_agents = await self.registry.get_agents_for_issue(issue_number)
            for existing in existing_agents:
                if existing.role == role:
                    logger.warning(
                        "Agent %s already exists for role=%s issue=#%d — skipping",
                        existing.agent_id,
                        role,
                        issue_number,
                    )
                    return existing

        # Check concurrency limit
        if self._agent_semaphore is not None:
            if self._agent_semaphore.locked():
                logger.warning("Agent concurrency limit reached — queueing %s", agent_id)
            await self._agent_semaphore.acquire()
            logger.debug(
                "Agent semaphore acquired for %s (%d slots remaining)",
                agent_id,
                self._agent_semaphore._value,  # noqa: SLF001
            )

        # Determine branch name (ephemeral agents don't need branches)
        branch = "" if is_ephemeral else self._branch_name(role, issue_number)

        # Create agent record
        record = AgentRecord(
            agent_id=agent_id,
            role=role,
            issue_number=issue_number,
            session_id=f"squadron-{agent_id}",
            status=AgentStatus.CREATED,
            branch=branch,
        )
        await self.registry.create_agent(record)

        if is_ephemeral:
            # Ephemeral: no worktree, use repo root directly (don't set worktree_path
            # so cleanup won't try to remove the shared repo clone)
            record.worktree_path = None
        else:
            # Stateful: create git worktree
            worktree_path = await self._create_worktree(record)
            record.worktree_path = str(worktree_path)

        # Transition to ACTIVE
        record.status = AgentStatus.ACTIVE
        record.active_since = datetime.now(timezone.utc)
        await self.registry.update_agent(record)

        # Create inbox
        self.agent_inboxes[agent_id] = asyncio.Queue()

        # Create CopilotAgent instance (one CLI subprocess per agent)
        copilot = CopilotAgent(
            runtime_config=self.config.runtime,
            working_directory=str(record.worktree_path or self.repo_root),
        )
        await copilot.start()
        self._copilot_agents[agent_id] = copilot

        # Start agent task
        agent_task = asyncio.create_task(
            self._run_agent(record, trigger_event),
            name=f"agent-{agent_id}",
        )
        self._agent_tasks[agent_id] = agent_task

        # Start duration watchdog (D-10: background timer enforcement)
        self._start_watchdog(agent_id, role)

        logger.info(
            "Created agent %s (lifecycle=%s, branch=%s)",
            agent_id,
            role_config.lifecycle if role_config else "persistent",
            branch,
        )
        return record

    async def wake_agent(self, agent_id: str, trigger_event: SquadronEvent) -> None:
        """Wake a sleeping agent when its blocker is resolved or PR feedback arrives."""
        agent = await self.registry.get_agent(agent_id)
        if agent is None:
            logger.error("Cannot wake unknown agent: %s", agent_id)
            return
        if agent.status != AgentStatus.SLEEPING:
            logger.warning("Agent %s is not sleeping (status=%s)", agent_id, agent.status)
            return

        # Check concurrency limit before waking
        if self._agent_semaphore is not None:
            if self._agent_semaphore.locked():
                logger.warning("Agent concurrency limit reached — queueing wake for %s", agent_id)
            await self._agent_semaphore.acquire()

        # Transition to ACTIVE
        agent.status = AgentStatus.ACTIVE
        agent.active_since = datetime.now(timezone.utc)
        agent.sleeping_since = None
        agent.iteration_count += 1  # Track sleep→wake cycles
        await self.registry.update_agent(agent)

        # Ensure inbox exists
        if agent_id not in self.agent_inboxes:
            self.agent_inboxes[agent_id] = asyncio.Queue()

        # Ensure CopilotAgent instance exists (may need restart after server restart)
        if agent_id not in self._copilot_agents:
            copilot = CopilotAgent(
                runtime_config=self.config.runtime,
                working_directory=str(agent.worktree_path or self.repo_root),
            )
            await copilot.start()
            self._copilot_agents[agent_id] = copilot

        # Start agent task (resume session)
        agent_task = asyncio.create_task(
            self._run_agent(agent, trigger_event, resume=True),
            name=f"agent-{agent_id}",
        )
        self._agent_tasks[agent_id] = agent_task

        # Restart duration watchdog for the woken agent
        self._start_watchdog(agent_id, agent.role)

        logger.info("Woke agent %s (trigger: %s)", agent_id, trigger_event.event_type)

    async def complete_agent(self, agent_id: str) -> None:
        """Mark an agent as COMPLETED and clean up its resources.

        Called by the reconciliation loop when it detects that an agent's
        issue was closed, PR was merged/closed, or issue was reassigned
        while the agent was sleeping.
        """
        agent = await self.registry.get_agent(agent_id)
        if agent is None:
            logger.warning("complete_agent: unknown agent %s", agent_id)
            return

        # Only complete agents that are SLEEPING or ACTIVE
        if agent.status not in (AgentStatus.SLEEPING, AgentStatus.ACTIVE):
            logger.debug(
                "complete_agent: agent %s already in terminal state %s",
                agent_id,
                agent.status,
            )
            return

        logger.info("Completing agent %s (was %s)", agent_id, agent.status)

        # Cancel any running task
        task = self._agent_tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()

        # Update registry
        agent.status = AgentStatus.COMPLETED
        await self.registry.update_agent(agent)

        # Clean up resources (but preserve branch for human use)
        await self._cleanup_agent(agent_id, destroy_session=True)

    async def spawn_workflow_agent(
        self,
        role: str,
        pr_number: int,
        event: SquadronEvent,
        *,
        workflow_run_id: str | None = None,
        stage_name: str | None = None,
        action: str | None = None,
    ) -> str | None:
        """Spawn a review agent for a workflow pipeline stage.

        Called by the WorkflowEngine to create an agent for each stage.
        The agent_id includes the workflow run ID to distinguish from
        approval flow agents.

        Args:
            role: Agent role name (e.g. "test-coverage", "security-review").
            pr_number: PR number under review.
            event: The triggering SquadronEvent.
            workflow_run_id: Workflow run ID for tracking.
            stage_name: Name of the workflow stage.
            action: Stage action ("review", "review_and_merge", etc.).

        Returns:
            The agent_id of the created agent, or None on failure.
        """
        # Build unique agent ID for workflow agents
        suffix = f"wf-{pr_number}"
        if stage_name:
            suffix = f"wf-{stage_name}-{pr_number}"
        agent_id = f"{role}-{suffix}"

        # Check for existing agent with same ID
        existing = await self.registry.get_agent(agent_id)
        if existing:
            logger.info("Workflow agent %s already exists — skipping", agent_id)
            return agent_id

        # Verify the role has a definition
        agent_def = self.agent_definitions.get(role)
        if not agent_def:
            logger.error("No agent definition for workflow role: %s", role)
            return None

        payload = event.data.get("payload", {})
        pr_data = payload.get("pull_request", {})

        # Determine issue number (from PR body or fallback to pr_number)
        source_issue = pr_data.get("body", "") or ""
        issue_number = self._extract_issue_number(source_issue) or pr_number

        record = AgentRecord(
            agent_id=agent_id,
            role=role,
            issue_number=issue_number,
            pr_number=pr_number,
            session_id=f"squadron-{agent_id}",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
            branch=pr_data.get("head", {}).get("ref", "unknown"),
        )
        await self.registry.create_agent(record)

        # Create inbox
        self.agent_inboxes[agent_id] = asyncio.Queue()

        # Create CopilotAgent (reviewers use repo root, no worktree needed)
        copilot = CopilotAgent(
            runtime_config=self.config.runtime,
            working_directory=str(self.repo_root),
        )
        await copilot.start()
        self._copilot_agents[agent_id] = copilot

        # Build review event with workflow metadata
        review_event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=pr_number,
            issue_number=issue_number,
            data={
                **event.data,
                "workflow_run_id": workflow_run_id,
                "workflow_stage": stage_name,
                "workflow_action": action,
            },
        )
        agent_task = asyncio.create_task(
            self._run_agent(record, review_event),
            name=f"agent-{agent_id}",
        )
        self._agent_tasks[agent_id] = agent_task

        logger.info(
            "Created workflow agent %s for PR #%d (stage=%s, action=%s, run=%s)",
            agent_id,
            pr_number,
            stage_name,
            action,
            workflow_run_id,
        )
        return agent_id

    async def _run_agent(
        self,
        record: AgentRecord,
        trigger_event: SquadronEvent | None = None,
        resume: bool = False,
    ) -> None:
        """Run an agent session via the Copilot SDK.

        Creates or resumes a CopilotSession, sends the initial prompt,
        waits for the agent to finish, then runs the post-turn state
        machine to handle lifecycle transitions.

        Post-turn states:
        - SLEEPING: Agent called report_blocked — session preserved, task removed
        - COMPLETED: Agent called report_complete — session destroyed, resources freed
        - ACTIVE: Agent finished turn without lifecycle tool — normal completion
        - ESCALATED: Unhandled exception or timeout

        Ephemeral agents always destroy their session after completion.
        """
        agent_def = self.agent_definitions.get(record.role)
        if not agent_def:
            logger.error("No agent definition for role: %s", record.role)
            return

        copilot = self._copilot_agents.get(record.agent_id)
        if not copilot:
            logger.error("No CopilotAgent instance for: %s", record.agent_id)
            return

        # Check lifecycle type for this role
        role_config = self.config.agent_roles.get(record.role)
        is_ephemeral = role_config.is_ephemeral if role_config else False

        # Interpolate template variables in agent definition prompt
        raw_prompt = agent_def.prompt or agent_def.raw_content
        system_message = self._interpolate_agent_def(raw_prompt, record, trigger_event)

        # Resolve circuit breaker limits for this role
        cb_limits = self.config.circuit_breakers.for_role(record.role)
        max_duration = cb_limits.max_active_duration  # seconds

        # Build hooks for Layer 1 circuit breaker (tool call counting)
        hooks = self._build_hooks(record, cb_limits)

        # Build custom_agents and MCP servers from agent definition
        custom_agents = self._build_custom_agents(agent_def)
        mcp_servers = self._build_mcp_servers(agent_def)

        # ── Tool selection: .md frontmatter is the single source of truth ──
        # The frontmatter `tools:` list is a mixed bag of:
        #   - Custom Squadron tools (names in ALL_TOOL_NAMES) → passed as tools=
        #   - SDK built-in tools (read_file, bash, git, etc.) → passed as available_tools=
        # We split them here so each goes to the right SDK config key.
        from squadron.tools.squadron_tools import ALL_TOOL_NAMES_SET

        if agent_def.tools is not None:
            custom_tool_names = [t for t in agent_def.tools if t in ALL_TOOL_NAMES_SET]
            # SDK available_tools must include both builtins AND custom tool names
            # so the model can see them all.  If the .md lists tools, use the
            # full list as the allowlist; otherwise leave it open (None).
            sdk_available_tools = agent_def.tools
        else:
            custom_tool_names = None  # → lifecycle-based defaults
            sdk_available_tools = None  # → all tools visible

        tools = self._tools.get_tools(
            record.agent_id,
            names=custom_tool_names,
            is_stateless=is_ephemeral,
        )

        session_config = build_session_config(
            role=record.role,
            issue_number=record.issue_number,
            system_message=system_message,
            working_directory=str(record.worktree_path or self.repo_root),
            runtime_config=self.config.runtime,
            tools=tools,
            hooks=hooks,
            custom_agents=custom_agents,
            mcp_servers=mcp_servers,
            available_tools=sdk_available_tools,
        )

        try:
            if resume and not is_ephemeral:
                logger.info(
                    "AGENT RESUME — %s (session=%s, trigger=%s)",
                    record.agent_id,
                    record.session_id,
                    trigger_event.event_type if trigger_event else "manual",
                )
                resume_config = build_resume_config(
                    role=record.role,
                    system_message=system_message,
                    working_directory=str(record.worktree_path or self.repo_root),
                    runtime_config=self.config.runtime,
                    tools=tools,
                    hooks=hooks,
                    custom_agents=custom_agents,
                    mcp_servers=mcp_servers,
                    available_tools=sdk_available_tools,
                )
                session = await copilot.resume_session(record.session_id, resume_config)
                prompt = await self._build_wake_prompt(record, trigger_event)
            else:
                logger.info(
                    "AGENT START — %s (issue=#%d, branch=%s, session=%s, lifecycle=%s)",
                    record.agent_id,
                    record.issue_number,
                    record.branch,
                    record.session_id,
                    role_config.lifecycle if role_config else "persistent",
                )
                session = await copilot.create_session(session_config)
                if is_ephemeral:
                    prompt = await self._build_stateless_prompt(record, trigger_event)
                else:
                    prompt = self._build_agent_prompt(record, trigger_event)

            # Layer 2 circuit breaker: pass max_duration as the SDK's own
            # send_and_wait timeout. The SDK defaults to 60s internally if
            # not specified, which was causing premature TimeoutErrors.
            try:
                result = await session.send_and_wait({"prompt": prompt}, timeout=max_duration)
            except asyncio.TimeoutError:
                logger.warning(
                    "CIRCUIT BREAKER — agent %s exceeded max_active_duration (%ds)",
                    record.agent_id,
                    max_duration,
                )
                record.status = AgentStatus.ESCALATED
                await self.registry.update_agent(record)
                await self._cleanup_agent(
                    record.agent_id,
                    destroy_session=True,
                    copilot=copilot,
                    session_id=record.session_id,
                )
                return
            except Exception:
                logger.exception("Agent %s send_and_wait failed", record.agent_id)
                record.status = AgentStatus.ESCALATED
                await self.registry.update_agent(record)
                await self._cleanup_agent(
                    record.agent_id,
                    destroy_session=True,
                    copilot=copilot,
                    session_id=record.session_id,
                )
                return

            logger.info(
                "AGENT [%s] completed turn — result=%s",
                record.agent_id,
                result.type.value if result else "no response",
            )

            # ── Post-turn state machine ──────────────────────────────────
            # Ephemeral agents always clean up after completion
            if is_ephemeral:
                logger.info("EPHEMERAL AGENT DONE — %s", record.agent_id)
                record.status = AgentStatus.COMPLETED
                await self.registry.update_agent(record)
                await self._cleanup_agent(
                    record.agent_id,
                    destroy_session=True,
                    copilot=copilot,
                    session_id=record.session_id,
                )
                return

            # Re-read agent status from registry (framework tools may have
            # mutated it during the turn via report_blocked / report_complete)
            updated = await self.registry.get_agent(record.agent_id)
            if updated is None:
                logger.warning("Agent %s disappeared from registry after turn", record.agent_id)
                return

            # Increment turn counter on the persisted record
            updated.turn_count += 1
            record.turn_count = updated.turn_count  # sync local copy
            await self.registry.update_agent(updated)

            if updated.status == AgentStatus.SLEEPING:
                # Agent called report_blocked → session preserved for later resume
                logger.info(
                    "AGENT SLEEP — %s (blockers=%s)",
                    record.agent_id,
                    updated.blocked_by,
                )
                # Remove task reference but keep CopilotAgent alive for resume
                self._agent_tasks.pop(record.agent_id, None)
                # Cancel watchdog — sleeping agents don't have active timers
                self._cancel_watchdog(record.agent_id)
                # Release concurrency slot — sleeping agents don't count
                self._release_semaphore()

            elif updated.status == AgentStatus.COMPLETED:
                # Agent called report_complete → full cleanup
                logger.info("AGENT COMPLETE — %s", record.agent_id)
                await self._cleanup_agent(
                    record.agent_id,
                    destroy_session=True,
                    copilot=copilot,
                    session_id=record.session_id,
                )

            else:
                # Agent finished turn without calling a lifecycle tool.
                # This is normal — the agent completed its work for this prompt.
                logger.info(
                    "AGENT TURN DONE — %s (status=%s)", record.agent_id, updated.status.value
                )

        except asyncio.CancelledError:
            logger.info("Agent %s cancelled", record.agent_id)
            # Best-effort cleanup on cancellation to avoid semaphore leak
            try:
                await self._cleanup_agent(
                    record.agent_id,
                    destroy_session=True,
                    copilot=copilot,
                    session_id=record.session_id,
                )
            except Exception:
                logger.exception("Cleanup failed for cancelled agent %s", record.agent_id)
            raise
        except Exception:
            logger.exception("Agent %s failed", record.agent_id)
            record.status = AgentStatus.ESCALATED
            await self.registry.update_agent(record)
            # Best-effort cleanup on failure
            try:
                await self._cleanup_agent(
                    record.agent_id,
                    destroy_session=True,
                    copilot=copilot,
                    session_id=record.session_id,
                )
            except Exception:
                logger.exception("Cleanup failed for escalated agent %s", record.agent_id)

    async def _cleanup_agent(
        self,
        agent_id: str,
        *,
        destroy_session: bool = True,
        copilot: CopilotAgent | None = None,
        session_id: str | None = None,
    ) -> None:
        """Clean up resources for an agent that is done or escalated.

        - Destroys the Copilot session (if requested)
        - Stops the CopilotAgent process
        - Removes from in-memory tracking dicts
        """
        # Destroy session
        if destroy_session and copilot and session_id:
            try:
                await copilot.delete_session(session_id)
            except Exception:
                logger.warning("Failed to delete session %s for agent %s", session_id, agent_id)

        # Stop CopilotAgent process
        agent_copilot = self._copilot_agents.pop(agent_id, None)
        if agent_copilot:
            try:
                await agent_copilot.stop()
            except Exception:
                logger.warning("Failed to stop CopilotAgent for %s", agent_id)

        # Remove task and inbox
        self._agent_tasks.pop(agent_id, None)
        # Cancel watchdog timer
        self._cancel_watchdog(agent_id)
        self.agent_inboxes.pop(agent_id, None)

        # Remove git worktree (if any)
        agent_record = await self.registry.get_agent(agent_id)
        if agent_record and agent_record.worktree_path:
            worktree = Path(agent_record.worktree_path)
            # Safety: never remove the main repo clone
            if worktree == self.repo_root:
                logger.warning(
                    "Refusing to remove worktree %s — it is the main repo root", worktree
                )
            elif worktree.exists():
                try:
                    await self._run_git(
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                        timeout=30,
                    )
                    logger.info("Removed worktree %s for agent %s", worktree, agent_id)
                except Exception:
                    logger.warning("Failed to remove worktree %s for agent %s", worktree, agent_id)

        # Release concurrency slot
        self._release_semaphore()

        logger.info("Cleaned up agent %s", agent_id)

    def _release_semaphore(self) -> None:
        """Release one concurrency slot (if semaphore is active)."""
        if self._agent_semaphore is not None:
            self._agent_semaphore.release()

    # ── Duration Watchdog (D-10) ─────────────────────────────────────────

    def _start_watchdog(self, agent_id: str, role: str) -> None:
        """Start a background duration timer for an agent.

        When max_active_duration is exceeded, the framework cancels the agent
        task directly — regardless of what the agent is doing. This is the
        hard enforcement mechanism (D-10).
        """
        cb_config = self.config.circuit_breakers.for_role(role)
        max_duration = cb_config.max_active_duration
        if max_duration <= 0:
            return

        # Cancel any existing watchdog for this agent
        self._cancel_watchdog(agent_id)

        watchdog = asyncio.create_task(
            self._duration_watchdog(agent_id, max_duration),
            name=f"watchdog-{agent_id}",
        )
        self._watchdog_tasks[agent_id] = watchdog
        logger.debug(
            "Started duration watchdog for %s (max_active_duration=%ds)",
            agent_id,
            max_duration,
        )

    def _cancel_watchdog(self, agent_id: str) -> None:
        """Cancel the duration watchdog for an agent (if running)."""
        watchdog = self._watchdog_tasks.pop(agent_id, None)
        if watchdog and not watchdog.done():
            watchdog.cancel()

    async def _duration_watchdog(self, agent_id: str, max_seconds: int) -> None:
        """Background timer that kills an agent when max_active_duration is exceeded.

        This is the primary circuit breaker enforcement mechanism. It runs
        independently of the agent's tool calls or reasoning — if the timer
        fires, the agent is cancelled and escalated.
        """
        try:
            await asyncio.sleep(max_seconds)
        except asyncio.CancelledError:
            return  # Agent completed normally, watchdog was cancelled

        # Timer expired — kill the agent
        logger.warning(
            "WATCHDOG FIRED — agent %s exceeded max_active_duration (%ds), cancelling",
            agent_id,
            max_seconds,
        )

        # Cancel the agent task
        agent_task = self._agent_tasks.get(agent_id)
        if agent_task and not agent_task.done():
            agent_task.cancel()

        # Mark agent as ESCALATED
        agent = await self.registry.get_agent(agent_id)
        if agent and agent.status == AgentStatus.ACTIVE:
            agent.status = AgentStatus.ESCALATED
            await self.registry.update_agent(agent)

            # Post escalation comment on the issue
            try:
                await self.github.comment_on_issue(
                    self.config.project.owner,
                    self.config.project.repo,
                    agent.issue_number,
                    f"**[squadron:{agent.role}]** ⚠️ **Agent timed out** — exceeded maximum "
                    f"active duration ({max_seconds}s). Escalating to human.\n\n"
                    f"Agent `{agent_id}` has been stopped. Branch `{agent.branch}` "
                    f"is preserved for manual pickup.",
                )
            except Exception:
                logger.exception("Failed to post watchdog escalation comment for %s", agent_id)

    def _build_custom_agents(self, agent_def: "AgentDefinition") -> list[dict[str, Any]] | None:
        """Build SDK CustomAgentConfig list from the role's configured subagents.

        Reads subagent names from config.yaml agent_roles (not from agent .md
        frontmatter) and resolves them to SDK CustomAgentConfig dicts.
        """
        # Look up subagents from config.yaml agent_roles
        role_config = self.config.agent_roles.get(agent_def.role)
        subagent_names = role_config.subagents if role_config else []
        if not subagent_names:
            return None

        configs: list[dict[str, Any]] = []
        for sub_name in subagent_names:
            sub_def = self.agent_definitions.get(sub_name)
            if sub_def:
                configs.append(sub_def.to_custom_agent_config())
            else:
                logger.warning(
                    "Subagent '%s' referenced by '%s' not found in definitions",
                    sub_name,
                    agent_def.role,
                )
        return configs if configs else None

    def _build_mcp_servers(self, agent_def: "AgentDefinition") -> dict[str, Any] | None:
        """Build SDK mcp_servers dict from an agent definition.

        Converts MCPServerDefinition models to SDK-compatible dicts.
        """
        if not agent_def.mcp_servers:
            return None

        servers: dict[str, Any] = {}
        for name, srv in agent_def.mcp_servers.items():
            servers[name] = srv.to_sdk_dict()
        return servers if servers else None

    def _interpolate_agent_def(
        self,
        raw_content: str,
        record: AgentRecord,
        trigger_event: SquadronEvent | None,
    ) -> str:
        """Interpolate template variables in agent definition markdown.

        Agent .md files use {project_name}, {issue_number}, {issue_title},
        {issue_body}, {branch_name}, {base_branch}, {max_iterations}, etc.
        Uses format_map with a defaultdict so missing keys become empty strings
        instead of raising KeyError.
        """
        from collections import defaultdict

        # Extract issue metadata from trigger event payload
        issue_title = ""
        issue_body = ""
        if trigger_event:
            payload = trigger_event.data.get("payload", {})
            issue_data = payload.get("issue", {})
            issue_title = issue_data.get("title", "")
            issue_body = issue_data.get("body", "")

        # Get circuit breaker limits for default values
        cb_limits = self.config.circuit_breakers.for_role(record.role)

        values = defaultdict(
            str,
            {
                "project_name": self.config.project.name,
                "issue_number": str(record.issue_number or ""),
                "issue_title": issue_title,
                "issue_body": issue_body,
                "branch_name": record.branch or "",
                "base_branch": self.config.project.default_branch,
                "max_iterations": str(cb_limits.max_iterations),
                "max_tool_calls": str(cb_limits.max_tool_calls),
                "max_turns": str(cb_limits.max_turns),
            },
        )

        try:
            return raw_content.format_map(values)
        except (KeyError, ValueError, IndexError):
            logger.warning(
                "Failed to interpolate agent def for %s — using raw content", record.agent_id
            )
            return raw_content

    def _build_hooks(
        self,
        record: AgentRecord,
        cb_limits: "CircuitBreakerDefaults",
    ) -> dict[str, Any]:
        """Build SDK hooks dict for circuit breaker Layer 1.

        The on_pre_tool_use hook increments tool_call_count on the
        AgentRecord and denies tool use if the limit is exceeded.

        Hook signature matches SDK PreToolUseHandler:
          (PreToolUseHookInput, dict[str, str]) -> PreToolUseHookOutput | None
        """
        registry = self.registry
        max_tool_calls = cb_limits.max_tool_calls

        async def on_pre_tool_use(
            hook_input: dict[str, Any], context: dict[str, str]
        ) -> dict[str, Any] | None:
            """Called before each tool invocation — enforces tool call limit.

            Args:
                hook_input: PreToolUseHookInput with toolName, toolArgs, timestamp, cwd.
                context: Session context metadata (key-value pairs).
            """
            tool_name = hook_input.get("toolName", "unknown")
            record.tool_call_count += 1

            if record.tool_call_count > max_tool_calls:
                logger.warning(
                    "CIRCUIT BREAKER L1 — agent %s exceeded max_tool_calls (%d/%d, tool=%s)",
                    record.agent_id,
                    record.tool_call_count,
                    max_tool_calls,
                    tool_name,
                )
                record.status = AgentStatus.ESCALATED
                await registry.update_agent(record)
                return {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Tool call limit exceeded ({max_tool_calls})",
                }

            # Persist counter periodically (every 10 calls to avoid DB thrashing)
            if record.tool_call_count % 10 == 0:
                await registry.update_agent(record)

            # Log at warning threshold
            threshold = int(max_tool_calls * cb_limits.warning_threshold)
            if record.tool_call_count == threshold:
                logger.warning(
                    "CIRCUIT BREAKER L1 WARNING — agent %s at %d%% of tool call limit (%d/%d)",
                    record.agent_id,
                    int(cb_limits.warning_threshold * 100),
                    record.tool_call_count,
                    max_tool_calls,
                )

            return {"permissionDecision": "allow"}

        return {"on_pre_tool_use": on_pre_tool_use}

    def _build_agent_prompt(
        self,
        record: AgentRecord,
        trigger_event: SquadronEvent | None,
    ) -> str:
        """Build the user-turn prompt for a new persistent agent session.

        Contains only structured event context — no workflow instructions.
        The agent's .md definition (system message) provides all behavioral guidance.
        """
        lines = [f"## Assignment: Issue #{record.issue_number}\n"]

        if trigger_event:
            payload = trigger_event.data.get("payload", {})
            issue_data = payload.get("issue", {})
            if issue_data:
                lines.append(f"**Title:** {issue_data.get('title', 'N/A')}")
                body = issue_data.get("body", "")
                if body:
                    lines.append(f"\n**Description:**\n{body}")
                labels = [lbl.get("name", "") for lbl in issue_data.get("labels", [])]
                if labels:
                    lines.append(f"\n**Labels:** {', '.join(labels)}")

        lines.append(f"\n**Your role:** {record.role}")
        lines.append(f"**Branch:** {record.branch}")

        return "\n".join(lines)

    async def _build_wake_prompt(
        self,
        record: AgentRecord,
        trigger_event: SquadronEvent | None,
    ) -> str:
        """Build the wake-up prompt for a resumed session.

        Contains only structured event context — no workflow instructions.
        The agent's .md definition provides the Wake Protocol.
        Agents should use `get_pr_feedback` tool to fetch review details.
        """
        lines = [f"## Session Resumed: {record.agent_id}\n"]

        if trigger_event:
            lines.append(f"**Trigger:** {trigger_event.event_type.value}")
            if trigger_event.issue_number:
                lines.append(f"**Issue:** #{trigger_event.issue_number}")
            if trigger_event.pr_number:
                lines.append(f"**PR:** #{trigger_event.pr_number}")

            payload = trigger_event.data.get("payload", {})
            # Include review summary from the triggering event
            review = payload.get("review", {})
            if review:
                state = review.get("state", "N/A")
                lines.append(f"\n**Review state:** {state}")
                review_body = review.get("body", "")
                if review_body:
                    lines.append(f"**Review comment:** {review_body}")
                reviewer = review.get("user", {}).get("login", "unknown")
                lines.append(f"**Reviewer:** {reviewer}")

            # Include resolved blocker info
            resolved = trigger_event.data.get("resolved_issue")
            if resolved:
                lines.append(f"\n**Resolved blocker:** Issue #{resolved} has been closed.")

            # Include comment text for mention-triggered wakes
            comment_data = payload.get("comment", {})
            if comment_data:
                commenter = comment_data.get("user", {}).get("login", "unknown")
                comment_body = comment_data.get("body", "")
                if comment_body:
                    lines.append(f"\n**Comment by {commenter}:**\n{comment_body[:1000]}")

        return "\n".join(lines)

    # ── Event Handlers ───────────────────────────────────────────────────

    async def _handle_issue_closed(self, event: SquadronEvent) -> None:
        """Handle issue closure — check if it unblocks any sleeping agents."""
        if event.issue_number is None:
            return

        blocked_agents = await self.registry.get_agents_blocked_by(event.issue_number)
        for agent in blocked_agents:
            await self.registry.remove_blocker(agent.agent_id, event.issue_number)

            # If no more blockers, wake the agent
            updated = await self.registry.get_agent(agent.agent_id)
            if updated and not updated.blocked_by:
                wake_event = SquadronEvent(
                    event_type=SquadronEventType.BLOCKER_RESOLVED,
                    issue_number=event.issue_number,
                    agent_id=agent.agent_id,
                    data={"resolved_issue": event.issue_number},
                )
                await self.wake_agent(agent.agent_id, wake_event)

    async def _handle_issue_assigned(self, event: SquadronEvent) -> None:
        """Handle issue assignment — abort agents if issue reassigned away from bot.

        D-12: Framework-level abort on reassignment. If a human reassigns the
        issue to themselves (or another user), we cancel any active/sleeping
        agents working on it. Preserves the branch for human use.
        """
        if event.issue_number is None:
            return

        payload = event.data.get("payload", {})
        assignee = payload.get("assignee", {})
        new_login = (assignee.get("login") or "").lower()

        # If assigned to the bot, let existing trigger logic handle it
        bot_login = (self.config.project.bot_username or "").lower()

        if bot_login and new_login == bot_login:
            logger.debug(
                "Issue #%d assigned to bot (%s) — ignoring (trigger system handles spawning)",
                event.issue_number,
                new_login,
            )
            return

        # Find all agents working on this issue
        agents = await self.registry.get_agents_for_issue(event.issue_number)
        if not agents:
            return

        for agent in agents:
            if agent.status not in (AgentStatus.ACTIVE, AgentStatus.SLEEPING):
                continue

            previous_status = agent.status
            logger.info(
                "Issue #%d reassigned to @%s — aborting agent %s (was %s)",
                event.issue_number,
                new_login,
                agent.agent_id,
                previous_status,
            )

            # Cancel any running task
            task = self._agent_tasks.pop(agent.agent_id, None)
            if task and not task.done():
                task.cancel()

            # Mark as COMPLETED (not FAILED — reassignment is intentional)
            agent.status = AgentStatus.COMPLETED
            await self.registry.update_agent(agent)

            # Clean up resources but preserve the branch
            await self._cleanup_agent(agent.agent_id, destroy_session=True)

            # Post a comment on the issue
            owner = self.config.project.owner
            repo = self.config.project.repo
            if owner and repo:
                try:
                    await self.github.comment_on_issue(
                        owner,
                        repo,
                        event.issue_number,
                        f"Agent `{agent.agent_id}` stopped — issue reassigned to @{new_login}. "
                        f"Branch `{agent.branch}` has been preserved.",
                    )
                except Exception:
                    logger.debug(
                        "Failed to post reassignment comment for agent %s",
                        agent.agent_id,
                    )

    @staticmethod
    def _extract_issue_number(body: str) -> int | None:
        """Extract an issue number from PR body (e.g., 'Closes #42')."""
        import re

        match = re.search(r"(?:closes|fixes|resolves)\s+#(\d+)", body, re.IGNORECASE)
        return int(match.group(1)) if match else None

    # ── Mention-Based Routing (Layer 2) ──────────────────────────────────

    def _get_sender_agent_role(self, event: SquadronEvent) -> str | None:
        """Determine if the comment sender is a squadron agent, and return its role.

        Uses the bot_username from config to detect bot-authored comments,
        then checks the comment prefix (``[squadron:role]``) to identify
        which agent role posted it.  Returns ``None`` for human senders.
        """
        import re as _re

        payload = event.data.get("payload", {})
        comment_data = payload.get("comment", {})
        sender = comment_data.get("user", {})

        # Only apply self-loop guard to bot-authored comments
        sender_login = (sender.get("login") or "").lower()
        bot_username = (self.config.project.bot_username or "").lower()

        # Match "squadron-dev[bot]" login or type == "Bot"
        is_bot = sender.get("type") == "Bot" or (bot_username and sender_login == bot_username)
        if not is_bot:
            return None

        # Extract role from comment body prefix: [squadron:role]
        body = comment_data.get("body", "")
        match = _re.match(r"\*?\*?\[squadron[:\s]*(\S+?)\]", body)
        if match:
            return match.group(1).lower()

        return None

    async def _handle_mention_routing(self, event: SquadronEvent) -> None:
        """Route comment events based on @role / /role mentions.

        This is Layer 2 routing — conversational, mention-based dispatch:

        1. Parse ``@role`` or ``/role`` mentions from the comment body
           (already populated on ``event.mentioned_roles`` by the router).
        2. Apply self-loop guard: if the comment was posted by a squadron
           agent of role X, filter out role X from the mention list.  This
           prevents the PM from re-triggering itself when it posts a comment
           mentioning ``@pm``, while still allowing PM to trigger ``@feat-dev``.
        3. For each mentioned role:
           - Ephemeral roles → spawn a new agent
           - Persistent roles with a SLEEPING agent for this issue → wake it
           - Persistent roles with an ACTIVE agent → deliver event to inbox
           - Persistent roles with no agent → spawn a new one

        Comments with no role mentions are silently ignored.
        """
        if not event.mentioned_roles:
            logger.debug(
                "Comment on issue #%s has no role mentions — skipping",
                event.issue_number,
            )
            return

        if event.issue_number is None:
            logger.warning("Comment event has no issue_number — skipping mention routing")
            return

        # Self-loop guard: determine which role (if any) posted this comment
        sender_role = self._get_sender_agent_role(event)

        for role_name in event.mentioned_roles:
            # Self-loop guard: skip if the bot posted this comment as this role
            if sender_role and sender_role == role_name:
                logger.info(
                    "Self-loop guard: skipping @%s mention (comment authored by same role)",
                    role_name,
                )
                continue

            role_config = self.config.agent_roles.get(role_name)
            if not role_config:
                logger.debug("Mentioned role @%s not in config — ignoring", role_name)
                continue

            logger.info(
                "Mention routing: @%s mentioned on issue #%d (sender_role=%s)",
                role_name,
                event.issue_number,
                sender_role or "human",
            )

            if role_config.is_ephemeral:
                # Ephemeral roles: always spawn a new agent
                await self._mention_spawn(role_name, role_config, event)
            else:
                # Persistent roles: wake, deliver, or spawn
                await self._mention_wake_or_spawn(role_name, role_config, event)

    async def _mention_spawn(
        self,
        role_name: str,
        role_config: Any,
        event: SquadronEvent,
    ) -> None:
        """Spawn an ephemeral agent via mention routing."""
        # Singleton guard — same as config triggers
        if role_config.singleton:
            all_active = await self.registry.get_all_active_agents()
            active_of_role = [a for a in all_active if a.role == role_name]
            if active_of_role:
                logger.info(
                    "Singleton role %s already has active agent %s — skipping mention spawn",
                    role_name,
                    active_of_role[0].agent_id,
                )
                return

        assert event.issue_number is not None
        logger.info(
            "Mention spawn: creating %s for issue #%d",
            role_name,
            event.issue_number,
        )
        record = await self.create_agent(role_name, event.issue_number, trigger_event=event)
        if record:
            self.last_spawn_time = datetime.now(timezone.utc).isoformat()

    async def _mention_wake_or_spawn(
        self,
        role_name: str,
        role_config: Any,
        event: SquadronEvent,
    ) -> None:
        """Handle mention of a persistent role: wake if sleeping, deliver if active, spawn if new."""
        assert event.issue_number is not None

        # Look for existing agents of this role for this issue
        existing_agents = await self.registry.get_agents_for_issue(event.issue_number)
        role_agents = [a for a in existing_agents if a.role == role_name]

        if role_agents:
            agent = role_agents[0]  # most recent

            if agent.status == AgentStatus.SLEEPING:
                # Wake the sleeping agent with this comment as context
                wake_event = SquadronEvent(
                    event_type=SquadronEventType.WAKE_AGENT,
                    issue_number=event.issue_number,
                    pr_number=event.pr_number,
                    agent_id=agent.agent_id,
                    mentioned_roles=event.mentioned_roles,
                    data=event.data,
                )
                logger.info(
                    "Mention wake: @%s → waking %s on issue #%d",
                    role_name,
                    agent.agent_id,
                    event.issue_number,
                )
                await self.wake_agent(agent.agent_id, wake_event)

            elif agent.status == AgentStatus.ACTIVE:
                # Agent is actively running — deliver to its inbox
                inbox = self.agent_inboxes.get(agent.agent_id)
                if inbox is not None:
                    await inbox.put(event)
                    logger.info(
                        "Mention deliver: @%s → queued event for active agent %s",
                        role_name,
                        agent.agent_id,
                    )
                else:
                    logger.warning(
                        "Mention deliver: @%s → agent %s is ACTIVE but has no inbox",
                        role_name,
                        agent.agent_id,
                    )
            else:
                logger.debug(
                    "Mention: @%s → agent %s is in terminal state %s — spawning new",
                    role_name,
                    agent.agent_id,
                    agent.status,
                )
                await self._mention_spawn_persistent(role_name, event)
        else:
            # No agent exists for this role + issue → spawn
            await self._mention_spawn_persistent(role_name, event)

    async def _mention_spawn_persistent(
        self,
        role_name: str,
        event: SquadronEvent,
    ) -> None:
        """Spawn a new persistent agent via mention routing."""
        assert event.issue_number is not None
        logger.info(
            "Mention spawn (persistent): creating %s for issue #%d",
            role_name,
            event.issue_number,
        )
        record = await self.create_agent(role_name, event.issue_number, trigger_event=event)
        if record:
            self.last_spawn_time = datetime.now(timezone.utc).isoformat()

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _build_stateless_prompt(
        self,
        record: AgentRecord,
        trigger_event: SquadronEvent | None,
    ) -> str:
        """Build the user-turn prompt for an ephemeral agent session.

        Contains only structured event context — no workflow instructions.
        The agent's .md definition (system message) provides all behavioral
        guidance.  Agents use introspection tools (check_registry,
        get_recent_history, list_agent_roles, etc.) to gather system state.
        """
        lines = [f"## Event for Issue #{record.issue_number}\n"]

        # ── Project context ──────────────────────────────────────────
        lines.append(f"**Project:** {self.config.project.name}")
        lines.append(f"**Repo:** {self.config.project.owner}/{self.config.project.repo}")
        lines.append(f"**Your role:** {record.role}")

        # ── Triggering event details ─────────────────────────────────
        if trigger_event:
            lines.append("\n### Triggering Event\n")
            lines.append(f"**Type:** {trigger_event.event_type.value}")
            payload = trigger_event.data.get("payload", {})

            issue_data = payload.get("issue", {})
            if issue_data:
                lines.append(f"**Title:** {issue_data.get('title', 'N/A')}")
                labels = [lbl.get("name", "") for lbl in issue_data.get("labels", [])]
                if labels:
                    lines.append(f"**Labels:** {', '.join(labels)}")
                body = issue_data.get("body", "")
                if body:
                    lines.append(f"\n**Description:**\n{body[:2000]}")

            comment_data = payload.get("comment", {})
            if comment_data:
                commenter = comment_data.get("user", {}).get("login", "unknown")
                lines.append(f"\n**Comment by {commenter}:**")
                lines.append(comment_data.get("body", "")[:1000])

            pr_data = payload.get("pull_request", {})
            if pr_data:
                lines.append(f"**PR Title:** {pr_data.get('title', 'N/A')}")

        return "\n".join(lines)

    def _branch_name(self, role: str, issue_number: int) -> str:
        """Generate a branch name from config templates.

        Priority: role config branch_template > BranchNamingConfig > generic default.
        """
        # 1. Per-role branch template from config
        role_config = self.config.agent_roles.get(role)
        if role_config and role_config.branch_template:
            return role_config.branch_template.format(issue_number=issue_number)

        # 2. Global branch naming config (role → template mapping)
        naming = self.config.branch_naming
        templates = {
            "feat-dev": naming.feature,
            "bug-fix": naming.bugfix,
            "security-review": naming.security,
            "docs-dev": naming.docs,
            "infra-dev": naming.infra,
        }
        template = templates.get(role, f"{role}/issue-{{issue_number}}")
        return template.format(issue_number=issue_number)

    async def _run_git(self, *args: str, timeout: int = 60) -> tuple[int, str, str]:
        """Run a git command asynchronously without blocking the event loop.

        Returns (returncode, stdout, stderr).
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            (stdout_bytes or b"").decode(),
            (stderr_bytes or b"").decode(),
        )

    async def _create_worktree(self, record: AgentRecord) -> Path:
        """Create a git worktree for an agent's branch.

        When ``config.runtime.sparse_checkout`` is enabled, only a minimal
        set of top-level metadata files is checked out initially.  The
        agent's Copilot CLI can read any file via git commands, so the
        full tree is accessible — but disk usage drops dramatically for
        large repos.  The agent will ``git sparse-checkout add <dir>``
        on-demand as it navigates the codebase.
        """
        worktree_base = (
            Path(self.config.runtime.worktree_dir)
            if self.config.runtime.worktree_dir
            else self.repo_root / ".squadron-data" / "worktrees"
        )
        worktree_dir = worktree_base / f"issue-{record.issue_number}"
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        if worktree_dir.exists():
            logger.info("Worktree already exists: %s", worktree_dir)
            return worktree_dir

        try:
            # Create branch from default branch
            default_branch = self.config.project.default_branch
            await self._run_git(
                "branch",
                record.branch,
                f"origin/{default_branch}",
            )

            if self.config.runtime.sparse_checkout:
                # Sparse worktree: --no-checkout first, then set up sparse-checkout cone
                returncode, stdout, stderr = await self._run_git(
                    "worktree",
                    "add",
                    "--no-checkout",
                    str(worktree_dir),
                    record.branch,
                )
                if returncode != 0:
                    logger.error("Failed to create sparse worktree: %s", stderr)
                    return self.repo_root

                # Initialize sparse-checkout in cone mode
                await self._run_git_in(
                    worktree_dir,
                    "sparse-checkout",
                    "init",
                    "--cone",
                )
                # Start with only top-level files (README, config, etc.)
                await self._run_git_in(
                    worktree_dir,
                    "sparse-checkout",
                    "set",
                    "/",
                )
                # Now do the actual checkout
                await self._run_git_in(worktree_dir, "checkout")

                logger.info("Created sparse worktree: %s → %s", record.branch, worktree_dir)
            else:
                # Full worktree (original behavior)
                returncode, stdout, stderr = await self._run_git(
                    "worktree",
                    "add",
                    str(worktree_dir),
                    record.branch,
                )
                if returncode != 0:
                    logger.error("Failed to create worktree: %s", stderr)
                    return self.repo_root

                logger.info("Created worktree: %s → %s", record.branch, worktree_dir)
        except Exception:
            logger.exception("Worktree creation failed, using repo root")
            return self.repo_root

        return worktree_dir

    async def _run_git_in(self, cwd: Path, *args: str, timeout: int = 60) -> tuple[int, str, str]:
        """Run a git command in a specific directory (e.g. inside a worktree)."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            (stdout_bytes or b"").decode(),
            (stderr_bytes or b"").decode(),
        )
