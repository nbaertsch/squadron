"""Pipeline registry — SQLite persistence for pipeline runs, stages, and gate checks.

AD-019: Unified registry replacing both AgentRegistry workflow tables and WorkflowRegistryV2.

Key exports:
    PipelineRegistry — All CRUD operations for pipeline_runs, pipeline_stage_runs,
        pipeline_gate_checks, pipeline_human_stage_state, pipeline_pr_associations,
        pr_review_requirements, pr_approvals, pr_sequence_state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from squadron.pipeline.models import (
    GateCheckRecord,
    HumanStageState,
    PipelineRun,
    PipelineRunStatus,
    PipelineScope,
    StageRun,
    StageRunStatus,
)

logger = logging.getLogger("squadron.pipeline.registry")


class PipelineRegistry:
    """SQLite-backed persistence for the unified pipeline system.

    Takes an already-open aiosqlite connection (shared with the rest of Squadron).
    Call `initialize()` to create tables.
    """

    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    async def initialize(self) -> None:
        """Create all pipeline tables if they don't exist."""
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        logger.info("Pipeline registry tables initialized")

    # ── Pipeline Run CRUD ────────────────────────────────────────────────────

    async def create_pipeline_run(self, run: PipelineRun) -> None:
        """Insert a new pipeline run."""
        await self._db.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, pipeline_name, definition_snapshot,
                trigger_event, trigger_delivery_id, issue_number, pr_number, scope,
                parent_run_id, parent_stage_id, nesting_depth,
                status, current_stage_id, context,
                created_at, started_at, completed_at,
                error_message, error_stage_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.pipeline_name,
                run.definition_snapshot,
                run.trigger_event,
                run.trigger_delivery_id,
                run.issue_number,
                run.pr_number,
                run.scope.value,
                run.parent_run_id,
                run.parent_stage_id,
                run.nesting_depth,
                run.status.value,
                run.current_stage_id,
                json.dumps(run.context),
                _dt_to_str(run.created_at),
                _dt_to_str(run.started_at),
                _dt_to_str(run.completed_at),
                run.error_message,
                run.error_stage_id,
            ),
        )
        await self._db.commit()

    async def get_pipeline_run(self, run_id: str) -> PipelineRun | None:
        """Fetch a pipeline run by ID."""
        cursor = await self._db.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_pipeline_run(row)

    async def get_pipeline_runs_by_pr(
        self, pr_number: int, *, status: PipelineRunStatus | None = None
    ) -> list[PipelineRun]:
        """Get all pipeline runs for a PR, optionally filtered by status."""
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM pipeline_runs WHERE pr_number = ? AND status = ? "
                "ORDER BY created_at DESC",
                (pr_number, status.value),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM pipeline_runs WHERE pr_number = ? ORDER BY created_at DESC",
                (pr_number,),
            )
        rows = await cursor.fetchall()
        return [_row_to_pipeline_run(r) for r in rows]

    async def get_pipeline_runs_by_issue(
        self, issue_number: int, *, status: PipelineRunStatus | None = None
    ) -> list[PipelineRun]:
        """Get all pipeline runs for an issue, optionally filtered by status."""
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM pipeline_runs WHERE issue_number = ? AND status = ? "
                "ORDER BY created_at DESC",
                (issue_number, status.value),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM pipeline_runs WHERE issue_number = ? ORDER BY created_at DESC",
                (issue_number,),
            )
        rows = await cursor.fetchall()
        return [_row_to_pipeline_run(r) for r in rows]

    async def get_active_pipeline_runs(self) -> list[PipelineRun]:
        """Get all pipeline runs with status pending or running."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_runs WHERE status IN (?, ?) ORDER BY created_at",
            (PipelineRunStatus.PENDING.value, PipelineRunStatus.RUNNING.value),
        )
        rows = await cursor.fetchall()
        return [_row_to_pipeline_run(r) for r in rows]

    async def get_child_pipelines(self, parent_run_id: str) -> list[PipelineRun]:
        """Get all child pipeline runs for a given parent run."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_runs WHERE parent_run_id = ? ORDER BY created_at",
            (parent_run_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_pipeline_run(r) for r in rows]

    async def get_running_pipelines_for_pr(self, pr_number: int) -> list[PipelineRun]:
        """Get running pipelines for a specific PR (including via PR associations)."""
        # Direct match
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_runs WHERE pr_number = ? AND status IN (?, ?)",
            (pr_number, PipelineRunStatus.PENDING.value, PipelineRunStatus.RUNNING.value),
        )
        rows = await cursor.fetchall()
        direct = {r["run_id"]: _row_to_pipeline_run(r) for r in rows}

        # Via associations (multi-PR pipelines)
        cursor = await self._db.execute(
            """
            SELECT pr.* FROM pipeline_runs pr
            JOIN pipeline_pr_associations ppa ON pr.run_id = ppa.pipeline_run_id
            WHERE ppa.pr_number = ? AND pr.status IN (?, ?)
            """,
            (pr_number, PipelineRunStatus.PENDING.value, PipelineRunStatus.RUNNING.value),
        )
        rows = await cursor.fetchall()
        for r in rows:
            rid = r["run_id"]
            if rid not in direct:
                direct[rid] = _row_to_pipeline_run(r)

        return list(direct.values())

    async def update_pipeline_run(self, run: PipelineRun) -> None:
        """Update a pipeline run's mutable fields."""
        await self._db.execute(
            """
            UPDATE pipeline_runs SET
                status = ?, current_stage_id = ?, context = ?,
                started_at = ?, completed_at = ?,
                error_message = ?, error_stage_id = ?
            WHERE run_id = ?
            """,
            (
                run.status.value,
                run.current_stage_id,
                json.dumps(run.context),
                _dt_to_str(run.started_at),
                _dt_to_str(run.completed_at),
                run.error_message,
                run.error_stage_id,
                run.run_id,
            ),
        )
        await self._db.commit()

    async def delete_pipeline_run(self, run_id: str) -> None:
        """Delete a pipeline run and all associated records (cascading)."""
        # Stage runs and gate checks cascade via FK
        await self._db.execute(
            "DELETE FROM pipeline_pr_associations WHERE pipeline_run_id = ?", (run_id,)
        )
        await self._db.execute("DELETE FROM pipeline_runs WHERE run_id = ?", (run_id,))
        await self._db.commit()

    # ── Stage Run CRUD ───────────────────────────────────────────────────────

    async def create_stage_run(self, stage_run: StageRun) -> int:
        """Insert a new stage run. Returns the auto-increment ID."""
        cursor = await self._db.execute(
            """
            INSERT INTO pipeline_stage_runs (
                run_id, stage_id, status, agent_id,
                branch_id, parent_stage_id, child_pipeline_run_id,
                outputs, error_message,
                attempt_number, max_attempts,
                started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage_run.run_id,
                stage_run.stage_id,
                stage_run.status.value,
                stage_run.agent_id,
                stage_run.branch_id,
                stage_run.parent_stage_id,
                stage_run.child_pipeline_run_id,
                json.dumps(stage_run.outputs),
                stage_run.error_message,
                stage_run.attempt_number,
                stage_run.max_attempts,
                _dt_to_str(stage_run.started_at),
                _dt_to_str(stage_run.completed_at),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_stage_run(self, stage_run_id: int) -> StageRun | None:
        """Fetch a stage run by its auto-increment ID."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_stage_runs WHERE id = ?", (stage_run_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_stage_run(row)

    async def get_stage_runs_for_pipeline(self, run_id: str) -> list[StageRun]:
        """Get all stage runs for a pipeline run, ordered by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_stage_runs WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_stage_run(r) for r in rows]

    async def get_latest_stage_run(self, run_id: str, stage_id: str) -> StageRun | None:
        """Get the most recent stage run for a given stage in a pipeline."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_stage_runs WHERE run_id = ? AND stage_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (run_id, stage_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_stage_run(row)

    async def get_stage_run_by_agent(self, agent_id: str) -> StageRun | None:
        """Find the stage run associated with a specific agent."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_stage_runs WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
            (agent_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_stage_run(row)

    async def update_stage_run(self, stage_run: StageRun) -> None:
        """Update a stage run's mutable fields."""
        if stage_run.id is None:
            msg = "Cannot update stage run without an ID"
            raise ValueError(msg)
        await self._db.execute(
            """
            UPDATE pipeline_stage_runs SET
                status = ?, agent_id = ?,
                child_pipeline_run_id = ?,
                outputs = ?, error_message = ?,
                started_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                stage_run.status.value,
                stage_run.agent_id,
                stage_run.child_pipeline_run_id,
                json.dumps(stage_run.outputs),
                stage_run.error_message,
                _dt_to_str(stage_run.started_at),
                _dt_to_str(stage_run.completed_at),
                stage_run.id,
            ),
        )
        await self._db.commit()

    # ── Gate Check Records ───────────────────────────────────────────────────

    async def create_gate_check(self, record: GateCheckRecord) -> int:
        """Record a gate check evaluation. Returns the auto-increment ID."""
        cursor = await self._db.execute(
            """
            INSERT INTO pipeline_gate_checks (
                stage_run_id, check_type, check_config,
                passed, message, result_data, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.stage_run_id,
                record.check_type,
                record.check_config,
                1 if record.passed else 0 if record.passed is not None else None,
                record.message,
                json.dumps(record.result_data),
                _dt_to_str(record.checked_at),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_gate_checks_for_stage(self, stage_run_id: int) -> list[GateCheckRecord]:
        """Get all gate check records for a stage run."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_gate_checks WHERE stage_run_id = ? ORDER BY checked_at",
            (stage_run_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_gate_check(r) for r in rows]

    # ── Human Stage State ────────────────────────────────────────────────────

    async def create_human_stage_state(self, state: HumanStageState) -> int:
        """Create a human stage state record. Returns the auto-increment ID."""
        cursor = await self._db.execute(
            """
            INSERT INTO pipeline_human_stage_state (
                stage_run_id, entry_notified_at, last_reminder_at,
                reminder_count, assigned_users,
                completed_by, completed_action
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.stage_run_id,
                _dt_to_str(state.entry_notified_at),
                _dt_to_str(state.last_reminder_at),
                state.reminder_count,
                json.dumps(state.assigned_users),
                state.completed_by,
                state.completed_action,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_human_stage_state(self, stage_run_id: int) -> HumanStageState | None:
        """Get the human stage state for a stage run."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_human_stage_state WHERE stage_run_id = ?",
            (stage_run_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_human_stage_state(row)

    async def update_human_stage_state(self, state: HumanStageState) -> None:
        """Update a human stage state record."""
        if state.id is None:
            msg = "Cannot update human stage state without an ID"
            raise ValueError(msg)
        await self._db.execute(
            """
            UPDATE pipeline_human_stage_state SET
                entry_notified_at = ?, last_reminder_at = ?,
                reminder_count = ?, assigned_users = ?,
                completed_by = ?, completed_action = ?
            WHERE id = ?
            """,
            (
                _dt_to_str(state.entry_notified_at),
                _dt_to_str(state.last_reminder_at),
                state.reminder_count,
                json.dumps(state.assigned_users),
                state.completed_by,
                state.completed_action,
                state.id,
            ),
        )
        await self._db.commit()

    # ── PR Associations (multi-PR pipelines) ─────────────────────────────────

    async def add_pr_association(
        self,
        pipeline_run_id: str,
        pr_number: int,
        repo: str,
        *,
        stage_id: str | None = None,
        role: str | None = None,
    ) -> None:
        """Associate a PR with a pipeline run."""
        await self._db.execute(
            """
            INSERT OR IGNORE INTO pipeline_pr_associations
                (pipeline_run_id, pr_number, repo, stage_id, role)
            VALUES (?, ?, ?, ?, ?)
            """,
            (pipeline_run_id, pr_number, repo, stage_id, role),
        )
        await self._db.commit()

    async def get_pr_associations(self, pipeline_run_id: str) -> list[dict[str, Any]]:
        """Get all PR associations for a pipeline run."""
        cursor = await self._db.execute(
            "SELECT * FROM pipeline_pr_associations WHERE pipeline_run_id = ?",
            (pipeline_run_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── PR Review Requirements ───────────────────────────────────────────────

    async def set_pr_requirements(
        self,
        pr_number: int,
        requirements: list[dict[str, Any]],
        *,
        pipeline_run_id: str | None = None,
    ) -> None:
        """Set review requirements for a PR (replaces existing)."""
        await self._db.execute(
            "DELETE FROM pr_review_requirements WHERE pr_number = ?",
            (pr_number,),
        )
        for req in requirements:
            await self._db.execute(
                """
                INSERT INTO pr_review_requirements
                    (pr_number, role, required_count, pipeline_run_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    pr_number,
                    req["role"],
                    req.get("count", 1),
                    pipeline_run_id,
                ),
            )
        await self._db.commit()

    async def get_pr_requirements(self, pr_number: int) -> list[dict[str, Any]]:
        """Get review requirements for a PR."""
        cursor = await self._db.execute(
            "SELECT * FROM pr_review_requirements WHERE pr_number = ?",
            (pr_number,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── PR Approvals ─────────────────────────────────────────────────────────

    async def record_pr_approval(
        self,
        pr_number: int,
        role: str,
        *,
        approved: bool,
        review_id: str | None = None,
    ) -> None:
        """Record a PR approval or rejection."""
        await self._db.execute(
            """
            INSERT INTO pr_approvals (pr_number, role, approved, review_id, stale)
            VALUES (?, ?, ?, ?, 0)
            """,
            (pr_number, role, 1 if approved else 0, review_id),
        )
        await self._db.commit()

    async def get_pr_approvals(
        self,
        pr_number: int,
        *,
        role: str | None = None,
        include_stale: bool = False,
    ) -> list[dict[str, Any]]:
        """Get PR approval records, optionally filtered by role."""
        conditions = ["pr_number = ?"]
        params: list[Any] = [pr_number]

        if not include_stale:
            conditions.append("stale = 0")
        if role:
            conditions.append("role = ?")
            params.append(role)

        where = " AND ".join(conditions)
        cursor = await self._db.execute(
            f"SELECT * FROM pr_approvals WHERE {where} ORDER BY recorded_at",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def invalidate_pr_approvals(self, pr_number: int) -> int:
        """Mark all approvals for a PR as stale (e.g. on synchronize).

        Returns the number of approvals invalidated.
        """
        cursor = await self._db.execute(
            "UPDATE pr_approvals SET stale = 1 WHERE pr_number = ? AND stale = 0",
            (pr_number,),
        )
        await self._db.commit()
        return cursor.rowcount

    async def check_pr_merge_ready(self, pr_number: int) -> tuple[bool, list[str]]:
        """Check if a PR has met all review requirements.

        Returns (is_ready, list_of_missing_reasons).
        """
        requirements = await self.get_pr_requirements(pr_number)
        if not requirements:
            return True, []

        approvals = await self.get_pr_approvals(pr_number)
        approval_counts: dict[str, int] = {}
        for a in approvals:
            if a["approved"]:
                role = a["role"]
                approval_counts[role] = approval_counts.get(role, 0) + 1

        missing: list[str] = []
        for req in requirements:
            role = req["role"]
            needed = req["required_count"]
            got = approval_counts.get(role, 0)
            if got < needed:
                missing.append(f"{role}: {got}/{needed} approvals")

        return len(missing) == 0, missing

    async def cleanup_pr_data(self, pr_number: int) -> None:
        """Remove all PR-related data (requirements, approvals, sequence state)."""
        await self._db.execute(
            "DELETE FROM pr_review_requirements WHERE pr_number = ?", (pr_number,)
        )
        await self._db.execute("DELETE FROM pr_approvals WHERE pr_number = ?", (pr_number,))
        await self._db.execute("DELETE FROM pr_sequence_state WHERE pr_number = ?", (pr_number,))
        await self._db.commit()

    # ── PR Sequence State ────────────────────────────────────────────────────

    async def set_pr_sequence(
        self,
        pr_number: int,
        sequence: list[str],
        *,
        pipeline_run_id: str | None = None,
    ) -> None:
        """Set the review sequence for a PR. Unlocks the first role."""
        await self._db.execute(
            """
            INSERT OR REPLACE INTO pr_sequence_state
                (pr_number, current_role, sequence_index, pipeline_run_id)
            VALUES (?, ?, 0, ?)
            """,
            (pr_number, sequence[0] if sequence else "", pipeline_run_id),
        )
        await self._db.commit()

    async def get_pr_sequence_state(self, pr_number: int) -> dict[str, Any] | None:
        """Get the current sequence state for a PR."""
        cursor = await self._db.execute(
            "SELECT * FROM pr_sequence_state WHERE pr_number = ?",
            (pr_number,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)


# ── SQL Schema ───────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    definition_snapshot TEXT NOT NULL DEFAULT '{}',

    trigger_event TEXT,
    trigger_delivery_id TEXT UNIQUE,
    issue_number INTEGER,
    pr_number INTEGER,
    scope TEXT DEFAULT 'single-pr',

    parent_run_id TEXT REFERENCES pipeline_runs(run_id),
    parent_stage_id TEXT,
    nesting_depth INTEGER DEFAULT 0,

    status TEXT DEFAULT 'pending',
    current_stage_id TEXT,

    context TEXT DEFAULT '{}',

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT,

    error_message TEXT,
    error_stage_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pr
    ON pipeline_runs(pr_number, status);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_issue
    ON pipeline_runs(issue_number, status);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_parent
    ON pipeline_runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status
    ON pipeline_runs(status);

CREATE TABLE IF NOT EXISTS pipeline_stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,

    status TEXT DEFAULT 'pending',
    agent_id TEXT,

    branch_id TEXT,
    parent_stage_id TEXT,

    child_pipeline_run_id TEXT REFERENCES pipeline_runs(run_id),

    outputs TEXT DEFAULT '{}',
    error_message TEXT,

    attempt_number INTEGER DEFAULT 1,
    max_attempts INTEGER DEFAULT 1,

    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_stage_runs_run
    ON pipeline_stage_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_stage_runs_agent
    ON pipeline_stage_runs(agent_id);

CREATE TABLE IF NOT EXISTS pipeline_gate_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_run_id INTEGER NOT NULL REFERENCES pipeline_stage_runs(id) ON DELETE CASCADE,
    check_type TEXT NOT NULL,
    check_config TEXT,

    passed INTEGER,
    message TEXT DEFAULT '',
    result_data TEXT DEFAULT '{}',

    checked_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_human_stage_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_run_id INTEGER NOT NULL REFERENCES pipeline_stage_runs(id) ON DELETE CASCADE,

    entry_notified_at TEXT,
    last_reminder_at TEXT,
    reminder_count INTEGER DEFAULT 0,

    assigned_users TEXT DEFAULT '[]',

    completed_by TEXT,
    completed_action TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_pr_associations (
    pipeline_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    pr_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    stage_id TEXT,
    role TEXT,

    PRIMARY KEY(pipeline_run_id, pr_number, repo)
);

CREATE TABLE IF NOT EXISTS pr_review_requirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    role TEXT NOT NULL,
    required_count INTEGER DEFAULT 1,
    pipeline_run_id TEXT REFERENCES pipeline_runs(run_id),

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pr_number, role)
);

CREATE INDEX IF NOT EXISTS idx_pr_review_requirements_pr
    ON pr_review_requirements(pr_number);

CREATE TABLE IF NOT EXISTS pr_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    role TEXT NOT NULL,
    approved INTEGER NOT NULL,
    review_id TEXT,
    stale INTEGER DEFAULT 0,

    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pr_approvals_pr
    ON pr_approvals(pr_number, stale);

CREATE TABLE IF NOT EXISTS pr_sequence_state (
    pr_number INTEGER NOT NULL PRIMARY KEY,
    current_role TEXT NOT NULL,
    sequence_index INTEGER DEFAULT 0,
    pipeline_run_id TEXT REFERENCES pipeline_runs(run_id)
);
"""


# ── Row-to-Model Converters ─────────────────────────────────────────────────


def _dt_to_str(dt: datetime | None) -> str | None:
    """Convert datetime to ISO string for SQLite storage."""
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: str | None) -> datetime | None:
    """Parse ISO string from SQLite back to datetime."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _row_to_pipeline_run(row: aiosqlite.Row) -> PipelineRun:
    """Convert a database row to a PipelineRun model."""
    context = row["context"]
    if isinstance(context, str):
        context = json.loads(context)

    return PipelineRun(
        run_id=row["run_id"],
        pipeline_name=row["pipeline_name"],
        definition_snapshot=row["definition_snapshot"],
        trigger_event=row["trigger_event"],
        trigger_delivery_id=row["trigger_delivery_id"],
        issue_number=row["issue_number"],
        pr_number=row["pr_number"],
        scope=PipelineScope(row["scope"]) if row["scope"] else PipelineScope.SINGLE_PR,
        parent_run_id=row["parent_run_id"],
        parent_stage_id=row["parent_stage_id"],
        nesting_depth=row["nesting_depth"] or 0,
        status=PipelineRunStatus(row["status"]),
        current_stage_id=row["current_stage_id"],
        context=context,
        created_at=_str_to_dt(row["created_at"]),
        started_at=_str_to_dt(row["started_at"]),
        completed_at=_str_to_dt(row["completed_at"]),
        error_message=row["error_message"],
        error_stage_id=row["error_stage_id"],
    )


def _row_to_stage_run(row: aiosqlite.Row) -> StageRun:
    """Convert a database row to a StageRun model."""
    outputs = row["outputs"]
    if isinstance(outputs, str):
        outputs = json.loads(outputs)

    return StageRun(
        id=row["id"],
        run_id=row["run_id"],
        stage_id=row["stage_id"],
        status=StageRunStatus(row["status"]),
        agent_id=row["agent_id"],
        branch_id=row["branch_id"],
        parent_stage_id=row["parent_stage_id"],
        child_pipeline_run_id=row["child_pipeline_run_id"],
        outputs=outputs,
        error_message=row["error_message"],
        attempt_number=row["attempt_number"] or 1,
        max_attempts=row["max_attempts"] or 1,
        started_at=_str_to_dt(row["started_at"]),
        completed_at=_str_to_dt(row["completed_at"]),
    )


def _row_to_gate_check(row: aiosqlite.Row) -> GateCheckRecord:
    """Convert a database row to a GateCheckRecord model."""
    result_data = row["result_data"]
    if isinstance(result_data, str):
        result_data = json.loads(result_data)

    passed_raw = row["passed"]
    passed = None if passed_raw is None else bool(passed_raw)

    return GateCheckRecord(
        id=row["id"],
        stage_run_id=row["stage_run_id"],
        check_type=row["check_type"],
        check_config=row["check_config"],
        passed=passed,
        message=row["message"] or "",
        result_data=result_data,
        checked_at=_str_to_dt(row["checked_at"]) or datetime.now(timezone.utc),
    )


def _row_to_human_stage_state(row: aiosqlite.Row) -> HumanStageState:
    """Convert a database row to a HumanStageState model."""
    assigned = row["assigned_users"]
    if isinstance(assigned, str):
        assigned = json.loads(assigned)

    return HumanStageState(
        id=row["id"],
        stage_run_id=row["stage_run_id"],
        entry_notified_at=_str_to_dt(row["entry_notified_at"]),
        last_reminder_at=_str_to_dt(row["last_reminder_at"]),
        reminder_count=row["reminder_count"] or 0,
        assigned_users=assigned or [],
        completed_by=row["completed_by"],
        completed_action=row["completed_action"],
    )
