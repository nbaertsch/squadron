"""Squadron CLI entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


# ── Default templates for `squadron init` ────────────────────────────────────

_DEFAULT_CONFIG = """\
# .squadron/config.yaml — Squadron project configuration

project:
  name: "{project_name}"
  owner: "{owner}"
  repo: "{repo}"
  default_branch: main

labels:
  types: [feature, bug, security, docs]
  priorities: [critical, high, medium, low]
  states: [needs-triage, in-progress, blocked, needs-human, needs-clarification]

branch_naming:
  feature: "feat/issue-{{issue_number}}"
  bugfix: "fix/issue-{{issue_number}}"
  security: "security/issue-{{issue_number}}"

agent_roles:
  pm:
    agent_definition: agents/pm.md
    singleton: true
  feat-dev:
    agent_definition: agents/feat-dev.md
    assignable_labels: [feature]
  bug-fix:
    agent_definition: agents/bug-fix.md
    assignable_labels: [bug]
  pr-review:
    agent_definition: agents/pr-review.md
    trigger: approval_flow
  security-review:
    agent_definition: agents/security-review.md
    trigger: approval_flow

circuit_breakers:
  defaults:
    max_iterations: 5
    max_tool_calls: 200
    max_turns: 50
    max_active_duration: 7200
    max_sleep_duration: 86400

runtime:
  default_model: claude-sonnet-4
  reconciliation_interval: 300
  provider:
    type: anthropic
    api_key_env: ANTHROPIC_API_KEY
"""

_DEFAULT_PM = """\
# PM Agent — {project_name}

## System Prompt

You are the Project Manager agent for {project_name}. Your role is to triage
incoming issues, classify them by type and priority, assign them to the
appropriate agent roles, and monitor overall project health.

## Tools

- create_issue: Create sub-tasks or blocker issues
- assign_issue: Assign issues to squadron[bot] for agent processing
- label_issue: Apply type/priority/state labels
- comment_on_issue: Post triage analysis
- check_registry: Monitor active agents
- read_issue: Read issue details

## Constraints

- Never modify code directly
- Always label issues before assigning
- Escalate ambiguous requirements to humans
"""

_DEFAULT_FEAT_DEV = """\
# Feature Development Agent — {project_name}

## System Prompt

You are a feature development agent for {project_name}. You implement new
features described in issue #{issue_number}: {issue_title}.

Work on branch `{branch_name}` (base: `{base_branch}`).

You have a maximum of {max_iterations} iterations and {max_tool_calls} tool calls.

## Constraints

- Create a PR when implementation is complete, then report_complete
- If blocked, use report_blocked or create_blocker_issue
- Run tests before submitting PR
- Follow existing code style
"""

_DEFAULT_BUG_FIX = """\
# Bug Fix Agent — {project_name}

## System Prompt

You are a bug fix agent for {project_name}. You fix the bug described
in issue #{issue_number}: {issue_title}.

Work on branch `{branch_name}` (base: `{base_branch}`).

## Constraints

- Write a regression test for the bug before fixing
- Create a PR when fix is complete, then report_complete
- Maximum {max_iterations} iterations
"""

_DEFAULT_PR_REVIEW = """\
# PR Review Agent — {project_name}

## System Prompt

You are a code review agent for {project_name}. Review the pull request
for correctness, style, test coverage, and potential issues.

## Constraints

- Submit review via GitHub PR review API
- Approve, request changes, or comment
- Do not push commits to the PR branch
"""

_DEFAULT_SECURITY_REVIEW = """\
# Security Review Agent — {project_name}

## System Prompt

You are a security review agent for {project_name}. Review the pull request
for security vulnerabilities, dependency issues, and unsafe patterns.

## Constraints

- Focus only on security concerns
- Escalate critical findings immediately
- Do not push commits to the PR branch
"""


def _init_project(repo_root: Path) -> None:
    """Scaffold a .squadron/ directory with default configuration."""
    squadron_dir = repo_root / ".squadron"
    agents_dir = squadron_dir / "agents"

    if squadron_dir.exists():
        print(f"Error: {squadron_dir} already exists", file=sys.stderr)
        print("Remove it first if you want to re-initialize.", file=sys.stderr)
        sys.exit(1)

    # Infer project metadata from current directory / git
    project_name = repo_root.name
    owner = ""
    repo = project_name

    try:
        import subprocess

        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # Parse github.com/owner/repo from SSH or HTTPS URL
            if "github.com" in url:
                parts = url.rstrip(".git").split("github.com")[-1]
                parts = parts.lstrip("/:").split("/")
                if len(parts) >= 2:
                    owner = parts[0]
                    repo = parts[1]
    except Exception:
        pass

    # Create directories
    agents_dir.mkdir(parents=True)

    # Write config
    config_content = _DEFAULT_CONFIG.format(
        project_name=project_name, owner=owner, repo=repo
    )
    (squadron_dir / "config.yaml").write_text(config_content)

    # Write agent definitions
    for filename, template in [
        ("pm.md", _DEFAULT_PM),
        ("feat-dev.md", _DEFAULT_FEAT_DEV),
        ("bug-fix.md", _DEFAULT_BUG_FIX),
        ("pr-review.md", _DEFAULT_PR_REVIEW),
        ("security-review.md", _DEFAULT_SECURITY_REVIEW),
    ]:
        (agents_dir / filename).write_text(
            template.format(project_name=project_name, issue_number="", issue_title="",
                            branch_name="", base_branch="main", max_iterations="5",
                            max_tool_calls="200")
        )

    print(f"Initialized Squadron project at {squadron_dir}")
    print(f"  Project: {project_name}")
    if owner:
        print(f"  Owner:   {owner}")
    print(f"  Repo:    {repo}")
    print()
    print("Next steps:")
    print(f"  1. Review {squadron_dir / 'config.yaml'}")
    print(f"  2. Set environment variables (GITHUB_APP_ID, GITHUB_PRIVATE_KEY_PATH, ANTHROPIC_API_KEY)")
    print(f"  3. Run: squadron --repo-root {repo_root}")


def main():
    parser = argparse.ArgumentParser(
        prog="squadron",
        description="Squadron — GitHub-Native multi-LLM-agent development framework",
    )

    subparsers = parser.add_subparsers(dest="command")

    # squadron init
    init_parser = subparsers.add_parser("init", help="Initialize a new Squadron project")
    init_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the repository root (default: current directory)",
    )

    # squadron serve (default / main command)
    serve_parser = subparsers.add_parser("serve", help="Start the Squadron webhook server")
    serve_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the repository root (default: current directory)",
    )
    serve_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind to (default: 8000)",
    )
    serve_parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    if args.command == "init":
        _init_project(args.repo_root)
        return

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # Default: serve
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Validate repo root
    squadron_dir = args.repo_root / ".squadron"
    if not squadron_dir.exists():
        print(f"Error: .squadron/ directory not found at {squadron_dir}", file=sys.stderr)
        print("Run 'squadron init' to create one, or specify --repo-root", file=sys.stderr)
        sys.exit(1)

    # Create and run app
    import uvicorn

    from squadron.server import create_app

    app = create_app(repo_root=args.repo_root)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
