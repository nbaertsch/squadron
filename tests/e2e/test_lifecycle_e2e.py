"""E2E lifecycle tests: webhook -> PM agent -> triage -> verify.

Full pipeline tests using real Copilot SDK sessions.  These exercise
the complete squadron lifecycle:

    1. Simulate a GitHub webhook (issue.opened)
    2. PM agent picks up the issue, creates sub-tasks
    3. Dev agent is spawned, starts a Copilot session
    4. Agent completes or blocks, verifying state transitions

Requires:
    - COPILOT_GITHUB_TOKEN (from SQ_COPILOT_TOKEN secret)
    - SQ_APP_ID_DEV, SQ_APP_PRIVATE_KEY, SQ_INSTALLATION_ID_DEV
    - E2E_TEST_OWNER, E2E_TEST_REPO

Run::

    pytest tests/e2e/test_lifecycle_e2e.py -v
"""

from __future__ import annotations

import os

import pytest

# Gate: these tests require the full set of credentials.
_HAS_CREDS = all(
    os.environ.get(v)
    for v in [
        "COPILOT_GITHUB_TOKEN",
        "SQ_APP_ID_DEV",
        "SQ_APP_PRIVATE_KEY",
        "SQ_INSTALLATION_ID_DEV",
    ]
)

pytestmark = pytest.mark.skipif(
    not _HAS_CREDS,
    reason="Lifecycle E2E requires COPILOT_GITHUB_TOKEN + GitHub App credentials",
)


class TestLifecycleE2E:
    """Placeholder for full lifecycle E2E tests.

    These will be implemented once the Copilot SDK integration is
    stabilized and the sandbox hardening proxy is proven on CI.

    The test structure will be:
        1. test_issue_opened_spawns_pm_agent
        2. test_pm_agent_creates_subtasks
        3. test_dev_agent_completes_task
        4. test_agent_blocks_and_sleeps
        5. test_wake_on_blocker_resolved
    """

    @pytest.mark.skip(reason="Lifecycle E2E not yet implemented — placeholder for CI")
    async def test_issue_opened_spawns_agent(self) -> None:
        """Simulate issue.opened webhook and verify an agent is spawned."""

    @pytest.mark.skip(reason="Lifecycle E2E not yet implemented — placeholder for CI")
    async def test_agent_completes_and_cleans_up(self) -> None:
        """Verify agent reaches COMPLETED status and resources are freed."""
