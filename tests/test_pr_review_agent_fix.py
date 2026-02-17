"""Test for PR review agent pr_number interpolation bug fix."""

from collections import defaultdict
from unittest.mock import Mock

import pytest

from squadron.agent_manager import AgentManager
from squadron.config import SquadronConfig
from squadron.event_types import SquadronEvent, SquadronEventType
from squadron.models import AgentRecord


class TestPRReviewAgentInterpolation:
    """Test PR number interpolation in PR review agent definitions."""

    def test_pr_number_interpolation_missing_bug(self):
        """Test that reproduces the bug - pr_number is missing from template variables."""
        # This test demonstrates the bug before the fix
        config = Mock(spec=SquadronConfig)
        config.project.name = "squadron"
        config.project.default_branch = "main"
        
        # Mock circuit breaker limits
        cb_limits = Mock()
        cb_limits.max_iterations = 5
        cb_limits.max_tool_calls = 100
        cb_limits.max_turns = 20
        config.circuit_breakers.for_role.return_value = cb_limits
        
        agent_manager = AgentManager(
            config=config,
            repo_root="/tmp/test",
            registry=Mock(),
            github=Mock(),
        )
        
        record = AgentRecord(
            agent_id="pr-review-issue-12",
            role="pr-review", 
            issue_number=12,
            branch="pr-review/issue-12"
        )
        
        # Create a PR opened event for PR #23
        trigger_event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=23,
            issue_number=12,
            data={}
        )
        
        # Agent definition that expects pr_number
        raw_content = "Review PR #{pr_number} and provide feedback."
        
        # Call the method that's buggy
        result = agent_manager._interpolate_agent_def(raw_content, record, trigger_event)
        
        # The bug: pr_number becomes empty string because it's not in the values dict
        assert result == "Review PR # and provide feedback."  # BUG: missing PR number
        
    def test_pr_number_interpolation_fixed(self):
        """Test that the fix works - pr_number is properly interpolated."""
        # This test will pass after the fix
        config = Mock(spec=SquadronConfig)
        config.project.name = "squadron"
        config.project.default_branch = "main"
        
        # Mock circuit breaker limits
        cb_limits = Mock()
        cb_limits.max_iterations = 5
        cb_limits.max_tool_calls = 100
        cb_limits.max_turns = 20
        config.circuit_breakers.for_role.return_value = cb_limits
        
        agent_manager = AgentManager(
            config=config,
            repo_root="/tmp/test",
            registry=Mock(),
            github=Mock(),
        )
        
        record = AgentRecord(
            agent_id="pr-review-issue-12",
            role="pr-review",
            issue_number=12,
            branch="pr-review/issue-12"
        )
        
        # Create a PR opened event for PR #23
        trigger_event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=23,
            issue_number=12,
            data={}
        )
        
        # Agent definition that expects pr_number
        raw_content = "Review PR #{pr_number} and provide feedback."
        
        # Call the method after fixing
        result = agent_manager._interpolate_agent_def(raw_content, record, trigger_event)
        
        # After fix: pr_number should be properly interpolated
        assert result == "Review PR #23 and provide feedback."
