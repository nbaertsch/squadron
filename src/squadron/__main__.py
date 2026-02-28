"""Squadron CLI entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


# ── Dashboard API Client Helper ─────────────────────────────────────────────


def _get_dashboard_url(args) -> str:
    """Resolve the dashboard base URL from --url arg or SQUADRON_URL env var."""
    url = getattr(args, "url", None) or os.environ.get("SQUADRON_URL", "").strip()
    if not url:
        print(
            "Error: No server URL. Use --url or set SQUADRON_URL env var.",
            file=sys.stderr,
        )
        sys.exit(1)
    return url.rstrip("/")


def _get_api_key(args) -> str | None:
    """Resolve the API key from --api-key arg or SQUADRON_DASHBOARD_API_KEY env var."""
    key = getattr(args, "api_key", None) or os.environ.get("SQUADRON_DASHBOARD_API_KEY", "").strip()
    return key or None


def _dashboard_request(
    method: str,
    url: str,
    api_key: str | None,
    *,
    params: dict | None = None,
) -> dict:
    """Make an HTTP request to a dashboard API endpoint. Returns parsed JSON."""
    import httpx

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method, url, headers=headers, params=params)
    except httpx.ConnectError:
        print(f"Error: Cannot connect to {url}", file=sys.stderr)
        sys.exit(1)
    except httpx.TimeoutException:
        print(f"Error: Request timed out: {url}", file=sys.stderr)
        sys.exit(1)

    if response.status_code == 401:
        print(
            "Error: Authentication required. Use --api-key or set SQUADRON_DASHBOARD_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)
    if response.status_code == 404:
        print(f"Error: Not found (404): {url}", file=sys.stderr)
        sys.exit(1)
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        print(f"Error: {response.status_code} — {detail}", file=sys.stderr)
        sys.exit(1)

    return response.json()


# ── Pipeline CLI Commands ────────────────────────────────────────────────────


def _pipelines_list(args) -> None:
    """List all registered pipeline definitions."""
    base_url = _get_dashboard_url(args)
    api_key = _get_api_key(args)
    data = _dashboard_request("GET", f"{base_url}/dashboard/pipelines", api_key)

    pipelines = data.get("pipelines", [])
    if not pipelines:
        print("No pipelines registered.")
        return

    print(f"{'NAME':<30} {'SCOPE':<12} {'STAGES':<8} {'TRIGGER':<20} DESCRIPTION")
    print("-" * 100)
    for p in pipelines:
        trigger = ""
        if p.get("trigger"):
            trigger = p["trigger"].get("event", "")
        print(
            f"{p['name']:<30} {p.get('scope', ''):<12} "
            f"{p.get('stage_count', 0):<8} {trigger:<20} "
            f"{p.get('description', '')[:40]}"
        )


def _pipelines_runs(args) -> None:
    """List pipeline runs (active by default, or filtered)."""
    base_url = _get_dashboard_url(args)
    api_key = _get_api_key(args)

    params: dict = {"limit": args.limit}
    if args.status:
        params["status"] = args.status
    if args.pipeline:
        params["pipeline_name"] = args.pipeline
    if args.pr:
        params["pr_number"] = args.pr
    if args.issue:
        params["issue_number"] = args.issue

    data = _dashboard_request("GET", f"{base_url}/dashboard/pipelines/runs", api_key, params=params)

    runs = data.get("runs", [])
    total = data.get("total", len(runs))
    if not runs:
        print("No pipeline runs found.")
        return

    print(f"Showing {len(runs)} of {total} runs\n")
    print(f"{'RUN ID':<38} {'PIPELINE':<25} {'STATUS':<12} {'PR':<6} {'ISSUE':<7} CREATED")
    print("-" * 110)
    for r in runs:
        pr = str(r.get("pr_number") or "-")
        issue = str(r.get("issue_number") or "-")
        created = (r.get("created_at") or "")[:19]
        print(
            f"{r['run_id']:<38} {r['pipeline_name']:<25} {r['status']:<12} "
            f"{pr:<6} {issue:<7} {created}"
        )


def _pipelines_run_detail(args) -> None:
    """Show detailed info for a specific pipeline run."""
    base_url = _get_dashboard_url(args)
    api_key = _get_api_key(args)

    data = _dashboard_request("GET", f"{base_url}/dashboard/pipelines/runs/{args.run_id}", api_key)

    run = data.get("run", {})
    stage_runs = data.get("stage_runs", [])
    children = data.get("children", [])

    print(f"Pipeline Run: {run.get('run_id', 'N/A')}")
    print(f"  Pipeline:   {run.get('pipeline_name', 'N/A')}")
    print(f"  Status:     {run.get('status', 'N/A')}")
    print(f"  Scope:      {run.get('scope', 'N/A')}")
    if run.get("pr_number"):
        print(f"  PR:         #{run['pr_number']}")
    if run.get("issue_number"):
        print(f"  Issue:      #{run['issue_number']}")
    if run.get("trigger_event"):
        print(f"  Trigger:    {run['trigger_event']}")
    if run.get("parent_run_id"):
        print(f"  Parent:     {run['parent_run_id']}")
    print(f"  Created:    {run.get('created_at', 'N/A')}")
    if run.get("started_at"):
        print(f"  Started:    {run['started_at']}")
    if run.get("completed_at"):
        print(f"  Completed:  {run['completed_at']}")
    if run.get("current_stage_id"):
        print(f"  Current:    {run['current_stage_id']}")
    if run.get("error_message"):
        print(f"  Error:      {run['error_message']}")

    if stage_runs:
        print(f"\nStage Runs ({len(stage_runs)}):")
        print(f"  {'STAGE':<20} {'STATUS':<12} {'AGENT':<20} {'DURATION':<12} ERROR")
        print(f"  {'-' * 80}")
        for sr in stage_runs:
            agent = sr.get("agent_id") or "-"
            if len(agent) > 18:
                agent = agent[:18] + ".."
            duration = ""
            if sr.get("duration_seconds") is not None:
                duration = f"{sr['duration_seconds']:.1f}s"
            error = (sr.get("error_message") or "")[:30]
            branch = f" [{sr['branch_id']}]" if sr.get("branch_id") else ""
            print(
                f"  {sr['stage_id'] + branch:<20} {sr['status']:<12} "
                f"{agent:<20} {duration:<12} {error}"
            )

    if children:
        print(f"\nChild Pipelines ({len(children)}):")
        for c in children:
            print(f"  {c['run_id']:<38} {c['pipeline_name']:<25} {c['status']}")


def _pipelines_cancel(args) -> None:
    """Cancel a running pipeline."""
    base_url = _get_dashboard_url(args)
    api_key = _get_api_key(args)

    data = _dashboard_request(
        "POST", f"{base_url}/dashboard/pipelines/runs/{args.run_id}/cancel", api_key
    )

    if data.get("cancelled"):
        print(f"Pipeline run {args.run_id} cancelled.")
    else:
        print(f"Failed to cancel pipeline run {args.run_id}.")


# ── Deploy Command ───────────────────────────────────────────────────────────


def _deploy(args) -> None:
    """Validate prerequisites and deploy to Azure Container Apps."""
    import shutil
    import subprocess

    repo_root: Path = args.repo_root
    squadron_dir = repo_root / ".squadron"

    # Validate .squadron/ exists
    if not squadron_dir.exists():
        print(f"Error: .squadron/ directory not found at {squadron_dir}", file=sys.stderr)
        print("Copy examples/.squadron/ into your repo root first.", file=sys.stderr)
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
        f"ghcrImage={image}",
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

    # Include repo URL if available (container clones at startup)
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            repo_url = result.stdout.strip()
            # Convert SSH to HTTPS for container clone
            if repo_url.startswith("git@github.com:"):
                repo_url = repo_url.replace("git@github.com:", "https://github.com/")
            if repo_url.endswith(".git"):
                repo_url = repo_url[:-4]
            deploy_cmd.append(f"repoUrl={repo_url}")
    except Exception:
        pass

    result = subprocess.run(deploy_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error deploying infrastructure:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print(result.stdout)

    print("\nDeployment complete!")
    print("\nConfigure your GitHub App webhook URL to the FQDN from the output above.")
    print("The webhook path is: /webhook")


def main():
    parser = argparse.ArgumentParser(
        prog="squadron",
        description="Squadron — GitHub-Native multi-LLM-agent development framework",
    )

    subparsers = parser.add_subparsers(dest="command")

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

    # squadron pipelines — pipeline visibility commands (AD-019)
    pipelines_parser = subparsers.add_parser(
        "pipelines",
        help="Pipeline management and visibility",
    )
    pipelines_parser.add_argument(
        "--url",
        help="Squadron server URL (or set SQUADRON_URL env var)",
    )
    pipelines_parser.add_argument(
        "--api-key",
        help="Dashboard API key (or set SQUADRON_DASHBOARD_API_KEY env var)",
    )

    pipelines_sub = pipelines_parser.add_subparsers(dest="pipelines_command")

    # squadron pipelines list
    pipelines_sub.add_parser("list", help="List registered pipeline definitions")

    # squadron pipelines runs
    runs_parser = pipelines_sub.add_parser("runs", help="List pipeline runs")
    runs_parser.add_argument(
        "--status",
        help="Filter by status (pending, running, completed, failed, cancelled, escalated)",
    )
    runs_parser.add_argument("--pipeline", help="Filter by pipeline name")
    runs_parser.add_argument("--pr", type=int, help="Filter by PR number")
    runs_parser.add_argument("--issue", type=int, help="Filter by issue number")
    runs_parser.add_argument("--limit", type=int, default=25, help="Max runs to show (default: 25)")

    # squadron pipelines run <run-id>
    run_parser = pipelines_sub.add_parser("run", help="Show pipeline run details")
    run_parser.add_argument("run_id", help="Pipeline run ID")

    # squadron pipelines cancel <run-id>
    cancel_parser = pipelines_sub.add_parser("cancel", help="Cancel a pipeline run")
    cancel_parser.add_argument("run_id", help="Pipeline run ID to cancel")

    args = parser.parse_args()

    if args.command == "deploy":
        _deploy(args)
        return

    if args.command == "pipelines":
        if args.pipelines_command == "list":
            _pipelines_list(args)
        elif args.pipelines_command == "runs":
            _pipelines_runs(args)
        elif args.pipelines_command == "run":
            _pipelines_run_detail(args)
        elif args.pipelines_command == "cancel":
            _pipelines_cancel(args)
        else:
            pipelines_parser.print_help()
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
    repo_url = os.environ.get("SQUADRON_REPO_URL", "").strip()
    if not squadron_dir.exists() and not repo_url:
        print(f"Error: .squadron/ directory not found at {squadron_dir}", file=sys.stderr)
        print(
            "Copy examples/.squadron/ into your repo root, or specify --repo-root",
            file=sys.stderr,
        )
        sys.exit(1)

    # Create and run app
    import uvicorn

    from squadron.server import create_app

    app = create_app(repo_root=args.repo_root)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
