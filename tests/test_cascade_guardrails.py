"""Tests for cascade-prevention guardrails (fixes 2-5).

Fix 2: PR-delegation guard in command routing
Fix 3: Issue-scope pipeline dedup
Fix 4: Self-wake filtering (bot-authored comment → no WAKE_AGENT)
Fix 5: max_issue_depth enforcement in create_blocker_issue & create_issue
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from squadron.models import (
    AgentRecord,
    AgentStatus,
    ParsedCommand,
    SquadronEvent,
    SquadronEventType,
)
from squadron.tools.squadron_tools import (
    CreateBlockerIssueParams,
    CreateIssueParams,
    SquadronTools,
)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_record(
    role: str,
    issue_number: int = 86,
    pr_number: int | None = None,
    blocked_by: list[int] | None = None,
) -> AgentRecord:
    return AgentRecord(
        agent_id=f"{role}-issue-{issue_number}",
        role=role,
        issue_number=issue_number,
        status=AgentStatus.ACTIVE,
        branch=f"fix/issue-{issue_number}",
        pr_number=pr_number,
        blocked_by=blocked_by or [],
    )


def _make_tools(
    agent_record: AgentRecord,
    *,
    max_issue_depth: int = 3,
    agents_blocked_by: dict[int, list[AgentRecord]] | None = None,
) -> SquadronTools:
    """Create a SquadronTools instance with mocked registry and GitHub client."""
    registry = AsyncMock()
    registry.get_agent = AsyncMock(return_value=agent_record)
    registry.update_agent = AsyncMock()
    registry.add_blocker = AsyncMock(return_value=True)

    # Mock get_agents_blocked_by for depth computation
    _blocked_by_map = agents_blocked_by or {}

    async def _get_agents_blocked_by(issue_number: int) -> list[AgentRecord]:
        return _blocked_by_map.get(issue_number, [])

    registry.get_agents_blocked_by = AsyncMock(side_effect=_get_agents_blocked_by)

    github = AsyncMock()
    github.create_issue = AsyncMock(return_value={"number": 200})
    github.comment_on_issue = AsyncMock()

    config = MagicMock()
    config.escalation.max_issue_depth = max_issue_depth

    tools = SquadronTools(
        registry=registry,
        github=github,
        agent_inboxes={},
        owner="testowner",
        repo="testrepo",
        config=config,
        agent_definitions={},
    )
    tools._log_activity = AsyncMock()
    return tools


# ═══════════════════════════════════════════════════════════════════════
# Fix 2: PR-delegation guard in _handle_command_routing
# ═══════════════════════════════════════════════════════════════════════


class TestPRDelegationGuard:
    """Fix 2: Bot-authored @mention commands targeting dev agents should be
    blocked when the issue already has a dev agent with an open PR."""

    @pytest.fixture
    def agent_manager(self):
        """Minimal mock of the AgentManager for testing _handle_command_routing."""
        mgr = AsyncMock()
        mgr.config = MagicMock()
        mgr.registry = AsyncMock()
        mgr._get_sender_agent_role = MagicMock()
        mgr._handle_help_command = AsyncMock()
        mgr._post_unknown_agent_error = AsyncMock()
        mgr._command_spawn = AsyncMock()
        mgr._command_wake_or_spawn = AsyncMock()
        return mgr

    def _make_event(self, agent_name: str, issue_number: int = 86) -> SquadronEvent:
        return SquadronEvent(
            event_type=SquadronEventType.ISSUE_COMMENT,
            issue_number=issue_number,
            command=ParsedCommand(agent_name=agent_name, message="fix the security issue"),
        )

    @pytest.mark.asyncio
    async def test_blocks_dev_spawn_when_pr_exists(self, agent_manager):
        """Bot comment targeting bug-fix is blocked when issue already has a dev PR."""
        from squadron.agent_manager import AgentManager

        event = self._make_event("bug-fix")
        existing_agent = _make_record("feat-dev", issue_number=86, pr_number=42)

        agent_manager._get_sender_agent_role.return_value = "security-review"
        agent_manager.config.agent_roles = {
            "bug-fix": MagicMock(is_ephemeral=False),
            "security-review": MagicMock(),
        }
        agent_manager.registry.get_all_agents_for_issue = AsyncMock(return_value=[existing_agent])

        # Call the actual method, bound to our mock
        await AgentManager._handle_command_routing(agent_manager, event)

        # Should NOT have spawned or woken any agent
        agent_manager._command_spawn.assert_not_called()
        agent_manager._command_wake_or_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_dev_spawn_when_no_pr(self, agent_manager):
        """Bot comment targeting bug-fix is allowed when no existing dev PR."""
        from squadron.agent_manager import AgentManager

        event = self._make_event("bug-fix")
        existing_agent = _make_record("feat-dev", issue_number=86, pr_number=None)

        agent_manager._get_sender_agent_role.return_value = "security-review"
        agent_manager.config.agent_roles = {
            "bug-fix": MagicMock(is_ephemeral=False),
            "security-review": MagicMock(),
        }
        agent_manager.registry.get_all_agents_for_issue = AsyncMock(return_value=[existing_agent])

        await AgentManager._handle_command_routing(agent_manager, event)

        # Should have routed (wake or spawn)
        agent_manager._command_wake_or_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_allows_human_to_dev_even_with_pr(self, agent_manager):
        """Human-authored comment targeting bug-fix is allowed even with existing PR."""
        from squadron.agent_manager import AgentManager

        event = self._make_event("bug-fix")
        existing_agent = _make_record("feat-dev", issue_number=86, pr_number=42)

        # sender_role is None for humans
        agent_manager._get_sender_agent_role.return_value = None
        agent_manager.config.agent_roles = {
            "bug-fix": MagicMock(is_ephemeral=False),
        }
        agent_manager.registry.get_all_agents_for_issue = AsyncMock(return_value=[existing_agent])

        await AgentManager._handle_command_routing(agent_manager, event)

        # Human commands should be routed regardless
        agent_manager._command_wake_or_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_allows_non_dev_target(self, agent_manager):
        """Bot targeting a non-dev agent (e.g. pm) is always allowed."""
        from squadron.agent_manager import AgentManager

        event = self._make_event("pm")
        existing_agent = _make_record("feat-dev", issue_number=86, pr_number=42)

        agent_manager._get_sender_agent_role.return_value = "security-review"
        agent_manager.config.agent_roles = {
            "pm": MagicMock(is_ephemeral=False),
            "security-review": MagicMock(),
        }
        agent_manager.registry.get_all_agents_for_issue = AsyncMock(return_value=[existing_agent])

        await AgentManager._handle_command_routing(agent_manager, event)

        agent_manager._command_wake_or_spawn.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# Fix 3: Issue-scope pipeline dedup in evaluate_event
# ═══════════════════════════════════════════════════════════════════════


class TestIssueScopePipelineDedup:
    """Fix 3: evaluate_event should not start a duplicate lifecycle pipeline
    for the same issue when one is already running."""

    @pytest.mark.asyncio
    async def test_skips_duplicate_issue_pipeline(self):
        """If a pipeline is already running for an issue, skip starting another."""
        from squadron.pipeline.engine import PipelineEngine

        # We need a real engine instance with mocked internals
        registry = AsyncMock()
        engine = PipelineEngine(registry=registry, gate_registry=MagicMock())

        # Mock a pipeline definition that triggers on issues.opened
        trigger = MagicMock()
        trigger.matches = MagicMock(return_value=True)

        defn = MagicMock()
        defn.trigger = trigger
        defn.on_events = {}

        engine._pipelines = {"issue-lifecycle": defn}

        # Simulate an existing running pipeline for issue #50
        existing_run = MagicMock()
        existing_run.pipeline_name = "issue-lifecycle"

        registry.get_pipeline_runs_by_issue = AsyncMock(return_value=[existing_run])
        registry.get_running_pipelines_for_pr = AsyncMock(return_value=[])

        engine._start_pipeline = AsyncMock()
        engine._route_reactive_event = AsyncMock()

        payload = {"issue": {"number": 50}}

        await engine.evaluate_event("issues.opened", payload)

        # _start_pipeline should NOT have been called (dedup)
        engine._start_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_first_issue_pipeline(self):
        """If no pipeline is running for an issue, allow starting one."""
        from squadron.pipeline.engine import PipelineEngine

        registry = AsyncMock()
        engine = PipelineEngine(registry=registry, gate_registry=MagicMock())

        trigger = MagicMock()
        trigger.matches = MagicMock(return_value=True)

        defn = MagicMock()
        defn.trigger = trigger
        defn.on_events = {}

        engine._pipelines = {"issue-lifecycle": defn}

        # No existing pipeline runs
        registry.get_pipeline_runs_by_issue = AsyncMock(return_value=[])
        registry.get_running_pipelines_for_pr = AsyncMock(return_value=[])

        engine._start_pipeline = AsyncMock(return_value=MagicMock())
        engine._route_reactive_event = AsyncMock()

        payload = {"issue": {"number": 50}}

        await engine.evaluate_event("issues.opened", payload)

        # _start_pipeline SHOULD have been called
        engine._start_pipeline.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# Fix 4: Self-wake filtering (bot-authored comments)
# ═══════════════════════════════════════════════════════════════════════


class TestSelfWakeFiltering:
    """Fix 4: _handle_reactive_wake_agent should skip the wake when the
    triggering event payload is a bot-authored comment."""

    @pytest.mark.asyncio
    async def test_skips_wake_for_bot_comment(self):
        """WAKE_AGENT is suppressed when the comment was posted by a Bot user."""
        from squadron.pipeline.engine import PipelineEngine

        registry = AsyncMock()
        engine = PipelineEngine(registry=registry, gate_registry=MagicMock())
        engine._spawn_agent = AsyncMock()

        run = MagicMock()
        run.run_id = "run-1"
        run.current_stage_id = "dev"

        definition = MagicMock()

        # Bot-authored comment payload
        payload = {
            "comment": {
                "user": {"login": "squadron-bot[bot]", "type": "Bot"},
                "body": "I found an issue...",
            }
        }

        await engine._handle_reactive_wake_agent(run, definition, payload)

        # spawn_agent should NOT have been called
        engine._spawn_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_wake_for_human_comment(self):
        """WAKE_AGENT proceeds when the comment was posted by a human User."""
        from squadron.pipeline.engine import PipelineEngine

        registry = AsyncMock()
        engine = PipelineEngine(registry=registry, gate_registry=MagicMock())
        engine._spawn_agent = AsyncMock()

        run = MagicMock()
        run.run_id = "run-1"
        run.current_stage_id = "dev"
        run.issue_number = 50
        run.pr_number = 10
        run.context = {}

        stage = MagicMock()
        stage.id = "dev"
        stage.agent = "feat-dev"

        definition = MagicMock()
        definition.get_stage = MagicMock(return_value=stage)

        latest_stage_run = MagicMock()
        latest_stage_run.agent_id = "feat-dev-issue-50"
        registry.get_latest_stage_run = AsyncMock(return_value=latest_stage_run)

        # Human-authored comment payload
        payload = {
            "comment": {
                "user": {"login": "nbaertsch", "type": "User"},
                "body": "Please fix the tests",
            }
        }

        await engine._handle_reactive_wake_agent(run, definition, payload)

        # spawn_agent SHOULD have been called
        engine._spawn_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_allows_wake_with_no_payload(self):
        """WAKE_AGENT proceeds when payload is None (non-comment events)."""
        from squadron.pipeline.engine import PipelineEngine

        registry = AsyncMock()
        engine = PipelineEngine(registry=registry, gate_registry=MagicMock())
        engine._spawn_agent = AsyncMock()

        run = MagicMock()
        run.run_id = "run-1"
        run.current_stage_id = "dev"
        run.issue_number = 50
        run.pr_number = 10
        run.context = {}

        stage = MagicMock()
        stage.id = "dev"
        stage.agent = "feat-dev"

        definition = MagicMock()
        definition.get_stage = MagicMock(return_value=stage)

        latest_stage_run = MagicMock()
        latest_stage_run.agent_id = "feat-dev-issue-50"
        registry.get_latest_stage_run = AsyncMock(return_value=latest_stage_run)

        await engine._handle_reactive_wake_agent(run, definition, None)

        engine._spawn_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_allows_wake_for_non_comment_payload(self):
        """WAKE_AGENT proceeds when payload has no 'comment' key."""
        from squadron.pipeline.engine import PipelineEngine

        registry = AsyncMock()
        engine = PipelineEngine(registry=registry, gate_registry=MagicMock())
        engine._spawn_agent = AsyncMock()

        run = MagicMock()
        run.run_id = "run-1"
        run.current_stage_id = "dev"
        run.issue_number = 50
        run.pr_number = 10
        run.context = {}

        stage = MagicMock()
        stage.id = "dev"
        stage.agent = "feat-dev"

        definition = MagicMock()
        definition.get_stage = MagicMock(return_value=stage)

        latest_stage_run = MagicMock()
        latest_stage_run.agent_id = "feat-dev-issue-50"
        registry.get_latest_stage_run = AsyncMock(return_value=latest_stage_run)

        # Payload without a comment (e.g. push event)
        payload = {"ref": "refs/heads/main"}

        await engine._handle_reactive_wake_agent(run, definition, payload)

        engine._spawn_agent.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# Fix 5: max_issue_depth enforcement
# ═══════════════════════════════════════════════════════════════════════


class TestMaxIssueDepthBlockerIssue:
    """Fix 5a: create_blocker_issue should refuse when the blocker chain
    has reached max_issue_depth."""

    @pytest.mark.asyncio
    async def test_blocks_at_max_depth(self):
        """Agent at depth 3 cannot create a blocker issue (max_issue_depth=3)."""
        # Build a chain: root (#10) → child (#20) → grandchild (#30) → great-grandchild (#40)
        root = _make_record("feat-dev", issue_number=10, blocked_by=[20])
        child = _make_record("bug-fix", issue_number=20, blocked_by=[30])
        grandchild = _make_record("bug-fix", issue_number=30, blocked_by=[40])
        great_grandchild = _make_record("bug-fix", issue_number=40)

        # The agent trying to create yet another blocker is 'great_grandchild' (depth 3)
        tools = _make_tools(
            great_grandchild,
            max_issue_depth=3,
            agents_blocked_by={
                # Who is blocked by issue #40? -> grandchild (issue #30)
                40: [grandchild],
                # Who is blocked by issue #30? -> child (issue #20)
                30: [child],
                # Who is blocked by issue #20? -> root (issue #10)
                20: [root],
                # Who is blocked by issue #10? -> nobody
                10: [],
            },
        )

        result = await tools.create_blocker_issue(
            agent_id=great_grandchild.agent_id,
            params=CreateBlockerIssueParams(
                title="Yet another blocker",
                body="This should be rejected",
            ),
        )

        # Should be blocked
        assert "cannot create" in result.lower()
        assert "escalate" in result.lower()
        # GitHub API should NOT have been called
        tools.github.create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_below_max_depth(self):
        """Agent at depth 1 can create a blocker issue (max_issue_depth=3)."""
        root = _make_record("feat-dev", issue_number=10, blocked_by=[20])
        child = _make_record("bug-fix", issue_number=20)

        tools = _make_tools(
            child,
            max_issue_depth=3,
            agents_blocked_by={
                20: [root],
                10: [],
            },
        )

        result = await tools.create_blocker_issue(
            agent_id=child.agent_id,
            params=CreateBlockerIssueParams(
                title="A valid blocker",
                body="This should be allowed",
            ),
        )

        # Should be allowed — GitHub API called
        tools.github.create_issue.assert_called_once()
        assert "200" in result  # issue #200 from the mock

    @pytest.mark.asyncio
    async def test_root_agent_can_create_blocker(self):
        """Root agent (depth 0) can always create a blocker."""
        root = _make_record("feat-dev", issue_number=10)

        tools = _make_tools(
            root,
            max_issue_depth=3,
            agents_blocked_by={10: []},
        )

        result = await tools.create_blocker_issue(
            agent_id=root.agent_id,
            params=CreateBlockerIssueParams(
                title="First blocker",
                body="Should be allowed",
            ),
        )

        tools.github.create_issue.assert_called_once()
        assert "200" in result


class TestMaxIssueDepthCreateIssue:
    """Fix 5b: create_issue should enforce a per-agent issue creation cap."""

    @pytest.mark.asyncio
    async def test_blocks_after_cap_reached(self):
        """Agent that already created max issues gets blocked."""
        agent = _make_record("pm", issue_number=10)
        tools = _make_tools(agent, max_issue_depth=3)

        # Simulate having already created 6 issues (cap = max_issue_depth * 2 = 6)
        tools._issues_created[agent.agent_id] = 6

        result = await tools.create_issue(
            agent_id=agent.agent_id,
            params=CreateIssueParams(
                title="One too many",
                body="Should be rejected",
            ),
        )

        assert "cannot create issue" in result.lower()
        tools.github.create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_under_cap(self):
        """Agent under the cap can create issues freely."""
        agent = _make_record("pm", issue_number=10)
        tools = _make_tools(agent, max_issue_depth=3)

        # No issues created yet
        result = await tools.create_issue(
            agent_id=agent.agent_id,
            params=CreateIssueParams(
                title="Valid issue",
                body="Should be allowed",
            ),
        )

        tools.github.create_issue.assert_called_once()
        assert "200" in result

    @pytest.mark.asyncio
    async def test_counter_increments(self):
        """Each successful create_issue call increments the counter."""
        agent = _make_record("pm", issue_number=10)
        tools = _make_tools(agent, max_issue_depth=3)

        # Create 3 issues
        for i in range(3):
            tools.github.create_issue = AsyncMock(return_value={"number": 200 + i})
            await tools.create_issue(
                agent_id=agent.agent_id,
                params=CreateIssueParams(title=f"Issue {i}", body=f"Body {i}"),
            )

        assert tools._issues_created[agent.agent_id] == 3

    @pytest.mark.asyncio
    async def test_cap_scales_with_max_depth(self):
        """Cap is 2× max_issue_depth, so depth=2 gives cap=4."""
        agent = _make_record("pm", issue_number=10)
        tools = _make_tools(agent, max_issue_depth=2)  # cap = 4

        tools._issues_created[agent.agent_id] = 4

        result = await tools.create_issue(
            agent_id=agent.agent_id,
            params=CreateIssueParams(title="Over cap", body="Rejected"),
        )

        assert "cannot create issue" in result.lower()
        assert "4" in result  # should mention the cap


class TestComputeBlockerDepth:
    """Unit tests for _compute_blocker_depth helper."""

    @pytest.mark.asyncio
    async def test_root_agent_depth_zero(self):
        """Agent with no parent in the blocker chain has depth 0."""
        root = _make_record("feat-dev", issue_number=10)
        tools = _make_tools(root, agents_blocked_by={10: []})

        depth = await tools._compute_blocker_depth(root)
        assert depth == 0

    @pytest.mark.asyncio
    async def test_depth_one(self):
        """Agent that is a direct blocker for one parent has depth 1."""
        root = _make_record("feat-dev", issue_number=10, blocked_by=[20])
        child = _make_record("bug-fix", issue_number=20)
        tools = _make_tools(
            child,
            agents_blocked_by={20: [root], 10: []},
        )

        depth = await tools._compute_blocker_depth(child)
        assert depth == 1

    @pytest.mark.asyncio
    async def test_depth_three(self):
        """Chain of length 3: root → child → grandchild → great-grandchild."""
        root = _make_record("feat-dev", issue_number=10, blocked_by=[20])
        child = _make_record("bug-fix", issue_number=20, blocked_by=[30])
        grandchild = _make_record("bug-fix", issue_number=30, blocked_by=[40])
        great_grandchild = _make_record("bug-fix", issue_number=40)

        tools = _make_tools(
            great_grandchild,
            agents_blocked_by={
                40: [grandchild],
                30: [child],
                20: [root],
                10: [],
            },
        )

        depth = await tools._compute_blocker_depth(great_grandchild)
        assert depth == 3
