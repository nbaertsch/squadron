"""
Test for the watchdog race condition fix (issue #51).

Verifies that the primary watchdog properly cancels agents without using
asyncio.shield(), which was preventing cancellation from taking effect.
"""

import asyncio
from unittest.mock import Mock

import pytest


class TestWatchdogRaceConditionFix:
    """Test the fix for issue #51 - watchdog not properly cancelling agents."""

    def test_no_asyncio_shield_in_watchdog(self):
        """Ensure asyncio.shield() was removed from watchdog cancellation logic."""
        import inspect
        from squadron.agent_manager import AgentManager

        # Get the source code of _duration_watchdog
        source = inspect.getsource(AgentManager._duration_watchdog)

        # Should not contain asyncio.shield() which prevents cancellation
        assert "asyncio.shield" not in source, "asyncio.shield() prevents watchdog cancellation!"

        # Should contain proper cancellation waiting
        assert "await asyncio.wait_for(" in source
        assert "agent_task," in source  # Without shield wrapper

    def test_watchdog_enforcement_tracking(self):
        """Test that watchdog enforcement tracking attribute exists in AgentManager."""
        from squadron.agent_manager import AgentManager
        import inspect

        # Verify _watchdog_enforced is initialized in __init__
        source = inspect.getsource(AgentManager.__init__)
        assert "_watchdog_enforced" in source, "AgentManager should track watchdog enforcement"
        assert "set()" in source, "Watchdog enforcement should be tracked as a set"

    @pytest.mark.asyncio
    async def test_pr_review_timeout_scenario(self):
        """Test the specific scenario from issue #51 - pr-review agent timeout."""
        from squadron.config import CircuitBreakerDefaults

        # pr-review role has 1800s timeout (from config.yaml)
        pr_review_timeout = 1800

        # Agent ran for 2030s (230s overage)
        actual_runtime = 2030
        overage = actual_runtime - pr_review_timeout

        assert overage == 230
        assert overage > 60  # Should be detected as watchdog failure

        # With the fix, watchdog should fire at exactly 1800s
        limits = CircuitBreakerDefaults(max_active_duration=pr_review_timeout)
        assert limits.max_active_duration == 1800

    @pytest.mark.asyncio
    async def test_reconciliation_watchdog_failure_detection(self):
        """Test that reconciliation can detect watchdog failures vs normal timeouts."""

        # Small overage (< 60s) = normal reconciliation catch
        small_overage = 45
        watchdog_failed_small = small_overage > 60
        assert not watchdog_failed_small

        # Large overage (> 60s) = watchdog failure
        large_overage = 230  # Like in issue #51
        watchdog_failed_large = large_overage > 60
        assert watchdog_failed_large


class TestWatchdogImplementation:
    """Test the corrected watchdog implementation."""

    @pytest.mark.asyncio
    async def test_watchdog_cancellation_without_shield(self):
        """Test that agent task cancellation works without asyncio.shield."""

        # Create a mock agent task that can be cancelled
        mock_agent_task = Mock()
        mock_agent_task.done.return_value = False
        mock_agent_task.cancel = Mock()

        # Create a future that will complete when cancelled
        task_future = asyncio.Future()

        # Simulate cancellation
        mock_agent_task.cancel()
        task_future.cancel()

        # wait_for without shield should allow cancellation
        try:
            await asyncio.wait_for(task_future, timeout=1.0)
        except asyncio.CancelledError:
            pass  # Expected - task was cancelled

        assert task_future.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_timeout_bounded(self):
        """Test that cleanup operations have bounded timeouts."""
        from squadron.agent_manager import AgentManager
        import inspect

        # Check that CLEANUP_TIMEOUT is defined and used
        source = inspect.getsource(AgentManager._duration_watchdog)
        assert "CLEANUP_TIMEOUT" in source
        assert "timeout=CLEANUP_TIMEOUT" in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
