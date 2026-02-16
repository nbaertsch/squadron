"""GitHub-based state reconstruction for server restart recovery (3.2).

When the container restarts, container-local state (SDK sessions, worktrees,
asyncio tasks) is gone.  This module reconstructs the registry from GitHub —
the durable state layer — so agents can be resumed or properly failed.

Flow:
  1. Mark any stale ACTIVE/CREATED agents as FAILED (3.3)
  2. Query GitHub for open issues with squadron-managed labels
  3. Query GitHub for open PRs on squadron-managed branches
  4. Reconstruct agent records where missing
  5. For SLEEPING agents: let reconciliation handle wake
  6. For previously-active agents: mark FAILED + post issue comment
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from squadron.models import AgentRecord, AgentStatus

if TYPE_CHECKING:
    from squadron.config import SquadronConfig
    from squadron.github_client import GitHubClient
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)

# Labels that indicate squadron-managed issues
MANAGED_LABELS = {"in-progress", "blocked", "needs-human"}

# Pattern for squadron branch names: role/issue-{number}
BRANCH_RE = re.compile(r"^(?:feat|fix|security|docs|infra|hotfix)/issue-(\d+)$")

# Pattern to extract issue references from PR bodies:  "Fixes #42", "Closes #42"
ISSUE_REF_RE = re.compile(r"(?:fixes|closes|resolves)\s+#(\d+)", re.IGNORECASE)


async def recover_on_startup(
    config: SquadronConfig,
    registry: AgentRegistry,
    github: GitHubClient,
) -> dict[str, int]:
    """Full recovery sequence — called once at server start.

    Returns a summary dict with counts of each action taken.
    """
    summary = {"failed": 0, "reconstructed": 0, "sleeping": 0, "skipped": 0}

    # ── Phase 1: Fail stale agents (3.3) ─────────────────────────────────
    # Any agent that was ACTIVE or CREATED when we last shut down has lost
    # its in-memory state. Mark them FAILED so the dashboard shows them.
    for status in (AgentStatus.ACTIVE, AgentStatus.CREATED):
        stale = await registry.get_agents_by_status(status)
        for agent in stale:
            agent.status = AgentStatus.FAILED
            agent.active_since = None
            await registry.update_agent(agent)
            summary["failed"] += 1
            logger.warning(
                "Marked stale %s agent %s as FAILED",
                status.value,
                agent.agent_id,
            )

            # Best-effort: post comment on the issue
            if agent.issue_number:
                try:
                    await github.comment_on_issue(
                        config.project.owner,
                        config.project.repo,
                        agent.issue_number,
                        f"**[squadron:{agent.role}]** ⚠️ Agent lost due to server restart. "
                        f"Status changed from {status.value} → failed. "
                        "A human may need to re-trigger this work.",
                    )
                except Exception:
                    logger.debug("Failed to post FAILED comment for %s", agent.agent_id)

    # ── Phase 2: Reconstruct from GitHub ─────────────────────────────────
    # Look for open issues with squadron labels that we don't have records for.
    owner = config.project.owner
    repo = config.project.repo
    if not owner or not repo:
        logger.warning("No owner/repo configured — skipping GitHub reconstruction")
        return summary

    try:
        await _reconstruct_from_issues(config, registry, github, summary)
    except Exception:
        logger.exception("Failed to reconstruct from GitHub issues")

    try:
        await _reconstruct_from_prs(config, registry, github, summary)
    except Exception:
        logger.exception("Failed to reconstruct from GitHub PRs")

    logger.info(
        "Recovery complete: %d failed, %d reconstructed, %d sleeping, %d skipped",
        summary["failed"],
        summary["reconstructed"],
        summary["sleeping"],
        summary["skipped"],
    )
    return summary


async def _reconstruct_from_issues(
    config: SquadronConfig,
    registry: AgentRegistry,
    github: GitHubClient,
    summary: dict[str, int],
) -> None:
    """Reconstruct agents from open issues with squadron-managed labels."""
    owner = config.project.owner
    repo = config.project.repo

    for label in MANAGED_LABELS:
        try:
            issues = await github.list_issues(owner, repo, labels=label)
        except Exception:
            logger.warning("Failed to list issues with label=%s", label)
            continue

        for issue in issues:
            issue_number = issue["number"]
            issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}

            # Determine role from labels (match against configured roles)
            role = _infer_role_from_labels(issue_labels, config)
            if not role:
                logger.debug(
                    "Cannot determine role for issue #%d (labels=%s) — skipping",
                    issue_number,
                    issue_labels,
                )
                summary["skipped"] += 1
                continue

            # Check if we already have a record for this role + issue
            existing = await registry.get_agents_for_issue(issue_number)
            if any(a.role == role for a in existing):
                summary["skipped"] += 1
                continue

            # Determine status from labels
            if "blocked" in issue_labels:
                status = AgentStatus.SLEEPING
                summary["sleeping"] += 1
            elif "needs-human" in issue_labels:
                status = AgentStatus.ESCALATED
                summary["reconstructed"] += 1
            else:
                # "in-progress" — but we can't actually run it (no session),
                # so mark as FAILED for human attention
                status = AgentStatus.FAILED
                summary["reconstructed"] += 1

            agent_id = f"{role}-issue-{issue_number}"
            branch_config = config.branch_naming
            branch = _infer_branch(role, issue_number, branch_config)

            # Extract blockers from issue body
            blocked_by = _extract_blocker_refs(issue.get("body", "") or "")

            record = AgentRecord(
                agent_id=agent_id,
                role=role,
                issue_number=issue_number,
                status=status,
                branch=branch,
                blocked_by=blocked_by,
            )
            await registry.create_agent(record)
            logger.info(
                "Reconstructed agent %s (status=%s) from issue #%d",
                agent_id,
                status.value,
                issue_number,
            )


async def _reconstruct_from_prs(
    config: SquadronConfig,
    registry: AgentRegistry,
    github: GitHubClient,
    summary: dict[str, int],
) -> None:
    """Reconstruct agent records from open PRs on squadron-managed branches."""
    owner = config.project.owner
    repo = config.project.repo

    try:
        prs = await github.list_pull_requests(owner, repo, state="open")
    except Exception:
        logger.warning("Failed to list open PRs for reconstruction")
        return

    for pr in prs:
        head_ref = pr.get("head", {}).get("ref", "")
        match = BRANCH_RE.match(head_ref)
        if not match:
            continue  # Not a squadron branch

        issue_number_str = match.group(1)
        issue_number = int(issue_number_str)
        pr_number = pr["number"]

        # Also check body for explicit issue references
        body = pr.get("body", "") or ""
        body_issue = _extract_issue_ref(body)
        if body_issue:
            issue_number = body_issue

        # Determine role from branch prefix
        role = _infer_role_from_branch(head_ref, config)
        if not role:
            summary["skipped"] += 1
            continue

        # Check if already tracked
        existing = await registry.get_agents_for_issue(issue_number)
        if any(a.role == role for a in existing):
            # Update PR number if missing
            for a in existing:
                if a.role == role and not a.pr_number:
                    a.pr_number = pr_number
                    await registry.update_agent(a)
            summary["skipped"] += 1
            continue

        # PR exists but no agent record — the agent opened a PR then we
        # lost state. Mark as SLEEPING (waiting for review).
        record = AgentRecord(
            agent_id=f"{role}-issue-{issue_number}",
            role=role,
            issue_number=issue_number,
            pr_number=pr_number,
            status=AgentStatus.SLEEPING,
            branch=head_ref,
        )
        await registry.create_agent(record)
        summary["sleeping"] += 1
        logger.info(
            "Reconstructed sleeping agent %s from PR #%d",
            record.agent_id,
            pr_number,
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _infer_role_from_labels(
    labels: set[str],
    config: SquadronConfig,
) -> str | None:
    """Try to match issue labels to a configured agent role.

    Uses the trigger config: if a role has a trigger on ``issues.labeled``
    with a specific label, and the issue has that label, we return the role.
    Falls back to common label→role mappings.
    """
    # Check configured trigger labels first
    for role_name, role_config in config.agent_roles.items():
        for trigger in role_config.triggers:
            if trigger.label and trigger.label in labels:
                return role_name

    # Fallback heuristics
    LABEL_ROLE_MAP = {
        "feature": "feat-dev",
        "bug": "bug-fix",
        "security": "security-review",
        "docs": "docs-dev",
    }
    for label, role in LABEL_ROLE_MAP.items():
        if label in labels and role in config.agent_roles:
            return role

    return None


def _infer_role_from_branch(
    branch: str,
    config: SquadronConfig,
) -> str | None:
    """Infer agent role from branch name prefix."""
    PREFIX_ROLE_MAP = {
        "feat/": "feat-dev",
        "fix/": "bug-fix",
        "security/": "security-review",
        "docs/": "docs-dev",
        "infra/": "infra-dev",
        "hotfix/": "bug-fix",
    }
    for prefix, role in PREFIX_ROLE_MAP.items():
        if branch.startswith(prefix) and role in config.agent_roles:
            return role
    return None


def _infer_branch(
    role: str,
    issue_number: int,
    branch_config,
) -> str:
    """Build expected branch name for a role + issue."""
    templates = {
        "feat-dev": getattr(branch_config, "feature", "feat/issue-{issue_number}"),
        "bug-fix": getattr(branch_config, "bugfix", "fix/issue-{issue_number}"),
        "security-review": getattr(branch_config, "security", "security/issue-{issue_number}"),
        "docs-dev": getattr(branch_config, "docs", "docs/issue-{issue_number}"),
        "infra-dev": getattr(branch_config, "infra", "infra/issue-{issue_number}"),
    }
    template = templates.get(role, f"{role}/issue-{{issue_number}}")
    return template.format(issue_number=issue_number)


def _extract_blocker_refs(body: str) -> list[int]:
    """Extract blocker issue references from issue body.

    Looks for patterns like "Blocking #42" or "Blocked by #42".
    """
    pattern = re.compile(r"(?:block(?:ing|ed\s+by))\s+#(\d+)", re.IGNORECASE)
    return [int(m.group(1)) for m in pattern.finditer(body)]


def _extract_issue_ref(body: str) -> int | None:
    """Extract the first issue reference from PR body."""
    match = ISSUE_REF_RE.search(body)
    return int(match.group(1)) if match else None
