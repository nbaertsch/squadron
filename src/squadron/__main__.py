"""Squadron CLI entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


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

    args = parser.parse_args()

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
