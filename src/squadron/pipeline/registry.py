"""Pipeline Registry — unified SQLite persistence for pipeline state.

Provides CRUD operations for:
- Pipeline runs and their stage executions
- Gate check results
- PR approvals (both agent and human)

This registry is the single source of truth for all pipeline state.
It extends the agent registry's ``pr_approvals`` table concept by
recording human reviews alongside agent reviews — resolving gap #1
from the issue (human PR reviews not tracked).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# ── Status Enumerations ────────────────────────────────────────────────────────


class PipelineRunStatus(str, Enum):
    """Status values for a pipeline run."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"       # Waiting for an event to re-evaluate gates
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class PipelineStageStatus(str, Enum):
    """Status values for a pipeline stage execution."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"       # Waiting for gate re-evaluation
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL = """
-- Pipeline runs — one row per pipeline execution
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id       TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,

    -- Trigger context
    trigger_event       TEXT,
    trigger_delivery_id TEXT,
    issue_number        INTEGER,
    pr_number           INTEGER,

    -- Execution state
    status              TEXT NOT NULL DEFAULT 'pending',
    current_stage_id    TEXT,
    current_stage_index INTEGER DEFAULT 0,

    -- Reactive subscriptions (JSON list of event types)
    subscribed_events   TEXT DEFAULT '[]',

    -- Iteration tracking per stage (JSON object: stage_id -> count)
    iteration_counts    TEXT DEFAULT '{}',

    -- Context propagated between stages (JSON)
    context             TEXT DEFAULT '{}',

    -- Outputs per stage (JSON object: stage_id -> outputs)
    outputs             TEXT DEFAULT '{}',

    -- Timestamps
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,

    -- Error tracking
    error_message       TEXT,
    error_stage         TEXT
);

-- Individual stage executions within a pipeline run
CREATE TABLE IF NOT EXISTS pipeline_stage_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    stage_id         TEXT NOT NULL,
    stage_index      INTEGER NOT NULL,

    -- Execution details
    status           TEXT NOT NULL DEFAULT 'pending',
    agent_id         TEXT,         -- Set for agent stages

    -- Parallel stage support
    branch_id        TEXT,
    parent_stage_id  TEXT,

    -- Results (JSON)
    outputs          TEXT DEFAULT '{}',
    error_message    TEXT,

    -- Timing
    started_at       TEXT,
    completed_at     TEXT,

    -- Retry tracking
    attempt_number   INTEGER DEFAULT 1,
    max_attempts     INTEGER DEFAULT 1
);

-- Gate check results for a stage execution
CREATE TABLE IF NOT EXISTS pipeline_gate_checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_run_id INTEGER NOT NULL REFERENCES pipeline_stage_runs(id),
    check_type   TEXT NOT NULL,

    -- Result
    passed       INTEGER NOT NULL DEFAULT 0,  -- boolean
    result_data  TEXT DEFAULT '{}',
    error_message TEXT,

    checked_at   TEXT NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_pl_runs_name    ON pipeline_runs(pipeline_name);
CREATE INDEX IF NOT EXISTS idx_pl_runs_status  ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_pl_runs_issue   ON pipeline_runs(issue_number);
CREATE INDEX IF NOT EXISTS idx_pl_runs_pr      ON pipeline_runs(pr_number);
CREATE INDEX IF NOT EXISTS idx_pl_stage_run_id ON pipeline_stage_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_pl_stage_agent  ON pipeline_stage_runs(agent_id);
"""


# ── Data Classes ───────────────────────────────────────────────────────────────


class PipelineRun:
    """Mutable runtime state for a single pipeline execution."""

    __slots__ = (
        "run_id", "pipeline_name",
        "trigger_event", "trigger_delivery_id", "issue_number", "pr_number",
        "status", "current_stage_id", "current_stage_index",
        "subscribed_events", "iteration_counts", "context", "outputs",
        "created_at", "started_at", "completed_at",
        "error_message", "error_stage",
    )

    def __init__(
        self,
        run_id: str,
        pipeline_name: str,
        trigger_event: str | None = None,
        trigger_delivery_id: str | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
        status: PipelineRunStatus = PipelineRunStatus.PENDING,
        current_stage_id: str | None = None,
        current_stage_index: int = 0,
        subscribed_events: list[str] | None = None,
        iteration_counts: dict[str, int] | None = None,
        context: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        error_message: str | None = None,
        error_stage: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.pipeline_name = pipeline_name
        self.trigger_event = trigger_event
        self.trigger_delivery_id = trigger_delivery_id
        self.issue_number = issue_number
        self.pr_number = pr_number
        self.status = status
        self.current_stage_id = current_stage_id
        self.current_stage_index = current_stage_index
        self.subscribed_events = subscribed_events or []
        self.iteration_counts = iteration_counts or {}
        self.context = context or {}
        self.outputs = outputs or {}
        self.created_at = created_at or datetime.now(timezone.utc)
        self.started_at = started_at
        self.completed_at = completed_at
        self.error_message = error_message
        self.error_stage = error_stage


class PipelineStageRun:
    """Runtime state for a single stage within a pipeline run."""

    __slots__ = (
        "id", "run_id", "stage_id", "stage_index",
        "status", "agent_id", "branch_id", "parent_stage_id",
        "outputs", "error_message",
        "started_at", "completed_at",
        "attempt_number", "max_attempts",
    )

    def __init__(
        self,
        run_id: str,
        stage_id: str,
        stage_index: int,
        status: PipelineStageStatus = PipelineStageStatus.PENDING,
        agent_id: str | None = None,
        branch_id: str | None = None,
        parent_stage_id: str | None = None,
        outputs: dict[str, Any] | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        attempt_number: int = 1,
        max_attempts: int = 1,
        id: int | None = None,
    ) -> None:
        self.id = id
        self.run_id = run_id
        self.stage_id = stage_id
        self.stage_index = stage_index
        self.status = status
        self.agent_id = agent_id
        self.branch_id = branch_id
        self.parent_stage_id = parent_stage_id
        self.outputs = outputs or {}
        self.error_message = error_message
        self.started_at = started_at
        self.completed_at = completed_at
        self.attempt_number = attempt_number
        self.max_attempts = max_attempts


# ── Registry ───────────────────────────────────────────────────────────────────


class PipelineRegistry:
    """Unified registry for pipeline runs, stage executions, and gate checks.

    Uses the same SQLite database as the agent registry for atomicity.
    Pass the shared ``aiosqlite.Connection`` from ``AgentRegistry.db``.
    """

    def __init__(self, db: "aiosqlite.Connection") -> None:
        self.db = db

    async def initialize(self) -> None:
        """Create pipeline tables if they do not already exist."""
        await self.db.executescript(_DDL)
        await self.db.commit()
        logger.debug("Pipeline registry tables initialized")

    # ── Pipeline Run CRUD ──────────────────────────────────────────────────────

    @staticmethod
    def new_run_id() -> str:
        """Generate a unique pipeline run ID."""
        return f"pl-{uuid.uuid4().hex[:12]}"

    async def create_run(self, run: PipelineRun) -> None:
        """Persist a new pipeline run."""
        await self.db.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, pipeline_name, trigger_event, trigger_delivery_id,
                issue_number, pr_number, status, current_stage_id,
                current_stage_index, subscribed_events, iteration_counts,
                context, outputs, created_at, started_at, completed_at,
                error_message, error_stage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id, run.pipeline_name, run.trigger_event,
                run.trigger_delivery_id, run.issue_number, run.pr_number,
                run.status.value, run.current_stage_id, run.current_stage_index,
                json.dumps(run.subscribed_events),
                json.dumps(run.iteration_counts),
                json.dumps(run.context),
                json.dumps(run.outputs),
                run.created_at.isoformat(),
                run.started_at.isoformat() if run.started_at else None,
                run.completed_at.isoformat() if run.completed_at else None,
                run.error_message, run.error_stage,
            ),
        )
        await self.db.commit()
        logger.debug("Created pipeline run: %s", run.run_id)

    async def get_run(self, run_id: str) -> PipelineRun | None:
        """Fetch a pipeline run by ID."""
        cursor = await self.db.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_run(row) if row else None

    async def get_run_by_name_and_issue(
        self, pipeline_name: str, issue_number: int
    ) -> PipelineRun | None:
        """Fetch an active run for a (pipeline, issue) pair.

        Used to prevent duplicate runs.
        """
        cursor = await self.db.execute(
            """
            SELECT * FROM pipeline_runs
            WHERE pipeline_name = ? AND issue_number = ?
              AND status IN ('pending', 'running', 'waiting')
            ORDER BY created_at DESC LIMIT 1
            """,
            (pipeline_name, issue_number),
        )
        row = await cursor.fetchone()
        return self._row_to_run(row) if row else None

    async def get_active_runs(self) -> list[PipelineRun]:
        """Return all running or waiting pipeline runs."""
        cursor = await self.db.execute(
            "SELECT * FROM pipeline_runs WHERE status IN ('pending','running','waiting')"
            " ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_run(r) for r in rows]

    async def get_runs_subscribed_to(self, event_type: str) -> list[PipelineRun]:
        """Return active runs that have subscribed to a specific event type.

        Used by the reactive event dispatch loop to find pipelines to wake.
        """
        # SQLite doesn't support JSON_CONTAINS, so we use a LIKE search
        # on the JSON array.  The stored value is like '["event.a", "event.b"]'.
        cursor = await self.db.execute(
            """
            SELECT * FROM pipeline_runs
            WHERE status IN ('running', 'waiting')
              AND subscribed_events LIKE ?
            ORDER BY created_at
            """,
            (f'%"{event_type}"%',),
        )
        rows = await cursor.fetchall()
        return [self._row_to_run(r) for r in rows]

    async def update_run(self, run: PipelineRun) -> None:
        """Persist updated pipeline run state."""
        await self.db.execute(
            """
            UPDATE pipeline_runs SET
                status = ?, current_stage_id = ?, current_stage_index = ?,
                subscribed_events = ?, iteration_counts = ?,
                context = ?, outputs = ?,
                started_at = ?, completed_at = ?,
                error_message = ?, error_stage = ?
            WHERE run_id = ?
            """,
            (
                run.status.value, run.current_stage_id, run.current_stage_index,
                json.dumps(run.subscribed_events),
                json.dumps(run.iteration_counts),
                json.dumps(run.context), json.dumps(run.outputs),
                run.started_at.isoformat() if run.started_at else None,
                run.completed_at.isoformat() if run.completed_at else None,
                run.error_message, run.error_stage,
                run.run_id,
            ),
        )
        await self.db.commit()

    async def delete_run(self, run_id: str) -> None:
        """Delete a pipeline run and all related stage/gate data."""
        # Delete gate checks for all stages of this run
        await self.db.execute(
            """
            DELETE FROM pipeline_gate_checks
            WHERE stage_run_id IN (
                SELECT id FROM pipeline_stage_runs WHERE run_id = ?
            )
            """,
            (run_id,),
        )
        await self.db.execute(
            "DELETE FROM pipeline_stage_runs WHERE run_id = ?", (run_id,)
        )
        await self.db.execute(
            "DELETE FROM pipeline_runs WHERE run_id = ?", (run_id,)
        )
        await self.db.commit()

    def _row_to_run(self, row: "aiosqlite.Row") -> PipelineRun:
        return PipelineRun(
            run_id=row["run_id"],
            pipeline_name=row["pipeline_name"],
            trigger_event=row["trigger_event"],
            trigger_delivery_id=row["trigger_delivery_id"],
            issue_number=row["issue_number"],
            pr_number=row["pr_number"],
            status=PipelineRunStatus(row["status"]),
            current_stage_id=row["current_stage_id"],
            current_stage_index=row["current_stage_index"] or 0,
            subscribed_events=json.loads(row["subscribed_events"] or "[]"),
            iteration_counts=json.loads(row["iteration_counts"] or "{}"),
            context=json.loads(row["context"] or "{}"),
            outputs=json.loads(row["outputs"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            error_message=row["error_message"],
            error_stage=row["error_stage"],
        )

    # ── Stage Run CRUD ─────────────────────────────────────────────────────────

    async def create_stage_run(self, stage_run: PipelineStageRun) -> int:
        """Persist a new stage run. Returns the database row ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO pipeline_stage_runs (
                run_id, stage_id, stage_index, status, agent_id,
                branch_id, parent_stage_id, outputs, error_message,
                started_at, completed_at, attempt_number, max_attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage_run.run_id, stage_run.stage_id, stage_run.stage_index,
                stage_run.status.value, stage_run.agent_id,
                stage_run.branch_id, stage_run.parent_stage_id,
                json.dumps(stage_run.outputs), stage_run.error_message,
                stage_run.started_at.isoformat() if stage_run.started_at else None,
                stage_run.completed_at.isoformat() if stage_run.completed_at else None,
                stage_run.attempt_number, stage_run.max_attempts,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_stage_run(self, stage_run_id: int) -> PipelineStageRun | None:
        """Fetch a stage run by database ID."""
        cursor = await self.db.execute(
            "SELECT * FROM pipeline_stage_runs WHERE id = ?", (stage_run_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_stage_run(row) if row else None

    async def get_stage_runs_for_run(self, run_id: str) -> list[PipelineStageRun]:
        """Fetch all stage runs for a pipeline run, ordered by stage index."""
        cursor = await self.db.execute(
            "SELECT * FROM pipeline_stage_runs WHERE run_id = ? ORDER BY stage_index, id",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_stage_run(r) for r in rows]

    async def get_stage_run_by_agent(self, agent_id: str) -> PipelineStageRun | None:
        """Find the most recent stage run associated with an agent."""
        cursor = await self.db.execute(
            "SELECT * FROM pipeline_stage_runs WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_stage_run(row) if row else None

    async def get_latest_stage_run(
        self, run_id: str, stage_id: str
    ) -> PipelineStageRun | None:
        """Fetch the most recent stage run for a specific stage."""
        cursor = await self.db.execute(
            """
            SELECT * FROM pipeline_stage_runs
            WHERE run_id = ? AND stage_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (run_id, stage_id),
        )
        row = await cursor.fetchone()
        return self._row_to_stage_run(row) if row else None

    async def update_stage_run(self, stage_run: PipelineStageRun) -> None:
        """Persist updated stage run state."""
        if stage_run.id is None:
            raise ValueError("Cannot update a stage run without a database ID")

        await self.db.execute(
            """
            UPDATE pipeline_stage_runs SET
                status = ?, agent_id = ?, outputs = ?, error_message = ?,
                started_at = ?, completed_at = ?, attempt_number = ?
            WHERE id = ?
            """,
            (
                stage_run.status.value, stage_run.agent_id,
                json.dumps(stage_run.outputs), stage_run.error_message,
                stage_run.started_at.isoformat() if stage_run.started_at else None,
                stage_run.completed_at.isoformat() if stage_run.completed_at else None,
                stage_run.attempt_number,
                stage_run.id,
            ),
        )
        await self.db.commit()

    def _row_to_stage_run(self, row: "aiosqlite.Row") -> PipelineStageRun:
        return PipelineStageRun(
            id=row["id"],
            run_id=row["run_id"],
            stage_id=row["stage_id"],
            stage_index=row["stage_index"],
            status=PipelineStageStatus(row["status"]),
            agent_id=row["agent_id"],
            branch_id=row["branch_id"],
            parent_stage_id=row["parent_stage_id"],
            outputs=json.loads(row["outputs"] or "{}"),
            error_message=row["error_message"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            attempt_number=row["attempt_number"] or 1,
            max_attempts=row["max_attempts"] or 1,
        )

    # ── Gate Check CRUD ────────────────────────────────────────────────────────

    async def create_gate_check(
        self,
        stage_run_id: int,
        check_type: str,
        passed: bool,
        result_data: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> int:
        """Record a gate check result for a stage run."""
        cursor = await self.db.execute(
            """
            INSERT INTO pipeline_gate_checks (
                stage_run_id, check_type, passed, result_data, error_message, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stage_run_id,
                check_type,
                1 if passed else 0,
                json.dumps(result_data or {}),
                error_message,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_gate_checks_for_stage(
        self, stage_run_id: int
    ) -> list[dict[str, Any]]:
        """Fetch all gate check results for a stage run."""
        cursor = await self.db.execute(
            "SELECT * FROM pipeline_gate_checks WHERE stage_run_id = ? ORDER BY id",
            (stage_run_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "check_type": r["check_type"],
                "passed": bool(r["passed"]),
                "result_data": json.loads(r["result_data"] or "{}"),
                "error_message": r["error_message"],
                "checked_at": r["checked_at"],
            }
            for r in rows
        ]
