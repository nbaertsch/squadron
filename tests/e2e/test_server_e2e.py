"""E2E tests for the full Squadron server — NOTHING MOCKED.

Boots the real SquadronServer with real GitHub App credentials,
real SQLite, real config. Tests the full lifecycle:
  - Server startup (config load, DB init, label creation, agent recovery)
  - HTTP endpoints (/health, /agents)
  - Webhook processing with real payloads
  - Server shutdown

The only thing that doesn't run is the Copilot SDK (PM CopilotAgent)
because that requires authentication. The CopilotAgent is patched
only where it would block server startup — all GitHub operations are real.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from squadron.models import AgentRecord, AgentRole, AgentStatus
from squadron.registry import AgentRegistry

_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env")


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        pytest.skip(f"Missing env var {key}")
    return val


@pytest.fixture
def e2e_config_dir(tmp_path):
    """Create a .squadron/ config pointing at the real E2E test repo."""
    owner = _require_env("E2E_TEST_OWNER")
    repo = _require_env("E2E_TEST_REPO")

    sq_dir = tmp_path / ".squadron"
    agents_dir = sq_dir / "agents"
    agents_dir.mkdir(parents=True)

    (sq_dir / "config.yaml").write_text(f"""
project:
  name: squadron-e2e
  owner: "{owner}"
  repo: "{repo}"
  default_branch: main

labels:
  types: [e2e-feature, e2e-bug]
  priorities: [e2e-high, e2e-low]
  states: [e2e-in-progress]

runtime:
  default_model: "claude-sonnet-4"
  provider:
    type: anthropic
    base_url: "https://api.anthropic.com"
""")
    (agents_dir / "pm.md").write_text(
        "# PM Agent\n## System Prompt\nYou are the PM agent for E2E tests.\n"
    )
    return tmp_path


def _get_private_key() -> str:
    """Load PEM from env var (CI) or file path (local dev)."""
    pem = os.environ.get("SQ_APP_PRIVATE_KEY", "").strip()
    if pem:
        return pem

    key_file = os.environ.get("SQ_APP_PRIVATE_KEY_FILE", "").strip()
    if not key_file:
        pytest.skip("Missing SQ_APP_PRIVATE_KEY or SQ_APP_PRIVATE_KEY_FILE")
    key_path = Path(key_file)
    if not key_path.is_absolute():
        key_path = _project_root / key_path
    if not key_path.exists():
        pytest.skip(f"Private key file not found: {key_path}")
    return key_path.read_text()


def _env_with_creds() -> dict:
    """Build env dict with real GitHub App credentials."""
    return {
        "GITHUB_APP_ID": _require_env("SQ_APP_ID_DEV"),
        "GITHUB_PRIVATE_KEY": _get_private_key(),
        "GITHUB_INSTALLATION_ID": _require_env("SQ_INSTALLATION_ID_DEV"),
        "GITHUB_WEBHOOK_SECRET": "e2e-webhook-secret",
    }


# ── Server Boot ──────────────────────────────────────────────────────────────


class TestServerBootE2E:
    """Test the real server startup with real GitHub credentials."""

    async def test_full_boot_creates_labels_on_github(self, e2e_config_dir):
        """Server boot calls ensure_labels_exist on the REAL GitHub repo."""
        from squadron.server import SquadronServer

        env = _env_with_creds()

        server = SquadronServer(repo_root=e2e_config_dir)
        with patch.dict(os.environ, env):
            # Only mock CopilotAgent (requires Copilot auth)
            with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                mock_copilot = AsyncMock()
                MockCA.return_value = mock_copilot

                await server.start()

                try:
                    # Verify server booted with real config
                    assert server.config.project.name == "squadron-e2e"
                    assert server.config.project.owner == _require_env("E2E_TEST_OWNER")

                    # Verify GitHub client is real and authenticated
                    assert server.github is not None
                    # Token should have been obtained during label creation
                    if server.github._token is not None:
                        assert server.github._token.startswith("ghs_")

                    # Verify labels were actually created on GitHub
                    owner = server.config.project.owner
                    repo = server.config.project.repo
                    resp = await server.github._request(
                        "GET",
                        f"/repos/{owner}/{repo}/labels",
                    )
                    label_names = [lbl["name"] for lbl in resp.json()]
                    assert "e2e-feature" in label_names
                    assert "e2e-bug" in label_names
                    assert "e2e-high" in label_names
                finally:
                    await server.stop()

                    # Cleanup — remove E2E labels from the real repo
                    from squadron.github_client import GitHubClient

                    cleanup = GitHubClient(
                        app_id=env["GITHUB_APP_ID"],
                        private_key=env["GITHUB_PRIVATE_KEY"],
                        installation_id=env["GITHUB_INSTALLATION_ID"],
                    )
                    await cleanup.start()
                    owner = _require_env("E2E_TEST_OWNER")
                    repo = _require_env("E2E_TEST_REPO")
                    for label in [
                        "e2e-feature",
                        "e2e-bug",
                        "e2e-high",
                        "e2e-low",
                        "e2e-in-progress",
                    ]:
                        try:
                            await cleanup._request(
                                "DELETE",
                                f"/repos/{owner}/{repo}/labels/{label}",
                            )
                        except Exception:
                            pass
                    await cleanup.close()

    async def test_stale_agent_recovery_with_real_db(self, e2e_config_dir):
        """Stale ACTIVE agents are marked SLEEPING on boot with real SQLite."""
        from squadron.server import SquadronServer

        # Pre-populate real SQLite DB
        data_dir = e2e_config_dir / ".squadron-data"
        data_dir.mkdir()
        reg = AgentRegistry(str(data_dir / "registry.db"))
        await reg.initialize()
        await reg.create_agent(
            AgentRecord(
                agent_id="e2e-stale-agent",
                role=AgentRole.FEAT_DEV,
                issue_number=999,
                status=AgentStatus.ACTIVE,
            )
        )
        await reg.close()

        env = _env_with_creds()
        server = SquadronServer(repo_root=e2e_config_dir)
        with patch.dict(os.environ, env):
            with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                MockCA.return_value = AsyncMock()

                await server.start()

                try:
                    recovered = await server.registry.get_agent("e2e-stale-agent")
                    assert recovered.status == AgentStatus.SLEEPING
                finally:
                    await server.stop()


# ── HTTP Endpoints ───────────────────────────────────────────────────────────


class TestServerEndpointsE2E:
    """Test HTTP endpoints through real TestClient with real GitHub backend."""

    def test_health_returns_real_data(self, e2e_config_dir):
        from squadron.server import create_app

        env = _env_with_creds()
        with patch.dict(os.environ, env):
            with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                MockCA.return_value = AsyncMock()

                app = create_app(repo_root=e2e_config_dir)
                with TestClient(app) as client:
                    resp = client.get("/health")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["status"] == "ok"
                    assert data["project"] == "squadron-e2e"

    def test_webhook_with_real_signature_verification(self, e2e_config_dir):
        """Send a webhook with a valid HMAC signature through the real endpoint."""
        from squadron.server import create_app

        env = _env_with_creds()
        webhook_secret = env["GITHUB_WEBHOOK_SECRET"]

        with patch.dict(os.environ, env):
            with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                MockCA.return_value = AsyncMock()

                app = create_app(repo_root=e2e_config_dir)
                with TestClient(app) as client:
                    # Build a realistic webhook payload
                    payload = {
                        "action": "opened",
                        "issue": {
                            "number": 1,
                            "title": "E2E test issue",
                            "body": "test",
                            "state": "open",
                            "labels": [],
                            "user": {"login": "testuser", "type": "User"},
                        },
                        "repository": {
                            "full_name": f"{_require_env('E2E_TEST_OWNER')}/{_require_env('E2E_TEST_REPO')}",
                            "owner": {"login": _require_env("E2E_TEST_OWNER")},
                        },
                        "sender": {"login": "testuser", "type": "User"},
                        "installation": {"id": int(_require_env("SQ_INSTALLATION_ID_DEV"))},
                    }
                    body = json.dumps(payload).encode()

                    # Compute real HMAC signature
                    sig = (
                        "sha256="
                        + hmac.new(
                            webhook_secret.encode(),
                            body,
                            hashlib.sha256,
                        ).hexdigest()
                    )

                    resp = client.post(
                        "/webhook",
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-GitHub-Event": "issues",
                            "X-GitHub-Delivery": str(uuid.uuid4()),
                            "X-Hub-Signature-256": sig,
                        },
                    )
                    assert resp.status_code == 200

    def test_webhook_rejected_with_bad_signature(self, e2e_config_dir):
        """Webhook with invalid signature should be rejected."""
        from squadron.server import create_app

        env = _env_with_creds()

        with patch.dict(os.environ, env):
            with patch("squadron.agent_manager.CopilotAgent") as MockCA:
                MockCA.return_value = AsyncMock()

                app = create_app(repo_root=e2e_config_dir)
                with TestClient(app) as client:
                    payload = json.dumps({"action": "opened"}).encode()

                    resp = client.post(
                        "/webhook",
                        content=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-GitHub-Event": "issues",
                            "X-GitHub-Delivery": str(uuid.uuid4()),
                            "X-Hub-Signature-256": "sha256=invalid",
                        },
                    )
                    # Should be rejected (403 or similar)
                    assert resp.status_code in (400, 401, 403)
