"""Workflow registry — persistence for workflow runs and stage executions.

Provides CRUD operations for workflow state stored in SQLite.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from squadron.config import (
    GateCheckResult,
    StageRun,
    StageRunStatus,
    WorkflowRun,
    WorkflowRunStatus,
)

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


class WorkflowRegistryV2:
    """Registry for workflow v2 runs and stage executions."""

    def __init__(self, db: "aiosqlite.Connection"):
        self.db = db

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        await self.db.executescript(
            """
            -- Workflow runs
            CREATE TABLE IF NOT EXISTS workflow_runs_v2 (
                run_id TEXT PRIMARY KEY,
                workflow_name TEXT NOT NULL,

                -- Trigger context
                trigger_event TEXT,
                trigger_delivery_id TEXT,
                issue_number INTEGER,
                pr_number INTEGER,

                -- Execution state
                status TEXT DEFAULT 'pending',
                current_stage_id TEXT,
                current_stage_index INTEGER DEFAULT 0,

                -- Iteration tracking (JSON)
                iteration_counts TEXT DEFAULT '{}',

                -- Context (JSON)
                context TEXT DEFAULT '{}',
                outputs TEXT DEFAULT '{}',

                -- Timestamps
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                completed_at TEXT,

                -- Error tracking
                error_message TEXT,
                error_stage TEXT
            );

            -- Stage executions
            CREATE TABLE IF NOT EXISTS workflow_stage_runs_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES workflow_runs_v2(run_id),
                stage_id TEXT NOT NULL,
                stage_index INTEGER NOT NULL,

                -- Execution
                status TEXT DEFAULT 'pending',
                agent_id TEXT,

                -- For parallel stages
                branch_id TEXT,
                parent_stage_id TEXT,

                -- Results (JSON)
                outputs TEXT DEFAULT '{}',
                error_message TEXT,

                -- Timing
                started_at TEXT,
                completed_at TEXT,

                -- Retry tracking
                attempt_number INTEGER DEFAULT 1,
                max_attempts INTEGER DEFAULT 1
            );

            -- Gate check results
            CREATE TABLE IF NOT EXISTS workflow_gate_checks_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage_run_id INTEGER NOT NULL REFERENCES workflow_stage_runs_v2(id),
                check_type TEXT NOT NULL,

                -- Result
                passed INTEGER,
                result_data TEXT DEFAULT '{}',
                error_message TEXT,

                checked_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Indexes
            CREATE INDEX IF NOT EXISTS idx_wfr2_workflow_name ON workflow_runs_v2(workflow_name);
            CREATE INDEX IF NOT EXISTS idx_wfr2_status ON workflow_runs_v2(status);
            CREATE INDEX IF NOT EXISTS idx_wfr2_issue ON workflow_runs_v2(issue_number);
            CREATE INDEX IF NOT EXISTS idx_wfr2_pr ON workflow_runs_v2(pr_number);
            CREATE INDEX IF NOT EXISTS idx_wsr2_run_id ON workflow_stage_runs_v2(run_id);
            CREATE INDEX IF NOT EXISTS idx_wsr2_agent_id ON workflow_stage_runs_v2(agent_id);
            """
        )
        await self.db.commit()

    # ── Workflow Run CRUD ─────────────────────────────────────────────────────

    async def create_workflow_run(self, run: WorkflowRun) -> None:
        """Create a new workflow run."""
        await self.db.execute(
            """
            INSERT INTO workflow_runs_v2 (
                run_id, workflow_name, trigger_event, trigger_delivery_id,
                issue_number, pr_number, status, current_stage_id,
                current_stage_index, iteration_counts, context, outputs,
                created_at, started_at, completed_at, error_message, error_stage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.workflow_name,
                run.trigger_event,
                run.trigger_delivery_id,
                run.issue_number,
                run.pr_number,
                run.status.value,
                run.current_stage_id,
                run.current_stage_index,
                json.dumps(run.iteration_counts),
                json.dumps(run.context),
                json.dumps(run.outputs),
                run.created_at.isoformat() if run.created_at else None,
                run.started_at.isoformat() if run.started_at else None,
                run.completed_at.isoformat() if run.completed_at else None,
                run.error_message,
                run.error_stage,
            ),
        )
        await self.db.commit()
        logger.debug("Created workflow run: %s", run.run_id)

    async def get_workflow_run(self, run_id: str) -> WorkflowRun | None:
        """Get a workflow run by ID."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_runs_v2 WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_workflow_run(row)

    async def get_workflow_runs_by_issue(self, issue_number: int) -> list[WorkflowRun]:
        """Get all workflow runs for an issue."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_runs_v2 WHERE issue_number = ? ORDER BY created_at DESC",
            (issue_number,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_workflow_run(row) for row in rows]

    async def get_workflow_runs_by_pr(self, pr_number: int) -> list[WorkflowRun]:
        """Get all workflow runs for a PR."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_runs_v2 WHERE pr_number = ? ORDER BY created_at DESC",
            (pr_number,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_workflow_run(row) for row in rows]

    async def get_active_workflow_runs(self) -> list[WorkflowRun]:
        """Get all running workflow runs."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_runs_v2 WHERE status IN ('pending', 'running') ORDER BY created_at",
        )
        rows = await cursor.fetchall()
        return [self._row_to_workflow_run(row) for row in rows]

    async def get_workflow_run_by_name_and_issue(
        self, workflow_name: str, issue_number: int
    ) -> WorkflowRun | None:
        """Get active workflow run by name and issue (for duplicate prevention)."""
        cursor = await self.db.execute(
            """
            SELECT * FROM workflow_runs_v2
            WHERE workflow_name = ? AND issue_number = ? AND status IN ('pending', 'running')
            ORDER BY created_at DESC LIMIT 1
            """,
            (workflow_name, issue_number),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_workflow_run(row)

    async def update_workflow_run(self, run: WorkflowRun) -> None:
        """Update a workflow run."""
        await self.db.execute(
            """
            UPDATE workflow_runs_v2 SET
                status = ?,
                current_stage_id = ?,
                current_stage_index = ?,
                iteration_counts = ?,
                context = ?,
                outputs = ?,
                started_at = ?,
                completed_at = ?,
                error_message = ?,
                error_stage = ?
            WHERE run_id = ?
            """,
            (
                run.status.value,
                run.current_stage_id,
                run.current_stage_index,
                json.dumps(run.iteration_counts),
                json.dumps(run.context),
                json.dumps(run.outputs),
                run.started_at.isoformat() if run.started_at else None,
                run.completed_at.isoformat() if run.completed_at else None,
                run.error_message,
                run.error_stage,
                run.run_id,
            ),
        )
        await self.db.commit()

    async def delete_workflow_run(self, run_id: str) -> None:
        """Delete a workflow run and its stage runs."""
        # Delete gate checks first
        await self.db.execute(
            """
            DELETE FROM workflow_gate_checks_v2
            WHERE stage_run_id IN (
                SELECT id FROM workflow_stage_runs_v2 WHERE run_id = ?
            )
            """,
            (run_id,),
        )
        # Delete stage runs
        await self.db.execute(
            "DELETE FROM workflow_stage_runs_v2 WHERE run_id = ?",
            (run_id,),
        )
        # Delete workflow run
        await self.db.execute(
            "DELETE FROM workflow_runs_v2 WHERE run_id = ?",
            (run_id,),
        )
        await self.db.commit()

    def _row_to_workflow_run(self, row) -> WorkflowRun:
        """Convert a database row to a WorkflowRun model."""
        return WorkflowRun(
            run_id=row["run_id"],
            workflow_name=row["workflow_name"],
            trigger_event=row["trigger_event"],
            trigger_delivery_id=row["trigger_delivery_id"],
            issue_number=row["issue_number"],
            pr_number=row["pr_number"],
            status=WorkflowRunStatus(row["status"]),
            current_stage_id=row["current_stage_id"],
            current_stage_index=row["current_stage_index"] or 0,
            iteration_counts=json.loads(row["iteration_counts"] or "{}"),
            context=json.loads(row["context"] or "{}"),
            outputs=json.loads(row["outputs"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"])
            if row["completed_at"]
            else None,
            error_message=row["error_message"],
            error_stage=row["error_stage"],
        )

    # ── Stage Run CRUD ────────────────────────────────────────────────────────

    async def create_stage_run(self, stage_run: StageRun) -> int:
        """Create a new stage run. Returns the database ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO workflow_stage_runs_v2 (
                run_id, stage_id, stage_index, status, agent_id,
                branch_id, parent_stage_id, outputs, error_message,
                started_at, completed_at, attempt_number, max_attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage_run.run_id,
                stage_run.stage_id,
                stage_run.stage_index,
                stage_run.status.value,
                stage_run.agent_id,
                stage_run.branch_id,
                stage_run.parent_stage_id,
                json.dumps(stage_run.outputs),
                stage_run.error_message,
                stage_run.started_at.isoformat() if stage_run.started_at else None,
                stage_run.completed_at.isoformat() if stage_run.completed_at else None,
                stage_run.attempt_number,
                stage_run.max_attempts,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_stage_run(self, stage_run_id: int) -> StageRun | None:
        """Get a stage run by ID."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_stage_runs_v2 WHERE id = ?",
            (stage_run_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_stage_run(row)

    async def get_stage_runs_for_workflow(self, run_id: str) -> list[StageRun]:
        """Get all stage runs for a workflow run."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_stage_runs_v2 WHERE run_id = ? ORDER BY stage_index, id",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_stage_run(row) for row in rows]

    async def get_latest_stage_run(self, run_id: str, stage_id: str) -> StageRun | None:
        """Get the most recent stage run for a specific stage."""
        cursor = await self.db.execute(
            """
            SELECT * FROM workflow_stage_runs_v2
            WHERE run_id = ? AND stage_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (run_id, stage_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_stage_run(row)

    async def get_stage_run_by_agent(self, agent_id: str) -> StageRun | None:
        """Get stage run by agent ID."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_stage_runs_v2 WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
            (agent_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_stage_run(row)

    async def update_stage_run(self, stage_run: StageRun) -> None:
        """Update a stage run."""
        if stage_run.id is None:
            raise ValueError("Cannot update stage run without ID")

        await self.db.execute(
            """
            UPDATE workflow_stage_runs_v2 SET
                status = ?,
                agent_id = ?,
                outputs = ?,
                error_message = ?,
                started_at = ?,
                completed_at = ?,
                attempt_number = ?
            WHERE id = ?
            """,
            (
                stage_run.status.value,
                stage_run.agent_id,
                json.dumps(stage_run.outputs),
                stage_run.error_message,
                stage_run.started_at.isoformat() if stage_run.started_at else None,
                stage_run.completed_at.isoformat() if stage_run.completed_at else None,
                stage_run.attempt_number,
                stage_run.id,
            ),
        )
        await self.db.commit()

    def _row_to_stage_run(self, row) -> StageRun:
        """Convert a database row to a StageRun model."""
        return StageRun(
            id=row["id"],
            run_id=row["run_id"],
            stage_id=row["stage_id"],
            stage_index=row["stage_index"],
            status=StageRunStatus(row["status"]),
            agent_id=row["agent_id"],
            branch_id=row["branch_id"],
            parent_stage_id=row["parent_stage_id"],
            outputs=json.loads(row["outputs"] or "{}"),
            error_message=row["error_message"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"])
            if row["completed_at"]
            else None,
            attempt_number=row["attempt_number"] or 1,
            max_attempts=row["max_attempts"] or 1,
        )

    # ── Gate Check CRUD ───────────────────────────────────────────────────────

    async def create_gate_check(self, stage_run_id: int, result: GateCheckResult) -> int:
        """Record a gate check result."""
        cursor = await self.db.execute(
            """
            INSERT INTO workflow_gate_checks_v2 (
                stage_run_id, check_type, passed, result_data, error_message, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stage_run_id,
                result.check_type,
                1 if result.passed else 0,
                json.dumps(result.result_data),
                result.error_message,
                result.checked_at.isoformat(),
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_gate_checks_for_stage(self, stage_run_id: int) -> list[GateCheckResult]:
        """Get all gate check results for a stage run."""
        cursor = await self.db.execute(
            "SELECT * FROM workflow_gate_checks_v2 WHERE stage_run_id = ? ORDER BY id",
            (stage_run_id,),
        )
        rows = await cursor.fetchall()
        return [
            GateCheckResult(
                check_type=row["check_type"],
                passed=bool(row["passed"]),
                result_data=json.loads(row["result_data"] or "{}"),
                error_message=row["error_message"],
                checked_at=datetime.fromisoformat(row["checked_at"]) if row["checked_at"] else None,
            )
            for row in rows
        ]
