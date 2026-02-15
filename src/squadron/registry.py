"""Agent Registry — SQLite-backed agent state tracking (AD-013).

Tracks agent instances, their lifecycle status, blocker dependencies,
and provides BFS cycle detection for blocker graphs.
Also stores seen webhook delivery IDs for deduplication.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import aiosqlite

from squadron.models import AgentRecord, AgentRole, AgentStatus

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

CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_issue ON agents(issue_number);
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
                record.role.value,
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
        logger.info("Created agent: %s (role=%s, issue=#%s)", record.agent_id, record.role, record.issue_number)
        return record

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        """Get an agent by ID."""
        cursor = await self.db.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
        row = await cursor.fetchone()
        return self._row_to_record(row) if row else None

    async def get_agent_by_issue(self, issue_number: int) -> AgentRecord | None:
        """Get the active/sleeping agent assigned to an issue."""
        cursor = await self.db.execute(
            "SELECT * FROM agents WHERE issue_number = ? AND status IN ('created', 'active', 'sleeping') ORDER BY created_at DESC LIMIT 1",
            (issue_number,),
        )
        row = await cursor.fetchone()
        return self._row_to_record(row) if row else None

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
                record.role.value,
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
        cursor = await self.db.execute(
            "DELETE FROM seen_events WHERE received_at < ?", (cutoff,)
        )
        await self.db.commit()
        return cursor.rowcount

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> AgentRecord:
        """Convert a database row to an AgentRecord."""
        return AgentRecord(
            agent_id=row["agent_id"],
            role=AgentRole(row["role"]),
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
            active_since=datetime.fromisoformat(row["active_since"]) if row["active_since"] else None,
            sleeping_since=datetime.fromisoformat(row["sleeping_since"]) if row["sleeping_since"] else None,
        )
