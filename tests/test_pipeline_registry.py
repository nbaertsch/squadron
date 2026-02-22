"""Tests for the pipeline registry — SQLite CRUD for pipeline runs, stages, gates, etc."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio

from squadron.pipeline.models import (
    GateCheckRecord,
    HumanStageState,
    PipelineRun,
    PipelineRunStatus,
    PipelineScope,
    StageRun,
    StageRunStatus,
)
from squadron.pipeline.registry import PipelineRegistry


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = tmp_path / "test_pipeline.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


@pytest_asyncio.fixture
async def registry(db):
    reg = PipelineRegistry(db)
    await reg.initialize()
    return reg


# ── Factory Helpers ──────────────────────────────────────────────────────────


def make_pipeline_run(
    run_id: str = "run-001",
    pipeline_name: str = "test-pipeline",
    **overrides,
) -> PipelineRun:
    defaults: dict = dict(
        run_id=run_id,
        pipeline_name=pipeline_name,
        definition_snapshot="{}",
        status=PipelineRunStatus.PENDING,
        scope=PipelineScope.SINGLE_PR,
        pr_number=42,
    )
    defaults.update(overrides)
    return PipelineRun(**defaults)


def make_stage_run(
    run_id: str = "run-001",
    stage_id: str = "stage-1",
    **overrides,
) -> StageRun:
    defaults: dict = dict(
        run_id=run_id,
        stage_id=stage_id,
        status=StageRunStatus.PENDING,
    )
    defaults.update(overrides)
    return StageRun(**defaults)


def make_gate_check(
    stage_run_id: int = 1,
    check_type: str = "ci_status",
    **overrides,
) -> GateCheckRecord:
    defaults: dict = dict(
        stage_run_id=stage_run_id,
        check_type=check_type,
        passed=True,
        message="all checks passed",
        checked_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return GateCheckRecord(**defaults)


def make_human_stage_state(
    stage_run_id: int = 1,
    **overrides,
) -> HumanStageState:
    defaults: dict = dict(
        stage_run_id=stage_run_id,
        reminder_count=0,
        assigned_users=["alice"],
    )
    defaults.update(overrides)
    return HumanStageState(**defaults)


# ── Pipeline Run Tests ───────────────────────────────────────────────────────


class TestPipelineRunCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get_roundtrip(self, registry: PipelineRegistry):
        run = make_pipeline_run()
        await registry.create_pipeline_run(run)

        fetched = await registry.get_pipeline_run("run-001")
        assert fetched is not None
        assert fetched.run_id == "run-001"
        assert fetched.pipeline_name == "test-pipeline"
        assert fetched.status == PipelineRunStatus.PENDING
        assert fetched.scope == PipelineScope.SINGLE_PR
        assert fetched.pr_number == 42

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, registry: PipelineRegistry):
        result = await registry.get_pipeline_run("does-not-exist")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_pipeline_runs_by_pr(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run("run-a", pr_number=10))
        await registry.create_pipeline_run(make_pipeline_run("run-b", pr_number=10))
        await registry.create_pipeline_run(make_pipeline_run("run-c", pr_number=20))

        runs = await registry.get_pipeline_runs_by_pr(10)
        assert len(runs) == 2
        assert {r.run_id for r in runs} == {"run-a", "run-b"}

    @pytest.mark.asyncio
    async def test_get_pipeline_runs_by_pr_with_status_filter(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(
            make_pipeline_run("run-a", pr_number=10, status=PipelineRunStatus.PENDING)
        )
        await registry.create_pipeline_run(
            make_pipeline_run("run-b", pr_number=10, status=PipelineRunStatus.COMPLETED)
        )

        pending = await registry.get_pipeline_runs_by_pr(10, status=PipelineRunStatus.PENDING)
        assert len(pending) == 1
        assert pending[0].run_id == "run-a"

    @pytest.mark.asyncio
    async def test_get_pipeline_runs_by_issue(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(
            make_pipeline_run("run-a", issue_number=5, scope=PipelineScope.ISSUE)
        )
        await registry.create_pipeline_run(
            make_pipeline_run("run-b", issue_number=5, scope=PipelineScope.ISSUE)
        )
        await registry.create_pipeline_run(
            make_pipeline_run("run-c", issue_number=7, scope=PipelineScope.ISSUE)
        )

        runs = await registry.get_pipeline_runs_by_issue(5)
        assert len(runs) == 2
        assert {r.run_id for r in runs} == {"run-a", "run-b"}

    @pytest.mark.asyncio
    async def test_get_active_pipeline_runs(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(
            make_pipeline_run("r1", status=PipelineRunStatus.PENDING)
        )
        await registry.create_pipeline_run(
            make_pipeline_run("r2", status=PipelineRunStatus.RUNNING)
        )
        await registry.create_pipeline_run(
            make_pipeline_run("r3", status=PipelineRunStatus.COMPLETED)
        )
        await registry.create_pipeline_run(make_pipeline_run("r4", status=PipelineRunStatus.FAILED))

        active = await registry.get_active_pipeline_runs()
        assert len(active) == 2
        assert {r.run_id for r in active} == {"r1", "r2"}

    @pytest.mark.asyncio
    async def test_get_running_pipelines_for_pr_direct(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(
            make_pipeline_run("r1", pr_number=42, status=PipelineRunStatus.RUNNING)
        )
        await registry.create_pipeline_run(
            make_pipeline_run("r2", pr_number=42, status=PipelineRunStatus.COMPLETED)
        )

        running = await registry.get_running_pipelines_for_pr(42)
        assert len(running) == 1
        assert running[0].run_id == "r1"

    @pytest.mark.asyncio
    async def test_get_running_pipelines_for_pr_via_association(self, registry: PipelineRegistry):
        """Pipeline run on different PR but associated via pr_associations."""
        await registry.create_pipeline_run(
            make_pipeline_run(
                "r1",
                pr_number=99,
                status=PipelineRunStatus.RUNNING,
                scope=PipelineScope.MULTI_PR,
            )
        )
        await registry.add_pr_association("r1", 42, "owner/repo")

        running = await registry.get_running_pipelines_for_pr(42)
        assert len(running) == 1
        assert running[0].run_id == "r1"

    @pytest.mark.asyncio
    async def test_get_running_pipelines_for_pr_deduplicates(self, registry: PipelineRegistry):
        """Direct match + association match for the same run shouldn't duplicate."""
        await registry.create_pipeline_run(
            make_pipeline_run("r1", pr_number=42, status=PipelineRunStatus.RUNNING)
        )
        await registry.add_pr_association("r1", 42, "owner/repo")

        running = await registry.get_running_pipelines_for_pr(42)
        assert len(running) == 1

    @pytest.mark.asyncio
    async def test_update_pipeline_run(self, registry: PipelineRegistry):
        run = make_pipeline_run()
        await registry.create_pipeline_run(run)

        run.status = PipelineRunStatus.RUNNING
        run.current_stage_id = "build"
        run.error_message = "something went wrong"
        await registry.update_pipeline_run(run)

        fetched = await registry.get_pipeline_run("run-001")
        assert fetched is not None
        assert fetched.status == PipelineRunStatus.RUNNING
        assert fetched.current_stage_id == "build"
        assert fetched.error_message == "something went wrong"

    @pytest.mark.asyncio
    async def test_delete_pipeline_run(self, registry: PipelineRegistry):
        run = make_pipeline_run()
        await registry.create_pipeline_run(run)

        stage = make_stage_run("run-001", "s1")
        await registry.create_stage_run(stage)

        await registry.add_pr_association("run-001", 42, "owner/repo")

        await registry.delete_pipeline_run("run-001")

        assert await registry.get_pipeline_run("run-001") is None
        assert await registry.get_stage_runs_for_pipeline("run-001") == []
        assert await registry.get_pr_associations("run-001") == []

    @pytest.mark.asyncio
    async def test_pipeline_run_context_roundtrip(self, registry: PipelineRegistry):
        run = make_pipeline_run(context={"repo": "owner/repo", "ref": "main"})
        await registry.create_pipeline_run(run)

        fetched = await registry.get_pipeline_run("run-001")
        assert fetched is not None
        assert fetched.context == {"repo": "owner/repo", "ref": "main"}


# ── Stage Run Tests ──────────────────────────────────────────────────────────


class TestStageRunCRUD:
    @pytest.mark.asyncio
    async def test_create_returns_autoincrement_id(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        stage = make_stage_run()
        stage_id = await registry.create_stage_run(stage)
        assert isinstance(stage_id, int)
        assert stage_id >= 1

    @pytest.mark.asyncio
    async def test_get_stage_run_roundtrip(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        stage = make_stage_run(agent_id="agent-abc")
        row_id = await registry.create_stage_run(stage)

        fetched = await registry.get_stage_run(row_id)
        assert fetched is not None
        assert fetched.id == row_id
        assert fetched.run_id == "run-001"
        assert fetched.stage_id == "stage-1"
        assert fetched.status == StageRunStatus.PENDING
        assert fetched.agent_id == "agent-abc"

    @pytest.mark.asyncio
    async def test_get_stage_run_nonexistent(self, registry: PipelineRegistry):
        result = await registry.get_stage_run(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_stage_runs_for_pipeline_ordered(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        id1 = await registry.create_stage_run(make_stage_run(stage_id="s1"))
        id2 = await registry.create_stage_run(make_stage_run(stage_id="s2"))
        id3 = await registry.create_stage_run(make_stage_run(stage_id="s3"))

        stages = await registry.get_stage_runs_for_pipeline("run-001")
        assert len(stages) == 3
        assert [s.id for s in stages] == [id1, id2, id3]

    @pytest.mark.asyncio
    async def test_get_latest_stage_run(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        await registry.create_stage_run(
            make_stage_run(stage_id="build", status=StageRunStatus.FAILED)
        )
        id2 = await registry.create_stage_run(
            make_stage_run(stage_id="build", status=StageRunStatus.RUNNING)
        )

        latest = await registry.get_latest_stage_run("run-001", "build")
        assert latest is not None
        assert latest.id == id2
        assert latest.status == StageRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_get_latest_stage_run_nonexistent(self, registry: PipelineRegistry):
        result = await registry.get_latest_stage_run("run-001", "no-such-stage")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_stage_run_by_agent(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        row_id = await registry.create_stage_run(make_stage_run(agent_id="agent-xyz"))

        fetched = await registry.get_stage_run_by_agent("agent-xyz")
        assert fetched is not None
        assert fetched.id == row_id
        assert fetched.agent_id == "agent-xyz"

    @pytest.mark.asyncio
    async def test_get_stage_run_by_agent_nonexistent(self, registry: PipelineRegistry):
        result = await registry.get_stage_run_by_agent("no-such-agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_stage_run(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        row_id = await registry.create_stage_run(make_stage_run())

        fetched = await registry.get_stage_run(row_id)
        assert fetched is not None
        fetched.status = StageRunStatus.COMPLETED
        fetched.outputs = {"result": "success"}
        await registry.update_stage_run(fetched)

        updated = await registry.get_stage_run(row_id)
        assert updated is not None
        assert updated.status == StageRunStatus.COMPLETED
        assert updated.outputs == {"result": "success"}

    @pytest.mark.asyncio
    async def test_update_stage_run_without_id_raises(self, registry: PipelineRegistry):
        stage = make_stage_run()
        assert stage.id is None
        with pytest.raises(ValueError, match="Cannot update stage run without an ID"):
            await registry.update_stage_run(stage)


# ── Gate Check Tests ─────────────────────────────────────────────────────────


class TestGateCheckRecords:
    @pytest.mark.asyncio
    async def test_create_gate_check_returns_id(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        stage_id = await registry.create_stage_run(make_stage_run())

        record = make_gate_check(stage_run_id=stage_id)
        gate_id = await registry.create_gate_check(record)
        assert isinstance(gate_id, int)
        assert gate_id >= 1

    @pytest.mark.asyncio
    async def test_get_gate_checks_for_stage_ordered(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        stage_id = await registry.create_stage_run(make_stage_run())

        t1 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

        await registry.create_gate_check(
            make_gate_check(stage_run_id=stage_id, check_type="ci_status", checked_at=t1)
        )
        await registry.create_gate_check(
            make_gate_check(stage_run_id=stage_id, check_type="approval", checked_at=t2)
        )

        checks = await registry.get_gate_checks_for_stage(stage_id)
        assert len(checks) == 2
        assert checks[0].check_type == "ci_status"
        assert checks[1].check_type == "approval"

    @pytest.mark.asyncio
    async def test_gate_check_passed_none(self, registry: PipelineRegistry):
        """Gate check with passed=None (not yet evaluated)."""
        await registry.create_pipeline_run(make_pipeline_run())
        stage_id = await registry.create_stage_run(make_stage_run())

        record = make_gate_check(stage_run_id=stage_id, passed=None)
        await registry.create_gate_check(record)

        checks = await registry.get_gate_checks_for_stage(stage_id)
        assert len(checks) == 1
        assert checks[0].passed is None

    @pytest.mark.asyncio
    async def test_gate_check_result_data_roundtrip(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        stage_id = await registry.create_stage_run(make_stage_run())

        record = make_gate_check(
            stage_run_id=stage_id,
            result_data={"workflows": ["build", "test"], "all_passed": True},
        )
        await registry.create_gate_check(record)

        checks = await registry.get_gate_checks_for_stage(stage_id)
        assert checks[0].result_data == {
            "workflows": ["build", "test"],
            "all_passed": True,
        }


# ── Human Stage State Tests ──────────────────────────────────────────────────


class TestHumanStageState:
    @pytest.mark.asyncio
    async def test_create_returns_id(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        stage_id = await registry.create_stage_run(make_stage_run())

        state = make_human_stage_state(stage_run_id=stage_id)
        hss_id = await registry.create_human_stage_state(state)
        assert isinstance(hss_id, int)
        assert hss_id >= 1

    @pytest.mark.asyncio
    async def test_get_human_stage_state_roundtrip(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        stage_id = await registry.create_stage_run(make_stage_run())

        state = make_human_stage_state(stage_run_id=stage_id, assigned_users=["alice", "bob"])
        await registry.create_human_stage_state(state)

        fetched = await registry.get_human_stage_state(stage_id)
        assert fetched is not None
        assert fetched.stage_run_id == stage_id
        assert fetched.assigned_users == ["alice", "bob"]
        assert fetched.reminder_count == 0

    @pytest.mark.asyncio
    async def test_get_human_stage_state_nonexistent(self, registry: PipelineRegistry):
        result = await registry.get_human_stage_state(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_human_stage_state(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        stage_id = await registry.create_stage_run(make_stage_run())

        state = make_human_stage_state(stage_run_id=stage_id)
        await registry.create_human_stage_state(state)

        fetched = await registry.get_human_stage_state(stage_id)
        assert fetched is not None
        fetched.reminder_count = 3
        fetched.completed_by = "alice"
        fetched.completed_action = "approved"
        await registry.update_human_stage_state(fetched)

        updated = await registry.get_human_stage_state(stage_id)
        assert updated is not None
        assert updated.reminder_count == 3
        assert updated.completed_by == "alice"
        assert updated.completed_action == "approved"

    @pytest.mark.asyncio
    async def test_update_human_stage_state_without_id_raises(self, registry: PipelineRegistry):
        state = make_human_stage_state()
        assert state.id is None
        with pytest.raises(ValueError, match="Cannot update human stage state without an ID"):
            await registry.update_human_stage_state(state)


# ── PR Association Tests ─────────────────────────────────────────────────────


class TestPRAssociations:
    @pytest.mark.asyncio
    async def test_add_and_get_pr_association(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        await registry.add_pr_association("run-001", 42, "owner/repo")

        assocs = await registry.get_pr_associations("run-001")
        assert len(assocs) == 1
        assert assocs[0]["pr_number"] == 42
        assert assocs[0]["repo"] == "owner/repo"

    @pytest.mark.asyncio
    async def test_add_duplicate_pr_association_no_error(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        await registry.add_pr_association("run-001", 42, "owner/repo")
        await registry.add_pr_association("run-001", 42, "owner/repo")

        assocs = await registry.get_pr_associations("run-001")
        assert len(assocs) == 1

    @pytest.mark.asyncio
    async def test_multiple_pr_associations(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        await registry.add_pr_association("run-001", 42, "owner/repo")
        await registry.add_pr_association("run-001", 43, "owner/repo")
        await registry.add_pr_association("run-001", 44, "owner/other-repo")

        assocs = await registry.get_pr_associations("run-001")
        assert len(assocs) == 3
        pr_numbers = {a["pr_number"] for a in assocs}
        assert pr_numbers == {42, 43, 44}

    @pytest.mark.asyncio
    async def test_pr_association_with_stage_and_role(self, registry: PipelineRegistry):
        await registry.create_pipeline_run(make_pipeline_run())
        await registry.add_pr_association(
            "run-001", 42, "owner/repo", stage_id="review", role="reviewer"
        )

        assocs = await registry.get_pr_associations("run-001")
        assert len(assocs) == 1
        assert assocs[0]["stage_id"] == "review"
        assert assocs[0]["role"] == "reviewer"


# ── PR Review Requirements Tests ─────────────────────────────────────────────


class TestPRReviewRequirements:
    @pytest.mark.asyncio
    async def test_set_and_get_requirements(self, registry: PipelineRegistry):
        requirements = [
            {"role": "reviewer", "count": 2},
            {"role": "security", "count": 1},
        ]
        await registry.set_pr_requirements(42, requirements)

        fetched = await registry.get_pr_requirements(42)
        assert len(fetched) == 2
        roles = {r["role"] for r in fetched}
        assert roles == {"reviewer", "security"}

    @pytest.mark.asyncio
    async def test_set_requirements_replaces_existing(self, registry: PipelineRegistry):
        await registry.set_pr_requirements(42, [{"role": "reviewer", "count": 2}])
        await registry.set_pr_requirements(42, [{"role": "security", "count": 1}])

        fetched = await registry.get_pr_requirements(42)
        assert len(fetched) == 1
        assert fetched[0]["role"] == "security"

    @pytest.mark.asyncio
    async def test_get_requirements_empty(self, registry: PipelineRegistry):
        fetched = await registry.get_pr_requirements(999)
        assert fetched == []

    @pytest.mark.asyncio
    async def test_requirements_default_count(self, registry: PipelineRegistry):
        """When 'count' is omitted, defaults to 1."""
        await registry.set_pr_requirements(42, [{"role": "reviewer"}])

        fetched = await registry.get_pr_requirements(42)
        assert fetched[0]["required_count"] == 1


# ── PR Approvals Tests ───────────────────────────────────────────────────────


class TestPRApprovals:
    @pytest.mark.asyncio
    async def test_record_and_get_approval(self, registry: PipelineRegistry):
        await registry.record_pr_approval(42, "reviewer", approved=True)

        approvals = await registry.get_pr_approvals(42)
        assert len(approvals) == 1
        assert approvals[0]["role"] == "reviewer"
        assert approvals[0]["approved"] == 1
        assert approvals[0]["stale"] == 0

    @pytest.mark.asyncio
    async def test_get_approvals_excludes_stale_by_default(self, registry: PipelineRegistry):
        await registry.record_pr_approval(42, "reviewer", approved=True)
        await registry.invalidate_pr_approvals(42)

        approvals = await registry.get_pr_approvals(42)
        assert len(approvals) == 0

    @pytest.mark.asyncio
    async def test_get_approvals_include_stale(self, registry: PipelineRegistry):
        await registry.record_pr_approval(42, "reviewer", approved=True)
        await registry.invalidate_pr_approvals(42)

        approvals = await registry.get_pr_approvals(42, include_stale=True)
        assert len(approvals) == 1
        assert approvals[0]["stale"] == 1

    @pytest.mark.asyncio
    async def test_invalidate_pr_approvals_returns_count(self, registry: PipelineRegistry):
        await registry.record_pr_approval(42, "reviewer", approved=True)
        await registry.record_pr_approval(42, "security", approved=True)

        count = await registry.invalidate_pr_approvals(42)
        assert count == 2

    @pytest.mark.asyncio
    async def test_invalidate_already_stale_returns_zero(self, registry: PipelineRegistry):
        await registry.record_pr_approval(42, "reviewer", approved=True)
        await registry.invalidate_pr_approvals(42)

        count = await registry.invalidate_pr_approvals(42)
        assert count == 0

    @pytest.mark.asyncio
    async def test_check_pr_merge_ready_no_requirements(self, registry: PipelineRegistry):
        """No requirements means always ready."""
        ready, reasons = await registry.check_pr_merge_ready(42)
        assert ready is True
        assert reasons == []

    @pytest.mark.asyncio
    async def test_check_pr_merge_ready_satisfied(self, registry: PipelineRegistry):
        await registry.set_pr_requirements(42, [{"role": "reviewer", "count": 2}])
        await registry.record_pr_approval(42, "reviewer", approved=True)
        await registry.record_pr_approval(42, "reviewer", approved=True)

        ready, reasons = await registry.check_pr_merge_ready(42)
        assert ready is True
        assert reasons == []

    @pytest.mark.asyncio
    async def test_check_pr_merge_ready_not_satisfied(self, registry: PipelineRegistry):
        await registry.set_pr_requirements(
            42, [{"role": "reviewer", "count": 2}, {"role": "security", "count": 1}]
        )
        await registry.record_pr_approval(42, "reviewer", approved=True)

        ready, reasons = await registry.check_pr_merge_ready(42)
        assert ready is False
        assert len(reasons) == 2
        assert any("reviewer" in r and "1/2" in r for r in reasons)
        assert any("security" in r and "0/1" in r for r in reasons)

    @pytest.mark.asyncio
    async def test_check_pr_merge_ready_ignores_stale(self, registry: PipelineRegistry):
        await registry.set_pr_requirements(42, [{"role": "reviewer", "count": 1}])
        await registry.record_pr_approval(42, "reviewer", approved=True)
        await registry.invalidate_pr_approvals(42)

        ready, reasons = await registry.check_pr_merge_ready(42)
        assert ready is False

    @pytest.mark.asyncio
    async def test_check_pr_merge_ready_rejections_not_counted(self, registry: PipelineRegistry):
        await registry.set_pr_requirements(42, [{"role": "reviewer", "count": 1}])
        await registry.record_pr_approval(42, "reviewer", approved=False)

        ready, reasons = await registry.check_pr_merge_ready(42)
        assert ready is False

    @pytest.mark.asyncio
    async def test_get_approvals_filtered_by_role(self, registry: PipelineRegistry):
        await registry.record_pr_approval(42, "reviewer", approved=True)
        await registry.record_pr_approval(42, "security", approved=True)

        reviewer_approvals = await registry.get_pr_approvals(42, role="reviewer")
        assert len(reviewer_approvals) == 1
        assert reviewer_approvals[0]["role"] == "reviewer"


# ── PR Sequence State Tests ──────────────────────────────────────────────────


class TestPRSequenceState:
    @pytest.mark.asyncio
    async def test_set_and_get_sequence(self, registry: PipelineRegistry):
        await registry.set_pr_sequence(42, ["reviewer", "security", "lead"])

        state = await registry.get_pr_sequence_state(42)
        assert state is not None
        assert state["pr_number"] == 42
        assert state["current_role"] == "reviewer"
        assert state["sequence_index"] == 0

    @pytest.mark.asyncio
    async def test_get_sequence_state_nonexistent(self, registry: PipelineRegistry):
        result = await registry.get_pr_sequence_state(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_set_sequence_replaces_existing(self, registry: PipelineRegistry):
        await registry.set_pr_sequence(42, ["reviewer"])
        await registry.set_pr_sequence(42, ["security", "lead"])

        state = await registry.get_pr_sequence_state(42)
        assert state is not None
        assert state["current_role"] == "security"


# ── Cleanup Tests ────────────────────────────────────────────────────────────


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_pr_data(self, registry: PipelineRegistry):
        await registry.set_pr_requirements(42, [{"role": "reviewer", "count": 1}])
        await registry.record_pr_approval(42, "reviewer", approved=True)
        await registry.set_pr_sequence(42, ["reviewer"])

        await registry.cleanup_pr_data(42)

        assert await registry.get_pr_requirements(42) == []
        assert await registry.get_pr_approvals(42, include_stale=True) == []
        assert await registry.get_pr_sequence_state(42) is None

    @pytest.mark.asyncio
    async def test_cleanup_pr_data_idempotent(self, registry: PipelineRegistry):
        """Cleaning up a PR with no data should not error."""
        await registry.cleanup_pr_data(999)

    @pytest.mark.asyncio
    async def test_cleanup_does_not_affect_other_prs(self, registry: PipelineRegistry):
        await registry.set_pr_requirements(42, [{"role": "reviewer", "count": 1}])
        await registry.set_pr_requirements(43, [{"role": "security", "count": 1}])

        await registry.cleanup_pr_data(42)

        assert await registry.get_pr_requirements(42) == []
        reqs_43 = await registry.get_pr_requirements(43)
        assert len(reqs_43) == 1
        assert reqs_43[0]["role"] == "security"
