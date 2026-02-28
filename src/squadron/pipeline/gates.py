"""Gate check interface, built-in checks, and gate registry.

AD-019: Pluggable gate system for pipeline condition evaluation.

Key exports:
    GateCheck — Abstract base class for all gate checks
    GateCheckResult — Result of a gate check evaluation
    GateCheckRegistry — Registry that maps check names to GateCheck classes
    Built-in checks: CommandCheck, FileExistsCheck, PrApprovalsMetCheck,
        CiStatusCheck, LabelPresentCheck, NoChangesRequestedCheck,
        HumanApprovedCheck, BranchUpToDateCheck
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from squadron.github_client import GitHubClient

logger = logging.getLogger("squadron.pipeline.gates")


# ── Gate Check Result ────────────────────────────────────────────────────────


@dataclass
class GateCheckResult:
    """Result of evaluating a single gate condition."""

    passed: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


# ── Pipeline Context (passed to gate checks) ────────────────────────────────


@dataclass
class PipelineContext:
    """Contextual information available to gate checks during evaluation."""

    pr_number: int | None = None
    issue_number: int | None = None
    owner: str = ""
    repo: str = ""
    pipeline_run_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    # Injected dependencies (set by engine before evaluation)
    github_client: GitHubClient | None = None


# ── Command Runner Protocol ──────────────────────────────────────────────────


class CommandRunner(Protocol):
    """Protocol for running shell commands (for CommandCheck)."""

    async def __call__(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int = 300,
    ) -> tuple[int, str, str]: ...


# ── Abstract Gate Check ─────────────────────────────────────────────────────


class GateCheck(ABC):
    """Abstract base class for gate condition checks.

    Subclasses must:
        - Set `reactive_events` to the set of GitHub event types that should
          trigger re-evaluation of this check.
        - Implement `evaluate()` which receives check-specific config and
          pipeline context.
    """

    # Which GitHub events should trigger re-evaluation of this check.
    # Empty set = only evaluated on stage entry (no reactive re-eval).
    reactive_events: set[str] = set()

    @abstractmethod
    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        """Evaluate this gate condition.

        Args:
            config: Check-specific configuration from the YAML gate definition
                (everything except the 'check' key).
            context: Pipeline context with PR number, repo info, injected deps.

        Returns:
            GateCheckResult with passed/failed status and descriptive message.
        """
        ...


# ── Built-in Gate Checks ────────────────────────────────────────────────────


class CommandCheck(GateCheck):
    """Run a shell command and check exit code.

    Config:
        run: str — command to execute
        expect: str — "success" (default) or "failure"
    """

    reactive_events: set[str] = set()  # Manual only

    def __init__(self, command_runner: CommandRunner | None = None):
        self._command_runner = command_runner

    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        command = config.get("run", "")
        if not command:
            return GateCheckResult(passed=False, message="No command specified")

        if not self._command_runner:
            return GateCheckResult(passed=False, message="No command runner available")

        expect = config.get("expect", "success")

        try:
            exit_code, stdout, stderr = await self._command_runner(command, timeout=300)
        except Exception as exc:
            return GateCheckResult(
                passed=False,
                message=f"Command execution failed: {exc}",
                data={"error": str(exc)},
            )

        passed = (exit_code == 0) if expect == "success" else (exit_code != 0)
        return GateCheckResult(
            passed=passed,
            message=f"Command exited with code {exit_code}",
            data={"exit_code": exit_code, "stdout": stdout[:1000], "stderr": stderr[:1000]},
        )


class FileExistsCheck(GateCheck):
    """Check that specified files exist.

    Config:
        paths: list[str] — file paths to check (all must exist)
    """

    reactive_events: set[str] = set()  # Manual only

    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        paths = config.get("paths", [])
        if not paths:
            return GateCheckResult(passed=False, message="No paths specified")

        missing = [p for p in paths if not Path(p).exists()]
        if missing:
            return GateCheckResult(
                passed=False,
                message=f"Missing files: {', '.join(missing)}",
                data={"missing": missing},
            )
        return GateCheckResult(
            passed=True,
            message=f"All {len(paths)} files exist",
            data={"paths": paths},
        )


class PrApprovalsMetCheck(GateCheck):
    """Check that a PR has enough approvals from the required scope.

    Config:
        scope: "agents" | "humans" | "all" (default: "all")
        count: int — minimum approvals (default: 1)
    """

    reactive_events: set[str] = {
        "pull_request_review.submitted",
        "pull_request_review.dismissed",
    }

    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        if not context.pr_number or not context.github_client:
            return GateCheckResult(passed=False, message="No PR number or GitHub client available")

        scope = config.get("scope", "all")
        required_count = config.get("count", 1)
        bot_username = context.context.get("bot_username", "squadron-dev[bot]")

        try:
            reviews = await context.github_client.get_pr_reviews(
                context.owner, context.repo, context.pr_number
            )
        except Exception as exc:
            return GateCheckResult(
                passed=False,
                message=f"Failed to fetch PR reviews: {exc}",
            )

        # Build latest review state per user (last review wins)
        latest: dict[str, str] = {}
        for review in reviews:
            user = (review.get("user") or {}).get("login", "")
            state = review.get("state", "")
            if user and state in ("APPROVED", "CHANGES_REQUESTED"):
                latest[user] = state

        # Filter by scope
        approvals = 0
        for user, state in latest.items():
            if state != "APPROVED":
                continue
            is_bot = user.endswith("[bot]") or user == bot_username
            if scope == "agents" and not is_bot:
                continue
            if scope == "humans" and is_bot:
                continue
            approvals += 1

        passed = approvals >= required_count
        return GateCheckResult(
            passed=passed,
            message=(
                f"{approvals}/{required_count} approvals ({scope} scope)"
                if not passed
                else f"Approval requirement met: {approvals}/{required_count} ({scope} scope)"
            ),
            data={"approvals": approvals, "required": required_count, "scope": scope},
        )


class CiStatusCheck(GateCheck):
    """Check that CI checks have passed.

    Config:
        workflows: list[str] — specific check names to require (empty = all)
        expect: "success" (default)
    """

    reactive_events: set[str] = {"check_suite.completed", "check_run.completed", "status"}

    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        if not context.pr_number or not context.github_client:
            return GateCheckResult(passed=False, message="No PR number or GitHub client available")

        try:
            # Get the PR to find the head SHA
            pr = await context.github_client.get_pull_request(
                context.owner, context.repo, context.pr_number
            )
            head_sha = pr.get("head", {}).get("sha", "")
            if not head_sha:
                return GateCheckResult(passed=False, message="Could not determine head SHA")

            check_runs = await context.github_client.list_check_runs(
                context.owner, context.repo, head_sha
            )
        except Exception as exc:
            return GateCheckResult(passed=False, message=f"Failed to fetch CI status: {exc}")

        required_workflows = config.get("workflows", [])
        expect = config.get("expect", "success")

        if required_workflows:
            # Check specific workflows
            check_map = {cr["name"]: cr for cr in check_runs}
            missing = [w for w in required_workflows if w not in check_map]
            if missing:
                return GateCheckResult(
                    passed=False,
                    message=f"Missing CI checks: {', '.join(missing)}",
                    data={"missing": missing},
                )
            failed = []
            for name in required_workflows:
                cr = check_map[name]
                conclusion = cr.get("conclusion", "")
                if conclusion != expect:
                    failed.append(f"{name}: {conclusion}")

            if failed:
                return GateCheckResult(
                    passed=False,
                    message=f"CI checks not passing: {', '.join(failed)}",
                    data={"failed": failed},
                )
        else:
            # All checks must pass
            failed = []
            for cr in check_runs:
                conclusion = cr.get("conclusion", "")
                status = cr.get("status", "")
                if status != "completed":
                    failed.append(f"{cr['name']}: {status}")
                elif conclusion != expect:
                    failed.append(f"{cr['name']}: {conclusion}")
            if failed:
                return GateCheckResult(
                    passed=False,
                    message=f"CI checks not passing: {', '.join(failed)}",
                    data={"failed": failed},
                )

        return GateCheckResult(
            passed=True,
            message="All CI checks passing",
            data={"check_count": len(check_runs)},
        )


class LabelPresentCheck(GateCheck):
    """Check that a specific label is present on the PR.

    Config:
        label: str — label name to check for
    """

    reactive_events: set[str] = {"pull_request.labeled", "pull_request.unlabeled"}

    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        if not context.pr_number or not context.github_client:
            return GateCheckResult(passed=False, message="No PR number or GitHub client available")

        required_label = config.get("label", "")
        if not required_label:
            return GateCheckResult(passed=False, message="No label specified")

        try:
            pr = await context.github_client.get_pull_request(
                context.owner, context.repo, context.pr_number
            )
        except Exception as exc:
            return GateCheckResult(passed=False, message=f"Failed to fetch PR: {exc}")

        labels = [lbl.get("name", "") for lbl in pr.get("labels", [])]
        passed = required_label in labels
        return GateCheckResult(
            passed=passed,
            message=(
                f"Label '{required_label}' present"
                if passed
                else f"Label '{required_label}' not found (has: {', '.join(labels) or 'none'})"
            ),
            data={"required": required_label, "present": labels},
        )


class NoChangesRequestedCheck(GateCheck):
    """Check that no reviewer has requested changes on the PR.

    Config: (none)
    """

    reactive_events: set[str] = {
        "pull_request_review.submitted",
        "pull_request_review.dismissed",
    }

    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        if not context.pr_number or not context.github_client:
            return GateCheckResult(passed=False, message="No PR number or GitHub client available")

        try:
            reviews = await context.github_client.get_pr_reviews(
                context.owner, context.repo, context.pr_number
            )
        except Exception as exc:
            return GateCheckResult(passed=False, message=f"Failed to fetch reviews: {exc}")

        # Build latest review state per user
        latest: dict[str, str] = {}
        for review in reviews:
            user = (review.get("user") or {}).get("login", "")
            state = review.get("state", "")
            if user and state in ("APPROVED", "CHANGES_REQUESTED"):
                latest[user] = state

        blockers = [u for u, s in latest.items() if s == "CHANGES_REQUESTED"]
        if blockers:
            return GateCheckResult(
                passed=False,
                message=f"Changes requested by: {', '.join(blockers)}",
                data={"blockers": blockers},
            )
        return GateCheckResult(
            passed=True,
            message="No changes requested",
        )


class HumanApprovedCheck(GateCheck):
    """Check that at least one human (non-bot) has approved.

    Config:
        count: int — minimum human approvals (default: 1)
    """

    reactive_events: set[str] = {"pull_request_review.submitted"}

    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        if not context.pr_number or not context.github_client:
            return GateCheckResult(passed=False, message="No PR number or GitHub client available")

        required = config.get("count", 1)

        try:
            reviews = await context.github_client.get_pr_reviews(
                context.owner, context.repo, context.pr_number
            )
        except Exception as exc:
            return GateCheckResult(passed=False, message=f"Failed to fetch reviews: {exc}")

        latest: dict[str, str] = {}
        for review in reviews:
            user = (review.get("user") or {}).get("login", "")
            state = review.get("state", "")
            if user and state in ("APPROVED", "CHANGES_REQUESTED"):
                latest[user] = state

        human_approvals = sum(
            1
            for user, state in latest.items()
            if state == "APPROVED" and not user.endswith("[bot]")
        )

        passed = human_approvals >= required
        return GateCheckResult(
            passed=passed,
            message=(
                f"Human approval requirement met: {human_approvals}/{required}"
                if passed
                else f"{human_approvals}/{required} human approvals"
            ),
            data={"human_approvals": human_approvals, "required": required},
        )


class BranchUpToDateCheck(GateCheck):
    """Check that the PR branch is up to date with its base branch.

    Config: (none)
    """

    reactive_events: set[str] = {"push", "pull_request.synchronize"}

    async def evaluate(
        self,
        config: dict[str, Any],
        context: PipelineContext,
    ) -> GateCheckResult:
        if not context.pr_number or not context.github_client:
            return GateCheckResult(passed=False, message="No PR number or GitHub client available")

        try:
            pr = await context.github_client.get_pull_request(
                context.owner, context.repo, context.pr_number
            )
        except Exception as exc:
            return GateCheckResult(passed=False, message=f"Failed to fetch PR: {exc}")

        # GitHub's mergeable_state tells us if branch is behind
        mergeable_state = pr.get("mergeable_state", "unknown")
        mergeable = pr.get("mergeable")

        if mergeable_state == "behind":
            return GateCheckResult(
                passed=False,
                message="Branch is behind base — needs rebase or merge",
                data={"mergeable_state": mergeable_state, "mergeable": mergeable},
            )

        if mergeable is False:
            return GateCheckResult(
                passed=False,
                message=f"Branch is not mergeable (state: {mergeable_state})",
                data={"mergeable_state": mergeable_state, "mergeable": mergeable},
            )

        return GateCheckResult(
            passed=True,
            message=f"Branch is up to date (state: {mergeable_state})",
            data={"mergeable_state": mergeable_state, "mergeable": mergeable},
        )


# ── Gate Check Registry ─────────────────────────────────────────────────────


class GateCheckRegistry:
    """Registry mapping gate check names to GateCheck classes.

    Built-in checks are registered on construction. Custom checks can be
    loaded from user-specified Python modules via `load_custom_gates()`.
    """

    def __init__(self, command_runner: CommandRunner | None = None):
        self._checks: dict[str, GateCheck] = {}
        self._command_runner = command_runner
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register all built-in gate checks."""
        self.register("command", CommandCheck(command_runner=self._command_runner))
        self.register("file_exists", FileExistsCheck())
        self.register("pr_approvals_met", PrApprovalsMetCheck())
        self.register("ci_status", CiStatusCheck())
        self.register("label_present", LabelPresentCheck())
        self.register("no_changes_requested", NoChangesRequestedCheck())
        self.register("human_approved", HumanApprovedCheck())
        self.register("branch_up_to_date", BranchUpToDateCheck())

    def register(self, name: str, check: GateCheck) -> None:
        """Register a gate check instance under the given name.

        Raises ValueError if a check with this name is already registered.
        """
        if name in self._checks:
            msg = f"Gate check '{name}' already registered"
            raise ValueError(msg)
        self._checks[name] = check

    def load_custom_gates(self, gate_configs: list[dict[str, Any]]) -> None:
        """Load custom gate checks from user-specified Python modules.

        Expected config format:
            [{"module": "my_module", "checks": [{"name": "my_check", "class": "MyCheck"}]}]
        """
        for entry in gate_configs:
            module_name = entry.get("module", "")
            if not module_name:
                logger.warning("Skipping custom gate entry with no module name")
                continue

            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                logger.error("Failed to import custom gate module '%s': %s", module_name, exc)
                continue

            for check_def in entry.get("checks", []):
                check_name = check_def.get("name", "")
                class_name = check_def.get("class", "")
                if not check_name or not class_name:
                    logger.warning(
                        "Skipping malformed check definition in module '%s'", module_name
                    )
                    continue

                try:
                    cls = getattr(module, class_name)
                except AttributeError:
                    logger.error("Class '%s' not found in module '%s'", class_name, module_name)
                    continue

                if not isinstance(cls, type) or not issubclass(cls, GateCheck):
                    logger.error(
                        "'%s.%s' must be a subclass of GateCheck",
                        module_name,
                        class_name,
                    )
                    continue

                try:
                    instance = cls()
                    self.register(check_name, instance)
                    logger.info("Registered custom gate check '%s'", check_name)
                except Exception as exc:
                    logger.error("Failed to register custom gate '%s': %s", check_name, exc)

    def get(self, name: str) -> GateCheck:
        """Look up a gate check by name.

        Raises KeyError if the check is not registered.
        """
        if name not in self._checks:
            available = sorted(self._checks.keys())
            msg = f"Unknown gate check '{name}'. Available: {available}"
            raise KeyError(msg)
        return self._checks[name]

    def has(self, name: str) -> bool:
        """Check if a gate check name is registered."""
        return name in self._checks

    def get_reactive_events(self) -> dict[str, set[str]]:
        """Build a mapping of GitHub event type → set of check names that react to it.

        Used by the engine to know which gate checks need re-evaluation
        when a reactive event fires.
        """
        mapping: dict[str, set[str]] = {}
        for name, check in self._checks.items():
            for event in check.reactive_events:
                mapping.setdefault(event, set()).add(name)
        return mapping

    @property
    def check_names(self) -> list[str]:
        """Return sorted list of all registered check names."""
        return sorted(self._checks.keys())
