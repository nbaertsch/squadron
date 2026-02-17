# Squadron Troubleshooting Guide

Common issues and their solutions when running Squadron in development and production.

## Quick Diagnostics

### Check System Status
```bash
# Basic health check
squadron status

# Check recent activity  
squadron logs --last 1h

# Validate configuration
squadron validate-config .squadron/config.yaml

# Test webhook connectivity
curl -X POST http://localhost:8000/health
```

### Enable Debug Logging
```bash
# Temporary debug mode
LOG_LEVEL=DEBUG squadron serve

# Or set environment variable
export LOG_LEVEL=DEBUG
squadron serve
```

## Common Issues

### 1. Agent Not Responding to Issues

**Symptoms:**
- New issues are not being triaged
- No agent comments appear on issues
- No agent activity in logs

**Diagnostic Steps:**

1. **Check webhook delivery:**
   ```bash
   # In GitHub App settings → Advanced → Recent Deliveries
   # Look for failed deliveries or error responses
   ```

2. **Verify webhook URL:**
   ```bash
   # Should point to your Squadron instance
   curl -X POST https://your-domain.com/webhook \
     -H "Content-Type: application/json" \
     -d '{"test": true}'
   ```

3. **Check Squadron logs:**
   ```bash
   # Look for webhook reception
   grep "webhook" squadron.log
   
   # Look for PM agent invocation
   grep "pm-agent" squadron.log
   ```

**Common Causes & Solutions:**

**A. Webhook not reaching Squadron:**
```bash
# Check if service is running and accessible
curl -I http://localhost:8000/health

# Check firewall/networking
sudo netstat -tlnp | grep 8000

# For cloud deployments, check load balancer/ingress
kubectl get ingress squadron
```

**B. GitHub App permissions insufficient:**
- Go to GitHub App settings
- Ensure these permissions are granted:
  - **Repository permissions**: Contents (Read & write), Issues (Read & write), Pull requests (Read & write)
  - **Subscribe to events**: Issues, Issue comments, Pull requests

**C. Agent configuration issues:**
```bash
# Validate agent definitions
squadron validate-agents .squadron/agents/

# Check for syntax errors in pm.md
cat .squadron/agents/pm.md | head -20
```

**D. Environment variables missing:**
```bash
# Check required variables
echo $GITHUB_APP_ID
echo $GITHUB_WEBHOOK_SECRET
echo $OPENAI_API_KEY  # or ANTHROPIC_API_KEY
```

### 2. Permission Denied Errors

**Symptoms:**
- "403 Forbidden" errors in logs
- "Resource not accessible by integration" errors
- Git operations failing

**Solutions:**

**A. GitHub App permissions:**
1. Go to GitHub App settings → Permissions
2. Increase permissions as needed:
   - **Contents**: Read & write (for git operations)
   - **Pull requests**: Read & write (for PR creation)
   - **Issues**: Read & write (for issue management)

**B. GitHub App installation:**
```bash
# Check if app is installed on repository
# Go to GitHub Settings → Applications → Installed GitHub Apps
# Verify your app has access to the target repository
```

**C. Private key issues:**
```bash
# Verify private key format (should be PEM)
head -1 private-key.pem
# Should show: -----BEGIN PRIVATE KEY-----

# Check private key in environment
echo "$GITHUB_APP_PRIVATE_KEY" | head -1
```

### 3. LLM API Failures

**Symptoms:**
- "Rate limit exceeded" errors
- "Invalid API key" errors
- Agents timing out without response

**Solutions:**

**A. API key validation:**
```bash
# Test OpenAI API key
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"

# Test Anthropic API key
curl https://api.anthropic.com/v1/messages \
  -H "X-API-Key: $ANTHROPIC_API_KEY" \
  -H "Content-Type: application/json"
```

**B. Rate limiting:**
```yaml
# Add circuit breaker limits
# .squadron/config.yaml
circuit_breakers:
  defaults:
    max_tool_calls: 25  # Reduce from default
    max_turns: 15       # Reduce from default
```

**C. Model selection:**
```yaml
# Use cheaper/faster models for development
llm:
  model: "gpt-3.5-turbo"  # Instead of gpt-4
  # or
  model: "claude-3-haiku-20240307"  # Instead of claude-3-opus
```

### 4. Git Operations Failing

**Symptoms:**
- "Authentication failed" when pushing
- "Repository not found" errors
- Branch creation failures

**Solutions:**

**A. Git authentication:**
```bash
# For GitHub App authentication
# Ensure GITHUB_APP_ID and private key are correct

# Test git access
git ls-remote https://github.com/owner/repo.git
```

**B. Repository access:**
```bash
# Check if Squadron can access repository
squadron test-git-access --repo owner/repo

# Verify repository exists and is accessible
curl -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/owner/repo
```

**C. Branch protection rules:**
- Check repository Settings → Branches
- Ensure Squadron's branches aren't blocked by protection rules
- Add Squadron as an exception if needed

For more troubleshooting help, see the full guide in the repository documentation.
