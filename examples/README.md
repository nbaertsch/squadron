# Squadron Configuration Examples

This directory contains example Squadron configurations that you can copy into your repository and customize for your needs.

## Quick Setup

Copy the entire `.squadron/` directory to your repository root:

```bash
# From your repository root
cp -r /path/to/squadron/examples/.squadron .

# Or download directly
curl -L https://github.com/your-org/squadron/archive/main.tar.gz | tar xz --strip=2 squadron-main/examples/.squadron
```

## Configuration Files

### `.squadron/config.yaml`
Main project configuration containing:
- **Project metadata** (name, owner, repo)
- **Human groups** (maintainers, reviewers)
- **Label taxonomy** (types, priorities, states)
- **Branch naming conventions**
- **Agent triggers and circuit breaker overrides**

**Required customization:**
```yaml
project:
  name: "YOUR-PROJECT-NAME"    # Change this
  owner: "YOUR-GITHUB-ORG"     # Change this  
  repo: "YOUR-REPO-NAME"       # Change this

human_groups:
  maintainers: ["YOUR-USERNAME"]  # Change this
```

### `.squadron/agents/`
Agent definitions using Markdown with YAML frontmatter:

- **`pm.md`** - Project manager (triages issues, assigns work)
- **`feat-dev.md`** - Feature development agent
- **`bug-fix.md`** - Bug fix specialist
- **`pr-review.md`** - General code review
- **`security-review.md`** - Security-focused review

## Agent Roles and Triggers

### PM Agent (Project Manager)
- **Triggers**: New issues, issue updates, @squadron-dev mentions
- **Responsibilities**:
  - Triage incoming issues
  - Apply appropriate labels
  - Assign issues to development agents
  - Escalate complex issues to humans
- **Tools**: Issue management, registry introspection, escalation
- **Lifecycle**: Ephemeral (runs once per trigger)

**Example trigger:**
```
New issue labeled "feature" → PM agent triages → Assigns to feat-dev agent
```

### Development Agents

#### feat-dev (Feature Development)
- **Triggers**: Issues labeled `feature` 
- **Responsibilities**: Implement new features with tests and documentation
- **Tools**: Git operations, PR creation, issue management
- **Lifecycle**: Persistent (can sleep/wake across work sessions)

#### bug-fix (Bug Fixes)
- **Triggers**: Issues labeled `bug`
- **Responsibilities**: Fix bugs, regressions, and issues
- **Tools**: Git operations, PR creation, debugging tools
- **Lifecycle**: Persistent

#### docs-dev (Documentation)
- **Triggers**: Issues labeled `docs` or `documentation`
- **Responsibilities**: Update documentation, guides, and API docs
- **Tools**: Git operations, repository introspection
- **Lifecycle**: Persistent

### Review Agents

#### pr-review (Code Review)
- **Triggers**: PR opened by development agents
- **Responsibilities**: Review code quality, suggest improvements
- **Tools**: PR analysis, review submission, file inspection
- **Lifecycle**: Persistent

#### security-review (Security Review)
- **Triggers**: PRs labeled `security` or touching security-sensitive files
- **Responsibilities**: Security-focused code review
- **Tools**: PR analysis, security scanning, review submission
- **Lifecycle**: Persistent

## Customization Examples

### Adding a Custom Agent

Create `.squadron/agents/api-docs.md`:

```yaml
---
name: api-docs
description: API documentation specialist
tools:
  - read_issue
  - open_pr
  - git_push
  - check_for_events
  - report_complete
  - get_repo_info
circuit_breaker:
  max_turns: 30
  max_duration: 1800
---

# API Documentation Agent

You are responsible for maintaining comprehensive API documentation...

## Your Process
1. Read the issue to understand what API changes were made
2. Update OpenAPI/Swagger specifications
3. Generate and update documentation
4. Create a PR with the documentation changes

## Guidelines
- Always include code examples
- Update both reference docs and tutorials
- Ensure documentation is accurate and up-to-date
```

### Custom Label Configuration

```yaml
# .squadron/config.yaml
labels:
  types: [feature, bug, enhancement, task, question]
  priorities: [critical, high, normal, low]
  states: [needs-triage, in-progress, blocked, review, testing]
  custom: [frontend, backend, database, security]

# Custom agent triggers
agents:
  api-docs:
    triggers:
      - issue_labeled: ["api", "documentation"]
      - pr_opened: 
          paths: ["src/api/**", "docs/api/**"]
```

### Environment-Specific Configuration

Development environment:
```yaml
# .squadron/config.dev.yaml  
circuit_breakers:
  defaults:
    max_duration: 300  # Shorter timeouts for dev
    
logging:
  level: DEBUG
```

Production environment:
```yaml
# .squadron/config.prod.yaml
circuit_breakers:
  defaults:
    max_duration: 3600  # Longer timeouts for prod
    
monitoring:
  alerts: true
  slack_webhook: "https://hooks.slack.com/..."
```

### Advanced Tool Configurations

Custom tool selection for specialized agents:

```yaml
# High-privilege agent with administrative tools
---
name: release-manager
tools:
  - read_issue
  - open_pr
  - git_push
  - merge_pr
  - create_issue
  - label_issue
  - close_issue
  - delete_branch
---

# Security-focused agent with minimal tools
---
name: security-scanner
tools:
  - read_issue
  - comment_on_issue
  - submit_pr_review
  - escalate_to_human
---
```

## Best Practices

### Agent Design
1. **Single responsibility**: Each agent should have a clear, focused purpose
2. **Minimal tools**: Only grant tools the agent actually needs
3. **Clear instructions**: Provide detailed system prompts with examples
4. **Error handling**: Include guidance for common failure scenarios

### Configuration Management
1. **Version control**: Keep all `.squadron/` files in version control
2. **Environment separation**: Use different configs for dev/staging/prod
3. **Secrets management**: Never commit API keys or sensitive data
4. **Documentation**: Document any customizations you make

### Security
1. **Principle of least privilege**: Agents should have minimal required permissions
2. **Review permissions**: Regularly audit what tools each agent can access
3. **Monitor usage**: Track agent actions and resource consumption
4. **Human oversight**: Ensure critical actions require human approval

## Testing Your Configuration

### Validate Configuration
```bash
# Test configuration validity
squadron validate-config --config .squadron/config.yaml

# Test agent definitions
squadron validate-agents --agents-dir .squadron/agents/
```

### Local Testing
```bash
# Run Squadron locally
squadron serve --repo-root . --config .squadron/config.yaml

# Test with sample issues
squadron test-workflow --issue-template examples/test-issues/feature.md
```

### Gradual Rollout
1. **Start with PM only**: Test issue triaging first
2. **Add one dev agent**: Test feature development workflow
3. **Add review agents**: Test complete PR lifecycle
4. **Scale up**: Add more specialized agents as needed

## Troubleshooting

### Common Issues

**Agent not triggering:**
```yaml
# Check trigger configuration
agents:
  my-agent:
    triggers:
      - issue_labeled: ["correct-label-name"]  # Must match exactly
```

**Tool permission errors:**
```yaml
# Verify tool is available
---
tools:
  - open_pr  # Tool name must match exactly
---
```

**Configuration syntax errors:**
```bash
# Validate YAML syntax
squadron validate-config .squadron/config.yaml
```

### Debug Mode
```bash
# Run with verbose logging
LOG_LEVEL=DEBUG squadron serve --repo-root .

# Test specific agent
squadron test-agent --agent feat-dev --issue 123
```

For more troubleshooting help, see [docs/troubleshooting.md](../docs/troubleshooting.md).

## Next Steps

1. **Copy configuration** to your repository
2. **Customize** for your project needs
3. **Test locally** before deploying
4. **Deploy to production** using [deployment guide](../deploy/README.md)
5. **Monitor and iterate** based on usage patterns

For complete setup instructions, see [Getting Started Guide](../docs/getting-started.md).
