# Deploy Squadron to Azure Container Apps

This guide walks through deploying a Squadron instance on Azure Container Apps for a specific repository.

## Prerequisites

| Requirement | How to get it |
|---|---|
| **Azure subscription** | [Free tier works](https://azure.microsoft.com/free/) |
| **Azure CLI** | `brew install azure-cli` or [install docs](https://aka.ms/install-az) |
| **GitHub App** | [Create your own GitHub App](../github-app-setup.md) (one per repo) |
| **Squadron CLI** | `uv pip install squadron` (or `pip install squadron`) |

## What Gets Deployed

The Bicep template (`infra/main.bicep` in the Squadron repo) creates:

| Resource | Purpose |
|---|---|
| **Log Analytics Workspace** | Container logs and monitoring |
| **Container App Environment** | Managed environment for the container |
| **Storage Account + Azure Files** | Persistent volume for `.squadron/` config + SQLite DB |
| **Container App** | The Squadron server (`ghcr.io/nbaertsch/squadron:latest`) |

Storage architecture:
- **Azure Files** (`/data`) — persistent: `.squadron/` config, SQLite registry DB
- **Ephemeral disk** (`/tmp/squadron-worktrees`) — agent git worktrees (fast local I/O, recreated on demand)

Estimated cost: **~$5–15/month** on a single 1-CPU / 2GB container with scale-to-one.

---

## Step-by-Step Setup

### 1. Create and Install a GitHub App

Follow the **[GitHub App setup guide](../github-app-setup.md)** to:

1. Create a new GitHub App with the required permissions
2. Generate a private key
3. Install it on your target repository
4. Note the App ID, Installation ID, and webhook secret

You'll need these values for Step 5 (repository secrets).

### 2. Initialize Squadron config in your repo

```bash
cd your-repo

# Install squadron CLI (if not already installed)
uv pip install squadron

# Scaffold .squadron/ directory with default config and agent definitions
squadron init

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
    ├── bug-fix.md        # Bug fix agent
    ├── pr-review.md      # PR review agent
    └── security-review.md
```

### 3. Copy the deployment workflow

Copy the template workflow into your repo:

```bash
mkdir -p .github/workflows

# Option A: Download from Squadron repo
curl -sL https://raw.githubusercontent.com/nbaertsch/squadron/main/deploy/azure-container-apps/squadron-deploy.yml \
  -o .github/workflows/squadron-deploy.yml

# Option B: Copy manually from the Squadron repo
# deploy/azure-container-apps/squadron-deploy.yml → .github/workflows/squadron-deploy.yml
```

### 4. Create an Azure Service Principal

The workflow needs Azure credentials to deploy infrastructure:

```bash
# Login to Azure
az login

# Create a resource group (choose your region)
az group create --name squadron-rg --location eastus

# Create a service principal with Contributor access
az ad sp create-for-rbac \
  --name "squadron-deploy" \
  --role contributor \
  --scopes /subscriptions/$(az account show --query id -o tsv)/resourceGroups/squadron-rg \
  --sdk-auth
```

Copy the full JSON output — you'll need it for the next step.

### 5. Configure repository secrets

In your repo on GitHub, go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|---|---|
| `AZURE_CREDENTIALS` | Full JSON from `az ad sp create-for-rbac --sdk-auth` |
| `SQ_APP_ID` | GitHub App ID (e.g. `2868371`) |
| `SQ_PRIVATE_KEY` | GitHub App private key (full PEM file contents) |
| `SQ_INSTALLATION_ID` | Installation ID for this repo |
| `SQ_WEBHOOK_SECRET` | Webhook secret configured in the GitHub App |
| `SQ_COPILOT_TOKEN` | *(Optional)* GitHub PAT from a Copilot-licensed user for headless LLM auth |

### 6. Commit and push

```bash
git add .squadron/ .github/workflows/squadron-deploy.yml
git commit -m "chore: add squadron configuration and deployment workflow"
git push
```

### 7. Run the initial deployment

Go to **Actions → Squadron Deploy → Run workflow** and select:
- **Action**: `deploy`
- **Resource group**: `squadron-rg` (or your choice)
- **Location**: `eastus` (or your choice)

The workflow will:
1. Download the Squadron infrastructure template (Bicep)
2. Deploy all Azure resources
3. Upload your `.squadron/` config to Azure Files
4. Restart the container to load the config
5. Output the FQDN and webhook URL

### 8. Configure the webhook URL

Once the deployment completes, check the workflow's **"Output deployment info"** step for the FQDN. Then go back to your GitHub App settings and set the webhook URL as described in the ["After Deployment" section of the GitHub App guide](../github-app-setup.md#after-deployment-set-the-webhook-url):

- **Webhook URL**: `https://<FQDN>/webhook`
- **Content type**: `application/json`
- **Secret**: same value as `SQ_WEBHOOK_SECRET`

### 9. Verify

```bash
# Check health
curl https://<FQDN>/health

# Should return:
# {"status": "ok", "project": "your-project", "agents": {}, "resources": {...}}
```

### 10. Test it

Open an issue in your repo and watch the logs:

```bash
az containerapp logs show \
  --name <app-name> \
  --resource-group squadron-rg \
  --follow
```

---

## Config Sync

When you push changes to `.squadron/**` on the `main` branch, the workflow automatically:

1. Uploads the updated `.squadron/` files to Azure Files
2. Restarts the container to pick up the new config

No manual intervention needed. Edit your agent definitions or config, push, and the running instance updates within ~60 seconds.

## Workflow Actions

The template workflow supports three actions via **manual dispatch**:

| Action | What it does |
|---|---|
| `deploy` | Full infrastructure deployment + config sync |
| `sync-config` | Upload `.squadron/` to Azure Files + restart (no infra changes) |
| `destroy` | Tear down all Azure resources |

## Troubleshooting

### Container won't start
```bash
# Check container logs
az containerapp logs show --name <app-name> --resource-group squadron-rg --follow

# Check revision status
az containerapp revision list --name <app-name> --resource-group squadron-rg -o table
```

### Webhook not received
- Verify the webhook URL is correct: `https://<FQDN>/webhook`
- Check GitHub App webhook delivery log: **Settings → Developer settings → GitHub Apps → Your app → Advanced → Recent deliveries**
- Ensure the app is installed on the target repo

### Config not loading
```bash
# Verify files on Azure Files
STORAGE=$(az storage account list --resource-group squadron-rg --query "[0].name" -o tsv)
az storage file list --share-name squadron-data --account-name $STORAGE --path .squadron -o table
```

### Webhook signature errors (401)
- Ensure `SQ_WEBHOOK_SECRET` matches the secret in your GitHub App settings exactly

### Agent can't authenticate to Copilot
- Set `SQ_COPILOT_TOKEN` secret with a PAT from a Copilot-licensed GitHub user
- The PAT needs `copilot` scope
