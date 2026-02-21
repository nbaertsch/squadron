"""Regression tests for runtime bugs and type-narrowing fixes found during #117 security audit.

Covers:
- BUG 1: _find_existing_pr_for_issue uses self.config.project.owner/repo (not self.owner/repo)
- BUG 3: spawn_workflow_agent signature matches SpawnAgentCallback protocol
- Type narrowing: _create_worktree raises ValueError when record.branch is None
- Type narrowing: watchdog comment skipped when agent.issue_number is None
- Type narrowing: proxy.py response is always bound in _handle_connection finally block
"""

from __future__ import annotations

import asyncio
import inspect
import json
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
from squadron.models import AgentRecord, AgentStatus, SquadronEvent, SquadronEventType
from squadron.registry import AgentRegistry
from squadron.workflow.engine import SpawnAgentCallback


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_bugfix_regressions.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _make_agent(
    agent_id: str = "feat-dev-issue-42",
    role: str = "feat-dev",
    issue_number: int | None = 42,
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
    github.list_pull_requests = AsyncMock(return_value=[])
    github._ensure_token = AsyncMock(return_value="ghs_fake_token")
    return github


def _make_manager_deps(registry):
    """Create minimal AgentManager dependencies for testing."""
    config = MagicMock(spec=SquadronConfig)
    config.project = ProjectConfig(name="test", owner="testowner", repo="testrepo")
    config.runtime = RuntimeConfig()
    config.circuit_breakers = CircuitBreakerConfig()
    config.agent_roles = {}
    config.labels = LabelsConfig()

    github = _make_github_mock()
    router = MagicMock()
    return config, github, router


def _make_manager(config, registry, github, router):
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


# ── BUG 1: _find_existing_pr_for_issue uses config.project.owner/repo ───────


class TestFindExistingPrForIssue:
    """Verify _find_existing_pr_for_issue reads owner/repo from config.project.

    Previously used self.owner and self.repo which don't exist on AgentManager,
    causing an AttributeError at runtime.
    """

    async def test_uses_config_project_owner_repo(self, registry):
        """_find_existing_pr_for_issue calls list_pull_requests with
        self.config.project.owner and self.config.project.repo."""
        config, github, router = _make_manager_deps(registry)
        manager = _make_manager(config, registry, github, router)

        # Should not raise AttributeError for self.owner / self.repo
        result = await manager._find_existing_pr_for_issue(42)

        github.list_pull_requests.assert_awaited_once_with("testowner", "testrepo", state="open")
        assert result is None  # no matching PRs in empty list

    async def test_finds_pr_by_closing_keyword(self, registry):
        """Returns a PR whose body contains 'Closes #N'."""
        config, github, router = _make_manager_deps(registry)
        manager = _make_manager(config, registry, github, router)

        github.list_pull_requests.return_value = [
            {
                "number": 10,
                "body": "Closes #42\nSome description here",
                "head": {"ref": "feat/unrelated"},
            },
        ]

        result = await manager._find_existing_pr_for_issue(42)
        assert result is not None
        assert result["number"] == 10

    async def test_finds_pr_by_branch_pattern(self, registry):
        """Returns a PR whose branch matches fix/issue-N pattern."""
        config, github, router = _make_manager_deps(registry)
        manager = _make_manager(config, registry, github, router)

        github.list_pull_requests.return_value = [
            {
                "number": 20,
                "body": "General fix",
                "head": {"ref": "fix/issue-42"},
            },
        ]

        result = await manager._find_existing_pr_for_issue(42)
        assert result is not None
        assert result["number"] == 20


# ── BUG 3: spawn_workflow_agent matches SpawnAgentCallback protocol ──────────


class TestSpawnWorkflowAgentSignature:
    """Verify spawn_workflow_agent signature matches SpawnAgentCallback protocol.

    Previously had mismatched param names (pr_number vs issue_number,
    event vs trigger_event, stage_name vs stage_id) which would cause
    TypeError when the workflow engine tried to call it.
    """

    def test_signature_matches_protocol(self):
        """spawn_workflow_agent must accept the exact kwargs that
        WorkflowEngine._execute_agent_stage passes."""
        from squadron.agent_manager import AgentManager

        method_sig = inspect.signature(AgentManager.spawn_workflow_agent)
        proto_sig = inspect.signature(SpawnAgentCallback.__call__)

        # Extract parameter names (skip 'self')
        method_params = {
            name: param for name, param in method_sig.parameters.items() if name != "self"
        }
        proto_params = {
            name: param for name, param in proto_sig.parameters.items() if name != "self"
        }

        # Every protocol parameter must exist in the method with matching kind
        for name, proto_param in proto_params.items():
            assert name in method_params, (
                f"Protocol parameter '{name}' missing from spawn_workflow_agent"
            )
            method_param = method_params[name]
            assert method_param.kind == proto_param.kind, (
                f"Parameter '{name}' kind mismatch: "
                f"method={method_param.kind.name}, protocol={proto_param.kind.name}"
            )

    async def test_can_be_called_with_engine_kwargs(self, registry):
        """spawn_workflow_agent can be invoked with the exact keyword args
        that _execute_agent_stage uses, without TypeError."""
        config, github, router = _make_manager_deps(registry)
        manager = _make_manager(config, registry, github, router)

        # Provide a valid agent definition so the method progresses
        from squadron.config import AgentDefinition

        manager.agent_definitions["test-reviewer"] = AgentDefinition(
            role="test-reviewer",
            raw_content="---\nname: test-reviewer\n---\nYou are a test reviewer.",
            prompt="You are a test reviewer.",
        )

        trigger = SquadronEvent(
            event_type=SquadronEventType.PR_OPENED,
            pr_number=99,
            issue_number=42,
            data={
                "payload": {
                    "pull_request": {
                        "number": 99,
                        "head": {"ref": "feat/test"},
                        "body": "Closes #42",
                    }
                }
            },
        )

        with patch("squadron.agent_manager.CopilotAgent") as MockCA:
            mock_copilot = AsyncMock()
            mock_copilot.start = AsyncMock()
            MockCA.return_value = mock_copilot

            # This is the exact call pattern from engine._execute_agent_stage (line 383)
            agent_id = await manager.spawn_workflow_agent(
                role="test-reviewer",
                issue_number=42,
                trigger_event=trigger,
                workflow_run_id="run-abc",
                stage_id="review-stage",
                action="review",
            )

            assert agent_id is not None
            assert "test-reviewer" in agent_id


# ── Type narrowing: _create_worktree raises ValueError on None branch ────────


class TestCreateWorktreeNoneBranch:
    """_create_worktree must raise ValueError when record.branch is None.

    Previously, a None branch would propagate to _run_git calls as a None
    argument, causing cryptic git failures downstream.
    """

    async def test_raises_valueerror_when_branch_is_none(self, registry):
        config, github, router = _make_manager_deps(registry)
        manager = _make_manager(config, registry, github, router)

        record = _make_agent(branch=None)
        with pytest.raises(ValueError, match="branch is not set"):
            await manager._create_worktree(record)

    async def test_raises_valueerror_when_branch_is_empty(self, registry):
        config, github, router = _make_manager_deps(registry)
        manager = _make_manager(config, registry, github, router)

        record = _make_agent(branch="")
        with pytest.raises(ValueError, match="branch is not set"):
            await manager._create_worktree(record)


# ── Type narrowing: watchdog skips comment when issue_number is None ─────────


class TestWatchdogIssueNumberGuard:
    """Watchdog must not call comment_on_issue when agent.issue_number is None.

    Previously, a None issue_number was passed directly to comment_on_issue(int),
    which expects a non-optional int argument.
    """

    async def test_watchdog_skips_comment_when_issue_number_is_none(self, registry):
        """When issue_number is None, the watchdog should NOT attempt to post a comment."""
        config, github, router = _make_manager_deps(registry)
        manager = _make_manager(config, registry, github, router)

        # Create agent with no issue_number
        agent = _make_agent(agent_id="test-agent-99", issue_number=None, status=AgentStatus.ACTIVE)
        await registry.create_agent(agent)

        # Create a fake agent task that is already done
        done_future = asyncio.get_event_loop().create_future()
        done_future.set_result(None)
        manager._agent_tasks["test-agent-99"] = done_future

        # Run watchdog with a very short timeout so it fires immediately
        await manager._duration_watchdog("test-agent-99", max_seconds=0)

        # comment_on_issue should NOT have been called because issue_number is None
        github.comment_on_issue.assert_not_awaited()

    async def test_watchdog_posts_comment_when_issue_number_is_set(self, registry):
        """When issue_number is set, the watchdog SHOULD post a comment."""
        config, github, router = _make_manager_deps(registry)
        manager = _make_manager(config, registry, github, router)

        # Create agent with a valid issue_number
        agent = _make_agent(agent_id="test-agent-100", issue_number=42, status=AgentStatus.ACTIVE)
        await registry.create_agent(agent)

        # Create a fake agent task that is already done
        done_future = asyncio.get_event_loop().create_future()
        done_future.set_result(None)
        manager._agent_tasks["test-agent-100"] = done_future

        # Run watchdog with 0 timeout so it fires immediately
        await manager._duration_watchdog("test-agent-100", max_seconds=0)

        # comment_on_issue SHOULD have been called
        github.comment_on_issue.assert_awaited_once()
        call_args = github.comment_on_issue.call_args
        assert call_args[0][0] == "testowner"
        assert call_args[0][1] == "testrepo"
        assert call_args[0][2] == 42


# ── Type narrowing: proxy response always bound in finally block ─────────────


class TestProxyResponseInitialized:
    """ToolProxy._handle_connection must always have `response` bound in the finally block.

    Previously, if the try block returned early (e.g., on empty line), the
    `response` variable was unbound when the finally block tried to serialize it.
    """

    async def test_response_sent_on_empty_line(self):
        """When client sends empty bytes, proxy should still write a valid JSON response."""
        from squadron.sandbox.proxy import ToolProxy

        # Create a ToolProxy instance with minimal setup
        proxy = ToolProxy.__new__(ToolProxy)
        proxy._agent_id = "test-agent"
        proxy._allowed_tools = {"bash"}
        proxy._callbacks = {}
        proxy._server = None

        # Mock reader that returns empty bytes (simulating disconnection)
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.readline = AsyncMock(return_value=b"")

        # Mock writer
        writer = AsyncMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await proxy._handle_connection(reader, writer)

        # Writer should have been called with a valid JSON response
        writer.write.assert_called_once()
        written = writer.write.call_args[0][0]
        response = json.loads(written.rstrip(b"\n"))
        assert response["ok"] is False

    async def test_response_sent_on_malformed_json(self):
        """When client sends invalid JSON, proxy should write an error response."""
        from squadron.sandbox.proxy import ToolProxy

        proxy = ToolProxy.__new__(ToolProxy)
        proxy._agent_id = "test-agent"
        proxy._allowed_tools = {"bash"}
        proxy._callbacks = {}
        proxy._server = None

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.readline = AsyncMock(return_value=b"not valid json\n")

        writer = AsyncMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await proxy._handle_connection(reader, writer)

        writer.write.assert_called_once()
        written = writer.write.call_args[0][0]
        response = json.loads(written.rstrip(b"\n"))
        assert response["ok"] is False
        assert "malformed" in response["error"]
