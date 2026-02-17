# Squadron Agent Collaboration Guide

This document describes how Squadron agents collaborate using the @ mention system for coordinated multi-agent development.

## Overview

The Squadron framework includes multiple specialized agents that work together on complex issues. Agents can collaborate by using @ mentions to request help, delegate tasks, or coordinate work across domains.

## Agent Roster

| Agent | Role | Primary Responsibilities |
|-------|------|-------------------------|
| `@squadron-dev pm` | Project Manager | Issue triage, coordination, escalation |
| `@squadron-dev feat-dev` | Feature Developer | New feature implementation |
| `@squadron-dev bug-fix` | Bug Fix Specialist | Bug diagnosis and fixes |
| `@squadron-dev security-review` | Security Reviewer | Security analysis, vulnerability assessment |
| `@squadron-dev pr-review` | Pull Request Reviewer | Code quality review |
| `@squadron-dev docs-dev` | Documentation Developer | Documentation updates, API docs |
| `@squadron-dev infra-dev` | Infrastructure Developer | CI/CD, deployment, infrastructure |
| `@squadron-dev test-coverage` | Test Coverage Reviewer | Test adequacy, coverage analysis |

## Mention System Usage

### Proper Format

Always use the full mention format: `@squadron-dev {agent-role}`

**✅ Correct:**
- `@squadron-dev feat-dev`
- `@squadron-dev security-review` 
- `@squadron-dev pm`

**❌ Incorrect:**
- `@feat-dev` (missing prefix)
- `@squadron feat-dev` (missing hyphen)
- `@squadron-dev feature` (wrong role name)

### Best Practices

1. **Provide Context**: Include relevant background information
2. **Be Specific**: Clearly state what you need
3. **Reference Work**: Link to issues, PRs, files, or documentation
4. **Set Expectations**: Indicate priority and timeline if relevant
5. **Coordinate Dependencies**: Consider timing and dependencies

### Example Collaboration

```markdown
@squadron-dev security-review @squadron-dev feat-dev 

Security review needed for OAuth implementation in PR #45:

Security-review: Please assess:
- Token validation logic (src/auth/oauth.py:45-67)
- Session management (src/auth/session.py:120-145)  
- Rate limiting implementation (src/middleware/ratelimit.py)

Feat-dev: Please address any security findings before final approval.

Timeline: Security review by EOD, fixes by tomorrow.
```

## Common Collaboration Patterns

### 1. Security-First Development
- Feature development → Security review → Documentation
- Bug fixes → Security assessment → Deployment

### 2. Cross-Domain Coordination  
- Infrastructure changes → Security hardening → Documentation updates
- API changes → Security review → Test coverage → Documentation

### 3. Quality Assurance Pipeline
- Code implementation → Test coverage → Security review → PR review

### 4. Escalation and Coordination
- Complex issues → PM coordination → Multi-agent task delegation

## Agent-Specific Collaboration

### When Working on Features
- **Always mention** `@squadron-dev security-review` for authentication, authorization, or data handling
- **Consider mentioning** `@squadron-dev docs-dev` for user-facing features
- **Consider mentioning** `@squadron-dev infra-dev` for features requiring deployment changes

### When Fixing Bugs  
- **Always mention** `@squadron-dev security-review` for security-related bugs
- **Consider mentioning** `@squadron-dev docs-dev` if bugs reveal documentation issues
- **Consider mentioning** `@squadron-dev pm` for bugs affecting multiple components

### When Reviewing Code
- **Always mention** `@squadron-dev security-review` for security-sensitive changes
- **Always mention** `@squadron-dev test-coverage` when coverage is below standards  
- **Consider mentioning** `@squadron-dev infra-dev` for infrastructure-affecting changes

## Troubleshooting Mentions

If an agent doesn't respond to your mention:

1. **Check format**: Ensure correct `@squadron-dev {role}` format
2. **Wait appropriately**: Agents may be working on other high-priority tasks  
3. **Add context**: Agent may need more specific information
4. **Escalate if needed**: Mention `@squadron-dev pm` for persistent issues

## Configuration

Agent collaboration is enabled through:
- **Stateful agents**: Most agents use `lifecycle: stateful` for persistent context
- **Ephemeral agents**: PM and merge-conflict use `lifecycle: ephemeral` for task-specific work
- **Event triggers**: Agents wake for relevant events and mentions
- **Circuit breakers**: Prevent infinite loops and resource exhaustion

For configuration details, see `.squadron/config.yaml`.
