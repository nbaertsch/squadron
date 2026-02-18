"""Reconciliation Loop — periodic background task for state consistency.

Runs every N minutes (configurable, default 5) to:
1. Check SLEEPING agents whose blockers may have been resolved (missed webhook)
2. Detect stale ACTIVE agents exceeding max duration (circuit breaker)
3. Cross-check registry state vs GitHub issue/PR state

This is the safety net for missed webhooks (EC-008) and circuit breaker
enforcement layer 3 (AD-018).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from squadron.models import AgentStatus

if TYPE_CHECKING:
    from squadron.config import SquadronConfig
    from squadron.github_client import GitHubClient
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)


class ReconciliationLoop:
    """Periodic background reconciliation task."""

    def __init__(
        self,
        config: SquadronConfig,
        registry: AgentRegistry,
        github: GitHubClient,
        owner: str = "",
        repo: str = "",
        on_wake_agent: Any = None,  # Callable[[str, SquadronEvent], Awaitable[None]]
        on_complete_agent: Any = None,  # Callable[[str], Awaitable[None]]
    ):
        self.config = config
        self.registry = registry
        self.github = github
        self.owner = owner
        self.repo = repo
        self._on_wake_agent = on_wake_agent
        self._on_complete_agent = on_complete_agent

        self.interval = config.runtime.reconciliation_interval
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="reconciliation")
        logger.info("Reconciliation loop started (interval=%ds)", self.interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Reconciliation loop stopped")

    async def _loop(self) -> None:
        """Main reconciliation loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                await self.reconcile()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Reconciliation error")

    async def reconcile(self) -> None:
        """Run one reconciliation pass."""
        logger.debug("Reconciliation pass starting")

        await self._check_sleeping_agents()
        await self._check_stale_active_agents()
        await self._check_orphaned_agents()

        # Prune old webhook dedup entries (keep 72h)
        try:
            pruned = await self.registry.prune_old_events()
            if pruned:
                logger.info("Pruned %d old seen_events entries", pruned)
        except Exception:
            logger.debug("Failed to prune seen_events")

        logger.debug("Reconciliation pass complete")

    async def _check_sleeping_agents(self) -> None:
        """Check if any SLEEPING agents' blockers have been resolved.

        This catches missed webhooks (EC-008). If a blocker issue was
        closed while our webhook server was down, the reconciliation
        loop detects it here.
        """
        sleeping = await self.registry.get_agents_by_status(AgentStatus.SLEEPING)

        for agent in sleeping:
            if not agent.blocked_by:
                # Agent is sleeping with no blockers — might need waking
                # (e.g., waiting for PR review, but state inconsistency)
                logger.debug("Agent %s sleeping with no blockers", agent.agent_id)
                continue

            # Check max sleep duration (circuit breaker)
            if agent.sleeping_since:
                limits = self.config.circuit_breakers.for_role(agent.role)
                sleep_seconds = (datetime.now(timezone.utc) - agent.sleeping_since).total_seconds()

                if sleep_seconds > limits.max_sleep_duration:
                    logger.warning(
                        "Agent %s exceeded max sleep duration (%ds > %ds) — escalating",
                        agent.agent_id,
                        sleep_seconds,
                        limits.max_sleep_duration,
                    )
                    agent.status = AgentStatus.ESCALATED
                    await self.registry.update_agent(agent)
                    continue

            # Query GitHub to check if blocker issues have been closed
            # (catches webhooks missed while server was down — EC-008)
            for blocker_issue in list(agent.blocked_by):
                try:
                    issue_data = await self.github.get_issue(self.owner, self.repo, blocker_issue)
                    if issue_data.get("state") == "closed":
                        logger.info(
                            "Reconciliation found closed blocker #%d for agent %s",
                            blocker_issue,
                            agent.agent_id,
                        )
                        await self.registry.remove_blocker(agent.agent_id, blocker_issue)
                except Exception:
                    logger.debug(
                        "Could not check blocker #%d for agent %s",
                        blocker_issue,
                        agent.agent_id,
                    )

            # If all blockers resolved, wake the agent
            updated = await self.registry.get_agent(agent.agent_id)
            if updated and not updated.blocked_by and self._on_wake_agent:
                logger.info("All blockers resolved for %s — waking", agent.agent_id)
                from squadron.models import SquadronEvent, SquadronEventType

                wake_event = SquadronEvent(
                    event_type=SquadronEventType.BLOCKER_RESOLVED,
                    agent_id=agent.agent_id,
                    data={"source": "reconciliation"},
                )
                await self._on_wake_agent(agent.agent_id, wake_event)

    async def _check_stale_active_agents(self) -> None:
        """Detect ACTIVE agents that have exceeded their max duration.

        Circuit breaker enforcement layer 3 (AD-018): if the SDK hook
        and asyncio timer both fail, the reconciliation loop catches it.
        """
        active = await self.registry.get_agents_by_status(AgentStatus.ACTIVE)

        for agent in active:
            if not agent.active_since:
                continue

            limits = self.config.circuit_breakers.for_role(agent.role)
            active_seconds = (datetime.now(timezone.utc) - agent.active_since).total_seconds()

            # Warning threshold
            warning_at = limits.max_active_duration * limits.warning_threshold
            if active_seconds > warning_at and active_seconds < limits.max_active_duration:
                logger.warning(
                    "Agent %s approaching max active duration (%.0f/%ds)",
                    agent.agent_id,
                    active_seconds,
                    limits.max_active_duration,
                )

            # Hard limit
            if active_seconds > limits.max_active_duration:
                # Calculate overage to detect watchdog failures
                overage = int(active_seconds - limits.max_active_duration)
                
                # Detect if this is a watchdog failure (>60s overage suggests watchdog didn't fire)
                watchdog_failed = overage > 60
                failure_type = "PRIMARY WATCHDOG FAILED" if watchdog_failed else "Normal reconciliation catch"
                
                logger.error(
                    "RECONCILIATION CAUGHT TIMEOUT (layer 3) — Agent %s exceeded max active duration "
                    "(%ds > %ds, overage=%ds). %s",
                    agent.agent_id,
                    int(active_seconds),
                    limits.max_active_duration,
                    overage,
                    failure_type,
                )
                agent.status = AgentStatus.ESCALATED
                await self.registry.update_agent(agent)

                # Create needs-human issue via GitHub client
                if self.owner and self.repo:
                    try:
                        await self.github.create_issue(
                            self.owner,
                            self.repo,
                            title=f"[squadron] Agent {agent.agent_id} exceeded max active duration",
                            body=(
                                f"Agent `{agent.agent_id}` (role: {agent.role}) has been "
                                f"active for {int(active_seconds)}s, exceeding the configured "
                                f"limit of {limits.max_active_duration}s.\n\n"
                                f"**Issue:** #{agent.issue_number}\n"
                                f"**Branch:** {agent.branch}\n\n"
                                f"**Overage:** {overage}s ({'PRIMARY WATCHDOG FAILED' if watchdog_failed else 'normal reconciliation timeout'})\n\n"
                                "The agent has been escalated and stopped. "
                                "Please investigate and take manual action.\n\n"
                                "_Timeout enforced by: reconciliation loop (layer 3)_"
                            ),
                            labels=["needs-human", "escalation"] + (["watchdog-failure", "critical"] if watchdog_failed else ["timeout"]),
                        )
                    except Exception:
                        logger.exception(
                            "Failed to create escalation issue for %s",
                            agent.agent_id,
                        )

    async def _check_orphaned_agents(self) -> None:
        """Detect agents whose issue/PR has been closed while they were sleeping.

        Catches:
        - Issue closed while agent was SLEEPING → mark COMPLETED
        - PR merged while agent was SLEEPING → mark COMPLETED
        - PR closed (not merged) while agent was SLEEPING → mark COMPLETED
        - Issue reassigned to a non-bot user while SLEEPING → mark COMPLETED
        """
        sleeping = await self.registry.get_agents_by_status(AgentStatus.SLEEPING)

        for agent in sleeping:
            if not self.owner or not self.repo:
                continue

            # Check if the agent's issue was closed
            if agent.issue_number:
                try:
                    issue_data = await self.github.get_issue(
                        self.owner, self.repo, agent.issue_number
                    )
                    if issue_data.get("state") == "closed":
                        logger.info(
                            "Reconciliation: issue #%d closed while agent %s was sleeping — completing",
                            agent.issue_number,
                            agent.agent_id,
                        )
                        agent.status = AgentStatus.COMPLETED
                        await self.registry.update_agent(agent)
                        if self._on_complete_agent:
                            await self._on_complete_agent(agent.agent_id)
                        continue

                    # Check if issue was reassigned away from bot
                    assignees = issue_data.get("assignees", [])
                    bot_logins = {"squadron[bot]", "squadron-dev[bot]"}
                    if assignees and not any(
                        a.get("login", "").lower() in bot_logins for a in assignees
                    ):
                        logger.info(
                            "Reconciliation: issue #%d reassigned away from bot while agent %s was sleeping — completing",
                            agent.issue_number,
                            agent.agent_id,
                        )
                        agent.status = AgentStatus.COMPLETED
                        await self.registry.update_agent(agent)
                        if self._on_complete_agent:
                            await self._on_complete_agent(agent.agent_id)
                        continue
                except Exception:
                    logger.debug(
                        "Could not check issue #%d for orphaned agent %s",
                        agent.issue_number,
                        agent.agent_id,
                    )

            # Check if the agent's PR was merged or closed
            if agent.pr_number:
                try:
                    pr_data = await self.github.get_pull_request(
                        self.owner, self.repo, agent.pr_number
                    )
                    pr_state = pr_data.get("state", "")
                    pr_merged = pr_data.get("merged", False)

                    if pr_state == "closed" or pr_merged:
                        status_desc = "merged" if pr_merged else "closed"
                        logger.info(
                            "Reconciliation: PR #%d %s while agent %s was sleeping — completing",
                            agent.pr_number,
                            status_desc,
                            agent.agent_id,
                        )
                        agent.status = AgentStatus.COMPLETED
                        await self.registry.update_agent(agent)
                        if self._on_complete_agent:
                            await self._on_complete_agent(agent.agent_id)
                        continue
                except Exception:
                    logger.debug(
                        "Could not check PR #%d for orphaned agent %s",
                        agent.pr_number,
                        agent.agent_id,
                    )
