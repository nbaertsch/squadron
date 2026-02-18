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
    from squadron.config import (
        AgentDefinition,
        CircuitBreakerDefaults,
        FailureAction,
        SquadronConfig,
    )
    from squadron.event_router import EventRouter
    from squadron.github_client import GitHubClient
    from squadron.registry import AgentRegistry
    from squadron.workflow import WorkflowEngine

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
            agent_definitions=agent_definitions,
            pre_sleep_hook=self._wip_commit_and_push,
            git_push_callback=self._git_push_for_agent,
            auto_merge_callback=self._auto_merge_pr,
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

        # Register command-based routing for comment events (Layer 2)
        self.router.on(SquadronEventType.ISSUE_COMMENT, self._handle_command_routing)

        # Register lifecycle handler for issue close (unblocking)
        self.router.on(SquadronEventType.ISSUE_CLOSED, self._handle_issue_closed)

        # Register handler for issue reassignment (D-12: abort on reassign)
        self.router.on(SquadronEventType.ISSUE_ASSIGNED, self._handle_issue_assigned)

        # Register handler for PR synchronize (invalidate approvals on PR update)
        self.router.on(SquadronEventType.PR_SYNCHRONIZED, self._handle_pr_synchronize)

        # Register handler for PR opened (set up review requirements)
        self.router.on(SquadronEventType.PR_OPENED, self._handle_pr_opened)

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
        # approval_flow: true — only spawn if this role is required by review_policy
        if condition.get("approval_flow"):
            if not self.config.review_policy.enabled:
                return False
            pr_data = payload.get("pull_request", {})
            labels = [lbl.get("name", "") for lbl in pr_data.get("labels", [])]
            base_branch = pr_data.get("base", {}).get("ref", "")
            # TODO: could also pass changed_files for path-based rules
            required_roles = self.config.review_policy.get_required_roles(
                labels, changed_files=None, base_branch=base_branch
            )
            if role_name not in required_roles:
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

        # is_pr_comment: true — only match if comment is on a PR (not a plain issue)
        # GitHub includes "pull_request" key in issue payload for PR comments
        if condition.get("is_pr_comment"):
            issue_data = payload.get("issue", {})
            if not issue_data.get("pull_request"):
                return False

        # is_human_comment: true — only match if comment is from a human (not bot)
        if condition.get("is_human_comment"):
            comment = payload.get("comment", {})
            user = comment.get("user", {})
            user_type = user.get("type", "").lower()
            if user_type == "bot":
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
        # Use get_all_agents_for_issue to include completed/failed agents and prevent
        # UNIQUE constraint violations (issue #13)
        if not role_config.is_ephemeral:
            existing = await self.registry.get_all_agents_for_issue(issue_number)
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
                        f"{self._agent_signature(agent.role)}{reason} Task complete.",
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
                        f"{self._agent_signature(agent.role)}PR #{target_pr} opened. "
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

            # Push with GitHub App authentication
            rc, _, stderr = await self._run_git_in(
                worktree, "push", "origin", agent.branch, timeout=120, auth=True
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
        # Use get_all_agents_for_issue to include terminal agents (issue #13)
        if not is_ephemeral:
            existing_agents = await self.registry.get_all_agents_for_issue(issue_number)
            for existing in existing_agents:
                if existing.role == role and existing.status in (
                    AgentStatus.CREATED,
                    AgentStatus.ACTIVE,
                    AgentStatus.SLEEPING,
                ):
                    logger.warning(
                        "Agent %s already exists for role=%s issue=#%d — skipping",
                        existing.agent_id,
                        role,
                        issue_number,
                    )
                    return existing

            # Clean up stale terminal agent with the same ID so re-spawn
            # doesn't hit a UNIQUE constraint violation (issue #13).
            stale = await self.registry.get_agent(agent_id)
            if stale is not None:
                logger.info(
                    "Cleaning up terminal agent %s (status=%s) for re-spawn",
                    stale.agent_id,
                    stale.status,
                )
                await self.registry.delete_agent(stale.agent_id)

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

        # Create inbox BEFORE registering agent (prevents race condition where
        # events arrive before inbox exists — issue #30 agent responsiveness fix)
        self.agent_inboxes[agent_id] = asyncio.Queue()

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
            # Check if agent has a worktree path and if it exists
            working_directory = self.repo_root
            if agent.worktree_path:
                worktree_path = Path(agent.worktree_path)
                if not worktree_path.exists():
                    # Worktree is missing - recreate it
                    logger.warning(
                        "Worktree missing for agent %s at %s - recreating", agent_id, worktree_path
                    )
                    try:
                        # Recreate the worktree using existing infrastructure
                        new_worktree_path = await self._create_worktree(agent)
                        agent.worktree_path = str(new_worktree_path)
                        await self.registry.update_agent(agent)
                        working_directory = new_worktree_path
                        logger.info(
                            "Recreated worktree for agent %s: %s", agent_id, new_worktree_path
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to recreate worktree for agent %s: %s - using repo root",
                            agent_id,
                            e,
                        )
                        working_directory = self.repo_root
                else:
                    working_directory = worktree_path

            copilot = CopilotAgent(
                runtime_config=self.config.runtime,
                working_directory=str(working_directory),
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
            custom_tool_names = None  # → no Squadron tools (must be in frontmatter)
            sdk_available_tools = None  # → all SDK tools visible

        tools = self._tools.get_tools(
            record.agent_id,
            names=custom_tool_names,
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

        # Remove task
        self._agent_tasks.pop(agent_id, None)
        # Cancel watchdog timer
        self._cancel_watchdog(agent_id)

        # Drain inbox and re-queue pending commands for ephemeral singletons
        inbox = self.agent_inboxes.pop(agent_id, None)
        if inbox and not inbox.empty():
            agent_record = await self.registry.get_agent(agent_id)
            if agent_record:
                role_config = self.config.agent_roles.get(agent_record.role)
                if role_config and role_config.is_ephemeral and role_config.singleton:
                    pending_events = []
                    while not inbox.empty():
                        pending_events.append(inbox.get_nowait())
                    if pending_events:
                        logger.info(
                            "Agent %s completed with %d pending inbox events — re-spawning",
                            agent_id,
                            len(pending_events),
                        )
                        # Spawn new agent for each pending command
                        for event in pending_events:
                            asyncio.create_task(
                                self._command_spawn(agent_record.role, role_config, event),
                                name=f"respawn-{agent_record.role}-{event.issue_number}",
                            )

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

        Fix for issue #46: Bounded timeouts on all cleanup operations and
        proper cancellation waiting to prevent race conditions.
        """
        # Timeout for cleanup operations (30s is generous but bounded)
        CLEANUP_TIMEOUT = 30

        try:
            await asyncio.sleep(max_seconds)
        except asyncio.CancelledError:
            return  # Agent completed normally, watchdog was cancelled

        # Timer expired — kill the agent
        logger.warning(
            "WATCHDOG FIRED (layer 1) — agent %s exceeded max_active_duration (%ds), cancelling",
            agent_id,
            max_seconds,
        )

        # Cancel the agent task and WAIT for it to actually stop (fix race condition)
        agent_task = self._agent_tasks.get(agent_id)
        if agent_task and not agent_task.done():
            agent_task.cancel()
            try:
                # Wait up to CLEANUP_TIMEOUT for the task to actually stop
                await asyncio.wait_for(
                    asyncio.shield(agent_task),
                    timeout=CLEANUP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Agent %s did not stop within %ds after cancel — may be stuck in blocking operation",
                    agent_id,
                    CLEANUP_TIMEOUT,
                )
            except asyncio.CancelledError:
                pass  # Expected — task was cancelled

        # Mark agent as ESCALATED (with bounded timeout)
        try:
            agent = await asyncio.wait_for(
                self.registry.get_agent(agent_id),
                timeout=CLEANUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("Timed out fetching agent %s from registry", agent_id)
            return

        if agent and agent.status == AgentStatus.ACTIVE:
            agent.status = AgentStatus.ESCALATED
            try:
                await asyncio.wait_for(
                    self.registry.update_agent(agent),
                    timeout=CLEANUP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error("Timed out updating agent %s status to ESCALATED", agent_id)

            # Post escalation comment on the issue (with bounded timeout)
            try:
                await asyncio.wait_for(
                    self.github.comment_on_issue(
                        self.config.project.owner,
                        self.config.project.repo,
                        agent.issue_number,
                        f"{self._agent_signature(agent.role)}⚠️ **Agent timed out** — exceeded maximum "
                        f"active duration ({max_seconds}s). Escalating to human.\n\n"
                        f"Agent `{agent_id}` has been stopped. Branch `{agent.branch}` "
                        f"is preserved for manual pickup.\n\n"
                        f"_Timeout enforced by: watchdog (layer 1)_",
                    ),
                    timeout=CLEANUP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error("Timed out posting watchdog escalation comment for %s", agent_id)
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
        {issue_body}, {branch_name}, {base_branch}, {pr_number}, {max_iterations}, etc.
        Uses format_map with a defaultdict so missing keys become empty strings
        instead of raising KeyError.
        """
        from collections import defaultdict

        # Extract issue metadata from trigger event payload
        issue_title = ""
        issue_body = ""
        pr_number = ""
        if trigger_event:
            payload = trigger_event.data.get("payload", {})
            issue_data = payload.get("issue", {})
            issue_title = issue_data.get("title", "")
            issue_body = issue_data.get("body", "")

            # Extract PR number from trigger event if available
            if trigger_event.pr_number:
                pr_number = str(trigger_event.pr_number)

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
                "pr_number": pr_number,
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

    async def _handle_pr_opened(self, event: SquadronEvent) -> None:
        """Handle PR opened — set up review requirements based on policy.

        When a new PR is opened:
        1. Determine which roles need to review (from review_policy config)
        2. Store the requirements in the registry
        3. Set up sequence state if sequential reviews are configured
        """
        if not event.pr_number:
            return

        policy = self.config.review_policy
        if not policy.enabled:
            logger.debug("Review policy disabled — skipping PR #%d setup", event.pr_number)
            return

        payload = event.data.get("payload", {})
        pr_data = payload.get("pull_request", {})

        # Get PR labels and base branch
        labels = [lbl.get("name", "") for lbl in pr_data.get("labels", [])]
        base_branch = pr_data.get("base", {}).get("ref", "")

        # Get changed files (optional, may not be in the webhook payload)
        changed_files = None
        try:
            files = await self.github.list_pull_request_files(
                self.config.project.owner,
                self.config.project.repo,
                event.pr_number,
            )
            changed_files = [f.get("filename", "") for f in files]
        except Exception:
            logger.debug("Could not fetch changed files for PR #%d", event.pr_number)

        # Determine requirements
        requirements, sequence = policy.get_requirements_for_pr(labels, changed_files, base_branch)

        if not requirements:
            logger.debug("No review requirements for PR #%d", event.pr_number)
            return

        # Store in registry
        req_dicts = [{"role": r.role, "count": r.count} for r in requirements]
        await self.registry.set_pr_requirements(event.pr_number, req_dicts, sequence or None)

        logger.info(
            "Set up review requirements for PR #%d: %s (sequence=%s)",
            event.pr_number,
            [r.role for r in requirements],
            sequence,
        )

    async def _handle_pr_synchronize(self, event: SquadronEvent) -> None:
        """Handle PR synchronize — invalidate approvals when PR is updated.

        When a PR is updated with new commits:
        1. Invalidate all existing approvals (require full re-review)
        2. Reset sequence state to first role only
        3. Optionally respawn reviewer agents to re-check
        """
        if not event.pr_number:
            return

        policy = self.config.review_policy
        if not policy.enabled:
            return

        sync_config = policy.on_synchronize

        if sync_config.invalidate_approvals:
            invalidated = await self.registry.invalidate_pr_approvals(event.pr_number)
            if invalidated > 0:
                logger.info(
                    "PR #%d updated — invalidated %d approvals (full re-review required)",
                    event.pr_number,
                    invalidated,
                )

                # Post a comment about invalidation
                try:
                    await self.github.comment_on_issue(
                        self.config.project.owner,
                        self.config.project.repo,
                        event.pr_number,
                        "🔄 **PR Updated** — new commits detected. "
                        f"Previous approvals ({invalidated}) have been invalidated. "
                        "Full re-review required.",
                    )
                except Exception:
                    logger.debug("Failed to post invalidation comment on PR #%d", event.pr_number)

        # Note: Respawning reviewers is handled by config triggers with action: "wake"
        # which are already registered via _register_trigger_handlers

    @staticmethod
    def _extract_issue_number(body: str) -> int | None:
        """Extract an issue number from PR body.

        Tries multiple patterns in priority order:
        1. GitHub closing keywords: "Closes #42", "Fixes #42", "Resolves #42"
        2. Explicit references: "for issue #42", "addresses #42", "relates to #42"
        3. Branch name pattern: "feat/issue-42", "fix/issue-42"
        4. Any #N reference (fallback)

        Returns the first match found, or None if no issue reference found.
        """
        import re

        if not body:
            return None

        # Priority 1: GitHub closing keywords (most explicit intent)
        match = re.search(
            r"(?:closes|fixes|resolves|close|fix|resolve)\s*:?\s*#(\d+)",
            body,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1))

        # Priority 2: Explicit issue references
        match = re.search(
            r"(?:for|addresses|relates?\s+to|implements|refs?|see)\s+(?:issue\s+)?#(\d+)",
            body,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1))

        # Priority 3: Branch name patterns in body (e.g., "Branch: feat/issue-42")
        match = re.search(r"(?:feat|fix|bug|issue)[/-](?:issue[/-])?(\d+)", body, re.IGNORECASE)
        if match:
            return int(match.group(1))

        # Priority 4: Simple "issue #N" or "issue N"
        match = re.search(r"issue\s*#?(\d+)", body, re.IGNORECASE)
        if match:
            return int(match.group(1))

        # Priority 5: Any #N at word boundary (fallback, less reliable)
        match = re.search(r"\b#(\d+)\b", body)
        if match:
            return int(match.group(1))

        return None

    # ── Command-Based Routing (Layer 2) ──────────────────────────────────

    def _get_sender_agent_role(self, event: SquadronEvent) -> str | None:
        """Determine if the comment sender is a squadron agent, and return its role.

        Uses the bot_username from config to detect bot-authored comments,
        then extracts role from the emoji + display_name signature format.
        Returns ``None`` for human senders.
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

        # Extract role from comment body — match display_name against known agents
        body = comment_data.get("body", "")

        # Try to match emoji + **Display Name** pattern at start
        for role_name, agent_def in self.agent_definitions.items():
            display_name = agent_def.display_name or role_name
            emoji = agent_def.emoji
            # Match pattern like "🎯 **Project Manager**" or just "**Project Manager**"
            pattern = rf"^{_re.escape(emoji)}?\s*\*\*{_re.escape(display_name)}\*\*"
            if _re.match(pattern, body, _re.IGNORECASE):
                return role_name

        return None

    async def _handle_command_routing(self, event: SquadronEvent) -> None:
        """Route comment events based on @squadron-dev commands.

        This is Layer 2 routing — command-based dispatch:

        1. Parse ``@squadron-dev <agent>: <message>`` or ``@squadron-dev help``
           from the comment body (already populated on ``event.command``).
        2. Handle help command: post markdown table of available agents.
        3. Handle agent command: validate agent exists and route accordingly.
        4. Apply self-loop guard: if the comment was posted by a squadron
           agent of role X, don't let it re-trigger itself.

        Comments without @squadron-dev commands are silently ignored.
        """
        if not event.command:
            logger.debug(
                "Comment on issue #%s has no @squadron-dev command — skipping",
                event.issue_number,
            )
            return

        if event.issue_number is None:
            logger.warning("Comment event has no issue_number — skipping command routing")
            return

        # Handle help command
        if event.command.is_help:
            await self._handle_help_command(event)
            return

        # Handle agent routing command
        agent_name = event.command.agent_name
        if not agent_name:
            logger.warning("Command parsed but no agent_name — skipping")
            return

        # Self-loop guard: determine which role (if any) posted this comment
        sender_role = self._get_sender_agent_role(event)
        if sender_role and sender_role == agent_name:
            logger.info(
                "Self-loop guard: skipping @squadron-dev %s command (posted by same agent)",
                agent_name,
            )
            return

        # Validate agent exists in config (routable agents only)
        role_config = self.config.agent_roles.get(agent_name)
        if not role_config:
            await self._post_unknown_agent_error(event, agent_name)
            return

        logger.info(
            "Command routing: @squadron-dev %s: on issue #%d (sender_role=%s)",
            agent_name,
            event.issue_number,
            sender_role or "human",
        )

        if role_config.is_ephemeral:
            await self._command_spawn(agent_name, role_config, event)
        else:
            await self._command_wake_or_spawn(agent_name, role_config, event)

    async def _handle_help_command(self, event: SquadronEvent) -> None:
        """Handle @squadron-dev help — post markdown table of available agents."""
        assert event.issue_number is not None

        lines = ["📋 **Available Agents**", ""]
        lines.append("| Agent | Description | Tools |")
        lines.append("|-------|-------------|-------|")

        # Only list agents that are routable (defined in config.yaml agent_roles)
        for role_name in sorted(self.config.agent_roles.keys()):
            agent_def = self.agent_definitions.get(role_name)
            if agent_def:
                description = agent_def.description or "No description"
                # Truncate long descriptions for table display
                if len(description) > 80:
                    description = description[:77] + "..."
                # Clean up multiline descriptions
                description = " ".join(description.split())
                tools = ", ".join(agent_def.tools or []) or "default"
                lines.append(f"| `{role_name}` | {description} | {tools} |")
            else:
                lines.append(f"| `{role_name}` | *(definition not found)* | — |")

        lines.append("")
        lines.append("**Usage:** `@squadron-dev <agent>: <your message>`")
        lines.append("")
        lines.append("**Example:** `@squadron-dev pm: triage this issue`")

        await self.github.comment_on_issue(
            self.config.project.owner,
            self.config.project.repo,
            event.issue_number,
            "\n".join(lines),
        )
        logger.info("Posted help response on issue #%d", event.issue_number)

    async def _post_unknown_agent_error(self, event: SquadronEvent, agent_name: str) -> None:
        """Post error message when unknown agent is requested."""
        assert event.issue_number is not None

        available = sorted(self.config.agent_roles.keys())
        available_str = ", ".join(f"`{a}`" for a in available)

        message = (
            f"❌ **Unknown agent:** `{agent_name}`\n\n"
            f"**Available agents:** {available_str}\n\n"
            f"Use `@squadron-dev help` to see agent descriptions."
        )

        await self.github.comment_on_issue(
            self.config.project.owner,
            self.config.project.repo,
            event.issue_number,
            message,
        )
        logger.info(
            "Posted unknown agent error for '%s' on issue #%d",
            agent_name,
            event.issue_number,
        )

    async def _command_spawn(
        self,
        role_name: str,
        role_config: Any,
        event: SquadronEvent,
    ) -> None:
        """Spawn an ephemeral agent via command routing."""
        # Singleton guard — if agent already active, deliver to its inbox
        if role_config.singleton:
            all_active = await self.registry.get_all_active_agents()
            active_of_role = [a for a in all_active if a.role == role_name]
            if active_of_role:
                active_agent = active_of_role[0]
                inbox = self.agent_inboxes.get(active_agent.agent_id)
                if inbox is not None:
                    await inbox.put(event)
                    logger.info(
                        "Singleton %s already active (%s) — delivered command to inbox",
                        role_name,
                        active_agent.agent_id,
                    )
                else:
                    logger.warning(
                        "Singleton %s already active (%s) but no inbox — command dropped",
                        role_name,
                        active_agent.agent_id,
                    )
                return

        assert event.issue_number is not None
        logger.info(
            "Command spawn: creating %s for issue #%d",
            role_name,
            event.issue_number,
        )
        record = await self.create_agent(role_name, event.issue_number, trigger_event=event)
        if record:
            self.last_spawn_time = datetime.now(timezone.utc).isoformat()

    async def _command_wake_or_spawn(
        self,
        role_name: str,
        role_config: Any,
        event: SquadronEvent,
    ) -> None:
        """Handle command to a persistent role: wake if sleeping, deliver if active, spawn if new."""
        assert event.issue_number is not None

        # Look for existing agents of this role for this issue
        # Use get_all_agents_for_issue to find terminal agents too (issue #13)
        existing_agents = await self.registry.get_all_agents_for_issue(event.issue_number)
        role_agents = [a for a in existing_agents if a.role == role_name]

        if role_agents:
            agent = role_agents[0]  # most recent

            if agent.status == AgentStatus.SLEEPING:
                # Wake the sleeping agent with this command as context
                wake_event = SquadronEvent(
                    event_type=SquadronEventType.WAKE_AGENT,
                    issue_number=event.issue_number,
                    pr_number=event.pr_number,
                    agent_id=agent.agent_id,
                    command=event.command,
                    data=event.data,
                )
                logger.info(
                    "Command wake: %s → waking %s on issue #%d",
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
                        "Command deliver: %s → queued event for active agent %s",
                        role_name,
                        agent.agent_id,
                    )
                else:
                    logger.warning(
                        "Command deliver: %s → agent %s is ACTIVE but has no inbox",
                        role_name,
                        agent.agent_id,
                    )
            else:
                logger.debug(
                    "Command: %s → agent %s is in terminal state %s — spawning new",
                    role_name,
                    agent.agent_id,
                    agent.status,
                )
                await self._command_spawn_persistent(role_name, event)
        else:
            # No agent exists for this role + issue → spawn
            await self._command_spawn_persistent(role_name, event)

    async def _command_spawn_persistent(
        self,
        role_name: str,
        event: SquadronEvent,
    ) -> None:
        """Spawn a new persistent agent via command routing."""
        assert event.issue_number is not None
        logger.info(
            "Command spawn (persistent): creating %s for issue #%d",
            role_name,
            event.issue_number,
        )
        record = await self.create_agent(role_name, event.issue_number, trigger_event=event)
        if record:
            self.last_spawn_time = datetime.now(timezone.utc).isoformat()

    # ── Helpers ──────────────────────────────────────────────────────────

    def _agent_signature(self, role: str) -> str:
        """Build the agent signature prefix: emoji + display_name on its own line.

        Format: "🎯 **Project Manager**\n\n"
        Falls back to "🤖 **role**\n\n" if agent definition not found.
        """
        agent_def = self.agent_definitions.get(role)
        if agent_def:
            emoji = agent_def.emoji
            display_name = agent_def.display_name or role
        else:
            emoji = "🤖"
            display_name = role
        return f"{emoji} **{display_name}**\n\n"

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

    async def _run_git_in(
        self, cwd: Path, *args: str, timeout: int = 60, auth: bool = False
    ) -> tuple[int, str, str]:
        """Run a git command in a specific directory (e.g. inside a worktree).

        Args:
            cwd: Working directory for the git command
            args: Git command arguments
            timeout: Command timeout in seconds
            auth: If True, inject GitHub App token for authenticated operations (push, fetch)
        """
        env = None
        if auth:
            # Get fresh token and set up credential helper via environment
            env = await self._git_auth_env()

        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            env=env,
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

    async def _git_auth_env(self) -> dict[str, str]:
        """Build environment dict with GitHub App token for git authentication.

        Uses GIT_ASKPASS with a simple echo script that returns the token as password.
        This allows git push/fetch to authenticate without modifying the remote URL.
        """
        import os

        # Get fresh installation token from GitHubClient
        token = await self.github._ensure_token()

        # Copy current environment and add git auth
        env = os.environ.copy()

        # Disable interactive prompts
        env["GIT_TERMINAL_PROMPT"] = "0"

        # Use credential helper that reads from environment
        # This is cleaner than GIT_ASKPASS for our use case
        env["GIT_CONFIG_COUNT"] = "2"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""  # Clear any existing helpers
        env["GIT_CONFIG_KEY_1"] = "credential.helper"
        env["GIT_CONFIG_VALUE_1"] = (
            f"!f() {{ echo username=x-access-token; echo password={token}; }}; f"
        )

        return env

    async def _git_push_for_agent(
        self, agent: AgentRecord, force: bool = False
    ) -> tuple[int, str, str]:
        """Push an agent's branch to the remote using GitHub App authentication.

        This is the callback for the git_push tool. It runs git push with
        authentication injected via environment variables, ensuring the
        token is never exposed to the agent's bash environment.

        Args:
            agent: The agent record (must have worktree_path and branch set).
            force: If True, use --force-with-lease for force push.

        Returns:
            Tuple of (returncode, stdout, stderr) from the git command.
        """
        if not agent.worktree_path or not agent.branch:
            return (1, "", "No worktree or branch configured")

        worktree = Path(agent.worktree_path)
        if not worktree.exists():
            return (1, "", f"Worktree does not exist: {worktree}")

        args = ["push", "origin", agent.branch]
        if force:
            args.insert(1, "--force-with-lease")

        return await self._run_git_in(worktree, *args, timeout=120, auth=True)

    # ── Auto-Merge System ─────────────────────────────────────────────────

    async def _auto_merge_pr(self, pr_number: int) -> None:
        """Attempt to merge a PR after all required approvals are in place.

        This is the callback for the auto-merge system. It:
        1. Verifies all approvals are still valid
        2. Optionally waits for CI to pass (if configured)
        3. Merges the PR using the configured method
        4. Handles failures via YAML-configured handlers
        5. Deletes the branch after merge (if configured)

        Args:
            pr_number: The PR number to merge.
        """
        import httpx

        policy = self.config.review_policy
        if not policy.enabled or not policy.auto_merge.enabled:
            logger.info("Auto-merge disabled — skipping PR #%d", pr_number)
            return

        owner = self.config.project.owner
        repo = self.config.project.repo

        # Double-check merge readiness (approvals could have changed)
        is_ready, missing = await self.registry.check_pr_merge_ready(pr_number)
        if not is_ready:
            logger.warning("PR #%d not ready for merge: %s", pr_number, missing)
            return

        # Get PR details for branch info
        try:
            pr_data = await self.github.get_pull_request(owner, repo, pr_number)
        except Exception:
            logger.exception("Failed to get PR #%d details", pr_number)
            return

        head_branch = pr_data.get("head", {}).get("ref", "")
        pr_title = pr_data.get("title", f"PR #{pr_number}")

        # Optionally check CI status
        if policy.auto_merge.require_ci_pass:
            try:
                sha = pr_data.get("head", {}).get("sha", "")
                if sha:
                    status = await self.github.get_combined_status(owner, repo, sha)
                    state = status.get("state", "unknown")
                    if state == "failure":
                        logger.warning(
                            "PR #%d CI failed — invoking on_ci_failed handler", pr_number
                        )
                        await self._handle_merge_failure(
                            pr_number,
                            "ci_failed",
                            policy.auto_merge.on_ci_failed,
                            pr_data,
                        )
                        return
                    elif state == "pending":
                        logger.info("PR #%d CI still pending — will retry later", pr_number)
                        # TODO: schedule a retry instead of just returning
                        return
            except Exception:
                logger.warning("Failed to check CI status for PR #%d", pr_number, exc_info=True)

        # Attempt merge
        logger.info(
            "AUTO-MERGE — attempting to merge PR #%d (%s) via %s",
            pr_number,
            pr_title,
            policy.auto_merge.method,
        )

        try:
            await self.github.merge_pull_request(
                owner,
                repo,
                pr_number,
                merge_method=policy.auto_merge.method,
                commit_title=f"{pr_title} (#{pr_number})",
            )
            logger.info("AUTO-MERGE SUCCESS — PR #%d merged", pr_number)

            # Delete branch if configured
            if policy.auto_merge.delete_branch and head_branch:
                try:
                    await self.github.delete_branch(owner, repo, head_branch)
                    logger.info("Deleted branch %s after merge", head_branch)
                except Exception:
                    logger.warning("Failed to delete branch %s", head_branch, exc_info=True)

            # Clean up PR tracking data
            await self.registry.cleanup_pr_data(pr_number)

            # Post merge comment
            try:
                issue_number = self._extract_issue_number(pr_data.get("body", "") or "")
                if issue_number:
                    await self.github.comment_on_issue(
                        owner,
                        repo,
                        issue_number,
                        f"🎉 **Auto-merged** — PR #{pr_number} has been merged to "
                        f"`{pr_data.get('base', {}).get('ref', 'main')}`.",
                    )
            except Exception:
                logger.debug("Failed to post merge comment")

        except httpx.HTTPStatusError as e:
            error_body = e.response.text
            logger.warning(
                "AUTO-MERGE FAILED — PR #%d: %s %s",
                pr_number,
                e.response.status_code,
                error_body[:200],
            )

            # Determine failure type and invoke appropriate handler
            if "merge conflict" in error_body.lower() or e.response.status_code == 409:
                await self._handle_merge_failure(
                    pr_number,
                    "merge_conflict",
                    policy.auto_merge.on_merge_conflict,
                    pr_data,
                )
            else:
                await self._handle_merge_failure(
                    pr_number,
                    "unknown_error",
                    policy.auto_merge.on_unknown_error,
                    pr_data,
                    error_message=error_body[:500],
                )

        except Exception as e:
            logger.exception("AUTO-MERGE ERROR — PR #%d", pr_number)
            await self._handle_merge_failure(
                pr_number,
                "unknown_error",
                policy.auto_merge.on_unknown_error,
                pr_data,
                error_message=str(e),
            )

    async def _handle_merge_failure(
        self,
        pr_number: int,
        failure_type: str,
        handler: "FailureAction",
        pr_data: dict,
        error_message: str = "",
    ) -> None:
        """Handle a merge failure according to the configured action.

        Actions:
        - spawn: Spawn an agent to fix the issue (e.g. merge-conflict agent)
        - notify: Post a comment mentioning the configured human group
        - escalate: Add escalation labels and notify maintainers
        """

        owner = self.config.project.owner
        repo = self.config.project.repo

        logger.info(
            "Handling %s for PR #%d: action=%s, target=%s",
            failure_type,
            pr_number,
            handler.action,
            handler.target,
        )

        if handler.action == "spawn":
            # Spawn an agent to handle the failure
            role = handler.target
            if role not in self.config.agent_roles:
                logger.error(
                    "Cannot spawn %s for %s — role not configured",
                    role,
                    failure_type,
                )
                # Fall back to notify if spawn target doesn't exist
                if handler.fallback:
                    await self._handle_merge_failure(
                        pr_number, failure_type, handler.fallback, pr_data, error_message
                    )
                return

            # Extract issue number from PR
            issue_number = self._extract_issue_number(pr_data.get("body", "") or "")
            if not issue_number:
                issue_number = pr_number  # Use PR number as fallback

            # Create a synthetic event for the agent
            event = SquadronEvent(
                event_type=SquadronEventType.PR_SYNCHRONIZED,
                pr_number=pr_number,
                issue_number=issue_number,
                data={
                    "payload": {"pull_request": pr_data},
                    "failure_type": failure_type,
                    "error_message": error_message,
                },
            )

            await self.create_agent(role, issue_number, trigger_event=event)
            logger.info("Spawned %s agent to handle %s on PR #%d", role, failure_type, pr_number)

        elif handler.action == "notify":
            # Post a comment mentioning the configured group
            group_name = handler.target
            mentions = self._resolve_human_group(group_name)

            message_parts = [
                f"⚠️ **Merge Failed** — PR #{pr_number} could not be auto-merged.",
                f"**Reason:** {failure_type.replace('_', ' ').title()}",
            ]
            if error_message:
                message_parts.append(f"```\n{error_message[:500]}\n```")
            message_parts.append(f"\n{mentions} — please investigate and resolve.")

            await self.github.comment_on_issue(
                owner,
                repo,
                pr_number,
                "\n".join(message_parts),
            )
            logger.info("Posted merge failure notification for PR #%d", pr_number)

        elif handler.action == "escalate":
            # Add escalation labels and notify
            group_name = handler.target
            mentions = self._resolve_human_group(group_name)

            try:
                await self.github.add_labels(
                    owner,
                    repo,
                    pr_number,
                    self.config.escalation.escalation_labels,
                )
            except Exception:
                logger.warning("Failed to add escalation labels to PR #%d", pr_number)

            message_parts = [
                f"🚨 **Escalation** — PR #{pr_number} requires human intervention.",
                f"**Failure:** {failure_type.replace('_', ' ').title()}",
            ]
            if error_message:
                message_parts.append(f"```\n{error_message[:500]}\n```")
            message_parts.append(f"\n{mentions}")

            await self.github.comment_on_issue(
                owner,
                repo,
                pr_number,
                "\n".join(message_parts),
            )
            logger.info("Escalated PR #%d for human intervention", pr_number)

        # Try fallback if primary action might have failed
        if handler.fallback:
            logger.debug("Fallback handler available but not needed")

    def _resolve_human_group(self, group_name: str) -> str:
        """Resolve a human group name to @mentions.

        Looks up the group in config.human_groups. If not found,
        returns @group_name as a team mention.
        """
        human_config = self.config.human_invocation
        group_members = self.config.human_groups.get(group_name, [])

        if group_members:
            # Mention each member
            mentions = [human_config.mention_format.format(username=u) for u in group_members]
            return " ".join(mentions)
        else:
            # Assume it's a team name
            return human_config.mention_format.format(username=group_name)
