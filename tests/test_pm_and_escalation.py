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

        sdk_tools = tools.get_tools("pm-agent", is_stateless=True)

        assert len(sdk_tools) == 8
        # Verify each tool is callable and has the expected names
        tool_names = [t.name for t in sdk_tools]
        expected = {
            "create_issue",
            "assign_issue",
            "label_issue",
            "comment_on_issue",
            "check_registry",
            "read_issue",
            "escalate_to_human",
            "report_complete",
        }
        assert set(tool_names) == expected, f"Tool names mismatch: {tool_names}"


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


# ── PR Closed Handler ────────────────────────────────────────────────────────


class TestTriggerActions:
    """Test the unified trigger system's wake and complete actions."""

    async def test_trigger_complete_marks_agent_completed(self, registry):
        agent = _make_agent(pr_number=10, session_id="ses-1")
        await registry.create_agent(agent)

        mgr = _make_manager(registry)
        mgr._copilot_agents[agent.agent_id] = AsyncMock()

        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={"payload": {"pull_request": {"merged": True}}},
        )

        await mgr._trigger_complete("feat-dev", event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED

    async def test_trigger_complete_review_agent(self, registry):
        review_agent = _make_agent(
            agent_id="pr-review-pr-10",
            role="pr-review",
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

        await mgr._trigger_complete("pr-review", event)

        updated = await registry.get_agent(review_agent.agent_id)
        assert updated.status == AgentStatus.COMPLETED

    async def test_trigger_wake_sleeping_dev_agent(self, registry):
        agent = _make_agent(
            status=AgentStatus.SLEEPING,
            pr_number=10,
            session_id="ses-1",
        )
        await registry.create_agent(agent)

        mgr = _make_manager(registry)
        mgr.wake_agent = AsyncMock()

        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={"payload": {"pull_request": {"merged": False}}},
        )

        await mgr._trigger_wake("feat-dev", event)

        mgr.wake_agent.assert_called_once()
        assert mgr.wake_agent.call_args[0][0] == agent.agent_id

    async def test_trigger_complete_ignores_wrong_role(self, registry):
        agent = _make_agent(pr_number=10, session_id="ses-1", role="feat-dev")
        await registry.create_agent(agent)

        mgr = _make_manager(registry)

        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={"payload": {"pull_request": {"merged": True}}},
        )

        # Completing pr-review should not affect feat-dev
        await mgr._trigger_complete("pr-review", event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.ACTIVE  # unchanged

    async def test_trigger_sleep_active_dev_agent(self, registry):
        """Sleep action transitions an active agent to SLEEPING."""
        agent = _make_agent(
            status=AgentStatus.ACTIVE,
            pr_number=10,
            session_id="ses-1",
        )
        await registry.create_agent(agent)

        mgr = _make_manager(registry, github=_make_github_mock())
        # Simulate a running task
        task = AsyncMock()
        task.done = MagicMock(return_value=False)
        task.cancel = MagicMock()
        mgr._agent_tasks[agent.agent_id] = task

        event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=10,
            data={"payload": {"pull_request": {"body": "Closes #42"}}},
        )

        await mgr._trigger_sleep("feat-dev", event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.SLEEPING
        assert updated.sleeping_since is not None
        assert updated.active_since is None
        # Task should be cancelled
        task.cancel.assert_called_once()

    async def test_trigger_sleep_ignores_non_active(self, registry):
        """Sleep action should only affect ACTIVE agents."""
        agent = _make_agent(
            status=AgentStatus.SLEEPING,
            pr_number=10,
            session_id="ses-1",
        )
        await registry.create_agent(agent)

        mgr = _make_manager(registry)
        event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=10,
            data={},
        )

        await mgr._trigger_sleep("feat-dev", event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.SLEEPING  # unchanged

    async def test_trigger_sleep_matches_by_issue_from_pr_body(self, registry):
        """Sleep should match agents by issue number extracted from PR body."""
        agent = _make_agent(
            status=AgentStatus.ACTIVE,
            issue_number=42,
            pr_number=None,  # PR not yet associated
            session_id="ses-1",
        )
        await registry.create_agent(agent)

        mgr = _make_manager(registry, github=_make_github_mock())

        event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=15,
            data={"payload": {"pull_request": {"body": "Closes #42"}}},
        )

        await mgr._trigger_sleep("feat-dev", event)

        updated = await registry.get_agent(agent.agent_id)
        assert updated.status == AgentStatus.SLEEPING
        # PR should be associated with the agent
        assert updated.pr_number == 15

    async def test_trigger_sleep_posts_comment(self, registry):
        """Sleep action should post a comment on the agent's issue."""
        agent = _make_agent(
            status=AgentStatus.ACTIVE,
            pr_number=10,
            session_id="ses-1",
        )
        await registry.create_agent(agent)

        github = _make_github_mock()
        mgr = _make_manager(registry, github=github)

        event = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=10,
            data={},
        )

        await mgr._trigger_sleep("feat-dev", event)

        github.comment_on_issue.assert_called_once()
        comment_text = github.comment_on_issue.call_args[0][3]
        assert "PR #10" in comment_text
        assert "sleep" in comment_text.lower()


class TestEvaluateCondition:
    """Test condition evaluation including review_state."""

    def test_review_state_changes_requested(self, registry):
        mgr = _make_manager(registry)
        condition = {"review_state": "changes_requested"}
        event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=10,
            data={},
        )
        payload = {"review": {"state": "changes_requested"}}
        assert mgr._evaluate_condition(condition, event, "feat-dev", payload) is True

    def test_review_state_approved_does_not_match_changes_requested(self, registry):
        mgr = _make_manager(registry)
        condition = {"review_state": "changes_requested"}
        event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=10,
            data={},
        )
        payload = {"review": {"state": "approved"}}
        assert mgr._evaluate_condition(condition, event, "feat-dev", payload) is False

    def test_review_state_case_insensitive(self, registry):
        mgr = _make_manager(registry)
        condition = {"review_state": "CHANGES_REQUESTED"}
        event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=10,
            data={},
        )
        payload = {"review": {"state": "changes_requested"}}
        assert mgr._evaluate_condition(condition, event, "feat-dev", payload) is True

    def test_merged_true(self, registry):
        mgr = _make_manager(registry)
        condition = {"merged": True}
        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={},
        )
        payload = {"pull_request": {"merged": True}}
        assert mgr._evaluate_condition(condition, event, "feat-dev", payload) is True

    def test_merged_false_when_true(self, registry):
        mgr = _make_manager(registry)
        condition = {"merged": False}
        event = SquadronEvent(
            event_type=SquadronEventType.PR_CLOSED,
            pr_number=10,
            data={},
        )
        payload = {"pull_request": {"merged": True}}
        assert mgr._evaluate_condition(condition, event, "feat-dev", payload) is False


class TestWakePromptContext:
    """Test enriched wake prompt with review context."""

    async def test_wake_prompt_includes_review_context(self, registry):
        agent = _make_agent(pr_number=10)
        github = _make_github_mock()
        github.get_pr_review_comments = AsyncMock(
            return_value=[
                {
                    "path": "src/main.py",
                    "line": 42,
                    "body": "Fix this logic",
                    "user": {"login": "reviewer1"},
                },
            ]
        )
        github.list_pull_request_files = AsyncMock(
            return_value=[
                {"filename": "src/main.py", "status": "modified", "additions": 10, "deletions": 5},
            ]
        )

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
        assert "changes_requested" in prompt
        assert "Needs work" in prompt
        assert "src/main.py:42" in prompt
        assert "Fix this logic" in prompt
        assert "+10/-5" in prompt

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
        agent = _make_agent(pr_number=10)
        github = _make_github_mock()
        github.get_pr_review_comments = AsyncMock(side_effect=Exception("API error"))
        github.list_pull_request_files = AsyncMock(side_effect=Exception("API error"))

        mgr = _make_manager(registry, github=github)
        event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            pr_number=10,
            data={"payload": {"review": {"state": "changes_requested", "body": "Fix it"}}},
        )

        # Should not raise — gracefully degrades
        prompt = await mgr._build_wake_prompt(agent, event)
        assert "Session Resumed" in prompt
        assert "Fix it" in prompt


class TestStatelessPrompt:
    """Test enriched ephemeral PM prompt with workload, history, and escalations."""

    async def test_includes_workload_summary(self, registry):
        """Prompt should show active agent workload table."""
        agent1 = _make_agent(agent_id="feat-dev-issue-42", role="feat-dev", issue_number=42)
        agent2 = _make_agent(
            agent_id="bug-fix-issue-43",
            role="bug-fix",
            issue_number=43,
            status=AgentStatus.SLEEPING,
        )
        await registry.create_agent(agent1)
        await registry.create_agent(agent2)

        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        event = SquadronEvent(
            event_type=SquadronEventType.ISSUE_OPENED,
            issue_number=99,
            data={
                "payload": {
                    "issue": {"title": "New feature request", "body": "Please add X", "labels": []}
                }
            },
        )

        prompt = await mgr._build_stateless_prompt(pm_record, event)

        assert "Current Workload" in prompt
        assert "Total active" in prompt
        assert "feat-dev" in prompt
        assert "bug-fix" in prompt
        assert "#42" in prompt
        assert "#43" in prompt

    async def test_includes_escalated_agents(self, registry):
        """Prompt should highlight escalated agents needing human attention."""
        agent = _make_agent(
            agent_id="feat-dev-issue-50",
            role="feat-dev",
            issue_number=50,
            status=AgentStatus.ESCALATED,
        )
        await registry.create_agent(agent)

        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        prompt = await mgr._build_stateless_prompt(pm_record, None)

        assert "Pending Escalations" in prompt
        assert "feat-dev-issue-50" in prompt
        assert "#50" in prompt

    async def test_includes_recent_history(self, registry):
        """Prompt should include recently completed agents."""
        agent = _make_agent(
            agent_id="feat-dev-issue-30",
            role="feat-dev",
            issue_number=30,
            status=AgentStatus.COMPLETED,
        )
        await registry.create_agent(agent)

        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        prompt = await mgr._build_stateless_prompt(pm_record, None)

        assert "Recent History" in prompt
        assert "feat-dev-issue-30" in prompt
        assert "completed" in prompt

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

    async def test_includes_available_roles(self, registry):
        """Prompt should list available agent roles."""
        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        prompt = await mgr._build_stateless_prompt(pm_record, None)

        assert "Available Agent Roles" in prompt

    async def test_empty_workload(self, registry):
        """Prompt should handle no active agents gracefully."""
        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry)

        prompt = await mgr._build_stateless_prompt(pm_record, None)

        assert "No agents currently active" in prompt

    async def test_only_spawn_triggers_in_role_list(self, registry):
        """Available Roles section should only show spawn triggers, not wake/sleep/complete."""
        from squadron.config import AgentRoleConfig, AgentTrigger

        config = _make_config(
            agent_roles={
                "feat-dev": AgentRoleConfig(
                    agent_definition="agents/feat-dev.md",
                    triggers=[
                        AgentTrigger(event="issues.labeled", label="feature"),
                        AgentTrigger(event="pull_request.opened", action="sleep", filter_bot=False),
                        AgentTrigger(event="pull_request.closed", action="complete"),
                    ],
                ),
            }
        )
        pm_record = _make_agent(agent_id="pm-issue-99", role="pm", issue_number=99)
        mgr = _make_manager(registry, config=config)

        prompt = await mgr._build_stateless_prompt(pm_record, None)

        # Should show the spawn trigger
        assert "issues.labeled[feature]" in prompt
        # Should NOT show sleep/complete triggers in the roles list
        assert "pull_request.opened" not in prompt
        assert "pull_request.closed" not in prompt


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
            data={"payload": {"assignee": {"login": "squadron[bot]"}}},
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
