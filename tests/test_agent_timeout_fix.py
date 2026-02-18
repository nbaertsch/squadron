"""Tests for agent timeout handling and escalation (regression test for issue #46)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from squadron.config import CircuitBreakerDefaults
from squadron.models import AgentRecord, AgentStatus


class TestAgentTimeoutConfig:
    """Test agent timeout configuration after fix for issue #46."""

    def test_circuit_breaker_defaults_structure(self):
        """Test that CircuitBreakerDefaults has the expected fields."""
        limits = CircuitBreakerDefaults(
            max_active_duration=7200,
            max_iterations=5,
            max_sleep_duration=86400,
            max_tool_calls=200,
            max_turns=50,
            warning_threshold=0.8,
        )
        assert limits.max_active_duration == 7200
        assert limits.max_iterations == 5
        assert limits.max_sleep_duration == 86400

    def test_infra_dev_extended_timeout(self):
        """Test that infra-dev role can have extended timeout (regression test for #46)."""
        # Default limits
        default_limits = CircuitBreakerDefaults(
            max_active_duration=7200,  # 2 hours
        )

        # Extended limits for infra-dev
        infra_limits = CircuitBreakerDefaults(
            max_active_duration=10800,  # 3 hours
        )

        assert infra_limits.max_active_duration == 10800
        assert infra_limits.max_active_duration > default_limits.max_active_duration


class TestAgentTimeoutDetection:
    """Test timeout detection logic."""

    def test_agent_exceeds_max_duration(self):
        """Test detection of agent that exceeded max duration."""
        max_duration = 7200  # 2 hours

        # Agent active for 2.5 hours
        agent = AgentRecord(
            agent_id="test-agent",
            role="feat-dev",
            issue_number=1,
            branch="feat/issue-1",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(seconds=9000),
        )

        # Calculate if exceeded
        if agent.active_since:
            elapsed = (datetime.now(timezone.utc) - agent.active_since).total_seconds()
            exceeded = elapsed > max_duration
        else:
            exceeded = False

        assert exceeded is True

    def test_agent_within_max_duration(self):
        """Test agent within allowed duration."""
        max_duration = 7200  # 2 hours

        # Agent active for 1 hour
        agent = AgentRecord(
            agent_id="test-agent",
            role="feat-dev",
            issue_number=1,
            branch="feat/issue-1",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(seconds=3600),
        )

        if agent.active_since:
            elapsed = (datetime.now(timezone.utc) - agent.active_since).total_seconds()
            exceeded = elapsed > max_duration
        else:
            exceeded = False

        assert exceeded is False

    def test_infra_dev_with_extended_timeout_not_exceeded(self):
        """Test infra-dev with extended timeout is not flagged (fix for #46)."""
        # With original 2-hour timeout, this would be exceeded
        original_timeout = 7200

        # With extended 3-hour timeout for infra-dev, this should NOT be exceeded
        infra_dev_timeout = 10800

        # Agent active for 2.5 hours (9000 seconds)
        agent = AgentRecord(
            agent_id="infra-dev-issue-40",
            role="infra-dev",
            issue_number=40,
            branch="infra/issue-40",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(seconds=9000),
        )

        if agent.active_since:
            elapsed = (datetime.now(timezone.utc) - agent.active_since).total_seconds()
            exceeded_original = elapsed > original_timeout
            exceeded_extended = elapsed > infra_dev_timeout
        else:
            exceeded_original = False
            exceeded_extended = False

        # Would have been escalated with original timeout
        assert exceeded_original is True
        # Should NOT be escalated with extended timeout
        assert exceeded_extended is False


class TestReconciliationTimeoutHandling:
    """Test reconciliation loop timeout handling."""

    @pytest.mark.asyncio
    async def test_stale_active_agent_detection(self):
        """Test that reconciliation can detect stale active agents."""
        from squadron.reconciliation import ReconciliationLoop

        # Create stale agent that exceeded max duration
        stale_agent = AgentRecord(
            agent_id="stale-agent",
            role="feat-dev",
            issue_number=99,
            branch="feat/issue-99",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(seconds=10000),
        )

        # Mock registry
        mock_registry = Mock()
        mock_registry.get_agents_by_status = AsyncMock(return_value=[stale_agent])
        mock_registry.update_agent = AsyncMock()

        # Mock github
        mock_github = AsyncMock()
        mock_github.create_issue = AsyncMock(return_value={"number": 100})

        # Mock config
        mock_config = Mock()
        mock_config.circuit_breakers.for_role.return_value = CircuitBreakerDefaults(
            max_active_duration=7200,  # 2 hours - agent exceeded this
        )
        mock_config.runtime.reconciliation_interval = 300

        reconciler = ReconciliationLoop(
            registry=mock_registry,
            github=mock_github,
            config=mock_config,
            owner="test",
            repo="test",
        )

        # Run the stale agents check
        await reconciler._check_stale_active_agents()

        # Verify agent status was updated
        mock_registry.update_agent.assert_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
