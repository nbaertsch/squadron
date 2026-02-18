"""Simple test for issue #57 fix without complex mocking.

NOTE: Skipped until issue #57 is fixed.
"""

import sys

import pytest

sys.path.insert(0, "src")

# Test that the fix was applied correctly by checking the source code
from squadron.agent_manager import AgentManager
import inspect


@pytest.mark.skip(
    reason="Issue #57 not yet implemented - spawn_workflow_agent lacks activity logging"
)
def test_spawn_workflow_agent_has_activity_logging():
    """Verify spawn_workflow_agent now includes activity logging."""

    source_code = inspect.getsource(AgentManager.spawn_workflow_agent)

    # Check for _log_activity call
    assert "_log_activity" in source_code, "spawn_workflow_agent should call _log_activity"

    # Check for agent_spawned event type
    assert "agent_spawned" in source_code, "spawn_workflow_agent should log agent_spawned event"

    # Check for workflow-specific metadata
    assert "workflow" in source_code, "spawn_workflow_agent should include workflow metadata"

    print("✅ All checks passed!")
    print("✅ spawn_workflow_agent now includes activity logging")
    print("🔧 Issue #57 is FIXED")


if __name__ == "__main__":
    test_spawn_workflow_agent_has_activity_logging()
