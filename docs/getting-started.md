# Getting Started with Squadron

This guide walks you through setting up Squadron for your first repository, from initial configuration to your first automated feature implementation.

## Prerequisites

Before you begin, ensure you have:

- **Python 3.11+** installed
- **GitHub repository** with admin access
- **LLM API access** (OpenAI, Anthropic, or GitHub Copilot)
- **Basic familiarity** with GitHub Apps and webhooks

## Installation Options

### Option 1: Install from PyPI (Recommended)
```bash
pip install squadron
```

### Option 2: Install from Source
```bash
git clone https://github.com/your-org/squadron.git
cd squadron
pip install -e ".[dev]"
```

### Verify Installation
```bash
squadron --version
squadron --help
```

## Step 1: GitHub App Setup

Squadron requires a GitHub App to interact with your repository. You can create one manually or use our guided setup.

### Option A: Automated Setup (Recommended)
```bash
squadron setup-github-app
```

Follow the interactive prompts to:
1. Create the GitHub App
2. Generate and download credentials
3. Configure webhook settings
4. Set required permissions

### Option B: Manual Setup

1. **Go to GitHub App Settings**: `https://github.com/organizations/YOUR-ORG/settings/apps/new`

2. **Configure Basic Information**:
   - **Name**: `squadron-YOUR-REPO`
   - **Homepage URL**: `https://github.com/YOUR-ORG/squadron`
   - **Webhook URL**: `https://your-deployment-url.com/webhook`
   - **Webhook Secret**: Generate a random secret

3. **Set Permissions**:
   - **Repository permissions**:
     - Contents: Read & write
     - Issues: Read & write
     - Metadata: Read
     - Pull requests: Read & write
     - Actions: Read
     - Checks: Read
   - **Organization permissions**:
     - Members: Read (if using team assignments)

4. **Subscribe to Events**:
   - Issues
   - Issue comments
   - Pull requests
   - Pull request reviews
   - Push

5. **Generate Private Key**: Download and save the `.pem` file

See [GitHub App Setup Guide](../deploy/github-app-setup.md) for detailed screenshots and troubleshooting.

## Step 2: Repository Configuration

### Copy Example Configuration

```bash
# Navigate to your repository
cd /path/to/your/repo

# Copy Squadron configuration
curl -L https://github.com/your-org/squadron/archive/main.tar.gz | tar xz --strip=2 squadron-main/examples/.squadron

# Or if you have the Squadron repo cloned:
cp -r /path/to/squadron/examples/.squadron .
```

### Edit Project Configuration

Edit `.squadron/config.yaml`:

```yaml
# .squadron/config.yaml
project:
  name: "my-awesome-project"     # REQUIRED: Your project name
  owner: "my-github-org"         # REQUIRED: GitHub org/username
  repo: "my-repo"                # REQUIRED: Repository name
  default_branch: main

human_groups:
  maintainers: ["alice", "bob"]  # REQUIRED: GitHub usernames for escalations
  reviewers: ["charlie"]         # OPTIONAL: Additional reviewers

# Optional: Customize labels (defaults shown)
labels:
  types: [feature, bug, security, docs, infra]
  priorities: [critical, high, medium, low]
  states: [needs-triage, in-progress, blocked, needs-human, needs-clarification]

# Optional: Customize branch naming
branch_naming:
  feature: "feat/issue-{issue_number}"
  bugfix: "fix/issue-{issue_number}"
  security: "security/issue-{issue_number}"
  docs: "docs/issue-{issue_number}"
  infra: "infra/issue-{issue_number}"
```

### Review Agent Configurations

The example configuration includes 5 pre-configured agents:

- **`pm.md`**: Project manager (triages and assigns issues)
- **`feat-dev.md`**: Feature development
- **`bug-fix.md`**: Bug fixes
- **`pr-review.md`**: Code review
- **`security-review.md`**: Security-focused review

You can use these as-is or customize them for your needs.

## Step 3: Environment Setup

Create a `.env` file or set environment variables:

```bash
# GitHub App credentials (from Step 1)
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC...
-----END PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your-webhook-secret-here

# LLM API credentials (choose one)
OPENAI_API_KEY=sk-...
# OR
ANTHROPIC_API_KEY=sk-ant-...
# OR for GitHub Copilot
GITHUB_TOKEN=ghp_...

# Optional: Database and logging
DATABASE_URL=sqlite:///squadron.db
LOG_LEVEL=INFO
```

### Environment Variable Security

**For production deployments:**
- Use Azure Key Vault, AWS Secrets Manager, or similar
- Never commit API keys to version control
- Consider using GitHub's environment secrets for workflows

**For development:**
- Use a `.env` file (add to `.gitignore`)
- Or export variables in your shell session

## Step 4: Local Testing

Before deploying, test Squadron locally:

```bash
# Start the Squadron server
squadron serve --repo-root /path/to/your/repo

# In another terminal, test with ngrok (for webhook testing)
ngrok http 8000
```

Update your GitHub App webhook URL to the ngrok URL: `https://abc123.ngrok.io/webhook`

### Test the System

1. **Create a test issue** in your repository:
   ```
   Title: Add user authentication
   Labels: feature
   Body: Implement user login and registration functionality
   ```

2. **Watch the logs** for Squadron activity:
   ```bash
   tail -f squadron.log
   ```

3. **Verify PM agent response**:
   - Issue should be labeled and assigned
   - A feature development agent should be spawned
   - Check issue comments for agent updates

4. **Monitor agent progress**:
   - New branch should be created
   - Code changes should be committed
   - Pull request should be opened

## Step 5: Production Deployment

Once local testing is successful, deploy to production.

### Option A: Azure Container Apps (Recommended)

```bash
# Clone deployment templates
git clone https://github.com/your-org/squadron.git
cd squadron/deploy/azure-container-apps

# Configure deployment
cp parameters.template.json parameters.json
# Edit parameters.json with your values

# Deploy
az deployment group create \
  --resource-group your-rg \
  --template-file main.bicep \
  --parameters @parameters.json
```

### Option B: Docker Deployment

```bash
# Build image
docker build -t squadron .

# Run container
docker run -d \
  --name squadron \
  -p 8000:8000 \
  --env-file .env \
  -v /path/to/repo:/app/repo:ro \
  squadron
```

### Option C: Cloud Run / ECS / K8s

See [deployment guide](../deploy/README.md) for platform-specific instructions.

## Step 6: GitHub App Configuration

Update your GitHub App with the production webhook URL:

1. Go to GitHub App settings: `https://github.com/organizations/YOUR-ORG/settings/apps`
2. Click your Squadron app
3. Update **Webhook URL** to: `https://your-production-url.com/webhook`
4. Test the webhook delivery

## Step 7: Validation and Monitoring

### Test End-to-End Workflow

1. **Open a feature request**:
   ```
   Title: Add dark mode toggle
   Labels: feature, medium
   Body: Users should be able to toggle between light and dark themes
   ```

2. **Monitor the workflow**:
   - PM agent triages within 30 seconds
   - Feature dev agent creates branch and starts work
   - Code is committed and PR is opened
   - Review agent provides feedback

### Monitor System Health

```bash
# Check agent status
squadron status

# View recent activity
squadron logs --last 24h

# Monitor resource usage
squadron monitor
```

### Set Up Alerts

Configure monitoring for:
- Agent failures or timeouts
- High API usage
- Webhook delivery failures
- Circuit breaker activations

## Common Customizations

### Custom Agent for Documentation

```yaml
# .squadron/agents/docs-dev.md
---
name: docs-dev
description: Documentation specialist
tools:
  - read_issue
  - open_pr
  - git_push
  - check_for_events
  - report_complete
  - get_repo_info
---

# Documentation Agent

You maintain and improve project documentation...
```

### Custom Labels and Triggers

```yaml
# .squadron/config.yaml
labels:
  types: [feature, bug, enhancement, question]
  priorities: [p0, p1, p2, p3]
  
agents:
  docs-dev:
    triggers:
      - issue_labeled: ["documentation"]
      - issue_opened: ["docs"]
```

### Branch Protection Rules

Configure branch protection to require PR reviews:

1. Go to repository Settings → Branches
2. Add rule for `main` branch:
   - Require pull request reviews
   - Require status checks (CI)
   - Include administrators

## Troubleshooting

### Common Issues

**Agent not responding to issues:**
- Check webhook delivery in GitHub App settings
- Verify environment variables are correct
- Check Squadron logs for errors

**Permission denied errors:**
- Verify GitHub App permissions
- Check that App is installed on the repository
- Ensure private key is correct

**LLM API failures:**
- Verify API key is valid and has credits
- Check rate limiting settings
- Monitor API usage

### Debug Mode

```bash
# Enable debug logging
LOG_LEVEL=DEBUG squadron serve --repo-root .

# Test specific components
squadron test-webhook --payload webhook-payload.json
squadron test-agent --agent pm --issue 123
```

### Getting Help

- **Documentation**: [docs/](../README.md) directory
- **GitHub Issues**: [Report issues](https://github.com/your-org/squadron/issues)
- **Community**: [Discussions](https://github.com/your-org/squadron/discussions)

## Next Steps

Now that Squadron is running:

1. **Monitor and tune** agent performance
2. **Customize agents** for your workflow
3. **Add custom tools** for project-specific needs
4. **Scale up** to multiple repositories
5. **Contribute improvements** back to the project

## Security Considerations

- **Review agent permissions** regularly
- **Monitor API usage** to detect abuse
- **Audit agent actions** through GitHub's native audit log
- **Use principle of least privilege** for GitHub App permissions
- **Rotate credentials** periodically

Squadron is designed with security in mind - all actions are auditable, reversible, and subject to GitHub's built-in access controls.
