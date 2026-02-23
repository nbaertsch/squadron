# Getting Started with Squadron

This guide walks you through setting up Squadron for your first repository, from initial configuration to your first automated feature implementation.

## Prerequisites

Before you begin, ensure you have:

- **Python 3.11+** installed (3.12 or 3.13 recommended)
- **GitHub repository** with admin access
- **GitHub Copilot access** — or an LLM API key (OpenAI or Anthropic)
- **Basic familiarity** with GitHub Apps and webhooks

## Step 1: Install Squadron

Squadron is installed from source:

```bash
git clone https://github.com/your-org/squadron.git
cd squadron
pip install -e .
```

Verify the install:
```bash
squadron --help
```

Available commands:
- `squadron serve` — start the webhook server
- `squadron deploy` — deploy to Azure Container Apps

## Step 2: GitHub App Setup

Squadron requires a GitHub App to interact with your repository.

### Option A: Manual Setup (Recommended)

Follow the detailed [GitHub App Setup Guide](../deploy/github-app-setup.md). In brief:

1. Go to `https://github.com/settings/apps/new` (or org equivalent)
2. Set the app name, homepage URL, and webhook secret
3. Grant permissions: **Contents** (R/W), **Issues** (R/W), **Pull requests** (R/W), **Metadata** (R)
4. Subscribe to events: **Issues**, **Issue comment**, **Pull request**, **Pull request review**, **Push**
5. Create the app and note the **App ID**
6. Generate a **private key** (`.pem` file) and download it
7. Install the app on your repository and note the **Installation ID**

### Get the Installation ID

After installing the app, the Installation ID is in the URL:
```
https://github.com/settings/installations/<INSTALLATION_ID>
```

Or via the GitHub API:
```bash
curl -H "Authorization: Bearer $YOUR_GITHUB_TOKEN" \
  https://api.github.com/app/installations
```

## Step 3: Repository Configuration

### Copy Example Configuration

Navigate to your target repository and copy the example configuration:

```bash
cd /path/to/your/repo

# Copy from Squadron source
cp -r /path/to/squadron/examples/.squadron .
```

### Edit Project Configuration

Edit `.squadron/config.yaml`:

```yaml
project:
  name: "my-awesome-project"     # REQUIRED: Your project name
  owner: "my-github-org"         # REQUIRED: GitHub org/username
  repo: "my-repo"                # REQUIRED: Repository name
  default_branch: main

human_groups:
  maintainers: ["alice", "bob"]  # REQUIRED: GitHub usernames for escalations

# Optional: Customize labels (defaults shown)
labels:
  types: [feature, bug, security, documentation, infrastructure]
  priorities: [critical, high, medium, low]
  states: [needs-triage, in-progress, blocked, needs-human, needs-clarification]
```

### Review Agent Configurations

The example configuration includes 5 pre-configured agents in `.squadron/agents/`:

- **`pm.md`**: Project manager (triages and assigns issues)
- **`feat-dev.md`**: Feature development
- **`bug-fix.md`**: Bug fixes
- **`pr-review.md`**: Code review
- **`security-review.md`**: Security-focused review

You can use these as-is or customize them. See [Agent Configuration Reference](reference/agent-configuration.md).

## Step 4: Environment Setup

### For Local Development

Create a `.env` file in the Squadron source directory (not your target repo):

```bash
# .env (in squadron/ source directory)
SQ_APP_ID_DEV=123456
SQ_APP_CLIENT_ID_DEV=Iv1.abc...
SQ_APP_CLIENT_SECRET_DEV=abc...
SQ_INSTALLATION_ID_DEV=78901234

# Private key file path
SQ_APP_PRIVATE_KEY_FILE=squadron-dev.2026-01-01.private-key.pem

# Optional: Copilot SDK auth (or use `copilot auth login`)
# COPILOT_GITHUB_TOKEN=github_pat_...

# E2E test target
E2E_TEST_OWNER=your-github-username
E2E_TEST_REPO=squadron-e2e-test
```

### For Production Deployment

Set these environment variables in your deployment environment (Azure Container Apps secrets, GitHub Actions secrets, etc.):

```bash
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIIEvQ...
-----END PRIVATE KEY-----"
GITHUB_INSTALLATION_ID=78901234
GITHUB_WEBHOOK_SECRET=your-webhook-secret

# Copilot SDK authentication
COPILOT_GITHUB_TOKEN=github_pat_...

# Optional: Repository URL (container clones at startup)
SQUADRON_REPO_URL=https://github.com/your-org/your-repo
```

> **Security:** Never commit `.env` or `.pem` files. Both are in `.gitignore`.

## Step 5: Local Testing

Test Squadron locally before deploying to production.

### Start the Server

```bash
# Set environment variables
export GITHUB_APP_ID=123456
export GITHUB_PRIVATE_KEY="$(cat your-app.private-key.pem)"
export GITHUB_INSTALLATION_ID=78901234
export GITHUB_WEBHOOK_SECRET=your-webhook-secret
export COPILOT_GITHUB_TOKEN=github_pat_...

# Start the server
squadron serve --repo-root /path/to/your/repo
```

The server starts on `http://0.0.0.0:8000` by default.

### Expose for Webhook Testing

For GitHub to deliver webhooks to your local server, use [ngrok](https://ngrok.com/):

```bash
ngrok http 8000
```

Update your GitHub App webhook URL to the ngrok URL + `/webhook`:
```
https://abc123.ngrok.io/webhook
```

### Test the System

1. **Create a test issue** in your repository:
   ```
   Title: Add user authentication
   Labels: feature
   Body: Implement user login and registration functionality
   ```

2. **Check the server logs** for Squadron activity:
   ```bash
   # Logs appear in the terminal where squadron serve is running
   # Or view the observability dashboard at http://localhost:8000/dashboard/
   ```

3. **Verify PM agent response:**
   - Issue should be labeled and a triage comment should appear
   - A feature development agent should be spawned

4. **Monitor agent progress:**
   - A new branch should be created (`feat/issue-N`)
   - Code changes should be committed
   - A pull request should be opened

## Step 6: Production Deployment

Once local testing is successful, deploy to production.

### Azure Container Apps (Recommended)

```bash
# Set required environment variables
export GITHUB_APP_ID=123456
export GITHUB_PRIVATE_KEY="$(cat your-app.private-key.pem)"
export GITHUB_INSTALLATION_ID=78901234
export GITHUB_WEBHOOK_SECRET=your-webhook-secret

# Deploy
squadron deploy --repo-root /path/to/your/repo
```

The deploy command wraps `az deployment group create` with your Bicep template. See [Azure Container Apps Guide](../deploy/azure-container-apps/README.md) for details.

### Docker

```bash
docker build -t squadron .
docker run -d \
  --name squadron \
  -p 8000:8000 \
  -e GITHUB_APP_ID=$GITHUB_APP_ID \
  -e "GITHUB_PRIVATE_KEY=$(cat your-app.private-key.pem)" \
  -e GITHUB_INSTALLATION_ID=$GITHUB_INSTALLATION_ID \
  -e GITHUB_WEBHOOK_SECRET=$GITHUB_WEBHOOK_SECRET \
  -e COPILOT_GITHUB_TOKEN=$COPILOT_GITHUB_TOKEN \
  -e SQUADRON_REPO_URL=https://github.com/your-org/your-repo \
  squadron
```

### Update the Webhook URL

After deploying, update your GitHub App webhook URL:

1. Go to your app settings: `https://github.com/settings/apps/<your-app-name>`
2. Update **Webhook URL** to: `https://your-production-url.com/webhook`
3. Test the webhook delivery from the app settings

## Step 7: Monitoring

### Health Check

```bash
curl https://your-squadron-url.com/health
```

Response:
```json
{"status": "healthy"}
```

### Observability Dashboard

View real-time agent activity at:
```
https://your-squadron-url.com/dashboard/
```

Optionally protect with an API key:
```bash
export SQUADRON_DASHBOARD_API_KEY="your-random-key"
```

See [Observability Guide](observability.md) for the full dashboard API reference.

## Common Customizations

### Add a Custom Agent

Create `.squadron/agents/api-docs.md`:

```yaml
---
name: api-docs
display_name: API Documentation Agent
emoji: "📖"
description: Generates and maintains API documentation
tools:
  - read_file
  - write_file
  - bash
  - git_push
  - open_pr
  - read_issue
  - check_for_events
  - report_complete
lifecycle: persistent
---

# API Documentation Agent

You maintain comprehensive API documentation for the {project_name} project...
```

### Custom Label Triggers

Edit `.squadron/config.yaml` to add custom triggers:

```yaml
agent_roles:
  api-docs:
    triggers:
      - issue_labeled: "api-docs"
```

### Branch Protection Rules

Configure branch protection in GitHub (Settings → Branches → Add rule for `main`):
- Require pull request reviews before merging
- Require status checks to pass
- Include administrators

## Troubleshooting

### Agent Not Responding to Issues

1. Check webhook delivery in GitHub App settings → **Recent Deliveries**
2. Verify server is running: `curl http://localhost:8000/health`
3. Check server logs for webhook reception errors
4. Verify GitHub App is installed on the target repository

### Permission Errors

1. Verify GitHub App permissions: Contents (R/W), Issues (R/W), Pull requests (R/W)
2. Check that the App is installed on the repository
3. Verify the private key format (should start with `-----BEGIN PRIVATE KEY-----`)

### LLM/Copilot Failures

1. Verify `COPILOT_GITHUB_TOKEN` is a valid fine-grained PAT from a Copilot-licensed account
2. Or use `copilot auth login` for local development
3. Check circuit breaker limits in `config.yaml` if agents timeout

See [Troubleshooting Guide](troubleshooting.md) for more solutions.

## Next Steps

Now that Squadron is running:

1. **Customize agents** for your workflow in `.squadron/agents/`
2. **Tune circuit breakers** in `.squadron/config.yaml` based on observed behavior
3. **Add the observability dashboard** for production monitoring
4. **Contribute improvements** — see [Contributing Guide](../CONTRIBUTING.md)

## Security Considerations

- **Review agent permissions regularly** — agents only have the tools explicitly listed in their frontmatter
- **Monitor API usage** to detect unexpected activity
- **All agent actions are auditable** — GitHub's audit log records every API call
- **Rotate credentials periodically** — App private keys and webhook secrets
- **Use branch protection** — require reviews for the default branch
