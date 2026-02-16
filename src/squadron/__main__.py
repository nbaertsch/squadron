"""Squadron CLI entry point."""

from __future__ import annotations

import argparse
import logging
import os
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
    stateless: true
    triggers:
      - event: "issues.opened"
      - event: "issue_comment.created"
  feat-dev:
    agent_definition: agents/feat-dev.md
    triggers:
      - event: "issues.labeled"
        label: feature
  bug-fix:
    agent_definition: agents/bug-fix.md
    triggers:
      - event: "issues.labeled"
        label: bug
  pr-review:
    agent_definition: agents/pr-review.md
  security-review:
    agent_definition: agents/security-review.md
    triggers:
      - event: "issues.labeled"
        label: security

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
    config_content = _DEFAULT_CONFIG.format(project_name=project_name, owner=owner, repo=repo)
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
            template.format(
                project_name=project_name,
                issue_number="",
                issue_title="",
                branch_name="",
                base_branch="main",
                max_iterations="5",
                max_tool_calls="200",
            )
        )

    print(f"Initialized Squadron project at {squadron_dir}")
    print(f"  Project: {project_name}")
    if owner:
        print(f"  Owner:   {owner}")
    print(f"  Repo:    {repo}")
    print()
    print("Next steps:")
    print(f"  1. Review {squadron_dir / 'config.yaml'}")
    print(
        "  2. Set environment variables (GITHUB_APP_ID, GITHUB_PRIVATE_KEY_PATH, ANTHROPIC_API_KEY)"
    )
    print(f"  3. Run: squadron --repo-root {repo_root}")


def _deploy(args) -> None:
    """Validate prerequisites and deploy to Azure Container Apps."""
    import shutil
    import subprocess

    repo_root: Path = args.repo_root
    squadron_dir = repo_root / ".squadron"

    # Validate .squadron/ exists
    if not squadron_dir.exists():
        print(f"Error: .squadron/ directory not found at {squadron_dir}", file=sys.stderr)
        print("Run 'squadron init' first.", file=sys.stderr)
        sys.exit(1)

    # Validate infra/main.bicep exists
    bicep_path = repo_root / "infra" / "main.bicep"
    if not bicep_path.exists():
        print(f"Error: Bicep template not found at {bicep_path}", file=sys.stderr)
        sys.exit(1)

    # Check az CLI is installed
    if not shutil.which("az"):
        print(
            "Error: Azure CLI (az) not found. Install from https://aka.ms/install-az",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check az login
    result = subprocess.run(
        ["az", "account", "show", "--query", "name", "-o", "tsv"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        print("Error: Not logged in to Azure. Run 'az login' first.", file=sys.stderr)
        sys.exit(1)

    account_name = result.stdout.strip()
    print(f"Azure account: {account_name}")

    # Derive defaults
    app_name = args.app_name
    if not app_name:
        config_path = squadron_dir / "config.yaml"
        try:
            import yaml

            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            project_name = raw.get("project", {}).get("name", repo_root.name)
            app_name = f"squadron-{project_name}"[:32]
        except Exception:
            app_name = f"squadron-{repo_root.name}"[:32]

    image = args.image
    if not image:
        # Try to figure out owner from git remote
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                url = result.stdout.strip()
                parts = url.rstrip(".git").split("github.com")[-1].lstrip("/:").split("/")
                if len(parts) >= 2:
                    image = f"ghcr.io/{parts[0]}/squadron:latest"
        except Exception:
            pass
        if not image:
            print("Error: Cannot determine image. Use --image flag.", file=sys.stderr)
            sys.exit(1)

    resource_group = args.resource_group
    location = args.location

    # Check required env vars
    required_env = [
        "GITHUB_APP_ID",
        "GITHUB_PRIVATE_KEY",
        "GITHUB_INSTALLATION_ID",
        "GITHUB_WEBHOOK_SECRET",
    ]
    missing = [v for v in required_env if not os.environ.get(v)]
    if missing:
        print(f"Error: Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Set them before deploying, or use GitHub Actions workflow instead.", file=sys.stderr)
        sys.exit(1)

    print()
    print("Deployment plan:")
    print(f"  App name:       {app_name}")
    print(f"  Resource group: {resource_group}")
    print(f"  Location:       {location}")
    print(f"  Image:          {image}")
    print(f"  Bicep:          {bicep_path}")
    print()

    confirm = input("Continue? [y/N] ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

    # Create resource group if needed
    print(f"\nCreating resource group '{resource_group}'...")
    subprocess.run(
        ["az", "group", "create", "--name", resource_group, "--location", location],
        check=True,
    )

    # Deploy Bicep
    print("\nDeploying infrastructure via Bicep...")
    deploy_cmd = [
        "az",
        "deployment",
        "group",
        "create",
        "--resource-group",
        resource_group,
        "--template-file",
        str(bicep_path),
        "--parameters",
        f"appName={app_name}",
        f"location={location}",
        f"containerImage={image}",
        f"githubAppId={os.environ['GITHUB_APP_ID']}",
        f"githubPrivateKey={os.environ['GITHUB_PRIVATE_KEY']}",
        f"githubInstallationId={os.environ['GITHUB_INSTALLATION_ID']}",
        f"githubWebhookSecret={os.environ['GITHUB_WEBHOOK_SECRET']}",
        f"copilotGithubToken={os.environ.get('COPILOT_GITHUB_TOKEN', '')}",
        "--query",
        "properties.outputs",
        "-o",
        "json",
    ]
    result = subprocess.run(deploy_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error deploying infrastructure:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print(result.stdout)

    # Upload .squadron/ config to Azure Files
    print("\nUploading .squadron/ config to Azure Files...")
    storage_account = f"sq{app_name.replace('-', '')}sa"[:24]
    for file_path in squadron_dir.rglob("*"):
        if file_path.is_file():
            relative = file_path.relative_to(repo_root)
            upload_cmd = [
                "az",
                "storage",
                "file",
                "upload",
                "--account-name",
                storage_account,
                "--share-name",
                "squadron-data",
                "--source",
                str(file_path),
                "--path",
                str(relative),
                "--auth-mode",
                "login",
            ]
            subprocess.run(upload_cmd, capture_output=True)

    print("\nDeployment complete!")
    print("\nConfigure your GitHub App webhook URL to the FQDN from the output above.")
    print("The webhook path is: /webhook/github")


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

    # squadron deploy
    deploy_parser = subparsers.add_parser("deploy", help="Deploy Squadron to Azure Container Apps")
    deploy_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the repository root (default: current directory)",
    )
    deploy_parser.add_argument(
        "--app-name",
        help="Azure Container App name (default: derived from repo name)",
    )
    deploy_parser.add_argument(
        "--resource-group",
        default="squadron-rg",
        help="Azure resource group (default: squadron-rg)",
    )
    deploy_parser.add_argument(
        "--location",
        default="switzerlandnorth",
        help="Azure region (default: switzerlandnorth)",
    )
    deploy_parser.add_argument(
        "--image",
        help="Container image (default: ghcr.io/<owner>/squadron:latest)",
    )

    args = parser.parse_args()

    if args.command == "init":
        _init_project(args.repo_root)
        return

    if args.command == "deploy":
        _deploy(args)
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

    # Validate repo root (skip if SQUADRON_REPO_URL is set — server will clone at startup)
    squadron_dir = args.repo_root / ".squadron"
    if not squadron_dir.exists() and not os.environ.get("SQUADRON_REPO_URL"):
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
