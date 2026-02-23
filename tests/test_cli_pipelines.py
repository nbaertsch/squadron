"""Tests for CLI pipeline commands (AD-019 Phase 5).

Tests cover:
- squadron pipelines list
- squadron pipelines runs
- squadron pipelines run <run-id>
- squadron pipelines cancel <run-id>
- Error handling (connection errors, auth failures, 404s)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_mock_response(status_code: int = 200, json_data: dict | None = None):
    """Create a mock httpx Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = ""
    return resp


# ── Tests: pipelines list ────────────────────────────────────────────────────


class TestPipelinesList:
    def test_list_pipelines_output(self, capsys, monkeypatch):
        monkeypatch.setenv("SQUADRON_URL", "http://localhost:8000")
        monkeypatch.delenv("SQUADRON_DASHBOARD_API_KEY", raising=False)

        mock_resp = _make_mock_response(
            200,
            {
                "count": 2,
                "pipelines": [
                    {
                        "name": "pr-review",
                        "description": "Review PRs",
                        "scope": "single-pr",
                        "trigger": {"event": "pull_request.opened", "conditions": {}},
                        "stage_count": 3,
                        "stages": [],
                        "reactive_events": [],
                    },
                    {
                        "name": "deploy",
                        "description": "Deploy to prod",
                        "scope": "single-pr",
                        "trigger": {"event": "push", "conditions": {}},
                        "stage_count": 2,
                        "stages": [],
                        "reactive_events": [],
                    },
                ],
            },
        )

        with patch("squadron.__main__._dashboard_request", return_value=mock_resp.json()):
            from squadron.__main__ import _pipelines_list

            args = MagicMock()
            args.url = "http://localhost:8000"
            args.api_key = None
            _pipelines_list(args)

        captured = capsys.readouterr()
        assert "pr-review" in captured.out
        assert "deploy" in captured.out
        assert "NAME" in captured.out

    def test_list_empty(self, capsys, monkeypatch):
        monkeypatch.setenv("SQUADRON_URL", "http://localhost:8000")

        with patch(
            "squadron.__main__._dashboard_request",
            return_value={"count": 0, "pipelines": []},
        ):
            from squadron.__main__ import _pipelines_list

            args = MagicMock()
            args.url = "http://localhost:8000"
            args.api_key = None
            _pipelines_list(args)

        captured = capsys.readouterr()
        assert "No pipelines registered" in captured.out


# ── Tests: pipelines runs ────────────────────────────────────────────────────


class TestPipelinesRuns:
    def test_runs_output(self, capsys, monkeypatch):
        monkeypatch.setenv("SQUADRON_URL", "http://localhost:8000")

        with patch(
            "squadron.__main__._dashboard_request",
            return_value={
                "total": 1,
                "count": 1,
                "offset": 0,
                "runs": [
                    {
                        "run_id": "abc-123",
                        "pipeline_name": "pr-review",
                        "status": "running",
                        "pr_number": 42,
                        "issue_number": None,
                        "created_at": "2025-01-01T00:00:00+00:00",
                    }
                ],
            },
        ):
            from squadron.__main__ import _pipelines_runs

            args = MagicMock()
            args.url = "http://localhost:8000"
            args.api_key = None
            args.limit = 25
            args.status = None
            args.pipeline = None
            args.pr = None
            args.issue = None
            _pipelines_runs(args)

        captured = capsys.readouterr()
        assert "abc-123" in captured.out
        assert "pr-review" in captured.out
        assert "running" in captured.out

    def test_runs_empty(self, capsys):
        with patch(
            "squadron.__main__._dashboard_request",
            return_value={"total": 0, "count": 0, "offset": 0, "runs": []},
        ):
            from squadron.__main__ import _pipelines_runs

            args = MagicMock()
            args.url = "http://localhost:8000"
            args.api_key = None
            args.limit = 25
            args.status = None
            args.pipeline = None
            args.pr = None
            args.issue = None
            _pipelines_runs(args)

        captured = capsys.readouterr()
        assert "No pipeline runs found" in captured.out


# ── Tests: pipelines run <run-id> ────────────────────────────────────────────


class TestPipelinesRunDetail:
    def test_run_detail_output(self, capsys):
        with patch(
            "squadron.__main__._dashboard_request",
            return_value={
                "run": {
                    "run_id": "abc-123",
                    "pipeline_name": "pr-review",
                    "status": "running",
                    "scope": "single-pr",
                    "pr_number": 42,
                    "issue_number": None,
                    "trigger_event": "pull_request.opened",
                    "parent_run_id": None,
                    "created_at": "2025-01-01T00:00:00",
                    "started_at": "2025-01-01T00:00:01",
                    "completed_at": None,
                    "current_stage_id": "build",
                    "error_message": None,
                },
                "definition_stages": [
                    {"id": "build", "type": "agent"},
                    {"id": "review", "type": "gate"},
                ],
                "stage_runs": [
                    {
                        "id": 1,
                        "run_id": "abc-123",
                        "stage_id": "build",
                        "status": "completed",
                        "agent_id": "agent-1",
                        "branch_id": None,
                        "duration_seconds": 45.2,
                        "error_message": None,
                    },
                    {
                        "id": 2,
                        "run_id": "abc-123",
                        "stage_id": "review",
                        "status": "running",
                        "agent_id": None,
                        "branch_id": None,
                        "duration_seconds": None,
                        "error_message": None,
                    },
                ],
                "children": [],
            },
        ):
            from squadron.__main__ import _pipelines_run_detail

            args = MagicMock()
            args.url = "http://localhost:8000"
            args.api_key = None
            args.run_id = "abc-123"
            _pipelines_run_detail(args)

        captured = capsys.readouterr()
        assert "abc-123" in captured.out
        assert "pr-review" in captured.out
        assert "running" in captured.out
        assert "build" in captured.out
        assert "review" in captured.out
        assert "45.2s" in captured.out


# ── Tests: pipelines cancel <run-id> ────────────────────────────────────────


class TestPipelinesCancel:
    def test_cancel_success(self, capsys):
        with patch(
            "squadron.__main__._dashboard_request",
            return_value={"cancelled": True, "run_id": "abc-123"},
        ):
            from squadron.__main__ import _pipelines_cancel

            args = MagicMock()
            args.url = "http://localhost:8000"
            args.api_key = None
            args.run_id = "abc-123"
            _pipelines_cancel(args)

        captured = capsys.readouterr()
        assert "cancelled" in captured.out.lower()

    def test_cancel_failure(self, capsys):
        with patch(
            "squadron.__main__._dashboard_request",
            return_value={"cancelled": False, "run_id": "abc-123"},
        ):
            from squadron.__main__ import _pipelines_cancel

            args = MagicMock()
            args.url = "http://localhost:8000"
            args.api_key = None
            args.run_id = "abc-123"
            _pipelines_cancel(args)

        captured = capsys.readouterr()
        assert "Failed" in captured.out


# ── Tests: Error handling ────────────────────────────────────────────────────


class TestErrorHandling:
    def test_missing_url_exits(self, monkeypatch):
        monkeypatch.delenv("SQUADRON_URL", raising=False)
        from squadron.__main__ import _get_dashboard_url

        args = MagicMock()
        args.url = None
        with pytest.raises(SystemExit):
            _get_dashboard_url(args)

    def test_connection_error_exits(self, monkeypatch):
        import httpx

        monkeypatch.setenv("SQUADRON_URL", "http://localhost:9999")

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.request.side_effect = httpx.ConnectError("Connection refused")
            mock_client_cls.return_value = mock_client

            from squadron.__main__ import _dashboard_request

            with pytest.raises(SystemExit):
                _dashboard_request("GET", "http://localhost:9999/test", None)

    def test_auth_error_exits(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_resp = _make_mock_response(401)
            mock_client.request.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            from squadron.__main__ import _dashboard_request

            with pytest.raises(SystemExit):
                _dashboard_request("GET", "http://localhost:8000/test", None)
