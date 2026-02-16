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
from squadron.tools.framework import FrameworkTools
from squadron.tools.pm_tools import PMTools

if TYPE_CHECKING:
    from squadron.config import AgentDefinition, CircuitBreakerDefaults, SquadronConfig
    from squadron.event_router import EventRouter
    from squadron.github_client import GitHubClient
    from squadron.registry import AgentRegistry

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

        # Framework tools bridge (agent ↔ framework)
        self._framework_tools = FrameworkTools(
            registry=registry,
            github=github,
            agent_inboxes=self.agent_inboxes,
            owner=config.project.owner,
            repo=config.project.repo,
        )

        # PM-specific tools (issue CRUD, registry queries)
        self._pm_tools = PMTools(
            registry=registry,
            github=github,
            owner=config.project.owner,
            repo=config.project.repo,
        )

        # Per-agent CopilotAgent instances (one CLI subprocess each)
        self._copilot_agents: dict[str, CopilotAgent] = {}

        # Track active agent tasks
        self._agent_tasks: dict[str, asyncio.Task] = {}

        # PM CopilotAgent (fresh sessions per batch)
        self._pm_copilot: CopilotAgent | None = None

        # PM processing task
        self._pm_task: asyncio.Task | None = None
        self._running = False

        # Agent concurrency limiter
        max_concurrent = config.runtime.max_concurrent_agents
        self._agent_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None
        )

    async def start(self) -> None:
        """Start the agent manager — begin PM consumer loop."""
        self._running = True

        # Start PM CopilotAgent (always running, creates fresh sessions per batch)
        self._pm_copilot = CopilotAgent(
            runtime_config=self.config.runtime,
            working_directory=str(self.repo_root),
        )
        await self._pm_copilot.start()

        self._pm_task = asyncio.create_task(self._pm_consumer_loop(), name="pm-consumer")

        # Register event handlers
        self.router.on(SquadronEventType.ISSUE_ASSIGNED, self._handle_issue_assigned)
        self.router.on(SquadronEventType.ISSUE_LABELED, self._handle_issue_labeled)
        self.router.on(SquadronEventType.ISSUE_CLOSED, self._handle_issue_closed)
        self.router.on(SquadronEventType.PR_OPENED, self._handle_pr_opened)
        self.router.on(SquadronEventType.PR_CLOSED, self._handle_pr_closed)
        self.router.on(SquadronEventType.PR_REVIEW_SUBMITTED, self._handle_pr_review_received)
        self.router.on(SquadronEventType.PR_SYNCHRONIZED, self._handle_pr_updated)

        logger.info("Agent manager started")

    async def stop(self) -> None:
        """Stop all agents gracefully."""
        self._running = False

        # Cancel PM consumer
        if self._pm_task:
            self._pm_task.cancel()
            try:
                await self._pm_task
            except asyncio.CancelledError:
                pass

        # Stop all running agent tasks
        for agent_id, task in self._agent_tasks.items():
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

        # Stop all CopilotAgent instances (CLI subprocesses)
        for agent_id, copilot in self._copilot_agents.items():
            await copilot.stop()
        self._copilot_agents.clear()

        if self._pm_copilot:
            await self._pm_copilot.stop()

        self._agent_tasks.clear()
        logger.info("Agent manager stopped")

    # ── PM Consumer ──────────────────────────────────────────────────────

    async def _pm_consumer_loop(self) -> None:
        """Consume events from the PM queue and invoke PM agent.

        Batches events with a short delay for related events.
        """
        while self._running:
            try:
                # Wait for first event
                event = await asyncio.wait_for(self.router.pm_queue.get(), timeout=1.0)
                batch = [event]

                # Collect more events that arrive within 2 seconds (batching window)
                try:
                    while True:
                        event = await asyncio.wait_for(self.router.pm_queue.get(), timeout=2.0)
                        batch.append(event)
                except asyncio.TimeoutError:
                    pass

                logger.info("PM processing batch of %d events", len(batch))
                await self._invoke_pm(batch)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in PM consumer loop")
                await asyncio.sleep(5)  # Back off on error

    async def _invoke_pm(self, events: list[SquadronEvent]) -> None:
        """Invoke the PM agent with a batch of events.

        Creates a fresh session per batch (AD-017 — PM is stateless).
        """
        pm_def = self.agent_definitions.get("pm")
        if not pm_def:
            logger.error("PM agent definition not found")
            return

        # Build context for PM
        active_agents = await self.registry.get_all_active_agents()
        context = self._build_pm_context(events, active_agents)

        logger.info(
            "Invoking PM agent with context (%d events, %d active agents)",
            len(events),
            len(active_agents),
        )

        await self._run_pm_session(pm_def, context, events)

    def _build_pm_context(
        self,
        events: list[SquadronEvent],
        active_agents: list[AgentRecord],
    ) -> dict:
        """Build the context dictionary injected into each PM session."""
        return {
            "project_name": self.config.project.name,
            "label_taxonomy": {
                "types": self.config.labels.types,
                "priorities": self.config.labels.priorities,
                "states": self.config.labels.states,
            },
            "agent_roles": list(self.config.agent_roles.keys()),
            "active_agents": [
                {
                    "agent_id": a.agent_id,
                    "role": a.role,
                    "issue": a.issue_number,
                    "status": a.status.value,
                    "blocked_by": a.blocked_by,
                }
                for a in active_agents
            ],
            "events": [
                {
                    "type": e.event_type.value,
                    "issue": e.issue_number,
                    "pr": e.pr_number,
                    "data": {
                        "action": e.data.get("action"),
                        "sender": e.data.get("sender"),
                    },
                }
                for e in events
            ],
            "human_groups": self.config.human_groups,
            "escalation": {
                "default_notify": self.config.escalation.default_notify,
                "max_issue_depth": self.config.escalation.max_issue_depth,
            },
        }

    def _format_pm_prompt(self, events: list[SquadronEvent], context: dict) -> str:
        """Format the user prompt for a PM session with injected context."""
        lines = ["## Current Project State\n"]
        lines.append(f"Project: {context['project_name']}")
        lines.append(f"Active agents: {len(context['active_agents'])}")
        if context["active_agents"]:
            lines.append("\n### Active Agents")
            for a in context["active_agents"]:
                blocked = (
                    f" (blocked by #{', #'.join(str(b) for b in a['blocked_by'])})"
                    if a["blocked_by"]
                    else ""
                )
                lines.append(f"- {a['agent_id']}: {a['status']}{blocked}")

        lines.append("\n## New Events\n")
        for i, evt in enumerate(context["events"]):
            lines.append(f"- **{evt['type']}** issue=#{evt['issue']} pr=#{evt['pr']}")
            if i < len(events):
                payload = events[i].data.get("payload", {})
                issue_data = payload.get("issue", {})
                if issue_data:
                    lines.append(f"  Title: {issue_data.get('title', 'N/A')}")
                    labels = [lbl.get("name", "") for lbl in issue_data.get("labels", [])]
                    if labels:
                        lines.append(f"  Labels: {', '.join(labels)}")
                    body = issue_data.get("body", "")
                    if body:
                        lines.append(f"  Body: {body[:500]}")
                pr_data = payload.get("pull_request", {})
                if pr_data:
                    lines.append(f"  PR Title: {pr_data.get('title', 'N/A')}")

        lines.append("\n## Instructions")
        lines.append("Analyze these events and take appropriate action using your available tools.")
        lines.append(
            "For new issues: triage, classify with labels (type + priority), and post a triage comment."
        )
        lines.append(
            "IMPORTANT: Applying a type label (feature, bug, etc.) automatically spawns the appropriate agent — do NOT try to assign issues to bots."
        )
        lines.append("For closed issues: check if any blocked agents should be unblocked.")
        lines.append("For PR events: coordinate review assignments.")

        return "\n".join(lines)

    async def _run_pm_session(
        self,
        pm_def: AgentDefinition,
        context: dict,
        events: list[SquadronEvent],
    ) -> None:
        """Run a fresh PM session for a batch of events.

        PM is stateless — each batch gets a new session that is
        destroyed after processing (AD-017).
        """
        import time

        batch_id = f"batch-{int(time.time())}"
        session_id = f"squadron-pm-{batch_id}"

        # Build custom_agents list from PM's subagent references
        custom_agents = self._build_custom_agents(pm_def)
        # Build MCP servers dict from agent definition
        mcp_servers = self._build_mcp_servers(pm_def)

        session_config = build_session_config(
            role="pm",
            issue_number=None,
            system_message=pm_def.prompt or pm_def.raw_content,
            working_directory=str(self.repo_root),
            runtime_config=self.config.runtime,
            session_id_override=session_id,
            tools=self._pm_tools.get_tools(),
            custom_agents=custom_agents,
            mcp_servers=mcp_servers,
        )

        if not self._pm_copilot:
            logger.error("PM CopilotAgent not started")
            return

        session = await self._pm_copilot.create_session(session_config)

        try:
            # Build the prompt with injected context
            prompt = self._format_pm_prompt(events, context)
            logger.info(
                "PM SESSION [%s] — %d events, %d chars prompt, model=%s",
                session_id,
                len(events),
                len(prompt),
                session_config.get("model", "default"),
            )
            result = await session.send_and_wait({"prompt": prompt})
            logger.info(
                "PM SESSION [%s] completed — result=%s",
                session_id,
                result.type.value if result else "no response",
            )
        except Exception:
            logger.exception("PM session %s failed", session_id)
        finally:
            # PM sessions are stateless — destroy after use
            await session.destroy()
            await self._pm_copilot.delete_session(session_id)

    # ── Agent Creation ───────────────────────────────────────────────────

    async def create_agent(
        self,
        role: str,
        issue_number: int,
        trigger_event: SquadronEvent | None = None,
    ) -> AgentRecord:
        """Create a new agent for an issue.

        1. Create agent record in registry
        2. Create git worktree for branch isolation
        3. Start agent session
        """
        agent_id = f"{role}-issue-{issue_number}"

        # Check for existing agent on this issue
        existing = await self.registry.get_agent_by_issue(issue_number)
        if existing:
            logger.warning(
                "Agent already exists for issue #%d: %s", issue_number, existing.agent_id
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

        # Determine branch name
        branch = self._branch_name(role, issue_number)

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

        # Create git worktree
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

        logger.info("Created agent %s on branch %s", agent_id, branch)
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

        logger.info("Woke agent %s (trigger: %s)", agent_id, trigger_event.event_type)

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
        """
        agent_def = self.agent_definitions.get(record.role)
        if not agent_def:
            logger.error("No agent definition for role: %s", record.role)
            return

        copilot = self._copilot_agents.get(record.agent_id)
        if not copilot:
            logger.error("No CopilotAgent instance for: %s", record.agent_id)
            return

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

        session_config = build_session_config(
            role=record.role,
            issue_number=record.issue_number,
            system_message=system_message,
            working_directory=str(record.worktree_path or self.repo_root),
            runtime_config=self.config.runtime,
            tools=self._framework_tools.get_tools_for_agent(record.agent_id),
            hooks=hooks,
            custom_agents=custom_agents,
            mcp_servers=mcp_servers,
        )

        try:
            if resume:
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
                    tools=self._framework_tools.get_tools_for_agent(record.agent_id),
                    hooks=hooks,
                    custom_agents=custom_agents,
                    mcp_servers=mcp_servers,
                )
                session = await copilot.resume_session(record.session_id, resume_config)
                prompt = self._build_wake_prompt(record, trigger_event)
            else:
                logger.info(
                    "AGENT START — %s (issue=#%d, branch=%s, session=%s)",
                    record.agent_id,
                    record.issue_number,
                    record.branch,
                    record.session_id,
                )
                session = await copilot.create_session(session_config)
                prompt = self._build_agent_prompt(record, trigger_event)

            # Layer 2 circuit breaker: timeout around send_and_wait
            try:
                result = await asyncio.wait_for(
                    session.send_and_wait({"prompt": prompt}),
                    timeout=max_duration,
                )
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

            logger.info(
                "AGENT [%s] completed turn — result=%s",
                record.agent_id,
                result.type.value if result else "no response",
            )

            # ── Post-turn state machine ──────────────────────────────────
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
        self.agent_inboxes.pop(agent_id, None)

        # Remove git worktree (if any)
        agent_record = await self.registry.get_agent(agent_id)
        if agent_record and agent_record.worktree_path:
            worktree = Path(agent_record.worktree_path)
            if worktree.exists():
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
        """Build the initial prompt for a new agent session."""
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
        lines.append("\nBegin working on this issue. Use the available tools to read code,")
        lines.append("make changes, run tests, and report progress.")
        lines.append("Call `check_for_events` periodically to check for new instructions.")
        lines.append("Call `report_complete` when finished, or `report_blocked` if stuck.")

        return "\n".join(lines)

    def _build_wake_prompt(
        self,
        record: AgentRecord,
        trigger_event: SquadronEvent | None,
    ) -> str:
        """Build the wake-up prompt for a resumed session."""
        lines = [f"## Session Resumed: {record.agent_id}\n"]
        lines.append(
            "Your session has been resumed. Here's what happened while you were sleeping:\n"
        )

        if trigger_event:
            lines.append(f"**Trigger:** {trigger_event.event_type.value}")
            if trigger_event.issue_number:
                lines.append(f"**Issue:** #{trigger_event.issue_number}")
            if trigger_event.pr_number:
                lines.append(f"**PR:** #{trigger_event.pr_number}")

            payload = trigger_event.data.get("payload", {})
            # Include review comments if this is a PR review
            review = payload.get("review", {})
            if review:
                lines.append(f"\n**Review state:** {review.get('state', 'N/A')}")
                review_body = review.get("body", "")
                if review_body:
                    lines.append(f"**Review comment:** {review_body}")

            # Include resolved blocker info
            resolved = trigger_event.data.get("resolved_issue")
            if resolved:
                lines.append(f"\n**Resolved blocker:** Issue #{resolved} has been closed.")

        lines.append(
            "\nContinue your work. Check for any additional events with `check_for_events`."
        )

        return "\n".join(lines)

    # ── Event Handlers ───────────────────────────────────────────────────

    async def _handle_issue_assigned(self, event: SquadronEvent) -> None:
        """Handle issue assignment — create an agent if assigned to a bot role."""
        payload = event.data.get("payload", {})
        assignee = payload.get("assignee", {})
        issue = payload.get("issue", {})

        if not assignee or not issue:
            return

        # Check if assigned to an agent role (via labels)
        labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
        role = self._label_to_role(labels)

        if role:
            await self.create_agent(role, issue["number"], trigger_event=event)

    async def _handle_issue_labeled(self, event: SquadronEvent) -> None:
        """Handle issue labeled — spawn agent if label maps to an agent role.

        This is the primary agent spawn trigger. When the PM labels an issue
        with a type label (e.g. 'feature', 'bug'), and that label maps to an
        agent role via assignable_labels in config, we spawn the agent.
        """
        payload = event.data.get("payload", {})
        issue = payload.get("issue", {})
        label = payload.get("label", {})

        if not issue or not label:
            return

        label_name = label.get("name", "")
        issue_number = issue.get("number")
        if not label_name or not issue_number:
            return

        # Check if this specific label maps to an agent role
        for role_name, role_config in self.config.agent_roles.items():
            if role_config.assignable_labels and label_name in role_config.assignable_labels:
                # Don't spawn if an agent already exists for this issue+role
                existing = await self.registry.get_agents_for_issue(issue_number)
                if any(a.role == role_name for a in existing):
                    logger.info(
                        "Agent %s already exists for issue #%d — skipping",
                        role_name,
                        issue_number,
                    )
                    return

                logger.info(
                    "Label '%s' on issue #%d → spawning %s agent",
                    label_name,
                    issue_number,
                    role_name,
                )
                await self.create_agent(role_name, issue_number, trigger_event=event)
                return  # Only spawn one agent per label event

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

    async def _handle_pr_opened(self, event: SquadronEvent) -> None:
        """Handle PR opened — trigger review agents via approval flow.

        1. Extract PR labels and changed files
        2. Match against approval flow rules
        3. Create a review agent for each matched reviewer role

        NOTE: If a workflow engine already triggered a pipeline for this PR,
        the workflow-spawned agents handle reviews — approval flow is skipped
        for that PR automatically (workflow engine prevents duplicates).
        """
        if event.pr_number is None:
            return

        if not self.config.approval_flows.enabled:
            logger.info("Approval flows disabled — skipping PR #%d", event.pr_number)
            return

        payload = event.data.get("payload", {})
        pr_data = payload.get("pull_request", {})
        labels = [lbl.get("name", "") for lbl in pr_data.get("labels", [])]

        # Determine which reviewer roles to spawn
        reviewer_roles = self.config.approval_flows.get_reviewers_for_pr(labels)

        if not reviewer_roles:
            logger.info("PR #%d — no approval flow rules matched", event.pr_number)
            return

        logger.info(
            "PR #%d — approval flow matched %d reviewer roles: %s",
            event.pr_number,
            len(reviewer_roles),
            ", ".join(reviewer_roles),
        )

        # Find the source issue (if the dev agent linked the PR to an issue)
        source_issue = pr_data.get("body", "") or ""
        issue_number = self._extract_issue_number(source_issue) or event.pr_number

        for role_name in reviewer_roles:
            # Verify the role exists in config and is review-capable
            role_config = self.config.agent_roles.get(role_name)
            if not role_config:
                logger.warning("Review role %s not found in config — skipping", role_name)
                continue

            role = role_name

            # Create a review agent for this PR
            agent_id = f"{role_name}-pr-{event.pr_number}"
            existing = await self.registry.get_agent(agent_id)
            if existing:
                logger.info("Review agent %s already exists — skipping", agent_id)
                continue

            record = AgentRecord(
                agent_id=agent_id,
                role=role,
                issue_number=issue_number,
                pr_number=event.pr_number,
                session_id=f"squadron-{agent_id}",
                status=AgentStatus.ACTIVE,
                active_since=datetime.now(timezone.utc),
                branch=pr_data.get("head", {}).get("ref", "unknown"),
            )
            await self.registry.create_agent(record)

            # Create inbox
            self.agent_inboxes[agent_id] = asyncio.Queue()

            # Create CopilotAgent with repo root (reviewers don't need worktrees)
            copilot = CopilotAgent(
                runtime_config=self.config.runtime,
                working_directory=str(self.repo_root),
            )
            await copilot.start()
            self._copilot_agents[agent_id] = copilot

            # Start review agent task
            review_event = SquadronEvent(
                event_type=SquadronEventType.PR_OPENED,
                pr_number=event.pr_number,
                issue_number=issue_number,
                data=event.data,
            )
            agent_task = asyncio.create_task(
                self._run_agent(record, review_event),
                name=f"agent-{agent_id}",
            )
            self._agent_tasks[agent_id] = agent_task

            logger.info(
                "Created review agent %s for PR #%d",
                agent_id,
                event.pr_number,
            )

    @staticmethod
    def _extract_issue_number(body: str) -> int | None:
        """Extract an issue number from PR body (e.g., 'Closes #42')."""
        import re

        match = re.search(r"(?:closes|fixes|resolves)\s+#(\d+)", body, re.IGNORECASE)
        return int(match.group(1)) if match else None

    async def _handle_pr_closed(self, event: SquadronEvent) -> None:
        """Handle PR closed/merged — complete the dev agent and clean up review agents.

        When a PR is merged:
        - The dev agent that opened it is marked COMPLETED
        - Any review agents for this PR are marked COMPLETED
        When a PR is closed without merge:
        - The dev agent is woken to reassess
        """
        if event.pr_number is None:
            return

        payload = event.data.get("payload", {})
        pr_data = payload.get("pull_request", {})
        merged = pr_data.get("merged", False)

        # Find all agents associated with this PR
        all_agents = await self.registry.get_all_active_agents()
        for agent in all_agents:
            if agent.pr_number != event.pr_number:
                continue

            if merged:
                # PR merged — mark all associated agents as COMPLETED
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

                if agent.issue_number:
                    try:
                        await self.github.comment_on_issue(
                            self.config.project.owner,
                            self.config.project.repo,
                            agent.issue_number,
                            f"**[squadron:{agent.role}]** PR #{event.pr_number} merged. Task complete.",
                        )
                    except Exception:
                        logger.debug("Failed to post merge comment for agent %s", agent.agent_id)

                logger.info("AGENT COMPLETED (PR merged) — %s", agent.agent_id)
            else:
                # PR closed without merge — wake dev agent to reassess
                if agent.role in ("feat-dev", "bug-fix"):
                    if agent.status == AgentStatus.SLEEPING:
                        wake_event = SquadronEvent(
                            event_type=SquadronEventType.WAKE_AGENT,
                            pr_number=event.pr_number,
                            agent_id=agent.agent_id,
                            data={"reason": "PR closed without merge", **event.data},
                        )
                        await self.wake_agent(agent.agent_id, wake_event)
                elif agent.role in ("pr-review", "security-review"):
                    # Review agents are no longer needed
                    agent.status = AgentStatus.COMPLETED
                    await self.registry.update_agent(agent)
                    copilot = self._copilot_agents.get(agent.agent_id)
                    await self._cleanup_agent(
                        agent.agent_id,
                        destroy_session=True,
                        copilot=copilot,
                        session_id=agent.session_id,
                    )

    async def _handle_pr_review_received(self, event: SquadronEvent) -> None:
        """Handle PR review — deliver to dev agent if changes requested."""
        if event.pr_number is None:
            return

        # Find the dev agent who opened this PR
        agents = await self.registry.get_all_active_agents()
        for agent in agents:
            if agent.pr_number == event.pr_number and agent.status == AgentStatus.SLEEPING:
                review_event = SquadronEvent(
                    event_type=SquadronEventType.WAKE_AGENT,
                    pr_number=event.pr_number,
                    agent_id=agent.agent_id,
                    data=event.data,
                )
                await self.wake_agent(agent.agent_id, review_event)
                break

    async def _handle_pr_updated(self, event: SquadronEvent) -> None:
        """Handle PR synchronize (new commits pushed) — wake review agents."""
        if event.pr_number is None:
            return

        agents = await self.registry.get_all_active_agents()
        for agent in agents:
            if (
                agent.pr_number == event.pr_number
                and agent.role in ("pr-review", "security-review")
                and agent.status == AgentStatus.SLEEPING
            ):
                wake_event = SquadronEvent(
                    event_type=SquadronEventType.WAKE_AGENT,
                    pr_number=event.pr_number,
                    agent_id=agent.agent_id,
                    data=event.data,
                )
                await self.wake_agent(agent.agent_id, wake_event)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _branch_name(self, role: str, issue_number: int) -> str:
        """Generate a branch name from config templates."""
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

    def _label_to_role(self, labels: list[str]) -> str | None:
        """Map issue labels to an agent role using config."""
        for role_name, role_config in self.config.agent_roles.items():
            if role_config.assignable_labels:
                for label in labels:
                    if label in role_config.assignable_labels:
                        return role_name
        return None

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
