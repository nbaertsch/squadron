# Squadron Configuration Examples

This directory contains example Squadron configurations that you can copy into your repository and customize.

## Quick Setup

Copy the `.squadron/` directory to your repository root:

```bash
# From your repository root
cp -r /path/to/squadron/examples/.squadron .
```

Then edit `.squadron/config.yaml` with your project details:

```yaml
project:
  name: "YOUR-PROJECT-NAME"    # Change this
  owner: "YOUR-GITHUB-ORG"     # Change this
  repo: "YOUR-REPO-NAME"       # Change this

human_groups:
  maintainers: ["YOUR-USERNAME"]  # Change this
```

## Configuration Files

### `.squadron/config.yaml`

Main project configuration containing:
- **Project metadata** (name, owner, repo, default branch)
- **Human groups** (maintainers for escalations)
- **Label taxonomy** (types, priorities, states)
- **Branch naming conventions**
- **Circuit breaker defaults**

### `.squadron/agents/`

Agent definitions using Markdown with YAML frontmatter:

| File | Agent | Role |
|------|-------|------|
| `pm.md` | Project Manager | Triages issues, applies labels |
| `feat-dev.md` | Feature Developer | Implements new features |
| `bug-fix.md` | Bug Fix Agent | Diagnoses and fixes bugs |
| `pr-review.md` | PR Reviewer | Reviews code quality |
| `security-review.md` | Security Reviewer | Reviews for vulnerabilities |

## Label → Agent Trigger Mapping

| Label applied to issue | Agent spawned |
|-----------------------|---------------|
| `feature` | `feat-dev` |
| `bug` | `bug-fix` |
| `security` | `security-review` |
| `documentation` | `docs-dev` |
| `infrastructure` | Use `@squadron-dev infra-dev` mention |

## Customization Examples

### Adding a Documentation Agent

Create `.squadron/agents/docs-dev.md`:

```yaml
---
name: docs-dev
display_name: Documentation Developer
emoji: "📝"
description: Writes and updates documentation
tools:
  - read_file
  - write_file
  - bash
  - git_push
  - open_pr
  - read_issue
  - comment_on_issue
  - check_for_events
  - report_complete
lifecycle: persistent
---

# Documentation Agent

You are a Documentation Developer agent for the {project_name} project.

## Your Task

You have been assigned issue #{issue_number}: **{issue_title}**

Issue description:
{issue_body}

## Workflow

1. Read the issue to understand what documentation needs updating
2. Review existing documentation and the codebase
3. Create your branch: `{branch_name}` from `{base_branch}`
4. Write clear, accurate documentation with examples
5. Open a PR referencing `Fixes #{issue_number}`
6. Address review feedback
7. Call `report_complete` after the PR merges
```

Then add the trigger in `config.yaml`:

```yaml
agent_roles:
  docs-dev:
    triggers:
      - issue_labeled: "documentation"
    lifecycle: persistent
```

### Custom Labels and Priorities

```yaml
# .squadron/config.yaml
labels:
  types: [feature, bug, enhancement, task]
  priorities: [p0, p1, p2, p3]
  states: [needs-triage, in-progress, blocked, review]
```

### Circuit Breaker Tuning

```yaml
# Stricter limits for development/testing
circuit_breakers:
  defaults:
    max_turns: 20
    max_tool_calls: 50
    max_active_duration: 1800  # 30 minutes

# Production defaults
circuit_breakers:
  defaults:
    max_turns: 50
    max_tool_calls: 200
    max_active_duration: 7200  # 2 hours
```

## Best Practices

### Agent Design

1. **Single responsibility**: Each agent should have a clear, focused purpose
2. **Minimal tools**: Only grant tools the agent actually needs
3. **Clear system prompt**: Include step-by-step workflow and quality guidelines
4. **Error guidance**: Tell agents when to use `report_blocked` vs. `report_complete`

### Configuration Management

1. **Version control**: Commit all `.squadron/` files
2. **Secret separation**: Never put API keys in config files
3. **Test incrementally**: Add one agent at a time when customizing

### Security

1. **Principle of least privilege**: Agents get only what they need
2. **Review periodically**: Audit tool grants regularly
3. **Human oversight**: Keep `needs-human` escalation path clear

## Troubleshooting

### Agent not triggering

Verify the trigger label matches exactly what's in your GitHub repo:

```yaml
agent_roles:
  feat-dev:
    triggers:
      - issue_labeled: "feature"  # Must match the label name exactly
```

### Tool permission errors

Verify the tool name is spelled correctly in frontmatter:

```yaml
tools:
  - open_pr    # Correct
  - open-pr    # Wrong — use underscores
```

For more troubleshooting, see [docs/troubleshooting.md](../docs/troubleshooting.md).

## Next Steps

1. Copy configuration to your repository
2. Customize for your project needs
3. Deploy using the [deployment guide](../deploy/README.md)
4. Monitor activity via the [observability dashboard](../docs/observability.md)
