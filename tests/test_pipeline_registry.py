"""Tests for the PipelineRegistry — unified SQLite persistence."""

from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timezone

import aiosqlite

from squadron.pipeline.registry import (
    PipelineRegistry,
    PipelineRun,
    PipelineRunStatus,
    PipelineStageRun,
    PipelineStageStatus,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    db_file = tmp_path / "test_pipeline.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


@pytest_asyncio.fixture
async def registry(db):
    reg = PipelineRegistry(db)
    await reg.initialize()
    return reg


# ── Pipeline Run CRUD ──────────────────────────────────────────────────────────


class TestPipelineRunCrud:
    async def test_create_and_get_run(self, registry):
        run = PipelineRun(
            run_id="pl-test001",
            pipeline_name="test-pipeline",
            issue_number=42,
            pr_number=7,
            status=PipelineRunStatus.RUNNING,
        )
        await registry.create_run(run)

        fetched = await registry.get_run("pl-test001")
        assert fetched is not None
        assert fetched.run_id == "pl-test001"
        assert fetched.pipeline_name == "test-pipeline"
        assert fetched.issue_number == 42
        assert fetched.pr_number == 7
        assert fetched.status == PipelineRunStatus.RUNNING

    async def test_get_nonexistent_run(self, registry):
        result = await registry.get_run("nonexistent")
        assert result is None

    async def test_update_run_status(self, registry):
        run = PipelineRun(run_id="pl-upd001", pipeline_name="test")
        await registry.create_run(run)

        run.status = PipelineRunStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        await registry.update_run(run)

        fetched = await registry.get_run("pl-upd001")
        assert fetched.status == PipelineRunStatus.COMPLETED
        assert fetched.completed_at is not None

    async def test_get_active_runs(self, registry):
        run1 = PipelineRun(run_id="pl-a1", pipeline_name="p1", status=PipelineRunStatus.RUNNING)
        run2 = PipelineRun(run_id="pl-a2", pipeline_name="p2", status=PipelineRunStatus.WAITING)
        run3 = PipelineRun(run_id="pl-a3", pipeline_name="p3", status=PipelineRunStatus.COMPLETED)

        for r in [run1, run2, run3]:
            await registry.create_run(r)

        active = await registry.get_active_runs()
        ids = {r.run_id for r in active}
        assert "pl-a1" in ids
        assert "pl-a2" in ids
        assert "pl-a3" not in ids

    async def test_get_run_by_name_and_issue(self, registry):
        run = PipelineRun(
            run_id="pl-lookup1",
            pipeline_name="feature-pipeline",
            issue_number=100,
            status=PipelineRunStatus.RUNNING,
        )
        await registry.create_run(run)

        found = await registry.get_run_by_name_and_issue("feature-pipeline", 100)
        assert found is not None
        assert found.run_id == "pl-lookup1"

    async def test_get_run_by_name_and_issue_not_found(self, registry):
        result = await registry.get_run_by_name_and_issue("missing-pipeline", 999)
        assert result is None

    async def test_get_runs_subscribed_to_event(self, registry):
        run1 = PipelineRun(
            run_id="pl-sub1",
            pipeline_name="p1",
            status=PipelineRunStatus.WAITING,
            subscribed_events=["pull_request_review.submitted", "check_suite.completed"],
        )
        run2 = PipelineRun(
            run_id="pl-sub2",
            pipeline_name="p2",
            status=PipelineRunStatus.RUNNING,
            subscribed_events=["check_suite.completed"],
        )
        run3 = PipelineRun(
            run_id="pl-sub3",
            pipeline_name="p3",
            status=PipelineRunStatus.COMPLETED,
            subscribed_events=["pull_request_review.submitted"],
        )

        for r in [run1, run2, run3]:
            await registry.create_run(r)

        result = await registry.get_runs_subscribed_to("pull_request_review.submitted")
        ids = {r.run_id for r in result}
        assert "pl-sub1" in ids  # WAITING, subscribed
        assert "pl-sub2" not in ids  # not subscribed to this event
        assert "pl-sub3" not in ids  # COMPLETED, excluded

    async def test_delete_run(self, registry):
        run = PipelineRun(run_id="pl-del1", pipeline_name="test")
        await registry.create_run(run)

        stage_run = PipelineStageRun(
            run_id="pl-del1",
            stage_id="stage-1",
            stage_index=0,
            status=PipelineStageStatus.RUNNING,
        )
        sr_id = await registry.create_stage_run(stage_run)
        stage_run.id = sr_id

        await registry.create_gate_check(
            sr_id, "command", True, {"exit_code": 0}
        )

        await registry.delete_run("pl-del1")
        assert await registry.get_run("pl-del1") is None
        assert await registry.get_stage_run(sr_id) is None

    async def test_new_run_id_is_unique(self):
        ids = {PipelineRegistry.new_run_id() for _ in range(100)}
        assert len(ids) == 100

    async def test_context_and_outputs_persisted(self, registry):
        run = PipelineRun(
            run_id="pl-ctx1",
            pipeline_name="test",
            context={"issue_number": 5, "labels": ["feature"]},
            outputs={"stage-1": {"agent": "bot-123"}},
        )
        await registry.create_run(run)

        fetched = await registry.get_run("pl-ctx1")
        assert fetched.context["issue_number"] == 5
        assert fetched.context["labels"] == ["feature"]
        assert fetched.outputs["stage-1"]["agent"] == "bot-123"


# ── Stage Run CRUD ─────────────────────────────────────────────────────────────


class TestStageRunCrud:
    async def _make_run(self, registry, run_id="pl-sr-base"):
        run = PipelineRun(run_id=run_id, pipeline_name="test")
        await registry.create_run(run)
        return run

    async def test_create_and_get_stage_run(self, registry):
        await self._make_run(registry)

        sr = PipelineStageRun(
            run_id="pl-sr-base",
            stage_id="build",
            stage_index=0,
            status=PipelineStageStatus.RUNNING,
        )
        sr_id = await registry.create_stage_run(sr)
        assert sr_id is not None

        fetched = await registry.get_stage_run(sr_id)
        assert fetched is not None
        assert fetched.stage_id == "build"
        assert fetched.status == PipelineStageStatus.RUNNING

    async def test_update_stage_run(self, registry):
        await self._make_run(registry)

        sr = PipelineStageRun(
            run_id="pl-sr-base",
            stage_id="test",
            stage_index=1,
            status=PipelineStageStatus.RUNNING,
        )
        sr_id = await registry.create_stage_run(sr)
        sr.id = sr_id

        sr.status = PipelineStageStatus.COMPLETED
        sr.outputs = {"passed": True}
        await registry.update_stage_run(sr)

        fetched = await registry.get_stage_run(sr_id)
        assert fetched.status == PipelineStageStatus.COMPLETED
        assert fetched.outputs["passed"] is True

    async def test_update_without_id_raises(self, registry):
        sr = PipelineStageRun(run_id="x", stage_id="y", stage_index=0)
        with pytest.raises(ValueError, match="database ID"):
            await registry.update_stage_run(sr)

    async def test_get_stage_run_by_agent(self, registry):
        await self._make_run(registry)

        sr = PipelineStageRun(
            run_id="pl-sr-base",
            stage_id="review",
            stage_index=0,
            agent_id="agent-abc",
        )
        sr_id = await registry.create_stage_run(sr)

        found = await registry.get_stage_run_by_agent("agent-abc")
        assert found is not None
        assert found.stage_id == "review"

    async def test_get_stage_run_by_agent_not_found(self, registry):
        result = await registry.get_stage_run_by_agent("nonexistent-agent")
        assert result is None

    async def test_get_latest_stage_run(self, registry):
        await self._make_run(registry)

        for i in range(3):
            sr = PipelineStageRun(
                run_id="pl-sr-base",
                stage_id="gate",
                stage_index=1,
                attempt_number=i + 1,
            )
            await registry.create_stage_run(sr)

        latest = await registry.get_latest_stage_run("pl-sr-base", "gate")
        assert latest is not None
        assert latest.attempt_number == 3

    async def test_get_stage_runs_for_run(self, registry):
        await self._make_run(registry)

        for stage_id, idx in [("stage-a", 0), ("stage-b", 1), ("stage-c", 2)]:
            sr = PipelineStageRun(
                run_id="pl-sr-base",
                stage_id=stage_id,
                stage_index=idx,
            )
            await registry.create_stage_run(sr)

        all_runs = await registry.get_stage_runs_for_run("pl-sr-base")
        stage_ids = [r.stage_id for r in all_runs]
        assert stage_ids == ["stage-a", "stage-b", "stage-c"]


# ── Gate Check CRUD ────────────────────────────────────────────────────────────


class TestGateCheckCrud:
    async def _make_stage_run(self, registry):
        run = PipelineRun(run_id="pl-gc-base", pipeline_name="test")
        await registry.create_run(run)
        sr = PipelineStageRun(run_id="pl-gc-base", stage_id="gate", stage_index=0)
        sr_id = await registry.create_stage_run(sr)
        sr.id = sr_id
        return sr

    async def test_create_and_get_gate_check(self, registry):
        sr = await self._make_stage_run(registry)

        gc_id = await registry.create_gate_check(
            stage_run_id=sr.id,
            check_type="command",
            passed=True,
            result_data={"exit_code": 0},
            error_message=None,
        )
        assert gc_id is not None

        checks = await registry.get_gate_checks_for_stage(sr.id)
        assert len(checks) == 1
        assert checks[0]["check_type"] == "command"
        assert checks[0]["passed"] is True
        assert checks[0]["result_data"]["exit_code"] == 0

    async def test_multiple_gate_checks(self, registry):
        sr = await self._make_stage_run(registry)

        for check_type, passed in [("command", True), ("pr_approval", False)]:
            await registry.create_gate_check(sr.id, check_type, passed)

        checks = await registry.get_gate_checks_for_stage(sr.id)
        assert len(checks) == 2
        assert {c["check_type"] for c in checks} == {"command", "pr_approval"}
