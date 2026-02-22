"""Pipeline Gate Check Registry — pluggable gate evaluation for pipelines.

Provides a registry of named gate check functions with built-in checks and
support for user-extensible checks via Python modules.

Built-in checks:
    - ``command``          — run a shell command, pass on exit code / output
    - ``file_exists``      — require one or more files to be present
    - ``pr_approvals_met`` — check PR has the required number of approvals
    - ``no_changes_requested`` — no outstanding CHANGES_REQUESTED reviews
    - ``human_approved``   — at least one human (non-bot) has approved
    - ``label_present``    — require a label on the associated PR/issue
    - ``ci_status``        — check CI checks are passing on the PR
    - ``branch_up_to_date`` — PR branch is up-to-date with base branch

Each gate function receives a :class:`GateCheckContext` and returns a
:class:`GateCheckResult`.  Functions can be synchronous or async.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from squadron.config import GateCheckResult

logger = logging.getLogger(__name__)


# ── Gate Check Context ────────────────────────────────────────────────────────


@dataclass
class GateCheckContext:
    """Runtime context passed to each gate check function.

    Provides access to the PR/issue under evaluation, the registry for
    approval data, and the GitHub client for API calls.
    """

    # Check configuration (from YAML)
    params: dict[str, Any] = field(default_factory=dict)

    # PR/issue context
    pr_number: int | None = None
    issue_number: int | None = None
    owner: str = ""
    repo: str = ""
    base_branch: str = ""
    head_branch: str = ""

    # Workflow context propagated from the pipeline run
    run_context: dict[str, Any] = field(default_factory=dict)

    # Optional: live registry for approval lookups
    registry: Any = None  # AgentRegistry — avoid circular import

    # Optional: GitHub client for API calls
    github: Any = None  # GitHubClient — avoid circular import

    # Optional: command runner for ``command`` checks
    run_command: Callable[
        [str], Awaitable[tuple[int, str, str]]
    ] | None = None


# ── Gate Check Protocol ───────────────────────────────────────────────────────


@runtime_checkable
class GateCheckFunc(Protocol):
    """Protocol for a gate check function.

    Functions can be sync or async.  The registry wraps sync functions
    automatically so all checks can be called uniformly with ``await``.
    """

    async def __call__(self, ctx: GateCheckContext) -> GateCheckResult:
        """Evaluate the gate and return a result."""
        ...


# ── Gate Check Registry ───────────────────────────────────────────────────────


class GateCheckRegistry:
    """Registry mapping check names to gate check functions.

    Usage::

        registry = GateCheckRegistry()

        @registry.register("my_check")
        async def my_check(ctx: GateCheckContext) -> GateCheckResult:
            ...

    Built-in checks are pre-registered at construction time.
    """

    def __init__(self) -> None:
        self._checks: dict[str, Callable] = {}
        self._register_builtin_checks()

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self, name: str
    ) -> Callable[[Callable], Callable]:
        """Decorator to register a gate check function.

        Args:
            name: The check name used in YAML (e.g. ``pr_approvals_met``).

        Returns:
            Decorator that registers the function and returns it unchanged.
        """
        def decorator(fn: Callable) -> Callable:
            self._checks[name] = fn
            logger.debug("Registered gate check: %s", name)
            return fn
        return decorator

    def register_fn(self, name: str, fn: Callable) -> None:
        """Directly register a gate check function by name."""
        self._checks[name] = fn

    def load_plugin(self, module_path: str) -> int:
        """Load gate checks from a Python module path.

        The module must call ``registry.register(name)(fn)`` on the
        registry instance that is passed to it, or expose a
        ``register_checks(registry)`` function.

        Args:
            module_path: Dotted module path, e.g. ``myproject.pipeline_gates``.

        Returns:
            Number of checks registered from the module.
        """
        before = set(self._checks.keys())
        module = importlib.import_module(module_path)

        # Call register_checks(registry) if present
        if hasattr(module, "register_checks"):
            module.register_checks(self)

        added = len(self._checks) - len(before)
        logger.info("Loaded %d gate checks from plugin: %s", added, module_path)
        return added

    def get(self, name: str) -> Callable | None:
        """Look up a gate check by name."""
        return self._checks.get(name)

    def list_checks(self) -> list[str]:
        """Return all registered check names."""
        return sorted(self._checks.keys())

    # ── Evaluation ────────────────────────────────────────────────────────────

    async def evaluate(
        self, check_name: str, ctx: GateCheckContext
    ) -> GateCheckResult:
        """Evaluate a named gate check.

        Args:
            check_name: The registered check name.
            ctx: Runtime context for the check.

        Returns:
            ``GateCheckResult`` — passed/failed with metadata.
        """
        fn = self._checks.get(check_name)
        if fn is None:
            return GateCheckResult(
                check_type=check_name,
                passed=False,
                error_message=f"Unknown gate check: '{check_name}'. "
                f"Available: {self.list_checks()}",
            )

        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(ctx)
            else:
                result = fn(ctx)
            return result
        except Exception as exc:
            logger.exception("Gate check '%s' raised an exception", check_name)
            return GateCheckResult(
                check_type=check_name,
                passed=False,
                error_message=f"Gate check error: {exc}",
            )

    # ── Built-in Checks ───────────────────────────────────────────────────────

    def _register_builtin_checks(self) -> None:
        """Register all built-in gate check functions."""
        self.register_fn("command", _check_command)
        self.register_fn("file_exists", _check_file_exists)
        self.register_fn("pr_approvals_met", _check_pr_approvals_met)
        self.register_fn("no_changes_requested", _check_no_changes_requested)
        self.register_fn("human_approved", _check_human_approved)
        self.register_fn("label_present", _check_label_present)
        self.register_fn("ci_status", _check_ci_status)
        self.register_fn("branch_up_to_date", _check_branch_up_to_date)


# ── Built-in Gate Check Implementations ──────────────────────────────────────


async def _check_command(ctx: GateCheckContext) -> GateCheckResult:
    """Run a shell command; pass if exit code matches the expected value.

    Params:
        run: Command string to execute.
        expect: Optional condition string, e.g. ``"exit_code == 0"``.
                Defaults to requiring exit code 0.
        cwd: Optional working directory.
        timeout: Timeout in seconds (default: 300).
    """
    cmd = ctx.params.get("run")
    if not cmd:
        return GateCheckResult(
            check_type="command",
            passed=False,
            error_message="Missing required param 'run'",
        )

    if not ctx.run_command:
        return GateCheckResult(
            check_type="command",
            passed=False,
            error_message="No command runner available in gate context",
        )

    try:
        exit_code, stdout, stderr = await ctx.run_command(cmd)
        passed = _eval_command_expect(exit_code, stdout, ctx.params.get("expect"))
        return GateCheckResult(
            check_type="command",
            passed=passed,
            result_data={
                "exit_code": exit_code,
                "stdout_lines": len(stdout.splitlines()),
                "stderr_lines": len(stderr.splitlines()),
            },
            error_message=None if passed else f"Command failed with exit code {exit_code}",
        )
    except Exception as exc:
        return GateCheckResult(
            check_type="command",
            passed=False,
            error_message=str(exc),
        )


def _eval_command_expect(exit_code: int, stdout: str, expect: str | None) -> bool:
    """Evaluate a command result against an expect expression."""
    if not expect:
        return exit_code == 0

    expr = expect.strip()

    # Exit code comparisons: "exit_code == 0", "exit_code != 1"
    if "exit_code" in expr:
        for op, fn in [("==", lambda a, b: a == b), ("!=", lambda a, b: a != b),
                       ("<=", lambda a, b: a <= b), (">=", lambda a, b: a >= b),
                       ("<", lambda a, b: a < b), (">", lambda a, b: a > b)]:
            if op in expr:
                try:
                    expected = int(expr.split(op)[1].strip())
                    return fn(exit_code, expected)
                except (ValueError, IndexError):
                    pass

    # Stdout contains check: "stdout_contains: <text>"
    if expr.startswith("stdout_contains:"):
        needle = expr.split(":", 1)[1].strip()
        return needle in stdout

    return exit_code == 0


async def _check_file_exists(ctx: GateCheckContext) -> GateCheckResult:
    """Check that all specified file paths exist.

    Params:
        paths: List of file paths to check.
    """
    paths = ctx.params.get("paths", [])
    if not paths:
        return GateCheckResult(
            check_type="file_exists",
            passed=False,
            error_message="Missing required param 'paths'",
        )

    missing = [p for p in paths if not Path(p).exists()]
    passed = len(missing) == 0
    return GateCheckResult(
        check_type="file_exists",
        passed=passed,
        result_data={"checked": paths, "missing": missing},
        error_message=f"Missing files: {missing}" if missing else None,
    )


async def _check_pr_approvals_met(ctx: GateCheckContext) -> GateCheckResult:
    """Check that a PR has the required number of approvals.

    Counts both agent approvals (from the registry) and human approvals
    (from the GitHub API or from the registry when human reviews have been
    recorded via the framework-level human review tracking).

    Params:
        count: Required number of approvals (default: 1).
        roles: Optional list of specific roles that must have approved.
        include_humans: Whether to count human (non-bot) approvals (default: true).
    """
    pr_number = ctx.pr_number
    if not pr_number:
        return GateCheckResult(
            check_type="pr_approvals_met",
            passed=False,
            error_message="No PR number in gate context",
        )

    required_count = ctx.params.get("count", 1)
    required_roles = ctx.params.get("roles", [])
    include_humans = ctx.params.get("include_humans", True)

    if not ctx.registry:
        return GateCheckResult(
            check_type="pr_approvals_met",
            passed=False,
            error_message="No registry available for approval check",
        )

    # Fetch all approval records
    approvals = await ctx.registry.get_pr_approvals(pr_number, state="approved")

    # Count approvals, optionally filtering by role
    if required_roles:
        matching = [
            a for a in approvals
            if a.get("agent_role") in required_roles
            or (include_humans and a.get("agent_role") == "human")
        ]
    else:
        matching = approvals if include_humans else [
            a for a in approvals if a.get("agent_role") != "human"
        ]

    count = len(matching)
    passed = count >= required_count

    return GateCheckResult(
        check_type="pr_approvals_met",
        passed=passed,
        result_data={
            "required": required_count,
            "actual": count,
            "approvals": [
                {"role": a.get("agent_role"), "agent": a.get("agent_id")}
                for a in matching
            ],
        },
        error_message=None if passed else (
            f"PR #{pr_number} has {count}/{required_count} required approvals"
        ),
    )


async def _check_no_changes_requested(ctx: GateCheckContext) -> GateCheckResult:
    """Check that no reviewers have requested changes.

    Params:
        include_humans: Whether to check human reviews (default: true).
    """
    pr_number = ctx.pr_number
    if not pr_number:
        return GateCheckResult(
            check_type="no_changes_requested",
            passed=False,
            error_message="No PR number in gate context",
        )

    if not ctx.registry:
        return GateCheckResult(
            check_type="no_changes_requested",
            passed=False,
            error_message="No registry available",
        )

    include_humans = ctx.params.get("include_humans", True)

    changes_requested = await ctx.registry.get_pr_approvals(
        pr_number, state="changes_requested"
    )

    if not include_humans:
        changes_requested = [
            a for a in changes_requested if a.get("agent_role") != "human"
        ]

    blocking = len(changes_requested)
    passed = blocking == 0

    return GateCheckResult(
        check_type="no_changes_requested",
        passed=passed,
        result_data={
            "blocking_reviews": blocking,
            "reviewers": [a.get("agent_id") for a in changes_requested],
        },
        error_message=(
            f"{blocking} reviewer(s) requested changes" if not passed else None
        ),
    )


async def _check_human_approved(ctx: GateCheckContext) -> GateCheckResult:
    """Check that at least one human (non-bot) has approved the PR.

    Requires framework-level human review tracking to be enabled (gap #1 fix).

    Params:
        count: Minimum number of human approvals required (default: 1).
    """
    pr_number = ctx.pr_number
    if not pr_number:
        return GateCheckResult(
            check_type="human_approved",
            passed=False,
            error_message="No PR number in gate context",
        )

    if not ctx.registry:
        return GateCheckResult(
            check_type="human_approved",
            passed=False,
            error_message="No registry available",
        )

    required = ctx.params.get("count", 1)

    # Human reviews are recorded with role "human"
    human_approvals = await ctx.registry.get_pr_approvals(
        pr_number, role="human", state="approved"
    )
    count = len(human_approvals)
    passed = count >= required

    return GateCheckResult(
        check_type="human_approved",
        passed=passed,
        result_data={
            "required": required,
            "human_approvals": count,
            "reviewers": [a.get("agent_id") for a in human_approvals],
        },
        error_message=None if passed else (
            f"Requires {required} human approval(s); got {count}"
        ),
    )


async def _check_label_present(ctx: GateCheckContext) -> GateCheckResult:
    """Check that a required label is present on the PR/issue.

    Params:
        label: A single label name to require.
        labels: A list of label names; pass if any are present (OR logic).
        all_of: A list of label names; pass only if all are present (AND logic).
    """
    pr_number = ctx.pr_number or ctx.issue_number
    if not pr_number:
        return GateCheckResult(
            check_type="label_present",
            passed=False,
            error_message="No PR/issue number in gate context",
        )

    if not ctx.github or not ctx.owner or not ctx.repo:
        return GateCheckResult(
            check_type="label_present",
            passed=False,
            error_message="GitHub client not available for label check",
        )

    try:
        if ctx.pr_number:
            pr_data = await ctx.github.get_pull_request(ctx.owner, ctx.repo, ctx.pr_number)
            current_labels = {lbl.get("name", "") for lbl in pr_data.get("labels", [])}
        else:
            issue_data = await ctx.github.get_issue(ctx.owner, ctx.repo, ctx.issue_number)
            current_labels = {lbl.get("name", "") for lbl in issue_data.get("labels", [])}
    except Exception as exc:
        return GateCheckResult(
            check_type="label_present",
            passed=False,
            error_message=f"Failed to fetch labels: {exc}",
        )

    single = ctx.params.get("label")
    any_of = ctx.params.get("labels", [])
    all_of = ctx.params.get("all_of", [])

    if single:
        passed = single in current_labels
        return GateCheckResult(
            check_type="label_present",
            passed=passed,
            result_data={"required": single, "present": sorted(current_labels)},
            error_message=None if passed else f"Label '{single}' not present",
        )

    if all_of:
        missing = [lbl for lbl in all_of if lbl not in current_labels]
        passed = len(missing) == 0
        return GateCheckResult(
            check_type="label_present",
            passed=passed,
            result_data={"required_all": all_of, "missing": missing},
            error_message=f"Missing labels: {missing}" if missing else None,
        )

    if any_of:
        found = [lbl for lbl in any_of if lbl in current_labels]
        passed = len(found) > 0
        return GateCheckResult(
            check_type="label_present",
            passed=passed,
            result_data={"required_any": any_of, "found": found},
            error_message=f"None of the required labels present: {any_of}" if not passed else None,
        )

    return GateCheckResult(
        check_type="label_present",
        passed=False,
        error_message="No label criteria specified (use 'label', 'labels', or 'all_of')",
    )


async def _check_ci_status(ctx: GateCheckContext) -> GateCheckResult:
    """Check that CI checks are passing on the PR.

    Params:
        contexts: Optional list of specific CI context names to require.
        require_all: Whether all checks must pass (default: true).
    """
    pr_number = ctx.pr_number
    if not pr_number:
        return GateCheckResult(
            check_type="ci_status",
            passed=False,
            error_message="No PR number in gate context",
        )

    if not ctx.github or not ctx.owner or not ctx.repo:
        return GateCheckResult(
            check_type="ci_status",
            passed=False,
            error_message="GitHub client not available for CI status check",
        )

    try:
        pr_data = await ctx.github.get_pull_request(ctx.owner, ctx.repo, pr_number)
        head_sha = pr_data.get("head", {}).get("sha", "")
        if not head_sha:
            return GateCheckResult(
                check_type="ci_status",
                passed=False,
                error_message="Could not determine PR head SHA",
            )

        combined = await ctx.github.get_combined_status(ctx.owner, ctx.repo, head_sha)
        overall = combined.get("state", "pending")
    except Exception as exc:
        return GateCheckResult(
            check_type="ci_status",
            passed=False,
            error_message=f"Failed to fetch CI status: {exc}",
        )

    required_contexts = ctx.params.get("contexts", [])
    statuses = combined.get("statuses", [])

    if required_contexts:
        # Check only specific contexts
        context_map = {s.get("context", ""): s.get("state", "pending") for s in statuses}
        failing = [
            c for c in required_contexts
            if context_map.get(c, "pending") != "success"
        ]
        passed = len(failing) == 0
        return GateCheckResult(
            check_type="ci_status",
            passed=passed,
            result_data={
                "required_contexts": required_contexts,
                "failing": failing,
                "all_states": context_map,
            },
            error_message=f"CI contexts failing: {failing}" if not passed else None,
        )

    # Use overall combined status
    passed = overall == "success"
    return GateCheckResult(
        check_type="ci_status",
        passed=passed,
        result_data={
            "state": overall,
            "total_count": len(statuses),
            "failed": [s.get("context") for s in statuses if s.get("state") != "success"],
        },
        error_message=f"CI status is '{overall}' (not 'success')" if not passed else None,
    )


async def _check_branch_up_to_date(ctx: GateCheckContext) -> GateCheckResult:
    """Check that the PR branch is up-to-date with its base branch.

    Params:
        base_branch: Override the base branch to compare against.
    """
    pr_number = ctx.pr_number
    if not pr_number:
        return GateCheckResult(
            check_type="branch_up_to_date",
            passed=False,
            error_message="No PR number in gate context",
        )

    if not ctx.github or not ctx.owner or not ctx.repo:
        return GateCheckResult(
            check_type="branch_up_to_date",
            passed=False,
            error_message="GitHub client not available for branch status check",
        )

    try:
        pr_data = await ctx.github.get_pull_request(ctx.owner, ctx.repo, pr_number)
        mergeable_state = pr_data.get("mergeable_state", "unknown")
        behind_by = pr_data.get("behind_by", 0)
    except Exception as exc:
        return GateCheckResult(
            check_type="branch_up_to_date",
            passed=False,
            error_message=f"Failed to fetch PR merge state: {exc}",
        )

    # "behind" means the branch is behind the base
    passed = mergeable_state not in ("behind", "dirty") and behind_by == 0
    return GateCheckResult(
        check_type="branch_up_to_date",
        passed=passed,
        result_data={
            "mergeable_state": mergeable_state,
            "behind_by": behind_by,
        },
        error_message=(
            f"Branch is not up-to-date (state={mergeable_state}, behind_by={behind_by})"
            if not passed
            else None
        ),
    )


# ── Default Registry Instance ─────────────────────────────────────────────────

#: Module-level default registry — import and use directly for simple cases.
default_gate_registry = GateCheckRegistry()
