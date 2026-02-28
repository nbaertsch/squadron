"""Regression tests for dashboard API endpoint authentication enforcement.

Issue #56: Dashboard API endpoints do not enforce authentication
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def dashboard_app():
    """Create a FastAPI test app with dashboard router configured."""
    # Import fresh copy of dashboard to avoid module-level state issues
    import squadron.dashboard as dashboard_mod

    # Setup mocks for registry and activity logger
    mock_registry = MagicMock()
    mock_registry.get_all_active_agents = AsyncMock(return_value=[])
    mock_registry.get_recent_agents = AsyncMock(return_value=[])
    mock_registry.get_agent = AsyncMock(return_value=None)

    mock_activity = MagicMock()
    mock_activity.get_recent_activity = AsyncMock(return_value=[])
    mock_activity.get_agent_activity = AsyncMock(return_value=[])
    mock_activity.get_agent_stats = AsyncMock(
        return_value={
            "agent_id": "test-agent",
            "total_events": 0,
            "tool_calls": 0,
            "errors": 0,
            "avg_tool_duration_ms": 0.0,
        }
    )

    dashboard_mod.configure(mock_activity, mock_registry)

    app = FastAPI()
    app.include_router(dashboard_mod.router)
    return app


@pytest.fixture
def client_with_key(dashboard_app, monkeypatch):
    """Test client with SQUADRON_DASHBOARD_API_KEY configured."""
    monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "test-secret-key-12345")
    return TestClient(dashboard_app, raise_server_exceptions=False)


@pytest.fixture
def client_no_key(dashboard_app, monkeypatch):
    """Test client without SQUADRON_DASHBOARD_API_KEY configured."""
    monkeypatch.delenv("SQUADRON_DASHBOARD_API_KEY", raising=False)
    return TestClient(dashboard_app, raise_server_exceptions=False)


@pytest.fixture
def client_empty_key(dashboard_app, monkeypatch):
    """Test client with SQUADRON_DASHBOARD_API_KEY set to empty string.

    This represents a misconfigured deployment (e.g., Docker Compose env var
    without a value, or a secrets manager that resolves to empty).
    """
    monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "")
    return TestClient(dashboard_app, raise_server_exceptions=False)


# ── Tests: Endpoints return 401 when API key is configured ─────────────────


class TestAuthEnforcedWhenKeyConfigured:
    """When SQUADRON_DASHBOARD_API_KEY is set, all endpoints must require auth."""

    def test_agents_requires_auth_missing_header(self, client_with_key):
        """GET /dashboard/agents returns 401 when Authorization header is missing."""
        response = client_with_key.get("/dashboard/agents")
        assert response.status_code == 401, (
            f"Expected 401 Unauthorized, got {response.status_code}. "
            "Dashboard /agents endpoint must require authentication when API key is configured."
        )

    def test_activity_requires_auth_missing_header(self, client_with_key):
        """GET /dashboard/activity returns 401 when Authorization header is missing."""
        response = client_with_key.get("/dashboard/activity")
        assert response.status_code == 401, (
            f"Expected 401 Unauthorized, got {response.status_code}. "
            "Dashboard /activity endpoint must require authentication when API key is configured."
        )

    def test_activity_with_filter_requires_auth(self, client_with_key):
        """GET /dashboard/activity?event_types=... returns 401 without auth."""
        response = client_with_key.get("/dashboard/activity?event_types=tool_call_start")
        assert response.status_code == 401, (
            f"Expected 401 Unauthorized, got {response.status_code}. "
            "Filtered /activity endpoint must require authentication."
        )

    def test_agents_requires_auth_wrong_token(self, client_with_key):
        """GET /dashboard/agents returns 401 when wrong Bearer token is provided."""
        response = client_with_key.get(
            "/dashboard/agents",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401, (
            f"Expected 401 Unauthorized, got {response.status_code}. "
            "Dashboard /agents must reject invalid tokens."
        )

    def test_activity_requires_auth_wrong_token(self, client_with_key):
        """GET /dashboard/activity returns 401 when wrong Bearer token is provided."""
        response = client_with_key.get(
            "/dashboard/activity",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    def test_agents_succeeds_with_correct_token(self, client_with_key):
        """GET /dashboard/agents returns 200 when correct Bearer token is provided."""
        response = client_with_key.get(
            "/dashboard/agents",
            headers={"Authorization": "Bearer test-secret-key-12345"},
        )
        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}. Valid token must be accepted."
        )

    def test_activity_succeeds_with_correct_token(self, client_with_key):
        """GET /dashboard/activity returns 200 when correct Bearer token is provided."""
        response = client_with_key.get(
            "/dashboard/activity",
            headers={"Authorization": "Bearer test-secret-key-12345"},
        )
        assert response.status_code == 200

    def test_401_response_has_www_authenticate_header(self, client_with_key):
        """401 response should include WWW-Authenticate: Bearer header."""
        response = client_with_key.get("/dashboard/agents")
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers
        assert response.headers["WWW-Authenticate"] == "Bearer"


# ── Tests: Empty string API key should also enforce authentication ──────────


class TestEmptyStringApiKeyBypassRegression:
    """Regression tests for the empty-string API key bypass.

    When SQUADRON_DASHBOARD_API_KEY="" (empty string, e.g., misconfigured
    environment), the `if not expected_key:` check was falsy, bypassing
    authentication entirely. Fix: use `if expected_key is None:` instead.

    Reference: Issue #56 — security-review identified inconsistency between
    get_security_config() (which reports authentication_required=True for empty
    string) and require_api_key() (which allows all traffic through).
    """

    def test_agents_rejects_unauthenticated_when_key_is_empty_string(self, client_empty_key):
        """GET /dashboard/agents must NOT return data when API key is empty string.

        Empty string is not None — the env var IS configured (intentionally or
        by mistake). The endpoint should not silently allow all traffic through.
        """
        response = client_empty_key.get("/dashboard/agents")
        # Should NOT return 200 with data (this was the bug)
        assert response.status_code != 200, (
            "BUG: /dashboard/agents returned 200 when SQUADRON_DASHBOARD_API_KEY=''. "
            "An empty-string key must not bypass authentication. "
            "Fix: use `if expected_key is None:` instead of `if not expected_key:` "
            "in require_api_key() and validate_sse_token()."
        )

    def test_activity_rejects_unauthenticated_when_key_is_empty_string(self, client_empty_key):
        """GET /dashboard/activity must NOT return data when API key is empty string."""
        response = client_empty_key.get("/dashboard/activity")
        assert response.status_code != 200, (
            "BUG: /dashboard/activity returned 200 when SQUADRON_DASHBOARD_API_KEY=''. "
            "An empty-string key must not bypass authentication."
        )

    def test_security_config_consistent_with_auth_behavior_empty_string(
        self, client_empty_key, monkeypatch
    ):
        """security config and actual auth behavior must be consistent for empty string.

        get_security_config() returns authentication_required: True for empty string
        (since '' is not None), but require_api_key was allowing all requests through.
        This tests that the behavior is now consistent.
        """
        from squadron.dashboard_security import get_security_config

        config = get_security_config()
        # Empty string is not None, so config says auth is required
        # (this is expected - empty key means misconfigured but "set")
        # The auth behavior should match: if config says required, requests without
        # auth should fail.
        response = client_empty_key.get("/dashboard/agents")
        if config["authentication_required"]:
            assert response.status_code != 200, (
                "Inconsistency: get_security_config() reports authentication_required=True "
                "but /dashboard/agents returned 200 without credentials."
            )


# ── Tests: No authentication required when key is not configured ─────────────


class TestNoAuthWhenKeyNotConfigured:
    """When SQUADRON_DASHBOARD_API_KEY is not set, endpoints are open."""

    def test_agents_accessible_without_auth(self, client_no_key):
        """GET /dashboard/agents returns 200 without auth when no key configured."""
        response = client_no_key.get("/dashboard/agents")
        assert response.status_code == 200

    def test_activity_accessible_without_auth(self, client_no_key):
        """GET /dashboard/activity returns 200 without auth when no key configured."""
        response = client_no_key.get("/dashboard/activity")
        assert response.status_code == 200


# ── Tests: SSE endpoints ─────────────────────────────────────────────────────


class TestSseEndpointAuth:
    """SSE endpoints must validate ?token=<api_key> query parameter."""

    def test_sse_stream_requires_token_when_key_configured(self, client_with_key):
        """GET /dashboard/stream returns 401 when token query param is missing."""
        response = client_with_key.get("/dashboard/stream")
        assert response.status_code == 401, (
            f"Expected 401, got {response.status_code}. "
            "SSE /stream endpoint must require ?token= when API key is configured."
        )

    def test_sse_stream_rejects_wrong_token(self, client_with_key):
        """GET /dashboard/stream returns 401 with wrong token."""
        response = client_with_key.get("/dashboard/stream?token=wrong-token")
        assert response.status_code == 401

    def test_sse_rejects_unauthenticated_when_key_is_empty_string(self, client_empty_key):
        """GET /dashboard/stream must NOT return data when API key is empty string."""
        response = client_empty_key.get("/dashboard/stream")
        assert response.status_code != 200, (
            "BUG: /dashboard/stream returned 200 when SQUADRON_DASHBOARD_API_KEY=''. "
            "An empty-string key must not bypass authentication in validate_sse_token()."
        )
