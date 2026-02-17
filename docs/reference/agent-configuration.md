# Agent Configuration Reference

Squadron agents are defined using Markdown files with YAML frontmatter. This document provides a comprehensive guide to configuring agents for your project.

## File Structure

Agent definitions are stored in `.squadron/agents/` directory:

```
.squadron/
├── config.yaml          # Project configuration
└── agents/              # Agent definitions
    ├── pm.md            # Project manager agent
    ├── feat-dev.md      # Feature development agent
    ├── bug-fix.md       # Bug fix agent
    ├── pr-review.md     # PR review agent
    └── security-review.md # Security review agent
```

## Agent Definition Format

Each agent is defined in a Markdown file with YAML frontmatter:

```yaml
---
name: agent-name
description: Brief description of the agent's role
tools:
  - tool1
  - tool2
  - tool3
circuit_breaker:
  max_turns: 50
  max_tool_calls: 100
  max_duration: 3600
lifecycle: ephemeral  # or persistent
---

# Agent System Prompt

The markdown content becomes the agent's system prompt.
Use this to define:

## Your Role
What the agent is responsible for...

## Your Process
Step-by-step workflow...

## Guidelines
Best practices and constraints...
```

## YAML Frontmatter Fields

### Required Fields

- **`name`** (string): Unique identifier for the agent (must match filename without .md)
- **`description`** (string): Brief description of the agent's purpose
- **`tools`** (list): Array of tool names the agent can access

### Optional Fields

- **`circuit_breaker`** (object): Override default circuit breaker limits
  - `max_turns`: Maximum conversation turns (default: varies by agent type)
  - `max_tool_calls`: Maximum tool invocations (default: varies by agent type)  
  - `max_duration`: Maximum runtime in seconds (default: varies by agent type)
- **`lifecycle`** (string): Agent lifecycle type
  - `ephemeral`: Agent runs once and terminates (default for PM)
  - `persistent`: Agent can be resumed after sleeping (default for dev/review agents)

## Agent Lifecycle Types

### Ephemeral Agents
- Run once per triggering event
- Process queued commands and terminate
- Ideal for: PM triaging, one-time analysis tasks
- Examples: `pm`, quick review agents

### Persistent Agents  
- Can sleep when blocked and wake when blockers resolve
- Maintain state across wake/sleep cycles
- Ideal for: Development work, ongoing reviews
- Examples: `feat-dev`, `bug-fix`, `pr-review`

## Tool Selection Guidelines

### PM Agent Tools
```yaml
tools:
  - read_issue
  - label_issue  
  - assign_issue
  - create_issue
  - comment_on_issue
  - check_registry
  - escalate_to_human
  - get_recent_history
  - list_agent_roles
  - list_issues
  - list_issue_comments
```

### Development Agent Tools
```yaml
tools:
  - read_issue
  - comment_on_issue
  - open_pr
  - git_push
  - check_for_events
  - report_blocked
  - report_complete
  - get_pr_feedback
  - list_issue_comments
```

### Review Agent Tools
```yaml
tools:
  - get_pr_details
  - get_pr_feedback
  - list_pr_files
  - submit_pr_review
  - comment_on_issue
  - check_for_events
  - report_complete
```

## System Prompt Best Practices

### Structure Your Prompt

1. **Role Definition**: Clearly state what the agent is responsible for
2. **Process Description**: Step-by-step workflow the agent should follow
3. **Guidelines**: Constraints, best practices, and quality standards
4. **Context Awareness**: How to use introspection tools to understand state

### Key Sections to Include

```markdown
# Agent Name

You are the [role] agent for the {project_name} repository.

## Your Responsibilities
- Primary responsibility 1
- Primary responsibility 2
- Escalation criteria

## Your Process
1. Step 1: Use `tool_name` to...
2. Step 2: Analyze the results...
3. Step 3: Take action by...

## Guidelines
- Quality standard 1
- Quality standard 2
- When to escalate
```

### Template Variables

Available in system prompts:
- `{project_name}`: Project name from config
- `{agent_id}`: Current agent's unique identifier
- `{issue_number}`: Issue number (for issue-triggered agents)
- `{pr_number}`: PR number (for PR-triggered agents)

## Circuit Breaker Configuration

Circuit breakers prevent runaway agents by enforcing limits:

```yaml
circuit_breaker:
  max_turns: 25          # Maximum conversation turns
  max_tool_calls: 50     # Maximum tool invocations
  max_duration: 1800     # Maximum runtime (seconds)
```

### Default Limits by Agent Type

**PM Agent (ephemeral):**
- max_turns: 15
- max_tool_calls: 25
- max_duration: 300 (5 minutes)

**Development Agents (persistent):**
- max_turns: 50
- max_tool_calls: 100
- max_duration: 3600 (1 hour)

**Review Agents (persistent):**
- max_turns: 25
- max_tool_calls: 50
- max_duration: 1800 (30 minutes)

## Complete Example

```yaml
---
name: feat-dev
description: Feature development agent that implements new functionality
tools:
  - read_issue
  - comment_on_issue
  - open_pr
  - git_push
  - check_for_events
  - report_blocked
  - report_complete
  - create_blocker_issue
  - get_pr_feedback
  - list_issue_comments
circuit_breaker:
  max_turns: 50
  max_tool_calls: 100
  max_duration: 3600
lifecycle: persistent
---

# Feature Development Agent

You are the feature development agent for the {project_name} repository.

## Your Responsibilities

You implement new features by writing code, tests, and documentation.

## Your Process

1. **Analyze the Issue**: Use `read_issue` to understand requirements
2. **Plan Implementation**: Break down the work into steps
3. **Write Code**: Implement the feature with proper testing
4. **Create PR**: Use `open_pr` to submit your changes
5. **Handle Feedback**: Use `get_pr_feedback` to address review comments

## Guidelines

- Always include tests for new functionality
- Update documentation for user-facing changes  
- Follow existing code patterns and style
- Use `report_blocked` if you need human input
- Use `report_complete` when work is done
```

## Validation

Squadron validates agent configurations at startup:
- YAML frontmatter must be valid
- All referenced tools must exist
- Agent names must be unique
- Required fields must be present

Invalid configurations will prevent the server from starting with detailed error messages.
