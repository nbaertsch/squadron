"""Tests for the pluggable gate check registry.

Covers:
- GateCheckRegistry registration and lookup
- Built-in gate check implementations
- Plugin loading
- Error handling (unknown check type, exceptions in check functions)
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from squadron.pipeline.gates import (
    GateCheckContext,
    GateCheckRegistry,
    default_gate_registry,
)
from squadron.config import GateCheckResult


# ── Helpers ────────────────────────────────────────────────────────────────────


def make_ctx(**kwargs) -> GateCheckContext:
    return GateCheckContext(**kwargs)


# ── Registry Tests ─────────────────────────────────────────────────────────────


class TestGateCheckRegistry:
    def test_list_builtin_checks(self):
        reg = GateCheckRegistry()
        checks = reg.list_checks()
        assert "command" in checks
        assert "file_exists" in checks
        assert "pr_approvals_met" in checks
        assert "no_changes_requested" in checks
        assert "human_approved" in checks
        assert "label_present" in checks
        assert "ci_status" in checks
        assert "branch_up_to_date" in checks

    def test_register_decorator(self):
        reg = GateCheckRegistry()

        @reg.register("my_custom_check")
        async def my_check(ctx: GateCheckContext) -> GateCheckResult:
            return GateCheckResult(check_type="my_custom_check", passed=True)

        assert "my_custom_check" in reg.list_checks()

    def test_register_fn(self):
        reg = GateCheckRegistry()

        async def check_fn(ctx: GateCheckContext) -> GateCheckResult:
            return GateCheckResult(check_type="test", passed=True)

        reg.register_fn("test_fn", check_fn)
        assert "test_fn" in reg.list_checks()
        assert reg.get("test_fn") is check_fn

    async def test_evaluate_unknown_check(self):
        reg = GateCheckRegistry()
        ctx = make_ctx()
        result = await reg.evaluate("nonexistent_check", ctx)
        assert result.passed is False
        assert "nonexistent_check" in result.error_message
        assert "Unknown gate check" in result.error_message

    async def test_evaluate_check_exception(self):
        """An exception in a gate check function is caught and returned as failure."""
        reg = GateCheckRegistry()

        async def broken_check(ctx: GateCheckContext) -> GateCheckResult:
            raise RuntimeError("intentional error")

        reg.register_fn("broken", broken_check)
        result = await reg.evaluate("broken", make_ctx())
        assert result.passed is False
        assert "intentional error" in result.error_message

    async def test_evaluate_sync_check(self):
        """Sync (non-async) gate check functions work correctly."""
        reg = GateCheckRegistry()

        def sync_check(ctx: GateCheckContext) -> GateCheckResult:
            return GateCheckResult(check_type="sync", passed=True)

        reg.register_fn("sync_check", sync_check)
        result = await reg.evaluate("sync_check", make_ctx())
        assert result.passed is True

    def test_default_registry_is_populated(self):
        checks = default_gate_registry.list_checks()
        assert len(checks) >= 8  # at least the 8 built-ins


# ── command check ──────────────────────────────────────────────────────────────


class TestCommandCheck:
    async def test_missing_run_param(self):
        reg = GateCheckRegistry()
        result = await reg.evaluate("command", make_ctx(params={}))
        assert result.passed is False
        assert "run" in result.error_message

    async def test_no_runner(self):
        result = await default_gate_registry.evaluate(
            "command", make_ctx(params={"run": "echo hi"}, run_command=None)
        )
        assert result.passed is False
        assert "command runner" in result.error_message.lower()

    async def test_success_exit_0(self):
        async def runner(cmd, **kw):
            return 0, "hello", ""

        result = await default_gate_registry.evaluate(
            "command",
            make_ctx(params={"run": "echo hello"}, run_command=runner),
        )
        assert result.passed is True
        assert result.result_data["exit_code"] == 0

    async def test_failure_nonzero_exit(self):
        async def runner(cmd, **kw):
            return 1, "", "error"

        result = await default_gate_registry.evaluate(
            "command",
            make_ctx(params={"run": "false"}, run_command=runner),
        )
        assert result.passed is False

    async def test_expect_exit_code_1(self):
        async def runner(cmd, **kw):
            return 1, "", ""

        result = await default_gate_registry.evaluate(
            "command",
            make_ctx(
                params={"run": "false", "expect": "exit_code == 1"},
                run_command=runner,
            ),
        )
        assert result.passed is True

    async def test_expect_stdout_contains(self):
        async def runner(cmd, **kw):
            return 0, "PASS: all tests passed", ""

        result = await default_gate_registry.evaluate(
            "command",
            make_ctx(
                params={
                    "run": "pytest",
                    "expect": "stdout_contains: PASS",
                },
                run_command=runner,
            ),
        )
        assert result.passed is True

    async def test_runner_exception_caught(self):
        async def failing_runner(cmd, **kw):
            raise OSError("no such file")

        result = await default_gate_registry.evaluate(
            "command",
            make_ctx(params={"run": "missing-cmd"}, run_command=failing_runner),
        )
        assert result.passed is False
        assert "no such file" in result.error_message


# ── file_exists check ─────────────────────────────────────────────────────────


class TestFileExistsCheck:
    async def test_missing_paths_param(self):
        result = await default_gate_registry.evaluate(
            "file_exists", make_ctx(params={})
        )
        assert result.passed is False
        assert "paths" in result.error_message

    async def test_existing_file(self, tmp_path):
        f = tmp_path / "foo.txt"
        f.write_text("hello")

        result = await default_gate_registry.evaluate(
            "file_exists",
            make_ctx(params={"paths": [str(f)]}),
        )
        assert result.passed is True
        assert result.result_data["missing"] == []

    async def test_missing_file(self, tmp_path):
        missing_path = str(tmp_path / "does_not_exist.txt")
        result = await default_gate_registry.evaluate(
            "file_exists",
            make_ctx(params={"paths": [missing_path]}),
        )
        assert result.passed is False
        assert missing_path in result.result_data["missing"]

    async def test_partial_missing(self, tmp_path):
        existing = tmp_path / "exists.txt"
        existing.write_text("x")
        missing = str(tmp_path / "missing.txt")

        result = await default_gate_registry.evaluate(
            "file_exists",
            make_ctx(params={"paths": [str(existing), missing]}),
        )
        assert result.passed is False
        assert missing in result.result_data["missing"]


# ── pr_approvals_met check ────────────────────────────────────────────────────


class TestPrApprovalsMet:
    async def test_no_pr_number(self):
        result = await default_gate_registry.evaluate(
            "pr_approvals_met", make_ctx(params={})
        )
        assert result.passed is False
        assert "PR number" in result.error_message

    async def test_no_registry(self):
        result = await default_gate_registry.evaluate(
            "pr_approvals_met",
            make_ctx(params={}, pr_number=1, registry=None),
        )
        assert result.passed is False
        assert "registry" in result.error_message.lower()

    async def test_sufficient_approvals(self):
        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [
                    {"agent_role": "pr-review", "agent_id": "agent-1"},
                    {"agent_role": "human", "agent_id": "alice"},
                ]

        result = await default_gate_registry.evaluate(
            "pr_approvals_met",
            make_ctx(params={"count": 2}, pr_number=10, registry=MockRegistry()),
        )
        assert result.passed is True
        assert result.result_data["actual"] == 2

    async def test_insufficient_approvals(self):
        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [{"agent_role": "pr-review", "agent_id": "agent-1"}]

        result = await default_gate_registry.evaluate(
            "pr_approvals_met",
            make_ctx(params={"count": 2}, pr_number=10, registry=MockRegistry()),
        )
        assert result.passed is False
        assert "1/2" in result.error_message

    async def test_exclude_humans(self):
        """When include_humans=False, human approvals are not counted."""

        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [
                    {"agent_role": "human", "agent_id": "alice"},
                ]

        result = await default_gate_registry.evaluate(
            "pr_approvals_met",
            make_ctx(
                params={"count": 1, "include_humans": False},
                pr_number=10,
                registry=MockRegistry(),
            ),
        )
        # Human approval excluded, so 0 matching approvals
        assert result.passed is False

    async def test_role_filter(self):
        """When roles is specified, only matching roles count."""

        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [
                    {"agent_role": "security-review", "agent_id": "sec-1"},
                    {"agent_role": "pr-review", "agent_id": "pr-1"},
                ]

        result = await default_gate_registry.evaluate(
            "pr_approvals_met",
            make_ctx(
                params={"count": 1, "roles": ["security-review"]},
                pr_number=10,
                registry=MockRegistry(),
            ),
        )
        assert result.passed is True
        assert result.result_data["actual"] == 1


# ── no_changes_requested check ────────────────────────────────────────────────


class TestNoChangesRequested:
    async def test_no_blocking_reviews(self):
        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return []

        result = await default_gate_registry.evaluate(
            "no_changes_requested",
            make_ctx(params={}, pr_number=1, registry=MockRegistry()),
        )
        assert result.passed is True

    async def test_with_blocking_review(self):
        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [{"agent_role": "pr-review", "agent_id": "reviewer"}]

        result = await default_gate_registry.evaluate(
            "no_changes_requested",
            make_ctx(params={}, pr_number=1, registry=MockRegistry()),
        )
        assert result.passed is False
        assert "1 reviewer" in result.error_message

    async def test_exclude_human_changes_requested(self):
        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [{"agent_role": "human", "agent_id": "alice"}]

        result = await default_gate_registry.evaluate(
            "no_changes_requested",
            make_ctx(
                params={"include_humans": False},
                pr_number=1,
                registry=MockRegistry(),
            ),
        )
        assert result.passed is True


# ── human_approved check ───────────────────────────────────────────────────────


class TestHumanApproved:
    async def test_no_pr_number(self):
        result = await default_gate_registry.evaluate(
            "human_approved", make_ctx(params={})
        )
        assert result.passed is False

    async def test_human_approved(self):
        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return [{"agent_role": "human", "agent_id": "alice"}]

        result = await default_gate_registry.evaluate(
            "human_approved",
            make_ctx(params={}, pr_number=1, registry=MockRegistry()),
        )
        assert result.passed is True
        assert result.result_data["human_approvals"] == 1

    async def test_no_human_approval(self):
        class MockRegistry:
            async def get_pr_approvals(self, pr_number, state=None, role=None):
                return []

        result = await default_gate_registry.evaluate(
            "human_approved",
            make_ctx(params={"count": 1}, pr_number=1, registry=MockRegistry()),
        )
        assert result.passed is False
        assert "0" in result.error_message
