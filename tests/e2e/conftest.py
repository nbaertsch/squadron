"""E2E conftest — loads credentials from .env / CI secrets and provides real clients.

These tests hit REAL GitHub APIs and the REAL Copilot SDK.
Nothing is mocked.

Requires:
  - GitHub App creds: SQ_APP_ID_DEV, SQ_INSTALLATION_ID_DEV,
    and EITHER  SQ_APP_PRIVATE_KEY  (PEM content — preferred in CI)
    OR          SQ_APP_PRIVATE_KEY_FILE  (path to PEM file — local dev)
  - E2E_TEST_OWNER, E2E_TEST_REPO
  - squadron-dev GitHub App installed on the test repo
  - Copilot CLI binary (ships with github-copilot-sdk pip package)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv

# Load .env from project root
_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env")


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        pytest.skip(f"Missing env var {key} — set it in .env to run E2E tests")
    return val


@pytest.fixture(scope="session")
def e2e_owner() -> str:
    return _require_env("E2E_TEST_OWNER")


@pytest.fixture(scope="session")
def e2e_repo() -> str:
    return _require_env("E2E_TEST_REPO")


@pytest.fixture(scope="session")
def app_id() -> str:
    return _require_env("SQ_APP_ID_DEV")


@pytest.fixture(scope="session")
def private_key() -> str:
    """Load PEM from env var (CI) or file path (local dev)."""
    # Prefer direct PEM content — this is what CI secrets provide
    pem = os.environ.get("SQ_APP_PRIVATE_KEY", "").strip()
    if pem:
        return pem

    # Fall back to file path for local development
    key_file = os.environ.get("SQ_APP_PRIVATE_KEY_FILE", "").strip()
    if not key_file:
        pytest.skip(
            "Missing SQ_APP_PRIVATE_KEY (PEM content) or "
            "SQ_APP_PRIVATE_KEY_FILE (path) — set one to run E2E tests"
        )
    key_path = Path(key_file)
    if not key_path.is_absolute():
        key_path = _project_root / key_path
    if not key_path.exists():
        pytest.skip(f"Private key file not found: {key_path}")
    return key_path.read_text()


@pytest.fixture(scope="session")
def installation_id() -> str:
    return _require_env("SQ_INSTALLATION_ID_DEV")


_cached_token: str | None = None
_cached_token_expires: float = 0


@pytest_asyncio.fixture
async def github_client(app_id, private_key, installation_id):
    """A real, authenticated GitHubClient. No mocks.

    Caches the installation token across test instances to avoid
    JWT throttling from GitHub (rate-limits rapid JWT exchanges).
    """
    import time

    from squadron.github_client import GitHubClient

    global _cached_token, _cached_token_expires

    client = GitHubClient(
        app_id=app_id,
        private_key=private_key,
        installation_id=installation_id,
    )
    await client.start()

    # Reuse cached token if still valid (avoids JWT → token exchange per test)
    if _cached_token and time.time() < _cached_token_expires - 60:
        client._token = _cached_token
        client._token_expires_at = _cached_token_expires
    else:
        await client._ensure_token()
        _cached_token = client._token
        _cached_token_expires = client._token_expires_at

    yield client
    await client.close()
