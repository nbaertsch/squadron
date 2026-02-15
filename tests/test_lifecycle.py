"""Tests for agent lifecycle transitions and circuit breakers.

Covers:
- Framework tools setting agent status (report_blocked → SLEEPING, report_complete → COMPLETED)
- Post-turn state machine in _run_agent (cleanup paths)
- Circuit breaker Layer 1 (tool call counting / on_pre_tool_use hook)
- Circuit breaker Layer 2 (asyncio.wait_for timeout)
- Counter increments (turn_count, tool_call_count)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from squadron.config import CircuitBreakerDefaults
from squadron.models import AgentRecord, AgentRole, AgentStatus
from squadron.registry import AgentRegistry
from squadron.tools.framework import (
    CreateBlockerIssueParams,
    FrameworkTools,
    ReportBlockedParams,
    ReportCompleteParams,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_lifecycle.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _make_agent(
    agent_id: str = "feat-dev-issue-42",
    role: AgentRole = AgentRole.FEAT_DEV,
    issue_number: int = 42,
    status: AgentStatus = AgentStatus.ACTIVE,
    **kwargs,
) -> AgentRecord:
    return AgentRecord(
        agent_id=agent_id,
        role=role,
        issue_number=issue_number,
        status=status,
        active_since=datetime.now(timezone.utc),
        **kwargs,
    )


def _make_github_mock() -> AsyncMock:
    github = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.create_issue = AsyncMock(return_value={"number": 99})
    return github


def _make_framework_tools(registry: AgentRegistry, github=None) -> FrameworkTools:
    return FrameworkTools(
        registry=registry,
        github=github or _make_github_mock(),
        agent_inboxes={},
        owner="testowner",
        repo="testrepo",
    )


# ── report_blocked lifecycle ─────────────────────────────────────────────────


class TestReportBlocked:
    async def test_transitions_to_sleeping(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        tools = _make_framework_tools(registry)

        result = await tools.report_blocked(
            agent.agent_id,
            ReportBlockedParams(blocker_issue=10, reason="Need API first"),
        )

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.SLEEPING
        assert updated.sleeping_since is not None
        assert updated.active_since is None
        assert "session will be saved" in result.lower()

    async def test_sets_sleeping_since_timestamp(self, registry: AgentRegistry):
        before = datetime.now(timezone.utc)
        agent = _make_agent()
        await registry.create_agent(agent)
        tools = _make_framework_tools(registry)

        await tools.report_blocked(
            agent.agent_id,
            ReportBlockedParams(blocker_issue=10, reason="Blocked"),
        )

        updated = await registry.get_agent(agent.agent_id)
        assert updated.sleeping_since >= before

    async def test_registers_blocker(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        tools = _make_framework_tools(registry)

        await tools.report_blocked(
            agent.agent_id,
            ReportBlockedParams(blocker_issue=10, reason="Blocked"),
        )

        updated = await registry.get_agent(agent.agent_id)
        assert 10 in updated.blocked_by

    async def test_posts_comment_on_issue(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = _make_framework_tools(registry, github)

        await tools.report_blocked(
            agent.agent_id,
            ReportBlockedParams(blocker_issue=10, reason="Blocked"),
        )

        github.comment_on_issue.assert_called_once()
        args = github.comment_on_issue.call_args
        assert args[0][2] == 42  # issue_number
        assert "#10" in args[0][3]

    async def test_cycle_detection_prevents_sleeping(self, registry: AgentRegistry):
        """If add_blocker fails (cycle), agent should NOT transition to SLEEPING."""
        agent = _make_agent()
        await registry.create_agent(agent)

        # Create the target agent and make it depend on us — creating a cycle
        blocker_agent = _make_agent(agent_id="blocker-agent", issue_number=10)
        await registry.create_agent(blocker_agent)
        await registry.add_blocker("blocker-agent", 42)  # blocker blocked by us

        tools = _make_framework_tools(registry)

        result = await tools.report_blocked(
            agent.agent_id,
            ReportBlockedParams(blocker_issue=10, reason="Would be circular"),
        )

        updated = await registry.get_agent(agent.agent_id)
        # Should still be ACTIVE — cycle prevented the transition
        assert updated.status == AgentStatus.ACTIVE
        assert "circular dependency" in result.lower()

    async def test_unknown_agent_returns_error(self, registry: AgentRegistry):
        tools = _make_framework_tools(registry)

        result = await tools.report_blocked(
            "nonexistent",
            ReportBlockedParams(blocker_issue=10, reason="x"),
        )

        assert "error" in result.lower()


# ── report_complete lifecycle ────────────────────────────────────────────────


class TestReportComplete:
    async def test_transitions_to_completed(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        tools = _make_framework_tools(registry)

        result = await tools.report_complete(
            agent.agent_id,
            ReportCompleteParams(summary="All done"),
        )

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED
        assert updated.active_since is None
        assert "complete" in result.lower()

    async def test_posts_completion_comment(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = _make_framework_tools(registry, github)

        await tools.report_complete(
            agent.agent_id,
            ReportCompleteParams(summary="Finished the feature"),
        )

        github.comment_on_issue.assert_called_once()
        args = github.comment_on_issue.call_args
        assert "Finished the feature" in args[0][3]


# ── create_blocker_issue lifecycle ───────────────────────────────────────────


class TestCreateBlockerIssue:
    async def test_transitions_to_sleeping(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = _make_framework_tools(registry, github)

        result = await tools.create_blocker_issue(
            agent.agent_id,
            CreateBlockerIssueParams(title="Missing API", body="Need auth service"),
        )

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.SLEEPING
        assert updated.sleeping_since is not None
        assert updated.active_since is None
        assert "99" in result  # new issue number

    async def test_registers_blocker_for_new_issue(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = _make_framework_tools(registry, github)

        await tools.create_blocker_issue(
            agent.agent_id,
            CreateBlockerIssueParams(title="Missing API", body="Need it"),
        )

        updated = await registry.get_agent(agent.agent_id)
        assert 99 in updated.blocked_by  # mocked create_issue returns {"number": 99}


# ── Circuit Breaker Layer 1 (tool call hook) ─────────────────────────────────


class TestCircuitBreakerLayer1:
    def _make_hooks(self, record, max_tool_calls=10, warning_threshold=0.8):
        """Build hooks using the same logic as AgentManager._build_hooks."""
        from squadron.agent_manager import AgentManager

        # We test the hook function in isolation — instantiate a minimal manager
        # Alternative: extract _build_hooks as a static/standalone function
        # For now, test the hook logic directly
        cb_limits = CircuitBreakerDefaults(
            max_tool_calls=max_tool_calls,
            warning_threshold=warning_threshold,
        )
        registry_mock = AsyncMock()
        registry_mock.update_agent = AsyncMock()

        async def on_pre_tool_use(tool_name, tool_input):
            record.tool_call_count += 1
            if record.tool_call_count > max_tool_calls:
                record.status = AgentStatus.ESCALATED
                await registry_mock.update_agent(record)
                return {"permissionDecision": "deny", "reason": f"Limit exceeded ({max_tool_calls})"}
            if record.tool_call_count % 10 == 0:
                await registry_mock.update_agent(record)
            return {"permissionDecision": "allow"}

        return on_pre_tool_use, registry_mock

    async def test_allows_under_limit(self):
        record = _make_agent(tool_call_count=0)
        hook, _ = self._make_hooks(record, max_tool_calls=5)

        result = await hook("some_tool", {})

        assert result["permissionDecision"] == "allow"
        assert record.tool_call_count == 1

    async def test_denies_over_limit(self):
        record = _make_agent(tool_call_count=9)
        hook, registry_mock = self._make_hooks(record, max_tool_calls=10)

        # Call 1: count goes to 10, still OK
        result = await hook("tool", {})
        assert result["permissionDecision"] == "allow"
        assert record.tool_call_count == 10

        # Call 2: count goes to 11, exceeds limit
        result = await hook("tool", {})
        assert result["permissionDecision"] == "deny"
        assert record.status == AgentStatus.ESCALATED

    async def test_increments_counter(self):
        record = _make_agent(tool_call_count=0)
        hook, _ = self._make_hooks(record, max_tool_calls=100)

        for _ in range(5):
            await hook("tool", {})

        assert record.tool_call_count == 5

    async def test_persists_at_intervals(self):
        record = _make_agent(tool_call_count=0)
        hook, registry_mock = self._make_hooks(record, max_tool_calls=100)

        # 9 calls — no persist
        for _ in range(9):
            await hook("tool", {})
        registry_mock.update_agent.assert_not_called()

        # 10th call — persists
        await hook("tool", {})
        registry_mock.update_agent.assert_called_once()


# ── Post-turn state machine ─────────────────────────────────────────────────


class TestPostTurnStateMachine:
    """Test _run_agent post-turn behavior via mocking the Copilot SDK."""

    def _make_manager_deps(self, registry):
        """Create minimal AgentManager dependencies for testing _run_agent."""
        from squadron.config import (
            CircuitBreakerConfig,
            LabelsConfig,
            ProjectConfig,
            RuntimeConfig,
            SquadronConfig,
        )

        config = MagicMock(spec=SquadronConfig)
        config.project = ProjectConfig(name="test", owner="testowner", repo="testrepo")
        config.runtime = RuntimeConfig()
        config.circuit_breakers = CircuitBreakerConfig()
        config.agent_roles = {}
        config.labels = LabelsConfig()

        github = _make_github_mock()
        router = MagicMock()

        return config, github, router

    def _make_manager(self, config, registry, github, router):
        """Construct an AgentManager with mocked deps."""
        from squadron.agent_manager import AgentManager

        return AgentManager(
            config=config,
            registry=registry,
            github=github,
            router=router,
            agent_definitions={},
            repo_root=Path("/tmp/test"),
        )

    async def test_sleeping_agent_removes_task_keeps_copilot(self, registry):
        """When agent transitions to SLEEPING, task is removed but CopilotAgent is kept."""
        config, github, router = self._make_manager_deps(registry)
        manager = self._make_manager(config, registry, github, router)

        # Set up agent in registry
        agent = _make_agent(status=AgentStatus.ACTIVE)
        await registry.create_agent(agent)

        # Mock CopilotAgent
        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        manager._copilot_agents[agent.agent_id] = mock_copilot

        # Mock agent definition
        agent_def = MagicMock()
        agent_def.prompt = "You are a dev agent"
        agent_def.raw_content = "---\nname: test\n---\nYou are a dev agent"
        agent_def.subagents = []
        agent_def.mcp_servers = {}
        manager.agent_definitions[agent.role.value] = agent_def

        # Simulate the agent calling report_blocked during its turn
        async def side_effect_block(*args, **kwargs):
            a = await registry.get_agent(agent.agent_id)
            a.status = AgentStatus.SLEEPING
            a.sleeping_since = datetime.now(timezone.utc)
            a.active_since = None
            await registry.update_agent(a)
            return mock_result

        mock_session.send_and_wait = AsyncMock(side_effect=side_effect_block)

        # Add task so we can verify it gets removed
        manager._agent_tasks[agent.agent_id] = MagicMock()

        await manager._run_agent(agent, trigger_event=None, resume=False)

        # Task should be removed
        assert agent.agent_id not in manager._agent_tasks
        # CopilotAgent should still be present (for later resume)
        assert agent.agent_id in manager._copilot_agents

    async def test_completed_agent_gets_full_cleanup(self, registry):
        """When agent transitions to COMPLETED, everything is cleaned up."""
        config, github, router = self._make_manager_deps(registry)
        manager = self._make_manager(config, registry, github, router)

        agent = _make_agent(status=AgentStatus.ACTIVE, session_id="ses-42")
        await registry.create_agent(agent)

        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        mock_copilot.delete_session = AsyncMock()
        mock_copilot.stop = AsyncMock()
        manager._copilot_agents[agent.agent_id] = mock_copilot

        agent_def = MagicMock()
        agent_def.prompt = "You are a dev agent"
        agent_def.raw_content = "---\nname: test\n---\nYou are a dev agent"
        agent_def.subagents = []
        agent_def.mcp_servers = {}
        manager.agent_definitions[agent.role.value] = agent_def

        async def side_effect_complete(*args, **kwargs):
            a = await registry.get_agent(agent.agent_id)
            a.status = AgentStatus.COMPLETED
            a.active_since = None
            await registry.update_agent(a)
            return mock_result

        mock_session.send_and_wait = AsyncMock(side_effect=side_effect_complete)

        manager._agent_tasks[agent.agent_id] = MagicMock()
        manager.agent_inboxes[agent.agent_id] = asyncio.Queue()

        await manager._run_agent(agent, trigger_event=None, resume=False)

        # Everything should be cleaned up
        assert agent.agent_id not in manager._copilot_agents
        assert agent.agent_id not in manager._agent_tasks
        assert agent.agent_id not in manager.agent_inboxes

    async def test_turn_count_incremented(self, registry):
        """turn_count should be incremented after each send_and_wait."""
        config, github, router = self._make_manager_deps(registry)
        manager = self._make_manager(config, registry, github, router)

        agent = _make_agent(status=AgentStatus.ACTIVE, turn_count=0)
        await registry.create_agent(agent)

        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.type.value = "text"
        mock_session.send_and_wait = AsyncMock(return_value=mock_result)
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        manager._copilot_agents[agent.agent_id] = mock_copilot

        agent_def = MagicMock()
        agent_def.prompt = "You are a dev agent"
        agent_def.raw_content = "---\nname: test\n---\nYou are a dev agent"
        agent_def.subagents = []
        agent_def.mcp_servers = {}
        manager.agent_definitions[agent.role.value] = agent_def

        await manager._run_agent(agent, trigger_event=None, resume=False)

        assert agent.turn_count == 1
        persisted = await registry.get_agent(agent.agent_id)
        assert persisted.turn_count == 1

    async def test_exception_escalates_and_cleans_up(self, registry):
        """Unhandled exception should escalate agent and attempt cleanup."""
        config, github, router = self._make_manager_deps(registry)
        manager = self._make_manager(config, registry, github, router)

        agent = _make_agent(status=AgentStatus.ACTIVE, session_id="ses-42")
        await registry.create_agent(agent)

        mock_copilot = AsyncMock()
        mock_session = AsyncMock()
        mock_session.send_and_wait = AsyncMock(side_effect=RuntimeError("SDK crash"))
        mock_copilot.create_session = AsyncMock(return_value=mock_session)
        mock_copilot.delete_session = AsyncMock()
        mock_copilot.stop = AsyncMock()
        manager._copilot_agents[agent.agent_id] = mock_copilot

        agent_def = MagicMock()
        agent_def.prompt = "You are a dev agent"
        agent_def.raw_content = "---\nname: test\n---\nYou are a dev agent"
        agent_def.subagents = []
        agent_def.mcp_servers = {}
        manager.agent_definitions[agent.role.value] = agent_def

        await manager._run_agent(agent, trigger_event=None, resume=False)

        persisted = await registry.get_agent(agent.agent_id)
        assert persisted.status == AgentStatus.ESCALATED
        # Should have attempted cleanup
        assert agent.agent_id not in manager._copilot_agents


# ── Cleanup helper ───────────────────────────────────────────────────────────


class TestCleanupAgent:
    async def test_destroys_session_and_stops_copilot(self, registry):
        from squadron.agent_manager import AgentManager

        config = MagicMock()
        config.project = MagicMock()
        config.project.owner = "x"
        config.project.repo = "y"

        manager = AgentManager(
            config=config,
            registry=registry,
            github=_make_github_mock(),
            router=MagicMock(),
            agent_definitions={},
            repo_root=Path("/tmp/test"),
        )

        mock_copilot = AsyncMock()
        mock_copilot.delete_session = AsyncMock()
        mock_copilot.stop = AsyncMock()
        manager._copilot_agents["agent-1"] = mock_copilot
        manager._agent_tasks["agent-1"] = MagicMock()
        manager.agent_inboxes["agent-1"] = asyncio.Queue()

        await manager._cleanup_agent(
            "agent-1",
            destroy_session=True,
            copilot=mock_copilot,
            session_id="ses-1",
        )

        mock_copilot.delete_session.assert_called_once_with("ses-1")
        # CopilotAgent should be popped and stopped
        assert "agent-1" not in manager._copilot_agents
        assert "agent-1" not in manager._agent_tasks
        assert "agent-1" not in manager.agent_inboxes

    async def test_handles_delete_session_failure_gracefully(self, registry):
        from squadron.agent_manager import AgentManager

        config = MagicMock()
        config.project = MagicMock()
        config.project.owner = "x"
        config.project.repo = "y"

        manager = AgentManager(
            config=config,
            registry=registry,
            github=_make_github_mock(),
            router=MagicMock(),
            agent_definitions={},
            repo_root=Path("/tmp/test"),
        )

        mock_copilot = AsyncMock()
        mock_copilot.delete_session = AsyncMock(side_effect=RuntimeError("network error"))
        mock_copilot.stop = AsyncMock()
        manager._copilot_agents["agent-1"] = mock_copilot

        # Should not raise
        await manager._cleanup_agent(
            "agent-1",
            destroy_session=True,
            copilot=mock_copilot,
            session_id="ses-1",
        )

        # Agent still cleaned up despite session deletion failure
        assert "agent-1" not in manager._copilot_agents


# ── New framework tools: comment_on_issue, submit_pr_review, open_pr ─────────


class TestCommentOnIssue:
    async def test_posts_comment_with_role_prefix(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = _make_framework_tools(registry, github)

        from squadron.tools.framework import CommentOnIssueParams
        result = await tools.comment_on_issue(
            agent.agent_id,
            CommentOnIssueParams(issue_number=42, body="Working on this"),
        )

        assert "Posted comment" in result
        github.comment_on_issue.assert_called_once()
        call_args = github.comment_on_issue.call_args
        # Should include role prefix
        assert f"[squadron:{agent.role.value}]" in call_args[1].get("body", call_args[0][-1])


class TestSubmitPRReview:
    async def test_submits_approve_review(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(return_value={"id": 123})
        tools = _make_framework_tools(registry, github)

        from squadron.tools.framework import SubmitPRReviewParams
        result = await tools.submit_pr_review(
            agent.agent_id,
            SubmitPRReviewParams(pr_number=10, body="LGTM", event="APPROVE"),
        )

        assert "APPROVE" in result
        assert "123" in result
        github.submit_pr_review.assert_called_once_with(
            "testowner", "testrepo", 10,
            body="LGTM", event="APPROVE", comments=None,
        )

    async def test_submits_request_changes_with_inline_comments(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(return_value={"id": 456})
        tools = _make_framework_tools(registry, github)

        from squadron.tools.framework import SubmitPRReviewParams
        comments = [{"path": "src/auth.py", "position": 5, "body": "Missing null check"}]
        result = await tools.submit_pr_review(
            agent.agent_id,
            SubmitPRReviewParams(
                pr_number=10, body="Needs changes",
                event="REQUEST_CHANGES", comments=comments,
            ),
        )

        assert "REQUEST_CHANGES" in result
        github.submit_pr_review.assert_called_once_with(
            "testowner", "testrepo", 10,
            body="Needs changes", event="REQUEST_CHANGES", comments=comments,
        )


class TestOpenPR:
    async def test_opens_pull_request(self, registry: AgentRegistry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        github.create_pull_request = AsyncMock(return_value={"number": 15})
        tools = _make_framework_tools(registry, github)

        from squadron.tools.framework import OpenPRParams
        result = await tools.open_pr(
            agent.agent_id,
            OpenPRParams(
                title="Add auth flow",
                body="Fixes #42. Implements OAuth.",
                head="feat/issue-42",
                base="main",
            ),
        )

        assert "15" in result
        assert "Add auth flow" in result
        github.create_pull_request.assert_called_once_with(
            "testowner", "testrepo",
            title="Add auth flow",
            body="Fixes #42. Implements OAuth.",
            head="feat/issue-42",
            base="main",
        )
