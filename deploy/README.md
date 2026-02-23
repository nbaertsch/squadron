# Squadron Deployment

Squadron runs as a **standalone service** that receives GitHub webhooks and orchestrates AI agents against your repository. You don't install Squadron into your repo — you deploy an instance and point it at your repo via the GitHub App.

## Architecture

```mermaid
flowchart LR
  subgraph repo["Your Repo"]
    config[".squadron/<br>config.yaml<br>agents/*.md"]
    workflow[".github/workflows/<br>squadron-deploy.yml"]
  end

  subgraph instance["Squadron Instance (Azure)"]
    clone["/tmp/squadron-repo/<br>(git clone of repo)"]
    worktrees["/tmp/squadron-worktrees/"]
    server["squadron serve<br>--repo-root /tmp/squadron-repo"]
  end

  workflow -- "deploy / restart" --> instance
  ghapp["GitHub App<br>(webhooks)"] --> server
  server -- "git pull on push event<br>(config hot-reload)" --> clone
```

- **Your repo** contains `.squadron/` configuration (agent definitions, project settings)
- **A GitHub Actions workflow** (copied from our templates) deploys the Squadron container
- **The deployed container** clones the repo at startup and receives webhooks from the GitHub App
- **Config hot-reload** happens automatically — push events to `main` that touch `.squadron/` trigger a git pull + config reload (no restart needed)

## Onboarding a New Repo

1. **[Create a GitHub App](github-app-setup.md)** for your repo (one app per repo)
2. **Copy `examples/.squadron/`** from the Squadron repo into your repo root and customize
3. **Copy the deployment workflow template** into `.github/workflows/`
4. **Set repository secrets** (Azure credentials, GitHub App credentials)
5. **Run the workflow** to deploy
6. **Configure the webhook URL** in the GitHub App settings

Start with the [GitHub App setup guide](github-app-setup.md) — it walks through creating the app, generating credentials, and what secrets you'll need.

## Deployment Targets

| Target | Status | Directory |
|--------|--------|-----------|
| [Azure Container Apps](azure-container-apps/README.md) | ✅ Supported | `deploy/azure-container-apps/` |

## Container Image

Squadron publishes a pre-built container image on every push to `main`:

```
ghcr.io/nbaertsch/squadron:latest
```

All deployment templates use this image by default. You never need to build your own.

## Environment Variables

The following environment variables must be set in your deployment environment:

```bash
# ── GitHub App Credentials ───────────────────────────────────────────
# From your GitHub App settings (see github-app-setup.md)
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIIEvQ...
-----END PRIVATE KEY-----"
GITHUB_INSTALLATION_ID=78901234
GITHUB_WEBHOOK_SECRET=your-random-secret   # openssl rand -hex 32

# ── Copilot SDK Authentication ───────────────────────────────────────
# Fine-grained PAT from a GitHub account with an active Copilot subscription.
# Required scopes: No additional scopes needed beyond default PAT scopes.
COPILOT_GITHUB_TOKEN=github_pat_...

# ── Repository Configuration ─────────────────────────────────────────
# Optional: If set, the container clones this repo at startup.
# If not set, mount the repo directory and use --repo-root.
SQUADRON_REPO_URL=https://github.com/your-org/your-repo

# ── Optional Configuration ───────────────────────────────────────────
LOG_LEVEL=INFO   # DEBUG, INFO, WARNING, ERROR (default: INFO)
SQUADRON_DASHBOARD_API_KEY=your-dashboard-key  # Secures /dashboard/ endpoints
```

> **Note for local development:** Use `SQ_APP_ID_DEV`, `SQ_APP_PRIVATE_KEY_FILE`, etc. as shown in `.env.example`. The production variable names listed above are used by the deployed container.

## Config Hot-Reload

Squadron supports hot-reloading of agent configurations. When you push changes to `.squadron/` files:

1. Squadron detects the push webhook
2. Pulls the latest changes to the cloned repo
3. Reloads agent definitions and validates configuration
4. Applies changes without service restart

This means you can iterate on agent configurations without redeploying the service.

## Monitoring Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Basic health check |
| `GET /dashboard/` | Web UI for real-time agent activity |
| `GET /dashboard/agents` | Active/recent agent list (API) |
| `GET /dashboard/stream` | SSE stream of all agent activity |
| `GET /dashboard/status` | Dashboard system status |

See [Observability Guide](../docs/observability.md) for the full dashboard API reference.

For complete setup instructions, see [Getting Started Guide](../docs/getting-started.md).
