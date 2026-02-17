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
| [Azure Container Apps](azure-container-apps/) | ✅ Supported | `deploy/azure-container-apps/` |

## Container Image

Squadron publishes a pre-built container image on every push to `main`:

```
ghcr.io/nbaertsch/squadron:latest
```

All deployment templates use this image by default. You never need to build your own.

## Recent Updates

**Note**: This deployment guide reflects the current Squadron architecture with the following recent improvements:

- **Tool-based agents**: 20+ specialized tools with per-agent selection
- **Introspection tools**: Agents use tools to understand state rather than injected context
- **GitHub App authentication**: Improved git operations with proper GitHub App credentials
- **Circuit breaker refinements**: Better resource limits and escalation flows
- **Auto-merge system**: PR approval tracking and automatic merging

### Updated Environment Variables

The following environment variables are now required:

```bash
# GitHub App credentials (unchanged)
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----..."
GITHUB_WEBHOOK_SECRET=your-webhook-secret

# LLM API credentials (unchanged)
OPENAI_API_KEY=sk-...
# OR
ANTHROPIC_API_KEY=sk-ant-...

# NEW: Optional GitHub token for enhanced API access
GITHUB_TOKEN=ghp_...

# Database and runtime (unchanged)
DATABASE_URL=sqlite:///squadron.db
LOG_LEVEL=INFO
```

### Configuration Hot-Reload

Squadron now supports hot-reloading of agent configurations. When you push changes to `.squadron/` files, the system automatically:

1. Detects changes via git pull
2. Reloads agent definitions
3. Validates new configuration
4. Applies changes without restart

This means you can iterate on agent configurations without redeploying the entire service.

### New Monitoring Endpoints

```bash
# Health check
GET /health

# Agent status
GET /api/agents/status

# Recent activity
GET /api/agents/history?limit=50

# Configuration validation
GET /api/config/validate
```

For complete setup instructions, see [Getting Started Guide](../docs/getting-started.md).
