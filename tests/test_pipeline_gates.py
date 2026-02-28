"""Tests for pipeline gate checks, results, context, and registry."""

from __future__ import annotations

from typing import Any

import pytest

from squadron.pipeline.gates import (
    BranchUpToDateCheck,
    CiStatusCheck,
    CommandCheck,
    FileExistsCheck,
    GateCheck,
    GateCheckRegistry,
    GateCheckResult,
    HumanApprovedCheck,
    LabelPresentCheck,
    NoChangesRequestedCheck,
    PipelineContext,
    PrApprovalsMetCheck,
)
from squadron.pipeline.models import GateConditionConfig


# ── Mock helpers ─────────────────────────────────────────────────────────────


class MockGitHubClient:
    """Fake GitHub client that returns canned responses for specific method calls."""

    def __init__(
        self,
        *,
        pr: dict[str, Any] | None = None,
        reviews: list[dict[str, Any]] | None = None,
        check_runs: list[dict[str, Any]] | None = None,
    ):
        self._pr = pr or {}
        self._reviews = reviews if reviews is not None else []
        self._check_runs = check_runs if check_runs is not None else []

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        return self._pr

    async def get_pr_reviews(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        return self._reviews

    async def list_check_runs(self, owner: str, repo: str, ref: str) -> list[dict]:
        return self._check_runs


def make_context(
    *,
    pr_number: int | None = 42,
    owner: str = "acme",
    repo: str = "widgets",
    github_client: Any = None,
    context: dict[str, Any] | None = None,
) -> PipelineContext:
    return PipelineContext(
        pr_number=pr_number,
        owner=owner,
        repo=repo,
        github_client=github_client,
        context=context or {},
    )


async def make_command_runner(exit_code: int = 0, stdout: str = "", stderr: str = ""):
    async def runner(command: str, *, cwd: str | None = None, timeout: int = 300):
        return (exit_code, stdout, stderr)

    return runner


async def make_failing_command_runner(exc: Exception):
    async def runner(command: str, *, cwd: str | None = None, timeout: int = 300):
        raise exc

    return runner


def _review(user: str, state: str) -> dict[str, Any]:
    """Shorthand to build a review dict."""
    return {"user": {"login": user}, "state": state}


# ── GateCheckResult & PipelineContext ────────────────────────────────────────


class TestGateCheckResult:
    def test_defaults(self):
        r = GateCheckResult(passed=True, message="ok")
        assert r.passed is True
        assert r.message == "ok"
        assert r.data == {}

    def test_custom_data(self):
        r = GateCheckResult(passed=False, message="nope", data={"key": 1})
        assert r.data == {"key": 1}


class TestPipelineContext:
    def test_defaults(self):
        ctx = PipelineContext()
        assert ctx.pr_number is None
        assert ctx.issue_number is None
        assert ctx.owner == ""
        assert ctx.repo == ""
        assert ctx.github_client is None
        assert ctx.context == {}

    def test_fields(self):
        client = MockGitHubClient()
        ctx = PipelineContext(
            pr_number=7,
            issue_number=3,
            owner="org",
            repo="project",
            github_client=client,
        )
        assert ctx.pr_number == 7
        assert ctx.issue_number == 3
        assert ctx.github_client is client


# ── Built-in Gate Checks ────────────────────────────────────────────────────


class TestCommandCheck:
    @pytest.mark.asyncio
    async def test_pass_on_exit_zero(self):
        runner = await make_command_runner(exit_code=0, stdout="done")
        check = CommandCheck(command_runner=runner)
        config = GateConditionConfig(check="command", run="echo hello")
        result = await check.evaluate(config.get_config(), make_context())
        assert result.passed is True
        assert result.data["exit_code"] == 0
        assert result.data["stdout"] == "done"

    @pytest.mark.asyncio
    async def test_fail_on_nonzero_exit(self):
        runner = await make_command_runner(exit_code=1, stderr="error")
        check = CommandCheck(command_runner=runner)
        config = GateConditionConfig(check="command", run="false")
        result = await check.evaluate(config.get_config(), make_context())
        assert result.passed is False
        assert result.data["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_expect_failure_inverts(self):
        runner = await make_command_runner(exit_code=1)
        check = CommandCheck(command_runner=runner)
        config = GateConditionConfig(check="command", run="false", expect="failure")
        result = await check.evaluate(config.get_config(), make_context())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_expect_failure_exit_zero_fails(self):
        runner = await make_command_runner(exit_code=0)
        check = CommandCheck(command_runner=runner)
        config = GateConditionConfig(check="command", run="true", expect="failure")
        result = await check.evaluate(config.get_config(), make_context())
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_no_command_fails(self):
        runner = await make_command_runner()
        check = CommandCheck(command_runner=runner)
        result = await check.evaluate({}, make_context())
        assert result.passed is False
        assert "No command" in result.message

    @pytest.mark.asyncio
    async def test_no_runner_fails(self):
        check = CommandCheck(command_runner=None)
        config = GateConditionConfig(check="command", run="echo hi")
        result = await check.evaluate(config.get_config(), make_context())
        assert result.passed is False
        assert "No command runner" in result.message

    @pytest.mark.asyncio
    async def test_runner_exception(self):
        runner = await make_failing_command_runner(RuntimeError("boom"))
        check = CommandCheck(command_runner=runner)
        config = GateConditionConfig(check="command", run="kaboom")
        result = await check.evaluate(config.get_config(), make_context())
        assert result.passed is False
        assert "boom" in result.message
        assert result.data["error"] == "boom"

    @pytest.mark.asyncio
    async def test_stdout_truncated_at_1000(self):
        long = "x" * 2000
        runner = await make_command_runner(exit_code=0, stdout=long)
        check = CommandCheck(command_runner=runner)
        config = GateConditionConfig(check="command", run="echo")
        result = await check.evaluate(config.get_config(), make_context())
        assert len(result.data["stdout"]) == 1000

    def test_reactive_events_empty(self):
        assert CommandCheck.reactive_events == set()


class TestFileExistsCheck:
    @pytest.mark.asyncio
    async def test_all_files_exist(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        check = FileExistsCheck()
        config = GateConditionConfig(
            check="file_exists",
            paths=[str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],
        )
        result = await check.evaluate(config.get_config(), make_context())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        check = FileExistsCheck()
        config = GateConditionConfig(
            check="file_exists",
            paths=[str(tmp_path / "a.txt"), str(tmp_path / "missing.txt")],
        )
        result = await check.evaluate(config.get_config(), make_context())
        assert result.passed is False
        assert "missing.txt" in result.message

    @pytest.mark.asyncio
    async def test_no_paths_fails(self):
        check = FileExistsCheck()
        result = await check.evaluate({}, make_context())
        assert result.passed is False
        assert "No paths" in result.message

    def test_reactive_events_empty(self):
        assert FileExistsCheck.reactive_events == set()


class TestPrApprovalsMetCheck:
    @pytest.mark.asyncio
    async def test_enough_approvals(self):
        client = MockGitHubClient(
            reviews=[
                _review("alice", "APPROVED"),
                _review("bob", "APPROVED"),
            ]
        )
        check = PrApprovalsMetCheck()
        config = GateConditionConfig(check="pr_approvals_met", count=2)
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is True
        assert result.data["approvals"] == 2

    @pytest.mark.asyncio
    async def test_not_enough_approvals(self):
        client = MockGitHubClient(reviews=[_review("alice", "APPROVED")])
        check = PrApprovalsMetCheck()
        config = GateConditionConfig(check="pr_approvals_met", count=2)
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is False
        assert result.data["approvals"] == 1

    @pytest.mark.asyncio
    async def test_no_pr_number_fails(self):
        check = PrApprovalsMetCheck()
        result = await check.evaluate({}, make_context(pr_number=None))
        assert result.passed is False
        assert "No PR number" in result.message

    @pytest.mark.asyncio
    async def test_no_github_client_fails(self):
        check = PrApprovalsMetCheck()
        result = await check.evaluate({}, make_context(github_client=None))
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_latest_review_wins(self):
        client = MockGitHubClient(
            reviews=[
                _review("alice", "CHANGES_REQUESTED"),
                _review("alice", "APPROVED"),
            ]
        )
        check = PrApprovalsMetCheck()
        config = GateConditionConfig(check="pr_approvals_met", count=1)
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_scope_humans_excludes_bots(self):
        client = MockGitHubClient(
            reviews=[
                _review("squadron-dev[bot]", "APPROVED"),
                _review("human-alice", "APPROVED"),
            ]
        )
        check = PrApprovalsMetCheck()
        config = GateConditionConfig(check="pr_approvals_met", count=2, scope="humans")
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is False
        assert result.data["approvals"] == 1

    @pytest.mark.asyncio
    async def test_scope_agents_includes_only_bots(self):
        client = MockGitHubClient(
            reviews=[
                _review("squadron-dev[bot]", "APPROVED"),
                _review("human-alice", "APPROVED"),
            ]
        )
        check = PrApprovalsMetCheck()
        config = GateConditionConfig(check="pr_approvals_met", count=1, scope="agents")
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is True
        assert result.data["approvals"] == 1

    def test_reactive_events(self):
        assert PrApprovalsMetCheck.reactive_events == {
            "pull_request_review.submitted",
            "pull_request_review.dismissed",
        }


class TestCiStatusCheck:
    @pytest.mark.asyncio
    async def test_all_checks_pass(self):
        client = MockGitHubClient(
            pr={"head": {"sha": "abc123"}},
            check_runs=[
                {"name": "lint", "status": "completed", "conclusion": "success"},
                {"name": "test", "status": "completed", "conclusion": "success"},
            ],
        )
        check = CiStatusCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is True
        assert result.data["check_count"] == 2

    @pytest.mark.asyncio
    async def test_some_checks_failing(self):
        client = MockGitHubClient(
            pr={"head": {"sha": "abc123"}},
            check_runs=[
                {"name": "lint", "status": "completed", "conclusion": "success"},
                {"name": "test", "status": "completed", "conclusion": "failure"},
            ],
        )
        check = CiStatusCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is False
        assert "test: failure" in result.message

    @pytest.mark.asyncio
    async def test_check_still_in_progress(self):
        client = MockGitHubClient(
            pr={"head": {"sha": "abc123"}},
            check_runs=[
                {"name": "lint", "status": "in_progress", "conclusion": None},
            ],
        )
        check = CiStatusCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is False
        assert "lint: in_progress" in result.message

    @pytest.mark.asyncio
    async def test_filter_by_workflows(self):
        client = MockGitHubClient(
            pr={"head": {"sha": "abc123"}},
            check_runs=[
                {"name": "lint", "status": "completed", "conclusion": "success"},
                {"name": "test", "status": "completed", "conclusion": "failure"},
                {"name": "deploy", "status": "completed", "conclusion": "success"},
            ],
        )
        check = CiStatusCheck()
        config = GateConditionConfig(check="ci_status", workflows=["lint", "deploy"])
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        # Only lint + deploy checked; both pass. test failure is ignored.
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_filter_by_workflows_missing_check(self):
        client = MockGitHubClient(
            pr={"head": {"sha": "abc123"}},
            check_runs=[
                {"name": "lint", "status": "completed", "conclusion": "success"},
            ],
        )
        check = CiStatusCheck()
        config = GateConditionConfig(check="ci_status", workflows=["lint", "e2e"])
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is False
        assert "Missing CI checks" in result.message
        assert "e2e" in result.message

    @pytest.mark.asyncio
    async def test_no_pr_number_fails(self):
        check = CiStatusCheck()
        result = await check.evaluate({}, make_context(pr_number=None))
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_no_head_sha_fails(self):
        client = MockGitHubClient(pr={"head": {}})
        check = CiStatusCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is False
        assert "head SHA" in result.message

    def test_reactive_events(self):
        assert CiStatusCheck.reactive_events == {
            "check_suite.completed",
            "check_run.completed",
            "status",
        }


class TestLabelPresentCheck:
    @pytest.mark.asyncio
    async def test_label_present(self):
        client = MockGitHubClient(pr={"labels": [{"name": "ready-to-merge"}, {"name": "bug"}]})
        check = LabelPresentCheck()
        config = GateConditionConfig(check="label_present", label="ready-to-merge")
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_label_absent(self):
        client = MockGitHubClient(pr={"labels": [{"name": "bug"}]})
        check = LabelPresentCheck()
        config = GateConditionConfig(check="label_present", label="ready-to-merge")
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is False
        assert "not found" in result.message

    @pytest.mark.asyncio
    async def test_no_label_config_fails(self):
        client = MockGitHubClient(pr={"labels": []})
        check = LabelPresentCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is False
        assert "No label specified" in result.message

    @pytest.mark.asyncio
    async def test_no_pr_number_fails(self):
        check = LabelPresentCheck()
        result = await check.evaluate({"label": "x"}, make_context(pr_number=None))
        assert result.passed is False

    def test_reactive_events(self):
        assert LabelPresentCheck.reactive_events == {
            "pull_request.labeled",
            "pull_request.unlabeled",
        }


class TestNoChangesRequestedCheck:
    @pytest.mark.asyncio
    async def test_no_changes_requested(self):
        client = MockGitHubClient(
            reviews=[
                _review("alice", "APPROVED"),
                _review("bob", "COMMENTED"),
            ]
        )
        check = NoChangesRequestedCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_active_changes_requested(self):
        client = MockGitHubClient(reviews=[_review("alice", "CHANGES_REQUESTED")])
        check = NoChangesRequestedCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is False
        assert "alice" in result.message

    @pytest.mark.asyncio
    async def test_changes_requested_then_approved(self):
        client = MockGitHubClient(
            reviews=[
                _review("alice", "CHANGES_REQUESTED"),
                _review("alice", "APPROVED"),
            ]
        )
        check = NoChangesRequestedCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_no_pr_fails(self):
        check = NoChangesRequestedCheck()
        result = await check.evaluate({}, make_context(pr_number=None))
        assert result.passed is False

    def test_reactive_events(self):
        assert NoChangesRequestedCheck.reactive_events == {
            "pull_request_review.submitted",
            "pull_request_review.dismissed",
        }


class TestHumanApprovedCheck:
    @pytest.mark.asyncio
    async def test_enough_human_approvals(self):
        client = MockGitHubClient(
            reviews=[
                _review("alice", "APPROVED"),
                _review("bob", "APPROVED"),
            ]
        )
        check = HumanApprovedCheck()
        config = GateConditionConfig(check="human_approved", count=2)
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is True
        assert result.data["human_approvals"] == 2

    @pytest.mark.asyncio
    async def test_not_enough_human_approvals(self):
        client = MockGitHubClient(reviews=[_review("alice", "APPROVED")])
        check = HumanApprovedCheck()
        config = GateConditionConfig(check="human_approved", count=2)
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is False
        assert result.data["human_approvals"] == 1

    @pytest.mark.asyncio
    async def test_bots_excluded(self):
        client = MockGitHubClient(
            reviews=[
                _review("ci-bot[bot]", "APPROVED"),
                _review("alice", "APPROVED"),
            ]
        )
        check = HumanApprovedCheck()
        config = GateConditionConfig(check="human_approved", count=2)
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is False
        assert result.data["human_approvals"] == 1

    @pytest.mark.asyncio
    async def test_default_count_is_one(self):
        client = MockGitHubClient(reviews=[_review("alice", "APPROVED")])
        check = HumanApprovedCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_no_pr_fails(self):
        check = HumanApprovedCheck()
        result = await check.evaluate({}, make_context(pr_number=None))
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_latest_review_state_wins(self):
        client = MockGitHubClient(
            reviews=[
                _review("alice", "APPROVED"),
                _review("alice", "CHANGES_REQUESTED"),
            ]
        )
        check = HumanApprovedCheck()
        config = GateConditionConfig(check="human_approved", count=1)
        result = await check.evaluate(config.get_config(), make_context(github_client=client))
        assert result.passed is False
        assert result.data["human_approvals"] == 0

    def test_reactive_events(self):
        assert HumanApprovedCheck.reactive_events == {
            "pull_request_review.submitted",
        }


class TestBranchUpToDateCheck:
    @pytest.mark.asyncio
    async def test_up_to_date(self):
        client = MockGitHubClient(pr={"mergeable_state": "clean", "mergeable": True})
        check = BranchUpToDateCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is True
        assert result.data["mergeable_state"] == "clean"

    @pytest.mark.asyncio
    async def test_behind_fails(self):
        client = MockGitHubClient(pr={"mergeable_state": "behind", "mergeable": True})
        check = BranchUpToDateCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is False
        assert "behind" in result.message.lower()

    @pytest.mark.asyncio
    async def test_not_mergeable_fails(self):
        client = MockGitHubClient(pr={"mergeable_state": "dirty", "mergeable": False})
        check = BranchUpToDateCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        assert result.passed is False
        assert "not mergeable" in result.message.lower()

    @pytest.mark.asyncio
    async def test_no_pr_fails(self):
        check = BranchUpToDateCheck()
        result = await check.evaluate({}, make_context(pr_number=None))
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_unknown_state_passes(self):
        client = MockGitHubClient(pr={"mergeable_state": "unknown", "mergeable": None})
        check = BranchUpToDateCheck()
        result = await check.evaluate({}, make_context(github_client=client))
        # unknown state with mergeable=None passes (not behind, not False)
        assert result.passed is True

    def test_reactive_events(self):
        assert BranchUpToDateCheck.reactive_events == {
            "push",
            "pull_request.synchronize",
        }


# ── GateCheckRegistry ───────────────────────────────────────────────────────


class TestGateCheckRegistry:
    def test_builtins_registered(self):
        registry = GateCheckRegistry()
        expected = sorted(
            [
                "branch_up_to_date",
                "ci_status",
                "command",
                "file_exists",
                "human_approved",
                "label_present",
                "no_changes_requested",
                "pr_approvals_met",
            ]
        )
        assert registry.check_names == expected

    def test_has_returns_true_for_builtins(self):
        registry = GateCheckRegistry()
        assert registry.has("command") is True
        assert registry.has("ci_status") is True

    def test_has_returns_false_for_unknown(self):
        registry = GateCheckRegistry()
        assert registry.has("nonexistent") is False

    def test_get_returns_instance(self):
        registry = GateCheckRegistry()
        check = registry.get("command")
        assert isinstance(check, CommandCheck)

    def test_get_missing_raises_key_error(self):
        registry = GateCheckRegistry()
        with pytest.raises(KeyError, match="nonexistent"):
            registry.get("nonexistent")

    def test_duplicate_register_raises(self):
        registry = GateCheckRegistry()
        with pytest.raises(ValueError, match="already registered"):
            registry.register("command", CommandCheck())

    def test_register_custom_check(self):
        registry = GateCheckRegistry()

        class CustomCheck(GateCheck):
            reactive_events: set[str] = {"custom.event"}

            async def evaluate(
                self, config: dict[str, Any], context: PipelineContext
            ) -> GateCheckResult:
                return GateCheckResult(passed=True, message="custom")

        registry.register("custom", CustomCheck())
        assert registry.has("custom") is True
        assert isinstance(registry.get("custom"), CustomCheck)

    def test_get_reactive_events(self):
        registry = GateCheckRegistry()
        mapping = registry.get_reactive_events()

        # Spot-check known mappings
        assert "pr_approvals_met" in mapping["pull_request_review.submitted"]
        assert "no_changes_requested" in mapping["pull_request_review.submitted"]
        assert "human_approved" in mapping["pull_request_review.submitted"]
        assert "ci_status" in mapping["check_run.completed"]
        assert "ci_status" in mapping["status"]
        assert "label_present" in mapping["pull_request.labeled"]
        assert "branch_up_to_date" in mapping["push"]

    def test_get_reactive_events_excludes_non_reactive(self):
        registry = GateCheckRegistry()
        mapping = registry.get_reactive_events()
        # command and file_exists have empty reactive_events
        all_check_names = set()
        for names in mapping.values():
            all_check_names.update(names)
        assert "command" not in all_check_names
        assert "file_exists" not in all_check_names

    def test_command_runner_injected(self):
        async def my_runner(command, *, cwd=None, timeout=300):
            return (0, "", "")

        registry = GateCheckRegistry(command_runner=my_runner)
        check = registry.get("command")
        assert isinstance(check, CommandCheck)
        assert check._command_runner is my_runner

    def test_check_names_sorted(self):
        registry = GateCheckRegistry()
        names = registry.check_names
        assert names == sorted(names)

    def test_load_custom_gates_success(self):
        """load_custom_gates imports a module and registers checks."""
        # We use the gates module itself as the source for a custom check class.
        registry = GateCheckRegistry()
        # Re-registering a built-in would fail; use a unique name.
        # Create a tiny module on-the-fly via sys.modules.
        import sys
        import types

        mod = types.ModuleType("_test_custom_gate_mod")

        class MyGate(GateCheck):
            reactive_events: set[str] = set()

            async def evaluate(
                self, config: dict[str, Any], context: PipelineContext
            ) -> GateCheckResult:
                return GateCheckResult(passed=True, message="custom")

        mod.MyGate = MyGate
        sys.modules["_test_custom_gate_mod"] = mod

        try:
            registry.load_custom_gates(
                [
                    {
                        "module": "_test_custom_gate_mod",
                        "checks": [{"name": "my_custom", "class": "MyGate"}],
                    }
                ]
            )
            assert registry.has("my_custom") is True
            assert isinstance(registry.get("my_custom"), MyGate)
        finally:
            del sys.modules["_test_custom_gate_mod"]

    def test_load_custom_gates_missing_module(self, caplog):
        registry = GateCheckRegistry()
        registry.load_custom_gates(
            [
                {
                    "module": "nonexistent_module_xyz",
                    "checks": [{"name": "x", "class": "X"}],
                }
            ]
        )
        assert not registry.has("x")

    def test_load_custom_gates_missing_class(self, caplog):
        import sys
        import types

        mod = types.ModuleType("_test_missing_class_mod")
        sys.modules["_test_missing_class_mod"] = mod
        try:
            registry = GateCheckRegistry()
            registry.load_custom_gates(
                [
                    {
                        "module": "_test_missing_class_mod",
                        "checks": [{"name": "bad", "class": "DoesNotExist"}],
                    }
                ]
            )
            assert not registry.has("bad")
        finally:
            del sys.modules["_test_missing_class_mod"]

    def test_load_custom_gates_not_a_gate_check(self):
        import sys
        import types

        mod = types.ModuleType("_test_not_gate_mod")
        mod.NotAGate = str  # type: ignore[attr-defined]
        sys.modules["_test_not_gate_mod"] = mod
        try:
            registry = GateCheckRegistry()
            registry.load_custom_gates(
                [
                    {
                        "module": "_test_not_gate_mod",
                        "checks": [{"name": "nope", "class": "NotAGate"}],
                    }
                ]
            )
            assert not registry.has("nope")
        finally:
            del sys.modules["_test_not_gate_mod"]


# ── GateConditionConfig.get_config ──────────────────────────────────────────


class TestGateConditionConfig:
    def test_get_config_includes_non_none(self):
        cfg = GateConditionConfig(check="ci_status", workflows=["lint"])
        d = cfg.get_config()
        assert d == {"workflows": ["lint"]}

    def test_get_config_excludes_check_key(self):
        cfg = GateConditionConfig(check="command", run="echo hi")
        d = cfg.get_config()
        assert "check" not in d
        assert d == {"run": "echo hi"}

    def test_get_config_multiple_fields(self):
        cfg = GateConditionConfig(check="pr_approvals_met", scope="humans", count=2)
        d = cfg.get_config()
        assert d == {"scope": "humans", "count": 2}

    def test_get_config_empty_when_all_none(self):
        cfg = GateConditionConfig(check="no_changes_requested")
        d = cfg.get_config()
        assert d == {}
