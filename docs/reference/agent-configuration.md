# Agent Configuration Reference

Squadron agents are defined using Markdown files with YAML frontmatter in `.squadron/agents/`. This document provides a comprehensive guide to all configuration options.

## File Structure

Agent definitions are stored in `.squadron/agents/`:

```
.squadron/
├── config.yaml              # Project configuration
└── agents/                  # Agent definitions
    ├── pm.md                # Project manager agent
    ├── feat-dev.md          # Feature development agent
    ├── bug-fix.md           # Bug fix agent
    ├── docs-dev.md          # Documentation agent
    ├── infra-dev.md         # Infrastructure agent
    ├── pr-review.md         # PR review agent
    ├── security-review.md   # Security review agent
    └── test-coverage.md     # Test coverage review agent
```

## Agent Definition Format

Each agent is defined in a Markdown file with YAML frontmatter:

```yaml
---
name: feat-dev
display_name: Feature Developer
emoji: "👨‍💻"
description: >
  Implements new features by writing code, tests, and opening pull requests.
infer: true
tools:
  - read_file
  - write_file
  - bash
  - git_push
  - open_pr
  - check_for_events
  - report_complete
skills:
  - squadron-internals
  - squadron-dev-guide
---

# Agent System Prompt

The markdown content becomes the agent's system prompt...
```

## YAML Frontmatter Fields

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier (must match filename without `.md`) |
| `description` | string | Brief description of the agent's purpose |
| `tools` | list | Tools the agent can access (see [Tools Reference](tools.md)) |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `display_name` | string | Same as `name` | Human-readable name shown in logs and comments |
| `emoji` | string | None | Agent signature emoji shown in issue/PR comments |
| `infer` | boolean | `true` | Whether the SDK should infer missing context from the prompt |
| `skills` | list | `[]` | Skill directories to append to the system prompt |
| `circuit_breaker` | object | (see defaults) | Override circuit breaker limits |
| `lifecycle` | string | `persistent` | Agent lifecycle type: `ephemeral` or `persistent` |
| `mcp_servers` | object | `{}` | MCP server definitions for additional capabilities |

### `display_name` and `emoji`

```yaml
display_name: Feature Developer
emoji: "👨‍💻"
```

These appear in agent-signed comments. Example output:
```
👨‍💻 **Feature Developer**

Starting implementation of #42...
```

### `infer`

```yaml
infer: true  # Default
```

When `true`, the Copilot SDK attempts to infer missing context from the system prompt. Generally leave this as `true` unless you have a specific reason to disable it.

### `skills`

```yaml
skills:
  - squadron-internals   # Knowledge of Squadron internals
  - squadron-dev-guide   # Development patterns and conventions
```

Skill names reference entries in `skills.definitions` in `config.yaml`. Each skill is a directory of Markdown files that are appended to the agent's system prompt. This gives agents knowledge about the project without repeating it in each agent definition.

### `circuit_breaker`

```yaml
circuit_breaker:
  max_turns: 50          # Maximum conversation turns
  max_tool_calls: 100    # Maximum tool invocations
  max_duration: 3600     # Maximum runtime in seconds
```

These override the defaults set in `config.yaml`. See [Configuration Reference](../configuration.md#circuit_breakers--resource-limits).

### `lifecycle`

```yaml
lifecycle: persistent  # or ephemeral
```

See [Agent Lifecycle Types](#agent-lifecycle-types) below.

### `mcp_servers`

```yaml
mcp_servers:
  github:
    type: http
    url: https://api.githubcopilot.com/mcp/
```

MCP (Model Context Protocol) server definitions for additional tool capabilities beyond Squadron's built-in tools.

---

## Agent Lifecycle Types

### Ephemeral Agents

```yaml
lifecycle: ephemeral
```

- Run once per triggering event
- Process their task and terminate
- No git worktree — all operations are in-memory or via API
- Ideal for: PM triaging, one-time analysis
- **Example:** `pm` agent — triages one issue and exits

### Persistent Agents

```yaml
lifecycle: persistent  # Default
```

- Can sleep when blocked and wake when blockers resolve
- Maintain state (git worktree, session context) across wake/sleep cycles
- Use `report_blocked` to sleep, wake when the blocker issue resolves
- Use `report_complete` when done
- Ideal for: Development work, ongoing reviews
- **Example:** `feat-dev`, `bug-fix`, `pr-review`

**Sleep/wake lifecycle:**
```
CREATED → ACTIVE → (report_blocked) → SLEEPING
                                          │
                                   (blocker resolved)
                                          │
                 ACTIVE ←── (woken up) ──┘
                    │
             (report_complete)
                    │
                COMPLETED
```

---

## Tool Selection

Tools are listed in the `tools:` frontmatter field. There are two categories of tools:

### Squadron Tools (Custom Tools)

Squadron-specific tools for GitHub operations and agent lifecycle. Examples:
- `read_issue`, `comment_on_issue`, `label_issue`
- `open_pr`, `get_pr_details`, `submit_pr_review`
- `check_for_events`, `report_blocked`, `report_complete`
- `check_registry`, `get_recent_history`, `list_agent_roles`

### SDK Built-in Tools

Tools provided by the Copilot SDK for file and shell operations:
- `read_file`, `write_file` — file I/O
- `bash` — shell command execution
- `git` — git operations
- `grep` — code search

### Tool Selection by Agent Type

**PM Agent (coordination only):**
```yaml
tools:
  - create_issue
  - read_issue
  - update_issue
  - close_issue
  - assign_issue
  - label_issue
  - list_issues
  - list_issue_comments
  - list_pull_requests
  - check_registry
  - get_recent_history
  - list_agent_roles
  - comment_on_issue
```

**Development Agents (feat-dev, bug-fix, docs-dev, infra-dev):**
```yaml
tools:
  # File + shell operations
  - read_file
  - write_file
  - grep
  - bash
  - git
  - git_push
  # Issue context
  - read_issue
  - list_issue_comments
  # PR operations
  - open_pr
  - get_pr_details
  - get_pr_feedback
  - list_pr_files
  - list_pr_reviews
  - get_review_details
  - get_pr_review_status
  # Communication
  - reply_to_review_comment
  - comment_on_pr
  - comment_on_issue
  # Lifecycle
  - check_for_events
  - report_blocked
  - report_complete
  - create_blocker_issue
```

**Review Agents (pr-review, security-review, test-coverage):**
```yaml
tools:
  # File reading (no write)
  - read_file
  - grep
  # PR context
  - list_pr_files
  - get_pr_details
  - get_pr_feedback
  - get_ci_status
  - list_pr_reviews
  - get_review_details
  - get_pr_review_status
  - list_requested_reviewers
  # Review actions
  - add_pr_line_comment
  - reply_to_review_comment
  - comment_on_pr
  - submit_pr_review
  # Lifecycle
  - check_for_events
  - report_complete
```

> **Principle of least privilege:** Only grant tools the agent actually needs. Review agents don't need `git_push`. PM agents don't need `open_pr`. Less access = reduced attack surface.

---

## Default Circuit Breaker Limits

| Limit | Default | Description |
|-------|---------|-------------|
| `max_turns` | 50 | Maximum conversation turns per session |
| `max_tool_calls` | 200 | Maximum tool invocations per session |
| `max_active_duration` | 7200 | Maximum runtime in seconds (2 hours) |
| `max_iterations` | 5 | Maximum sleep→wake cycles (persistent agents) |

**Typical overrides by agent type:**

| Agent Type | max_turns | max_tool_calls | max_active_duration |
|------------|-----------|----------------|---------------------|
| PM (ephemeral) | 15 | 25 | 300 (5 min) |
| Dev agents | 50 | 200 | 7200 (2 hrs) |
| Review agents | 25 | 50 | 1800 (30 min) |

---

## System Prompt Guidelines

The Markdown content (below the frontmatter) becomes the agent's system prompt. Write it carefully — it directly controls agent behavior.

### Recommended Structure

```markdown
# Agent Name

You are the [role] agent for the {project_name} project. [One sentence describing role.]
You operate under the identity `squadron[bot]`.

## Your Task

You have been assigned issue #{issue_number}: **{issue_title}**

Issue description:
{issue_body}

## Workflow

Follow this process precisely:

1. **Step 1** — [what to do]
2. **Step 2** — [what to do]
...

## Guidelines

- [Quality standard]
- [Constraint]
- When to use `report_blocked`
- When to use `report_complete`
```

### Template Variables

The following placeholders are replaced when the agent is invoked:

| Variable | Description |
|----------|-------------|
| `{project_name}` | Project name from `config.yaml` |
| `{issue_number}` | GitHub issue number |
| `{issue_title}` | GitHub issue title |
| `{issue_body}` | GitHub issue body |
| `{branch_name}` | Agent's assigned branch name |
| `{base_branch}` | Base branch (from `config.yaml`) |
| `{pr_number}` | PR number (for review agents) |
| `{max_iterations}` | Circuit breaker max iterations |
| `{max_tool_calls}` | Circuit breaker max tool calls |

---

## Complete Example

```yaml
---
name: feat-dev
display_name: Feature Developer
emoji: "👨‍💻"
description: >
  Implements new features by writing code, tests, and opening pull requests.
infer: true
tools:
  - read_file
  - write_file
  - grep
  - bash
  - git
  - git_push
  - read_issue
  - list_issue_comments
  - open_pr
  - get_pr_details
  - get_pr_feedback
  - list_pr_files
  - list_pr_reviews
  - get_review_details
  - get_pr_review_status
  - reply_to_review_comment
  - comment_on_pr
  - comment_on_issue
  - check_for_events
  - report_blocked
  - report_complete
  - create_blocker_issue
skills:
  - squadron-internals
  - squadron-dev-guide
---

# Feature Development Agent

You are a **Feature Development agent** for the {project_name} project.
You operate under the identity `squadron[bot]`.

## Your Task

You have been assigned issue #{issue_number}: **{issue_title}**

Issue description:
{issue_body}

## Workflow

1. **Understand** — Read the issue carefully.
2. **Explore** — Read relevant codebase files.
3. **Plan** — Outline files to create/modify and tests to write.
4. **Branch** — Your branch is `{branch_name}`, branching from `{base_branch}`.
5. **Implement** — Write code and tests.
6. **Open PR** — Reference `Fixes #{issue_number}`.
7. **Address feedback** — Respond to review comments.
8. **Complete** — Call `report_complete` after merge.

## Guidelines

- Follow existing code style
- Include tests for all new functionality
- Use `report_blocked` if you need human input
- Use `report_complete` when your PR is merged
```

---

## Validation

Squadron validates agent configurations at startup:

- YAML frontmatter must be valid YAML
- `name` must match the filename (without `.md`)
- All listed `tools` must be registered in the tool registry
- Agent names must be unique
- Required fields (`name`, `tools`) must be present

Invalid configurations will prevent the server from starting, with detailed error messages indicating the specific problem.
