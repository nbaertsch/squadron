"""Tests for dashboard pipeline visibility endpoints (AD-019 Phase 5).

Tests cover:
- GET /dashboard/pipelines — list pipeline definitions
- GET /dashboard/pipelines/runs — list pipeline runs (pagination, filters)
- GET /dashboard/pipelines/runs/{run_id} — run detail with stage runs
- POST /dashboard/pipelines/runs/{run_id}/cancel — cancel pipeline run
- GET /dashboard/pipelines/stream — SSE stream (connection only)
- Authentication enforcement on all pipeline endpoints
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from squadron.pipeline.models import (
    PipelineDefinition,
    PipelineRun,
    PipelineRunStatus,
    PipelineScope,
    StageDefinition,
    StageRun,
    StageRunStatus,
    StageType,
    TriggerDefinition,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_pipeline_run(
    run_id: str = "run-001",
    pipeline_name: str = "test-pipeline",
    status: PipelineRunStatus = PipelineRunStatus.RUNNING,
    pr_number: int | None = 42,
    issue_number: int | None = None,
) -> PipelineRun:
    now = datetime.now(timezone.utc)
    return PipelineRun(
        run_id=run_id,
        pipeline_name=pipeline_name,
        definition_snapshot=json.dumps(
            {"stages": [{"id": "build", "type": "agent"}, {"id": "review", "type": "gate"}]}
        ),
        status=status,
        pr_number=pr_number,
        issue_number=issue_number,
        scope=PipelineScope.SINGLE_PR,
        created_at=now,
        started_at=now,
        current_stage_id="build",
    )


def _make_stage_run(
    run_id: str = "run-001",
    stage_id: str = "build",
    status: StageRunStatus = StageRunStatus.RUNNING,
    agent_id: str | None = "agent-abc",
) -> StageRun:
    now = datetime.now(timezone.utc)
    return StageRun(
        id=1,
        run_id=run_id,
        stage_id=stage_id,
        status=status,
        agent_id=agent_id,
        started_at=now,
    )


def _make_pipeline_definition(name: str = "test-pipeline") -> PipelineDefinition:
    return PipelineDefinition(
        description=f"Test pipeline {name}",
        trigger=TriggerDefinition(event="pull_request.opened"),
        scope=PipelineScope.SINGLE_PR,
        stages=[
            StageDefinition(id="build", type=StageType.AGENT, agent="builder"),
            StageDefinition(
                id="review",
                type=StageType.GATE,
                conditions=[{"check": "ci_status"}],
            ),
        ],
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_pipeline_engine():
    engine = MagicMock()
    engine.list_pipelines.return_value = ["pr-review", "deploy"]
    engine.get_pipeline.side_effect = lambda name: {
        "pr-review": _make_pipeline_definition("pr-review"),
        "deploy": _make_pipeline_definition("deploy"),
    }.get(name)
    engine.cancel_pipeline = AsyncMock(return_value=True)
    return engine


@pytest.fixture
def mock_pipeline_registry():
    registry = MagicMock()
    runs = [
        _make_pipeline_run("run-001", "pr-review", PipelineRunStatus.RUNNING, pr_number=10),
        _make_pipeline_run("run-002", "deploy", PipelineRunStatus.COMPLETED, pr_number=11),
    ]
    registry.get_recent_pipeline_runs = AsyncMock(return_value=runs)
    registry.count_pipeline_runs = AsyncMock(return_value=2)
    registry.get_active_pipeline_runs = AsyncMock(return_value=[runs[0]])
    registry.get_pipeline_run = AsyncMock(return_value=runs[0])
    registry.get_stage_runs_for_pipeline = AsyncMock(
        return_value=[
            _make_stage_run("run-001", "build", StageRunStatus.COMPLETED, "agent-1"),
            _make_stage_run("run-001", "review", StageRunStatus.RUNNING),
        ]
    )
    registry.get_child_pipelines = AsyncMock(return_value=[])
    registry.get_pipeline_runs_by_pr = AsyncMock(return_value=[runs[0]])
    registry.get_pipeline_runs_by_issue = AsyncMock(return_value=[])
    return registry


@pytest.fixture
def dashboard_app(mock_pipeline_engine, mock_pipeline_registry):
    """Create a FastAPI test app with dashboard router and pipeline deps configured."""
    import squadron.dashboard as dashboard_mod

    mock_registry = MagicMock()
    mock_registry.get_all_active_agents = AsyncMock(return_value=[])
    mock_registry.get_recent_agents = AsyncMock(return_value=[])

    mock_activity = MagicMock()
    mock_activity.get_recent_activity = AsyncMock(return_value=[])

    dashboard_mod.configure(
        mock_activity,
        mock_registry,
        pipeline_engine=mock_pipeline_engine,
        pipeline_registry=mock_pipeline_registry,
    )

    app = FastAPI()
    app.include_router(dashboard_mod.router)
    return app


@pytest.fixture
def client(dashboard_app, monkeypatch):
    """Test client with no auth key (open access)."""
    monkeypatch.delenv("SQUADRON_DASHBOARD_API_KEY", raising=False)
    return TestClient(dashboard_app, raise_server_exceptions=False)


@pytest.fixture
def auth_client(dashboard_app, monkeypatch):
    """Test client with auth key configured."""
    monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "test-key-123")
    return TestClient(dashboard_app, raise_server_exceptions=False)


# ── Tests: GET /dashboard/pipelines ──────────────────────────────────────────


class TestListPipelines:
    def test_returns_pipeline_definitions(self, client):
        response = client.get("/dashboard/pipelines")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["pipelines"]) == 2

        names = {p["name"] for p in data["pipelines"]}
        assert names == {"pr-review", "deploy"}

    def test_pipeline_detail_fields(self, client):
        response = client.get("/dashboard/pipelines")
        data = response.json()
        p = data["pipelines"][0]
        assert "name" in p
        assert "description" in p
        assert "scope" in p
        assert "trigger" in p
        assert "stage_count" in p
        assert "stages" in p
        assert p["stage_count"] == 2
        assert p["stages"][0]["id"] == "build"
        assert p["stages"][0]["type"] == "agent"

    def test_requires_auth_when_key_configured(self, auth_client):
        response = auth_client.get("/dashboard/pipelines")
        assert response.status_code == 401

    def test_succeeds_with_correct_auth(self, auth_client):
        response = auth_client.get(
            "/dashboard/pipelines",
            headers={"Authorization": "Bearer test-key-123"},
        )
        assert response.status_code == 200


# ── Tests: GET /dashboard/pipelines/runs ─────────────────────────────────────


class TestListPipelineRuns:
    def test_returns_recent_runs(self, client):
        response = client.get("/dashboard/pipelines/runs")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["count"] == 2
        assert len(data["runs"]) == 2

    def test_run_fields(self, client):
        response = client.get("/dashboard/pipelines/runs")
        data = response.json()
        run = data["runs"][0]
        assert "run_id" in run
        assert "pipeline_name" in run
        assert "status" in run
        assert "created_at" in run
        assert "pr_number" in run

    def test_status_filter(self, client, mock_pipeline_registry):
        response = client.get("/dashboard/pipelines/runs?status=running")
        assert response.status_code == 200
        # Verify the registry was called with the status filter
        mock_pipeline_registry.get_recent_pipeline_runs.assert_called()
        call_kwargs = mock_pipeline_registry.get_recent_pipeline_runs.call_args
        assert call_kwargs.kwargs["status"] == PipelineRunStatus.RUNNING

    def test_invalid_status_returns_400(self, client):
        response = client.get("/dashboard/pipelines/runs?status=bogus")
        assert response.status_code == 400

    def test_pr_filter(self, client, mock_pipeline_registry):
        response = client.get("/dashboard/pipelines/runs?pr_number=10")
        assert response.status_code == 200
        mock_pipeline_registry.get_pipeline_runs_by_pr.assert_called_once()

    def test_issue_filter(self, client, mock_pipeline_registry):
        response = client.get("/dashboard/pipelines/runs?issue_number=5")
        assert response.status_code == 200
        mock_pipeline_registry.get_pipeline_runs_by_issue.assert_called_once()

    def test_pagination(self, client, mock_pipeline_registry):
        response = client.get("/dashboard/pipelines/runs?limit=10&offset=5")
        assert response.status_code == 200
        call_kwargs = mock_pipeline_registry.get_recent_pipeline_runs.call_args
        assert call_kwargs.kwargs["limit"] == 10
        assert call_kwargs.kwargs["offset"] == 5

    def test_requires_auth_when_key_configured(self, auth_client):
        response = auth_client.get("/dashboard/pipelines/runs")
        assert response.status_code == 401


# ── Tests: GET /dashboard/pipelines/runs/{run_id} ───────────────────────────


class TestGetPipelineRunDetail:
    def test_returns_run_with_stages(self, client):
        response = client.get("/dashboard/pipelines/runs/run-001")
        assert response.status_code == 200
        data = response.json()
        assert data["run"]["run_id"] == "run-001"
        assert data["run"]["status"] == "running"
        assert len(data["stage_runs"]) == 2
        assert data["stage_runs"][0]["stage_id"] == "build"

    def test_includes_definition_stages(self, client):
        response = client.get("/dashboard/pipelines/runs/run-001")
        data = response.json()
        assert len(data["definition_stages"]) == 2
        assert data["definition_stages"][0]["id"] == "build"
        assert data["definition_stages"][1]["type"] == "gate"

    def test_includes_children(self, client, mock_pipeline_registry):
        child = _make_pipeline_run("run-003", "sub-pipeline", PipelineRunStatus.COMPLETED)
        mock_pipeline_registry.get_child_pipelines = AsyncMock(return_value=[child])
        response = client.get("/dashboard/pipelines/runs/run-001")
        data = response.json()
        assert len(data["children"]) == 1
        assert data["children"][0]["run_id"] == "run-003"

    def test_not_found(self, client, mock_pipeline_registry):
        mock_pipeline_registry.get_pipeline_run = AsyncMock(return_value=None)
        response = client.get("/dashboard/pipelines/runs/nonexistent")
        assert response.status_code == 404

    def test_requires_auth_when_key_configured(self, auth_client):
        response = auth_client.get("/dashboard/pipelines/runs/run-001")
        assert response.status_code == 401


# ── Tests: POST /dashboard/pipelines/runs/{run_id}/cancel ────────────────────


class TestCancelPipelineRun:
    def test_cancel_success(self, client, mock_pipeline_engine):
        response = client.post("/dashboard/pipelines/runs/run-001/cancel")
        assert response.status_code == 200
        data = response.json()
        assert data["cancelled"] is True
        assert data["run_id"] == "run-001"
        mock_pipeline_engine.cancel_pipeline.assert_awaited_once_with("run-001")

    def test_cancel_not_found(self, client, mock_pipeline_engine, mock_pipeline_registry):
        mock_pipeline_engine.cancel_pipeline = AsyncMock(return_value=False)
        mock_pipeline_registry.get_pipeline_run = AsyncMock(return_value=None)
        response = client.post("/dashboard/pipelines/runs/nonexistent/cancel")
        assert response.status_code == 404

    def test_cancel_already_completed(self, client, mock_pipeline_engine, mock_pipeline_registry):
        mock_pipeline_engine.cancel_pipeline = AsyncMock(return_value=False)
        completed_run = _make_pipeline_run("run-done", "test", PipelineRunStatus.COMPLETED)
        mock_pipeline_registry.get_pipeline_run = AsyncMock(return_value=completed_run)
        response = client.post("/dashboard/pipelines/runs/run-done/cancel")
        assert response.status_code == 409

    def test_requires_auth_when_key_configured(self, auth_client):
        response = auth_client.post("/dashboard/pipelines/runs/run-001/cancel")
        assert response.status_code == 401


# ── Tests: GET /dashboard/pipelines/stream ───────────────────────────────────


class TestPipelineStream:
    def test_sse_requires_token_when_key_configured(self, auth_client):
        response = auth_client.get("/dashboard/pipelines/stream")
        assert response.status_code == 401

    def test_sse_rejects_wrong_token(self, auth_client):
        response = auth_client.get("/dashboard/pipelines/stream?token=wrong")
        assert response.status_code == 401


# ── Tests: /dashboard/status includes pipeline info ──────────────────────────


class TestStatusIncludesPipelines:
    def test_status_shows_pipeline_engine(self, client):
        response = client.get("/dashboard/status")
        assert response.status_code == 200
        data = response.json()
        assert "pipeline_engine" in data
        assert "pipeline_registry" in data
        assert data["pipeline_engine"] is True
        assert data["pipeline_registry"] is True


# ── Tests: Pipeline endpoints return 503 when not configured ─────────────────


class TestPipelineEndpointsUnconfigured:
    """Endpoints should return 503 when pipeline engine/registry not configured."""

    @pytest.fixture
    def unconfigured_app(self):
        import squadron.dashboard as dashboard_mod

        mock_registry = MagicMock()
        mock_activity = MagicMock()
        # Configure WITHOUT pipeline engine/registry
        dashboard_mod.configure(mock_activity, mock_registry)

        app = FastAPI()
        app.include_router(dashboard_mod.router)
        return app

    @pytest.fixture
    def unconfigured_client(self, unconfigured_app, monkeypatch):
        monkeypatch.delenv("SQUADRON_DASHBOARD_API_KEY", raising=False)
        return TestClient(unconfigured_app, raise_server_exceptions=False)

    def test_list_pipelines_503(self, unconfigured_client):
        response = unconfigured_client.get("/dashboard/pipelines")
        assert response.status_code == 503

    def test_list_runs_503(self, unconfigured_client):
        response = unconfigured_client.get("/dashboard/pipelines/runs")
        assert response.status_code == 503

    def test_run_detail_503(self, unconfigured_client):
        response = unconfigured_client.get("/dashboard/pipelines/runs/run-001")
        assert response.status_code == 503

    def test_cancel_503(self, unconfigured_client):
        response = unconfigured_client.post("/dashboard/pipelines/runs/run-001/cancel")
        assert response.status_code == 503
