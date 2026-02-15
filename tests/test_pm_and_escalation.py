"""Tests for PM tools, escalation, template interpolation, and PR closed handler."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from squadron.config import (
    CircuitBreakerConfig,
    LabelsConfig,
    ProjectConfig,
    RuntimeConfig,
    SquadronConfig,
)
from squadron.models import AgentRecord, AgentRole, AgentStatus, SquadronEvent, SquadronEventType
from squadron.registry import AgentRegistry
from squadron.tools.framework import EscalateToHumanParams, FrameworkTools
from squadron.tools.pm_tools import (
    AssignIssueParams,
    CheckRegistryParams,
    CommentOnIssueParams,
    CreateIssueParams,
    LabelIssueParams,
    PMTools,
    ReadIssueParams,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_pm.db")
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
    github.comment_on_issue = AsyncMock(return_value={"id": 1})
    github.create_issue = AsyncMock(return_value={"number": 99})
    github.add_labels = AsyncMock()
    github.assign_issue = AsyncMock()
    github.get_issue = AsyncMock(return_value={
        "number": 42,
        "title": "Test Issue",
        "body": "Issue body",
        "state": "open",
        "labels": [{"name": "feature"}],
        "assignees": [{"login": "squadron[bot]"}],
    })
    return github


# ── PM Tools ─────────────────────────────────────────────────────────────────


class TestPMTools:
    def _make_pm_tools(self, registry, github=None):
        return PMTools(
            registry=registry,
            github=github or _make_github_mock(),
            owner="testowner",
            repo="testrepo",
        )

    async def test_create_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_pm_tools(registry, github)

        result = await tools.create_issue(CreateIssueParams(
            title="New bug", body="Something broke", labels=["bug"],
        ))

        github.create_issue.assert_called_once()
        assert "#99" in result

    async def test_assign_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_pm_tools(registry, github)

        result = await tools.assign_issue(AssignIssueParams(
            issue_number=42, assignees=["squadron[bot]"],
        ))

        github.assign_issue.assert_called_once_with("testowner", "testrepo", 42, ["squadron[bot]"])
        assert "#42" in result

    async def test_label_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_pm_tools(registry, github)

        result = await tools.label_issue(LabelIssueParams(
            issue_number=42, labels=["bug", "high"],
        ))

        github.add_labels.assert_called_once()
        assert "bug" in result

    async def test_comment_on_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_pm_tools(registry, github)

        result = await tools.comment_on_issue(CommentOnIssueParams(
            issue_number=42, body="Triage complete",
        ))

        github.comment_on_issue.assert_called_once()
        assert "#42" in result

    async def test_check_registry_empty(self, registry):
        tools = self._make_pm_tools(registry)

        result = await tools.check_registry(CheckRegistryParams())

        assert "No active agents" in result

    async def test_check_registry_with_agents(self, registry):
        agent = _make_agent()
        await registry.create_agent(agent)
        tools = self._make_pm_tools(registry)

        result = await tools.check_registry(CheckRegistryParams())

        assert "feat-dev-issue-42" in result
        assert "feat-dev" in result

    async def test_read_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_pm_tools(registry, github)

        result = await tools.read_issue(ReadIssueParams(issue_number=42))

        github.get_issue.assert_called_once_with("testowner", "testrepo", 42)
        assert "Test Issue" in result
        assert "feature" in result

    async def test_get_tools_returns_list(self, registry):
        tools = self._make_pm_tools(registry)

        sdk_tools = tools.get_tools()

        assert len(sdk_tools) == 6


# ── Escalate to Human ───────────────────────────────────────────────────────


class TestEscalateToHuman:
    async def test_transitions_to_escalated(self, registry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = FrameworkTools(
            registry=registry, github=github, agent_inboxes={},
            owner="testowner", repo="testrepo",
        )

        result = await tools.escalate_to_human(
            agent.agent_id,
            EscalateToHumanParams(reason="Architectural decision needed", category="architectural"),
        )

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.ESCALATED
        assert updated.active_since is None
        assert "escalated" in result.lower()

    async def test_labels_issue_needs_human(self, registry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = FrameworkTools(
            registry=registry, github=github, agent_inboxes={},
            owner="testowner", repo="testrepo",
        )

        await tools.escalate_to_human(
            agent.agent_id,
            EscalateToHumanParams(reason="Need human", category="policy"),
        )

        github.add_labels.assert_called_once()
        args = github.add_labels.call_args
        assert "needs-human" in args[0][3]

    async def test_posts_escalation_comment(self, registry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = FrameworkTools(
            registry=registry, github=github, agent_inboxes={},
            owner="testowner", repo="testrepo",
        )

        await tools.escalate_to_human(
            agent.agent_id,
            EscalateToHumanParams(reason="Ambiguous req", category="ambiguous"),
        )

        github.comment_on_issue.assert_called_once()
        comment_body = github.comment_on_issue.call_args[0][3]
        assert "Escalation" in comment_body
        assert "Ambiguous req" in comment_body


# ── Template Interpolation ───────────────────────────────────────────────────


def _make_config(**overrides) -> SquadronConfig:
    defaults = dict(
        project=ProjectConfig(name="squadron", owner="noahbaertsch", repo="squadron"),
    )
    defaults.update(overrides)
    return SquadronConfig(**defaults)


def _make_manager(registry, config=None, github=None):
    from squadron.agent_manager import AgentManager

    return AgentManager(
        config=config or _make_config(),
        registry=registry,
        github=github or _make_github_mock(),
        router=MagicMock(),
        agent_definitions={},
        repo_root=Path("/tmp/test"),
    )


class TestTemplateInterpolation:

    def test_interpolates_basic_variables(self, registry):
        manager = _make_manager(registry)
        agent = _make_agent(branch="feat/issue-42")

        template = "Working on {project_name} issue #{issue_number} on branch {branch_name}"
        result = manager._interpolate_agent_def(template, agent, None)

        assert result == "Working on squadron issue #42 on branch feat/issue-42"

    def test_interpolates_issue_from_event(self, registry):
        manager = _make_manager(registry)
        agent = _make_agent()
        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_ASSIGNED,
            issue_number=42,
            data={"payload": {"issue": {"title": "Add auth", "body": "We need OAuth"}}},
        )

        template = "Issue: {issue_title}\nBody: {issue_body}"
        result = manager._interpolate_agent_def(template, agent, event)

        assert "Add auth" in result
        assert "We need OAuth" in result

    def test_missing_keys_become_empty_string(self, registry):
        manager = _make_manager(registry)
        agent = _make_agent()

        template = "Custom: {nonexistent_key} and {project_name}"
        result = manager._interpolate_agent_def(template, agent, None)

        assert result == "Custom:  and squadron"

    def test_interpolates_circuit_breaker_defaults(self, registry):
        manager = _make_manager(registry)
        agent = _make_agent()

        template = "Max iterations: {max_iterations}, Max tools: {max_tool_calls}"
        result = manager._interpolate_agent_def(template, agent, None)

        assert "Max iterations: 5" in result
        assert "Max tools: 200" in result


# ── PR Closed Handler ────────────────────────────────────────────────────────


class TestHandlePRClosed:

    async def test_pr_merged_completes_dev_agent(self, registry):
        agent = _make_agent(pr_number=10, session_id="ses-1")
        await registry.create_agent(agent)

        mgr = _make_manager(registry)
        mgr._copilot_agents[agent.agent_id] = AsyncMock()

        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={"payload": {"pull_request": {"merged": True}}},
        )

        await mgr._handle_pr_closed(event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED

    async def test_pr_merged_cleans_up_review_agents(self, registry):
        review_agent = _make_agent(
            agent_id="pr-review-pr-10",
            role=AgentRole.PR_REVIEW,
            issue_number=10,
            pr_number=10,
            session_id="ses-review",
        )
        await registry.create_agent(review_agent)

        mgr = _make_manager(registry)
        mock_copilot = AsyncMock()
        mock_copilot.delete_session = AsyncMock()
        mock_copilot.stop = AsyncMock()
        mgr._copilot_agents[review_agent.agent_id] = mock_copilot

        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={"payload": {"pull_request": {"merged": True}}},
        )

        await mgr._handle_pr_closed(event)

        updated = await registry.get_agent(review_agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED

    async def test_pr_closed_without_merge_wakes_dev_agent(self, registry):
        agent = _make_agent(
            status=AgentStatus.SLEEPING,
            pr_number=10,
            session_id="ses-1",
        )
        await registry.create_agent(agent)

        mgr = _make_manager(registry)

        # Mock wake_agent
        mgr.wake_agent = AsyncMock()

        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={"payload": {"pull_request": {"merged": False}}},
        )

        await mgr._handle_pr_closed(event)

        mgr.wake_agent.assert_called_once()
        assert mgr.wake_agent.call_args[0][0] == agent.agent_id

    async def test_pr_closed_without_merge_cleans_review_agents(self, registry):
        review_agent = _make_agent(
            agent_id="pr-review-pr-10",
            role=AgentRole.PR_REVIEW,
            issue_number=10,
            pr_number=10,
            session_id="ses-review",
        )
        await registry.create_agent(review_agent)

        mgr = _make_manager(registry)
        mock_copilot = AsyncMock()
        mock_copilot.delete_session = AsyncMock()
        mock_copilot.stop = AsyncMock()
        mgr._copilot_agents[review_agent.agent_id] = mock_copilot

        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={"payload": {"pull_request": {"merged": False}}},
        )

        await mgr._handle_pr_closed(event)

        updated = await registry.get_agent(review_agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED

    async def test_no_pr_number_skips(self, registry):
        mgr = _make_manager(registry)
        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            data={"payload": {"pull_request": {"merged": True}}},
        )

        # Should not raise
        await mgr._handle_pr_closed(event)
