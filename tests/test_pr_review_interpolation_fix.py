"""Regression test for PR review agent pr_number interpolation bug fix."""

from collections import defaultdict
from unittest.mock import Mock

import pytest

from squadron.agent_manager import AgentManager
from squadron.config import SquadronConfig
from squadron.models import SquadronEvent, SquadronEventType, AgentRecord


class TestPRNumberInterpolationBugFix:
    """Regression test for issue #24: PR review agent pr_number interpolation."""
    
    def test_pr_number_interpolated_when_trigger_event_has_pr_number(self):
        """Test that pr_number template variable is properly interpolated from trigger_event.pr_number."""
        # Mock dependencies
        config = Mock(spec=SquadronConfig)
        config.project.name = "squadron"
        config.project.default_branch = "main"
        
        cb_limits = Mock()
        cb_limits.max_iterations = 5
        cb_limits.max_tool_calls = 100
        cb_limits.max_turns = 20
        config.circuit_breakers.for_role.return_value = cb_limits
        
        # Create agent manager
        agent_manager = AgentManager(
            config=config,
            repo_root="/tmp/test",
            registry=Mock(),
            github=Mock(),
        )
        
        # Create agent record
        record = AgentRecord(
            agent_id="pr-review-issue-12",
            role="pr-review",
            issue_number=12,
            branch="pr-review/issue-12"
        )
        
        # Create PR opened event with pr_number
        trigger_event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=23,  # This should be interpolated
            issue_number=12,
            data={}
        )
        
        # Test interpolation with pr_number template
        raw_content = "Review PR #{pr_number} and provide feedback."
        result = agent_manager._interpolate_agent_def(raw_content, record, trigger_event)
        
        assert result == "Review PR #23 and provide feedback."
        
    def test_pr_number_empty_when_no_trigger_event(self):
        """Test that pr_number becomes empty string when no trigger_event."""
        config = Mock(spec=SquadronConfig)
        config.project.name = "squadron"
        config.project.default_branch = "main"
        
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
        
        # No trigger event
        raw_content = "Review PR #{pr_number} and provide feedback."
        result = agent_manager._interpolate_agent_def(raw_content, record, None)
        
        assert result == "Review PR # and provide feedback."
        
    def test_pr_number_empty_when_trigger_event_has_no_pr_number(self):
        """Test that pr_number becomes empty string when trigger_event.pr_number is None."""
        config = Mock(spec=SquadronConfig)
        config.project.name = "squadron"
        config.project.default_branch = "main"
        
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
            agent_id="bug-fix-issue-42",
            role="bug-fix",
            issue_number=42,
            branch="fix/issue-42"
        )
        
        # Event with no PR number (e.g., issue.labeled event)
        trigger_event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_LABELED,
            pr_number=None,
            issue_number=42,
            data={}
        )
        
        raw_content = "Working on issue #{issue_number} for PR #{pr_number}."
        result = agent_manager._interpolate_agent_def(raw_content, record, trigger_event)
        
        assert result == "Working on issue #42 for PR #."
