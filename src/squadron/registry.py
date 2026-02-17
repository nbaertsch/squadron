"""Agent Registry — SQLite-backed agent state tracking (AD-013).

Tracks agent instances, their lifecycle status, blocker dependencies,
and provides BFS cycle detection for blocker graphs.
Also stores seen webhook delivery IDs for deduplication.

The DB is expected to live on local (container) disk, NOT on a network
filesystem.  State is ephemeral across container restarts; a future
state-rebuild system will reconstruct it from GitHub project state.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import aiosqlite

from squadron.models import AgentRecord, AgentStatus

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    issue_number INTEGER,
    pr_number INTEGER,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    branch TEXT,
    worktree_path TEXT,
    blocked_by TEXT NOT NULL DEFAULT '[]',
    iteration_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    turn_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active_since TEXT,
    sleeping_since TEXT
);

CREATE TABLE IF NOT EXISTS seen_events (
    delivery_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    pr_number INTEGER,
    issue_number INTEGER,
    current_stage TEXT NOT NULL,
    stage_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    stage_agent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_issue ON agents(issue_number);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_pr ON workflow_runs(pr_number);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);
"""


class AgentRegistry:
    """SQLite-backed agent registry with async access."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open database and create tables."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("Agent registry initialized: %s", self.db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Registry not initialized — call initialize() first")
        return self._db

    # ── CRUD ─────────────────────────────────────────────────────────────

    async def create_agent(self, record: AgentRecord) -> AgentRecord:
        """Insert a new agent record."""
        now = datetime.now(timezone.utc).isoformat()
        record.created_at = datetime.fromisoformat(now)
        record.updated_at = record.created_at

        await self.db.execute(
            """INSERT INTO agents
               (agent_id, role, issue_number, pr_number, session_id, status,
                branch, worktree_path, blocked_by,
                iteration_count, tool_call_count, turn_count,
                created_at, updated_at, active_since, sleeping_since)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.agent_id,
                record.role,
                record.issue_number,
                record.pr_number,
                record.session_id,
                record.status.value,
                record.branch,
                record.worktree_path,
                json.dumps(record.blocked_by),
                record.iteration_count,
                record.tool_call_count,
                record.turn_count,
                now,
                now,
                record.active_since.isoformat() if record.active_since else None,
                record.sleeping_since.isoformat() if record.sleeping_since else None,
            ),
        )
        await self.db.commit()
        logger.info(
            "Created agent: %s (role=%s, issue=#%s)",
            record.agent_id,
            record.role,
            record.issue_number,
        )
        return record

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        """Get an agent by ID."""
        cursor = await self.db.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
        row = await cursor.fetchone()
        return self._row_to_record(row) if row else None

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent record by ID (used to clean up terminal records before re-spawn)."""
        await self.db.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        await self.db.commit()
        logger.info("Deleted agent record: %s", agent_id)

    async def get_agent_by_issue(self, issue_number: int) -> AgentRecord | None:
        """Get the active/sleeping agent assigned to an issue."""
        cursor = await self.db.execute(
            "SELECT * FROM agents WHERE issue_number = ? AND status IN ('created', 'active', 'sleeping') ORDER BY created_at DESC LIMIT 1",
            (issue_number,),
        )
        row = await cursor.fetchone()
        return self._row_to_record(row) if row else None

    async def get_agents_for_issue(self, issue_number: int) -> list[AgentRecord]:
        """Get all active/sleeping agents assigned to an issue."""
        cursor = await self.db.execute(
            "SELECT * FROM agents WHERE issue_number = ? AND status IN ('created', 'active', 'sleeping') ORDER BY created_at DESC",
            (issue_number,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(row) for row in rows]

    async def get_agents_by_status(self, status: AgentStatus) -> list[AgentRecord]:
        """Get all agents with a given status."""
        cursor = await self.db.execute("SELECT * FROM agents WHERE status = ?", (status.value,))
        rows = await cursor.fetchall()
        return [self._row_to_record(row) for row in rows]

    async def get_all_active_agents(self) -> list[AgentRecord]:
        """Get all agents in CREATED, ACTIVE, or SLEEPING status."""
        cursor = await self.db.execute(
            "SELECT * FROM agents WHERE status IN ('created', 'active', 'sleeping')"
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(row) for row in rows]

    async def get_recent_agents(self, limit: int = 10) -> list[AgentRecord]:
        """Get recently completed, escalated, or failed agents, ordered by most recent.

        Useful for giving ephemeral agents (like PM) context about recent
        project activity and triage history.
        """
        cursor = await self.db.execute(
            "SELECT * FROM agents WHERE status IN ('completed', 'escalated', 'failed') "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(row) for row in rows]

    async def update_agent(self, record: AgentRecord) -> None:
        """Update an existing agent record."""
        record.updated_at = datetime.now(timezone.utc)
        await self.db.execute(
            """UPDATE agents SET
               role=?, issue_number=?, pr_number=?, session_id=?, status=?,
               branch=?, worktree_path=?, blocked_by=?,
               iteration_count=?, tool_call_count=?, turn_count=?,
               updated_at=?, active_since=?, sleeping_since=?
               WHERE agent_id=?""",
            (
                record.role,
                record.issue_number,
                record.pr_number,
                record.session_id,
                record.status.value,
                record.branch,
                record.worktree_path,
                json.dumps(record.blocked_by),
                record.iteration_count,
                record.tool_call_count,
                record.turn_count,
                record.updated_at.isoformat(),
                record.active_since.isoformat() if record.active_since else None,
                record.sleeping_since.isoformat() if record.sleeping_since else None,
                record.agent_id,
            ),
        )
        await self.db.commit()

    # ── Blocker Management ───────────────────────────────────────────────

    async def add_blocker(self, agent_id: str, blocker_issue: int) -> bool:
        """Add a blocker issue to an agent. Returns False if it would create a cycle."""
        agent = await self.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"Agent not found: {agent_id}")

        # Check for cycles before adding
        if await self._would_create_cycle(agent_id, blocker_issue):
            logger.warning(
                "Cycle detected: adding blocker #%d to agent %s would create circular dependency",
                blocker_issue,
                agent_id,
            )
            return False

        if blocker_issue not in agent.blocked_by:
            agent.blocked_by.append(blocker_issue)
            await self.update_agent(agent)
            logger.info("Agent %s now blocked by #%d", agent_id, blocker_issue)

        return True

    async def remove_blocker(self, agent_id: str, blocker_issue: int) -> None:
        """Remove a resolved blocker from an agent."""
        agent = await self.get_agent(agent_id)
        if agent and blocker_issue in agent.blocked_by:
            agent.blocked_by.remove(blocker_issue)
            await self.update_agent(agent)
            logger.info("Removed blocker #%d from agent %s", blocker_issue, agent_id)

    async def get_agents_blocked_by(self, issue_number: int) -> list[AgentRecord]:
        """Find all SLEEPING agents blocked by a given issue."""
        all_sleeping = await self.get_agents_by_status(AgentStatus.SLEEPING)
        return [a for a in all_sleeping if issue_number in a.blocked_by]

    async def _would_create_cycle(self, agent_id: str, new_blocker_issue: int) -> bool:
        """BFS cycle detection (AD-013).

        Check if adding `new_blocker_issue` as a blocker for `agent_id`
        would create a circular dependency in the blocker graph.
        """
        # Find the agent working on the new_blocker_issue
        blocker_agent = await self.get_agent_by_issue(new_blocker_issue)
        if blocker_agent is None:
            return False  # No agent on that issue — no cycle possible

        # BFS from the blocker agent's blockers back toward our agent
        agent = await self.get_agent(agent_id)
        if agent is None:
            return False

        visited: set[int] = set()
        queue: deque[int] = deque()

        # Start from the blocker agent's blockers
        for blocked_issue in blocker_agent.blocked_by:
            queue.append(blocked_issue)

        while queue:
            current_issue = queue.popleft()
            if current_issue in visited:
                continue
            visited.add(current_issue)

            # If we reach our agent's issue, it's a cycle
            if current_issue == agent.issue_number:
                return True

            # Follow the chain: who is working on current_issue, and what blocks them?
            current_agent = await self.get_agent_by_issue(current_issue)
            if current_agent:
                for bi in current_agent.blocked_by:
                    if bi not in visited:
                        queue.append(bi)

        return False

    # ── Webhook Deduplication ────────────────────────────────────────────

    async def has_seen_event(self, delivery_id: str) -> bool:
        """Check if a webhook delivery has already been processed."""
        cursor = await self.db.execute(
            "SELECT 1 FROM seen_events WHERE delivery_id = ?", (delivery_id,)
        )
        return await cursor.fetchone() is not None

    async def mark_event_seen(self, delivery_id: str, event_type: str) -> None:
        """Record that a webhook delivery has been processed."""
        await self.db.execute(
            "INSERT OR IGNORE INTO seen_events (delivery_id, event_type, received_at) VALUES (?, ?, ?)",
            (delivery_id, event_type, datetime.now(timezone.utc).isoformat()),
        )
        await self.db.commit()

    async def prune_old_events(self, max_age_hours: int = 72) -> int:
        """Delete seen_events older than max_age_hours. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        cursor = await self.db.execute("DELETE FROM seen_events WHERE received_at < ?", (cutoff,))
        await self.db.commit()
        return cursor.rowcount

    # ── Workflow Run Management ──────────────────────────────────────────

    async def create_workflow_run(
        self,
        run_id: str,
        workflow_name: str,
        current_stage: str,
        *,
        pr_number: int | None = None,
        issue_number: int | None = None,
        stage_index: int = 0,
        stage_agent_id: str | None = None,
    ) -> None:
        """Create a new workflow pipeline run."""
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """INSERT INTO workflow_runs
               (run_id, workflow_name, pr_number, issue_number,
                current_stage, stage_index, status, stage_agent_id,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
            (
                run_id,
                workflow_name,
                pr_number,
                issue_number,
                current_stage,
                stage_index,
                stage_agent_id,
                now,
                now,
            ),
        )
        await self.db.commit()
        logger.info(
            "Created workflow run: %s (workflow=%s, stage=%s, pr=#%s)",
            run_id,
            workflow_name,
            current_stage,
            pr_number,
        )

    async def get_workflow_run(self, run_id: str) -> dict | None:
        """Get a workflow run by ID."""
        cursor = await self.db.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "workflow_name": row["workflow_name"],
            "pr_number": row["pr_number"],
            "issue_number": row["issue_number"],
            "current_stage": row["current_stage"],
            "stage_index": row["stage_index"],
            "status": row["status"],
            "stage_agent_id": row["stage_agent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def get_workflow_runs_for_pr(self, pr_number: int) -> list[dict]:
        """Get all active workflow runs for a PR."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_runs WHERE pr_number = ? AND status = 'active'",
            (pr_number,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "run_id": r["run_id"],
                "workflow_name": r["workflow_name"],
                "pr_number": r["pr_number"],
                "issue_number": r["issue_number"],
                "current_stage": r["current_stage"],
                "stage_index": r["stage_index"],
                "status": r["status"],
                "stage_agent_id": r["stage_agent_id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    async def get_workflow_run_by_agent(self, agent_id: str) -> dict | None:
        """Find the workflow run that a given agent belongs to."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_runs WHERE stage_agent_id = ? AND status = 'active'",
            (agent_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "workflow_name": row["workflow_name"],
            "pr_number": row["pr_number"],
            "issue_number": row["issue_number"],
            "current_stage": row["current_stage"],
            "stage_index": row["stage_index"],
            "status": row["status"],
            "stage_agent_id": row["stage_agent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def advance_workflow_run(
        self,
        run_id: str,
        next_stage: str,
        stage_index: int,
        stage_agent_id: str | None = None,
    ) -> None:
        """Advance a workflow run to the next stage."""
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """UPDATE workflow_runs
               SET current_stage = ?, stage_index = ?, stage_agent_id = ?, updated_at = ?
               WHERE run_id = ?""",
            (next_stage, stage_index, stage_agent_id, now, run_id),
        )
        await self.db.commit()
        logger.info("Advanced workflow %s to stage %s (index=%d)", run_id, next_stage, stage_index)

    async def complete_workflow_run(self, run_id: str, status: str = "completed") -> None:
        """Mark a workflow run as completed or stopped."""
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status, now, run_id),
        )
        await self.db.commit()
        logger.info("Workflow run %s → %s", run_id, status)

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> AgentRecord:
        """Convert a database row to an AgentRecord."""
        return AgentRecord(
            agent_id=row["agent_id"],
            role=row["role"],
            issue_number=row["issue_number"],
            pr_number=row["pr_number"],
            session_id=row["session_id"],
            status=AgentStatus(row["status"]),
            branch=row["branch"],
            worktree_path=row["worktree_path"],
            blocked_by=json.loads(row["blocked_by"]),
            iteration_count=row["iteration_count"],
            tool_call_count=row["tool_call_count"],
            turn_count=row["turn_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            active_since=datetime.fromisoformat(row["active_since"])
            if row["active_since"]
            else None,
            sleeping_since=datetime.fromisoformat(row["sleeping_since"])
            if row["sleeping_since"]
            else None,
        )
