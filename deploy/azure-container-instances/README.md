# Deploy Squadron to Azure Container Instances

This guide walks through deploying a Squadron instance on Azure Container Instances (ACI) for a specific repository.

## Why ACI?

Squadron's sandbox requires Linux capabilities (`CAP_NET_ADMIN`, `CAP_SYS_ADMIN`) to create network namespaces for agent isolation. ACI supports granting these capabilities; Azure Container Apps does not.

| Feature | ACI | Container Apps |
|---|---|---|
| `CAP_NET_ADMIN` / `CAP_SYS_ADMIN` | Yes | No |
| Managed HTTPS ingress | No (use App Gateway) | Yes (auto-TLS) |
| Scale to zero | No | Yes |
| Revision management | No | Yes |
| Cost model | Per-second billing | Per-second + environment fee |

**Trade-off:** ACI exposes HTTP on a public IP with a DNS label. For production, front with Azure Application Gateway, Azure Front Door, or a reverse proxy for TLS termination.

## Prerequisites

| Requirement | How to get it |
|---|---|
| **Azure subscription** | [Free tier works](https://azure.microsoft.com/free/) |
| **Azure CLI** | `brew install azure-cli` or [install docs](https://aka.ms/install-az) |
| **GitHub App** | [Create your own GitHub App](../github-app-setup.md) (one per repo) |
| **Squadron CLI** | `uv pip install squadron` (or `pip install squadron`) |

## What Gets Deployed

The Bicep template (`infra/main-aci.bicep` in the Squadron repo) creates:

| Resource | Purpose |
|---|---|
| **Log Analytics Workspace** | Container diagnostics and log streaming |
| **Storage Account + Azure Files** | Persistent config (`.squadron/`) synced by GitHub Actions |
| **Container Group (ACI)** | The Squadron server with `CAP_NET_ADMIN` + `CAP_SYS_ADMIN` |

The container runs with explicit Linux capabilities:
```
securityContext:
  capabilities:
    add: [NET_ADMIN, SYS_ADMIN]
```

This enables the sandbox MitM proxy and network namespace isolation, preventing agent processes from making unauthorized network calls.

Storage architecture:
- **Git clone** (`/tmp/squadron-repo`) — the container clones the target repo at startup for `.squadron/` config
- **Ephemeral disk** (`/tmp/squadron-worktrees`) — agent git worktrees (fast local I/O, recreated on demand)
- **Ephemeral data** (`/tmp/squadron-data`) — SQLite DBs (WAL doesn't work on Azure Files)
- **Azure Files** (`/mnt/squadron-data/.squadron`) — persistent config synced by GitHub Actions
- **Config hot-reload** — push events to `main` trigger `git pull` + config reload (no restart needed)

Estimated cost: **~$5–15/month** on a single 1-CPU / 2GB container running continuously.

---

## Step-by-Step Setup

### 1. Create and Install a GitHub App

Follow the **[GitHub App setup guide](../github-app-setup.md)** to:

1. Create a new GitHub App with the required permissions
2. Generate a private key
3. Install it on your target repository
4. Note the App ID, Installation ID, and webhook secret

You'll need these values for Step 5 (repository secrets).

### 2. Add Squadron config to your repo

```bash
cd your-repo

# Copy example config from the Squadron repo
cp -r /path/to/squadron/examples/.squadron .squadron

# Or download directly from GitHub
mkdir -p .squadron/agents
curl -sL https://raw.githubusercontent.com/nbaertsch/squadron/main/examples/.squadron/config.yaml -o .squadron/config.yaml
for agent in pm feat-dev pr-review; do
  curl -sL https://raw.githubusercontent.com/nbaertsch/squadron/main/examples/.squadron/agents/${agent}.md -o .squadron/agents/${agent}.md
done

# Review and customize
$EDITOR .squadron/config.yaml
$EDITOR .squadron/agents/*.md
```

This creates:
```
.squadron/
├── config.yaml           # Project configuration
└── agents/
    ├── pm.md             # PM agent definition
    ├── feat-dev.md       # Feature developer agent
    └── pr-review.md      # PR review agent
```

### 3. Copy the deployment workflow

Copy the template workflow into your repo:

```bash
mkdir -p .github/workflows

# Option A: Download from Squadron repo
curl -sL https://raw.githubusercontent.com/nbaertsch/squadron/main/deploy/azure-container-instances/squadron-deploy.yml \
  -o .github/workflows/squadron-deploy.yml

# Option B: Copy manually from the Squadron repo
# deploy/azure-container-instances/squadron-deploy.yml → .github/workflows/squadron-deploy.yml
```

### 4. Create an Azure Service Principal

The workflow needs Azure credentials to deploy infrastructure:

```bash
# Login to Azure
az login

# Create a resource group (choose your region)
az group create --name squadron-rg --location eastus2

# Create a service principal with Contributor access
az ad sp create-for-rbac \
  --name "squadron-deploy" \
  --role contributor \
  --scopes /subscriptions/$(az account show --query id -o tsv)/resourceGroups/squadron-rg \
  --sdk-auth
```

Copy the full JSON output — you'll need it for the next step.

### 5. Configure repository secrets

In your repo on GitHub, go to **Settings > Secrets and variables > Actions** and add:

| Secret | Value |
|---|---|
| `AZURE_CREDENTIALS` | Full JSON from `az ad sp create-for-rbac --sdk-auth` |
| `SQ_APP_ID_DEV` | GitHub App ID (e.g. `2868371`) |
| `SQ_APP_PRIVATE_KEY` | GitHub App private key (full PEM file contents) |
| `SQ_INSTALLATION_ID_DEV` | Installation ID for this repo |
| `SQ_WEBHOOK_SECRET` | Webhook secret configured in the GitHub App |
| `SQ_COPILOT_TOKEN` | *(Optional)* GitHub PAT from a Copilot-licensed user for headless LLM auth |
| `SQUADRON_DASHBOARD_API_KEY` | *(Optional)* API key to protect `/dashboard/` endpoints |
| `GHCR_USERNAME` | *(Optional)* GitHub username for GHCR image pull (needed if image is private) |
| `GHCR_PASSWORD` | *(Optional)* GitHub PAT with `read:packages` scope for GHCR image pull |

### 6. Commit and push

```bash
git add .squadron/ .github/workflows/squadron-deploy.yml
git commit -m "chore: add squadron configuration and deployment workflow"
git push
```

### 7. Run the initial deployment

Go to **Actions > Squadron Deploy > Run workflow** and select:
- **Action**: `deploy`
- **Resource group**: `squadron-rg` (or your choice)
- **Location**: `eastus2` (or your choice)

The workflow will:
1. Download the Squadron ACI infrastructure template (Bicep)
2. Deploy all Azure resources (Log Analytics, Storage, Container Group)
3. Upload your `.squadron/` config to Azure Files
4. Restart the container to load the config
5. Output the FQDN and webhook URL

### 8. Configure the webhook URL

Once the deployment completes, check the workflow's **"Output deployment info"** step for the FQDN. Then go back to your GitHub App settings:

- **Webhook URL**: `http://<FQDN>:8000/webhook`
- **Content type**: `application/json`
- **Secret**: same value as `SQ_WEBHOOK_SECRET`

> **Note:** ACI exposes HTTP (not HTTPS) on port 8000. For production, front with Azure Application Gateway or Azure Front Door for TLS termination.

### 9. Verify

```bash
# Check health
curl http://<FQDN>:8000/health

# Should return:
# {"status": "ok", "project": "your-project", "agents": {}, "resources": {...}}
```

### 10. Test it

Open an issue in your repo and watch the logs:

```bash
az container logs \
  --name <app-name> \
  --resource-group squadron-rg \
  --container-name squadron \
  --follow
```

---

## Config Sync

When you push changes to `.squadron/**` on the `main` branch:

1. The running container detects the push via webhook
2. Automatically runs `git pull` to fetch the latest config
3. Validates the new config with Pydantic
4. Hot-reloads atomically — in-flight agents continue, new spawns use new config

No manual restart needed. Edit your agent definitions or config, push, and the running instance updates within seconds.

If the webhook-based hot-reload is missed, you can manually trigger a restart via the `sync-config` workflow dispatch action.

## Workflow Actions

The template workflow supports three actions via **manual dispatch**:

| Action | What it does |
|---|---|
| `deploy` | Full infrastructure deployment + config sync |
| `sync-config` | Restart the container to git pull latest config (fallback if hot-reload missed) |
| `destroy` | Tear down all Azure resources |

## Adding TLS (Production)

ACI does not provide managed TLS. Options:

1. **Azure Application Gateway** — Layer 7 load balancer with auto-TLS (Let's Encrypt via Key Vault). Route to the ACI private IP.
2. **Azure Front Door** — Global CDN + WAF + TLS. Route to ACI public FQDN.
3. **Cloudflare / Reverse proxy** — Point DNS to ACI FQDN and proxy through your preferred service.

## Migrating from Container Apps

If you have an existing Container Apps deployment:

1. Update your workflow file (replace the existing one with the ACI template)
2. The workflow will create new ACI resources in the same resource group
3. Delete the old Container Apps resources: `az containerapp delete --name <old-app> --resource-group squadron-rg`
4. Update the webhook URL in your GitHub App settings (new FQDN, port 8000, HTTP)

## Troubleshooting

### Container won't start
```bash
# Check container logs
az container logs --name <app-name> --resource-group squadron-rg --container-name squadron

# Check container state
az container show --name <app-name> --resource-group squadron-rg -o table
```

### Sandbox degradation warning
If you see the `DEGRADED SANDBOX` warning in logs, the container does not have `CAP_NET_ADMIN`. Verify the Bicep template includes `securityContext.capabilities.add: ["NET_ADMIN", "SYS_ADMIN"]`.

### Webhook not received
- Verify the webhook URL is correct: `http://<FQDN>:8000/webhook`
- Check GitHub App webhook delivery log: **Settings > Developer settings > GitHub Apps > Your app > Advanced > Recent deliveries**
- Ensure the app is installed on the target repo

### Config not loading
```bash
# Check container logs for config errors
az container logs --name <app-name> --resource-group squadron-rg --container-name squadron | grep -i config
```

### Webhook signature errors (401)
- Ensure `SQ_WEBHOOK_SECRET` matches the secret in your GitHub App settings exactly

### Agent can't authenticate to Copilot
- Set `SQ_COPILOT_TOKEN` secret with a PAT from a Copilot-licensed GitHub user
- The PAT needs `copilot` scope
