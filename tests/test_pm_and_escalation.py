"""Tests for PM tools, escalation, template interpolation, and PR closed handler."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio

from squadron.config import (
    ProjectConfig,
    SquadronConfig,
)
from squadron.models import AgentRecord, AgentStatus, SquadronEvent, SquadronEventType
from squadron.registry import AgentRegistry
from squadron.tools.squadron_tools import (
    AssignIssueParams,
    CheckRegistryParams,
    CommentOnIssueParams,
    CreateIssueParams,
    EscalateToHumanParams,
    LabelIssueParams,
    ReadIssueParams,
    SquadronTools,
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
    role: str = "feat-dev",
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
    github.get_issue = AsyncMock(
        return_value={
            "number": 42,
            "title": "Test Issue",
            "body": "Issue body",
            "state": "open",
            "labels": [{"name": "feature"}],
            "assignees": [{"login": "squadron[bot]"}],
        }
    )
    return github


# ── PM Tools ─────────────────────────────────────────────────────────────────


class TestPMTools:
    def _make_tools(self, registry, github=None):
        return SquadronTools(
            registry=registry,
            github=github or _make_github_mock(),
            agent_inboxes={},
            owner="testowner",
            repo="testrepo",
        )

    async def test_create_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_tools(registry, github)

        result = await tools.create_issue(
            "pm-agent",
            CreateIssueParams(
                title="New bug",
                body="Something broke",
                labels=["bug"],
            ),
        )

        github.create_issue.assert_called_once()
        assert "#99" in result

    async def test_assign_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_tools(registry, github)

        result = await tools.assign_issue(
            "pm-agent",
            AssignIssueParams(
                issue_number=42,
                assignees=["squadron[bot]"],
            ),
        )

        github.assign_issue.assert_called_once_with("testowner", "testrepo", 42, ["squadron[bot]"])
        assert "#42" in result

    async def test_label_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_tools(registry, github)

        result = await tools.label_issue(
            "pm-agent",
            LabelIssueParams(
                issue_number=42,
                labels=["bug", "high"],
            ),
        )

        github.add_labels.assert_called_once()
        assert "bug" in result

    async def test_comment_on_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_tools(registry, github)

        result = await tools.comment_on_issue(
            "pm-agent",
            CommentOnIssueParams(
                issue_number=42,
                body="Triage complete",
            ),
        )

        github.comment_on_issue.assert_called_once()
        assert "#42" in result

    async def test_check_registry_empty(self, registry):
        tools = self._make_tools(registry)

        result = await tools.check_registry("pm-agent", CheckRegistryParams())

        assert "No active agents" in result

    async def test_check_registry_with_agents(self, registry):
        agent = _make_agent()
        await registry.create_agent(agent)
        tools = self._make_tools(registry)

        result = await tools.check_registry("pm-agent", CheckRegistryParams())

        assert "feat-dev-issue-42" in result
        assert "feat-dev" in result

    async def test_read_issue(self, registry):
        github = _make_github_mock()
        tools = self._make_tools(registry, github)

        result = await tools.read_issue("pm-agent", ReadIssueParams(issue_number=42))

        github.get_issue.assert_called_once_with("testowner", "testrepo", 42)
        assert "Test Issue" in result
        assert "feature" in result

    async def test_get_tools_returns_list(self, registry):
        tools = self._make_tools(registry)

        # Explicitly request the tools we want (no defaults)
        requested_tools = [
            "create_issue",
            "assign_issue",
            "label_issue",
            "comment_on_issue",
            "check_registry",
            "read_issue",
            "escalate_to_human",
            "report_complete",
        ]
        sdk_tools = tools.get_tools("pm-agent", requested_tools)

        assert len(sdk_tools) == 8
        # Verify each tool is callable and has the expected names
        tool_names = [t.name for t in sdk_tools]
        assert set(tool_names) == set(requested_tools), f"Tool names mismatch: {tool_names}"


# ── Escalate to Human ───────────────────────────────────────────────────────


class TestEscalateToHuman:
    async def test_transitions_to_escalated(self, registry):
        agent = _make_agent()
        await registry.create_agent(agent)
        github = _make_github_mock()
        tools = SquadronTools(
            registry=registry,
            github=github,
            agent_inboxes={},
            owner="testowner",
            repo="testrepo",
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
        tools = SquadronTools(
            registry=registry,
            github=github,
            agent_inboxes={},
            owner="testowner",
            repo="testrepo",
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
        tools = SquadronTools(
            registry=registry,
            github=github,
            agent_inboxes={},
            owner="testowner",
            repo="testrepo",
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


class TestWakePromptContext:
    """Test enriched wake prompt with review context."""

    async def test_wake_prompt_includes_review_context(self, registry):
        agent = _make_agent(pr_number=10)
        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)
        event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=10,
            data={
                "payload": {
                    "review": {
                        "state": "changes_requested",
                        "body": "Needs work",
                        "user": {"login": "reviewer1"},
                    }
                }
            },
        )

        prompt = await mgr._build_wake_prompt(agent, event)
        # Event-level review info is still included as context
        # Review state is now uppercased for clarity (issue #112)
        assert "CHANGES_REQUESTED" in prompt.upper()
        assert "Needs work" in prompt
        assert "reviewer1" in prompt
        # Inline review comments are no longer injected -- agents use
        # the get_pr_feedback tool to fetch them on demand

    async def test_wake_prompt_handles_missing_review(self, registry):
        agent = _make_agent(pr_number=None)
        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.BLOCKER_RESOLVED,
            issue_number=42,
            data={"resolved_issue": 99},
        )

        prompt = await mgr._build_wake_prompt(agent, event)
        assert "Resolved blocker" in prompt
        assert "#99" in prompt

    async def test_wake_prompt_survives_github_errors(self, registry):
        """Wake prompt works even without github API calls (review context
        is now fetched via get_pr_feedback tool, not injected)."""
        agent = _make_agent(pr_number=10)
        github = _make_github_mock()

        mgr = _make_manager(registry, github=github)
        event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=10,
            data={"payload": {"review": {"state": "changes_requested", "body": "Fix it"}}},
        )

        prompt = await mgr._build_wake_prompt(agent, event)
        assert "Session Resumed" in prompt
        assert "Fix it" in prompt


class TestStatelessPrompt:
    """Test ephemeral (PM) prompt — context-only, no pipeline instructions.

    The prompt now contains only structured event context (project, role,
    triggering event details).  Workload, history, escalations, and
    available roles are fetched on-demand via introspection tools.
    """

    async def test_includes_project_and_role(self, registry):
        """Prompt should show project name, repo, and role."""
        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        prompt = await mgr._build_stateless_prompt(pm_record, None)

        assert "squadron" in prompt
        assert "pm" in prompt

    async def test_includes_event_details(self, registry):
        """Prompt should include triggering event information."""
        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_OPENED,
            issue_number=99,
            data={
                "payload": {
                    "issue": {
                        "title": "Add OAuth",
                        "body": "We need OAuth2 support",
                        "labels": [{"name": "feature"}],
                    }
                }
            },
        )

        prompt = await mgr._build_stateless_prompt(pm_record, event)

        assert "Add OAuth" in prompt
        assert "OAuth2 support" in prompt
        assert "feature" in prompt
        assert "issue.opened" in prompt

    async def test_includes_comment_details(self, registry):
        """Prompt should include comment text when triggered by a comment event."""
        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_COMMENT,
            issue_number=99,
            data={
                "payload": {
                    "issue": {"title": "Something", "body": "body", "labels": []},
                    "comment": {
                        "body": "What is the status?",
                        "user": {"login": "alice"},
                    },
                }
            },
        )

        prompt = await mgr._build_stateless_prompt(pm_record, event)

        assert "What is the status?" in prompt
        assert "alice" in prompt

    async def test_includes_pr_data(self, registry):
        """Prompt should include PR title when triggered by a PR event."""
        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=10,
            issue_number=99,
            data={
                "payload": {
                    "pull_request": {"title": "feat: add OAuth support"},
                }
            },
        )

        prompt = await mgr._build_stateless_prompt(pm_record, event)

        assert "feat: add OAuth support" in prompt

    async def test_no_event_still_has_context(self, registry):
        """Prompt should still contain project/role context even without an event."""
        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        prompt = await mgr._build_stateless_prompt(pm_record, None)

        assert "squadron" in prompt
        assert "pm" in prompt
        assert "#99" in prompt


# ── Issue Reassignment (D-12) ───────────────────────────────────────────────


class TestHandleIssueAssigned:
    """Test _handle_issue_assigned — abort agents on reassignment."""

    async def test_reassign_away_completes_active_agent(self, registry):
        agent = _make_agent(status=AgentStatus.ACTIVE, issue_number=42)
        await registry.create_agent(agent)

        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_ASSIGNED,
            issue_number=42,
            data={"payload": {"assignee": {"login": "humandev"}}},
        )
        await mgr._handle_issue_assigned(event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED

    async def test_reassign_away_completes_sleeping_agent(self, registry):
        agent = _make_agent(status=AgentStatus.SLEEPING, issue_number=42)
        await registry.create_agent(agent)

        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_ASSIGNED,
            issue_number=42,
            data={"payload": {"assignee": {"login": "humandev"}}},
        )
        await mgr._handle_issue_assigned(event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED

    async def test_reassign_to_bot_is_ignored(self, registry):
        agent = _make_agent(status=AgentStatus.ACTIVE, issue_number=42)
        await registry.create_agent(agent)

        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_ASSIGNED,
            issue_number=42,
            data={"payload": {"assignee": {"login": "squadron-dev[bot]"}}},
        )
        await mgr._handle_issue_assigned(event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.ACTIVE  # unchanged

    async def test_reassign_posts_comment(self, registry):
        agent = _make_agent(status=AgentStatus.ACTIVE, issue_number=42, branch="feat/issue-42")
        await registry.create_agent(agent)

        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_ASSIGNED,
            issue_number=42,
            data={"payload": {"assignee": {"login": "humandev"}}},
        )
        await mgr._handle_issue_assigned(event)

        github.comment_on_issue.assert_called_once()
        comment_body = github.comment_on_issue.call_args[0][3]
        assert "humandev" in comment_body
        assert "stopped" in comment_body

    async def test_no_agents_for_issue_is_noop(self, registry):
        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_ASSIGNED,
            issue_number=999,
            data={"payload": {"assignee": {"login": "humandev"}}},
        )
        await mgr._handle_issue_assigned(event)

        github.comment_on_issue.assert_not_called()

    async def test_completed_agent_not_affected(self, registry):
        agent = _make_agent(status=AgentStatus.COMPLETED, issue_number=42)
        await registry.create_agent(agent)

        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_ASSIGNED,
            issue_number=42,
            data={"payload": {"assignee": {"login": "humandev"}}},
        )
        await mgr._handle_issue_assigned(event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED
        github.comment_on_issue.assert_not_called()

    async def test_missing_issue_number_is_noop(self, registry):
        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_ASSIGNED,
            data={"payload": {"assignee": {"login": "humandev"}}},
        )
        await mgr._handle_issue_assigned(event)

        github.comment_on_issue.assert_not_called()
