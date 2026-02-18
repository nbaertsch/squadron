"""Tests for feat-dev agent cleanup workflow after PR merge.

This test reproduces issue #63 where feat-dev agent fails to perform 
required cleanup steps when PR is merged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from squadron.agent_manager import AgentManager
from squadron.models import AgentRecord, AgentStatus, SquadronEvent, SquadronEventType
from squadron.registry import AgentRegistry


@pytest_asyncio.fixture
async def registry(tmp_path):
    """Create a fresh registry for each test."""
    db_path = str(tmp_path / "test_registry.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


class TestFeatDevCleanupWorkflow:
    """Test the feat-dev cleanup workflow bug."""
    
    async def test_trigger_complete_bypasses_cleanup_workflow(self, registry):
        """Focused test showing that _trigger_complete immediately marks agent as COMPLETED.
        
        This is the core issue: when a PR merge event triggers completion,
        the agent is immediately completed without running its cleanup workflow.
        """
        # Create minimal agent manager for testing _trigger_complete directly
        mock_config = MagicMock()
        mock_config.project.owner = "test-org"
        mock_config.project.repo = "test-repo" 
        mock_config.runtime.max_concurrent_agents = 10  # Fix the comparison issue
        
        github = AsyncMock()
        github.comment_on_issue = AsyncMock()
        
        # Create the agent manager with minimal setup
        mgr = AgentManager.__new__(AgentManager)  # Create without calling __init__
        mgr.config = mock_config
        mgr.registry = registry
        mgr.github = github
        mgr._copilot_agents = {}
        
        # Create a sleeping feat-dev agent that opened a PR
        agent_id = "feat-dev-issue-42"
        agent = AgentRecord(
            agent_id=agent_id,
            role="feat-dev",
            issue_number=42,
            pr_number=36,
            status=AgentStatus.SLEEPING,  # Agent is sleeping after opening PR
            session_id="test-session-123",
            created_at=datetime.now(timezone.utc),
            sleeping_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)
        
        # Verify agent is initially sleeping
        initial_agent = await registry.get_agent(agent_id)
        assert initial_agent.status == AgentStatus.SLEEPING
        
        # Create PR merge event
        merge_event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            issue_number=42,
            pr_number=36,
            timestamp=datetime.now(timezone.utc),
            data={
                "payload": {
                    "action": "closed",
                    "pull_request": {
                        "number": 36,
                        "merged": True,
                        "head": {"ref": "feat/issue-42"}
                    }
                }
            }
        )
        
        # Call _trigger_complete directly (this is what happens in the real system)
        await mgr._trigger_complete("feat-dev", merge_event)
        
        # Verify the agent was immediately marked as COMPLETED
        completed_agent = await registry.get_agent(agent_id)
        assert completed_agent.status == AgentStatus.COMPLETED
        
        # This is the problem: agent was completed immediately without any cleanup
        # The agent never got a chance to:
        # 1. Post a comment mentioning the PM
        # 2. Clean up the merged branch
        # 3. Perform any other cleanup tasks
        
        # No comment was posted because the agent was never woken up
        github.comment_on_issue.assert_not_called()
        
    async def test_manual_report_complete_posts_comment_but_lacks_pm_mention(self, registry):
        """Test that manual report_complete posts a comment but lacks PM mention.
        
        This shows that even the manual completion path doesn't include
        the required PM mention and cleanup workflow.
        """
        from squadron.tools.squadron_tools import SquadronTools, ReportCompleteParams
        
        agent_id = "feat-dev-issue-42"
        agent = AgentRecord(
            agent_id=agent_id,
            role="feat-dev", 
            issue_number=42,
            pr_number=36,
            status=AgentStatus.ACTIVE,
            session_id="test-session-123",
            created_at=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)
        
        github = AsyncMock()
        tools = SquadronTools(
            registry=registry,
            github=github,
            owner="test-org",
            repo="test-repo",
        )
        
        # Call report_complete
        params = ReportCompleteParams(summary="Feature implementation complete")
        result = await tools.report_complete(agent_id, params)
        
        # Verify agent is marked complete
        updated_agent = await registry.get_agent(agent_id)
        assert updated_agent.status == AgentStatus.COMPLETED
        
        # Verify completion comment was posted
        github.comment_on_issue.assert_called_once()
        call_args = github.comment_on_issue.call_args
        assert call_args[0] == ("test-org", "test-repo", 42)
        comment_text = call_args[0][3]
        
        # Current comment format doesn't include PM mention
        assert "Feature Developer" in comment_text
        assert "Task complete: Feature implementation complete" in comment_text
        
        # But it lacks the required PM mention:
        assert "@squadron-dev pm:" not in comment_text
        # And lacks branch cleanup confirmation
        assert "branch" not in comment_text.lower()
        
        # This shows that both completion paths (trigger and manual) need fixes
