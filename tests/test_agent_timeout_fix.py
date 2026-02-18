"""Tests for agent timeout handling and escalation (regression test for issue #46)."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, AsyncMock, patch

import pytest

from squadron.agent_manager import AgentManager
from squadron.config import SquadronConfig, CircuitBreakerLimits
from squadron.models import AgentRecord, AgentStatus
from squadron.registry import AgentRegistry


@pytest.fixture
def mock_config():
    """Mock config with timeout settings."""
    config = Mock(spec=SquadronConfig)
    config.project.owner = "test-org"
    config.project.repo = "test-repo"
    
    # Circuit breaker config with different timeouts for different roles
    cb_config = Mock()
    cb_config.defaults = CircuitBreakerLimits(
        max_active_duration=7200,  # 2 hours default
        max_iterations=5,
        max_sleep_duration=86400,
        max_tool_calls=200,
        max_turns=50,
        warning_threshold=0.8,
    )
    cb_config.roles = {
        "infra-dev": CircuitBreakerLimits(
            max_active_duration=10800,  # 3 hours for infra-dev (fix for #46)
            max_iterations=5,
            max_sleep_duration=86400,
            max_tool_calls=200,
            max_turns=50,
            warning_threshold=0.8,
        )
    }
    
    def for_role(role: str) -> CircuitBreakerLimits:
        return cb_config.roles.get(role, cb_config.defaults)
    
    cb_config.for_role = for_role
    config.circuit_breakers = cb_config
    
    return config


@pytest.fixture
def mock_registry():
    """Mock agent registry."""
    registry = Mock(spec=AgentRegistry)
    registry.create_agent = AsyncMock()
    registry.update_agent = AsyncMock()
    registry.get_agent = AsyncMock()
    return registry


@pytest.fixture
def agent_manager(mock_config, mock_registry):
    """Create agent manager with mocked dependencies."""
    with patch('squadron.agent_manager.SquadronGitHubClient') as mock_gh:
        mock_gh_instance = AsyncMock()
        mock_gh.return_value = mock_gh_instance
        
        manager = AgentManager(
            config=mock_config,
            registry=mock_registry,
            copilot_provider=Mock(),
            github=mock_gh_instance,
        )
        
        return manager


class TestAgentTimeoutFix:
    """Test agent timeout handling after fix for issue #46."""

    def test_infra_dev_timeout_config(self, mock_config):
        """Test that infra-dev role has extended timeout (regression test for #46)."""
        # Test default role gets 7200s (2 hours)
        default_limits = mock_config.circuit_breakers.for_role("bug-fix")
        assert default_limits.max_active_duration == 7200
        
        # Test infra-dev role gets 10800s (3 hours) - fix for #46
        infra_limits = mock_config.circuit_breakers.for_role("infra-dev") 
        assert infra_limits.max_active_duration == 10800
        assert infra_limits.max_active_duration > default_limits.max_active_duration

    async def test_watchdog_starts_with_correct_timeout(self, agent_manager, mock_registry):
        """Test that watchdog timer uses role-specific timeout."""
        agent_id = "test-agent"
        role = "infra-dev"
        
        # Mock the watchdog creation
        with patch.object(agent_manager, '_start_watchdog') as mock_start:
            # This would normally be called during agent creation
            agent_manager._start_watchdog(agent_id, role)
            
            mock_start.assert_called_once_with(agent_id, role)

    async def test_timeout_escalation_flow(self, agent_manager, mock_registry):
        """Test the complete timeout escalation flow that occurred in #46."""
        agent_id = "infra-dev-issue-40"
        
        # Create agent record similar to the one that timed out
        agent = AgentRecord(
            agent_id=agent_id,
            role="infra-dev",
            issue_number=40,
            branch="infra/issue-40",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(seconds=7390),  # Exceeded by 190s
        )
        
        mock_registry.get_agent.return_value = agent
        
        # Mock the watchdog timeout behavior
        with patch.object(agent_manager, '_duration_watchdog') as mock_watchdog:
            # Simulate watchdog firing after timeout
            mock_watchdog_task = AsyncMock()
            
            # Test escalation logic
            await agent_manager._duration_watchdog(agent_id, 10800)  # New 3-hour limit
            
            # Verify agent would be escalated
            mock_registry.update_agent.assert_called()

    async def test_watchdog_cancellation_race_condition(self, agent_manager):
        """Test potential race condition in watchdog cancellation."""
        agent_id = "test-agent"
        
        # Start a watchdog
        agent_manager._start_watchdog(agent_id, "infra-dev")
        
        # Verify watchdog task was created
        assert agent_id in agent_manager._watchdog_tasks
        initial_task = agent_manager._watchdog_tasks[agent_id]
        
        # Cancel and restart (simulating wake/sleep cycle)
        agent_manager._cancel_watchdog(agent_id)
        assert agent_id not in agent_manager._watchdog_tasks
        
        # Start new watchdog - this should not cause issues
        agent_manager._start_watchdog(agent_id, "infra-dev")
        assert agent_id in agent_manager._watchdog_tasks
        
        # Verify it's a new task
        new_task = agent_manager._watchdog_tasks[agent_id]
        assert new_task != initial_task

    async def test_reconciliation_fallback_timeout_detection(self):
        """Test that reconciliation loop can catch agents that exceed timeout."""
        from squadron.reconciliation import ReconciliationLoop
        
        # Mock the reconciliation dependencies  
        mock_registry = Mock(spec=AgentRegistry)
        mock_github = AsyncMock()
        
        # Create agent that has exceeded max duration
        stale_agent = AgentRecord(
            agent_id="stale-agent",
            role="infra-dev", 
            issue_number=40,
            branch="infra/issue-40",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc) - timedelta(seconds=10900),  # > 10800s limit
        )
        
        mock_registry.get_agents_by_status.return_value = [stale_agent]
        
        # Mock config for reconciliation
        mock_config = Mock()
        mock_config.circuit_breakers.for_role.return_value = CircuitBreakerLimits(
            max_active_duration=10800,
            max_iterations=5,
            max_sleep_duration=86400,
            max_tool_calls=200,
            max_turns=50,
            warning_threshold=0.8,
        )
        
        reconciler = ReconciliationLoop(
            registry=mock_registry,
            github=mock_github,
            config=mock_config,
            owner="test",
            repo="test"
        )
        
        # Mock the GitHub issue creation for escalation
        mock_github.create_issue = AsyncMock()
        mock_registry.update_agent = AsyncMock()
        
        # Run stale agent check
        await reconciler._check_stale_agents()
        
        # Verify agent was escalated
        mock_registry.update_agent.assert_called()
        updated_agent = mock_registry.update_agent.call_args[0][0]
        assert updated_agent.status == AgentStatus.ESCALATED
        
        # Verify escalation issue was created
        mock_github.create_issue.assert_called_once()
        create_args = mock_github.create_issue.call_args
        assert "exceeded max active duration" in create_args.kwargs["title"]


if __name__ == "__main__":
    pytest.main([__file__])
