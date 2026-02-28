# Squadron Configuration Reference

This document describes all configuration options for `config.yaml` — the project-level configuration file for Squadron.

## Overview

Squadron uses two separate configuration artifacts:

| File | Purpose |
|------|---------|
| `.squadron/config.yaml` | **Project configuration** — triggers, workflows, labels, human groups, circuit breakers |
| `.squadron/agents/*.md` | **Agent definitions** — per-agent prompts, tools, and lifecycle settings |

This document covers `config.yaml`. See [Agent Configuration Reference](reference/agent-configuration.md) for agent definition files.

---

## Minimal Configuration

The minimum required configuration:

```yaml
# .squadron/config.yaml
project:
  name: "my-project"
  owner: "my-github-org"
  repo: "my-repo"
  default_branch: main

human_groups:
  maintainers: ["alice"]   # REQUIRED — controls event access and escalation
```

---

## Full Configuration Reference

### `project` — Project Metadata

```yaml
project:
  name: "my-project"        # REQUIRED: Project name (used in agent prompts)
  owner: "my-github-org"    # REQUIRED: GitHub org or username
  repo: "my-repo"           # REQUIRED: Repository name
  default_branch: main      # REQUIRED: Default branch (agents branch from this)
  bot_username: "squadron-dev[bot]"  # Optional: GitHub App bot username for self-event filtering
```

**Notes:**
- `bot_username` must match the `[bot]` suffix added by GitHub Apps. Used to filter self-triggered events (prevents infinite loops).
- `name` is injected into agent system prompts as `{project_name}`.

---

### `human_groups` — Human Contact Groups

Named groups of GitHub usernames used for escalation, review assignment, and — most
importantly — **event access control**.

```yaml
human_groups:
  maintainers: ["alice", "bob"]   # REQUIRED — authorized event actors + escalation targets
  reviewers: ["charlie", "diana"] # Optional — can be used in review assignments
```

**The `maintainers` group is required.** It serves two purposes:

1. **Event gate (security):** Only users listed in `human_groups.maintainers` can trigger
   Squadron system events (agent spawning, PM triage, command routing, label-triggered
   workflows). Events from all other users are **silently dropped**.
2. **Escalation target:** When agents encounter issues requiring human judgment, they
   escalate to the `maintainers` group.

**Security model:**

- If a GitHub webhook event is sent by a user **not** in `human_groups.maintainers`, it is
  **silently dropped** — no agent is spawned, no comment is posted, no side effects occur.
- The Squadron bot identity (configured as `project.bot_username`, default:
  `squadron-dev[bot]`) is **always permitted** regardless of this list. This prevents
  self-blocking on bot-generated events (e.g. when the PM labels an issue to trigger a
  feature-dev agent).
- An **empty or missing** `maintainers` group means no human-originated events are
  processed. A warning is logged at startup in this case.

**Notes:**

- Usernames are GitHub handles (without `@`), matched case-insensitively.
- Dropped events are logged at `INFO` level including: actor login, event type, and issue/PR number.
- At least one `maintainers` entry is required for Squadron to process human events.
- This is a flat allowlist — role-based access control (restricting which agents a
  maintainer can trigger) is a future enhancement.

---


### `labels` — Label Taxonomy

Defines the label categories Squadron uses:

```yaml
labels:
  types:
    - feature       # New functionality
    - bug           # Something is broken
    - security      # Security vulnerability or concern
    - documentation # Documentation update
    - infrastructure # CI/CD, tooling, deployment
  
  priorities:
    - critical  # Blocks work or affects production
    - high      # Important, address soon
    - medium    # Standard priority
    - low       # Nice to have
  
  states:
    - needs-triage        # New, not yet classified
    - in-progress         # Agent working on it
    - blocked             # Waiting on dependency
    - needs-human         # Requires human judgment
    - needs-clarification # Unclear requirements
```

**Notes:**
- Label names must match exactly what exists in your GitHub repository.
- Type labels are the primary trigger mechanism: applying a type label spawns the matching agent.
- Create these labels in your GitHub repo before using Squadron.

---

### `branch_naming` — Branch Name Templates

Controls how agents name their branches:

```yaml
branch_naming:
  feature: "feat/issue-{issue_number}"
  bugfix: "fix/issue-{issue_number}"
  security: "security/issue-{issue_number}"
  docs: "docs/issue-{issue_number}"
  infra: "infra/issue-{issue_number}"
```

**Template variables:**
- `{issue_number}` — GitHub issue number

---

### `circuit_breakers` — Resource Limits

Prevents runaway agents by enforcing hard limits:

```yaml
circuit_breakers:
  defaults:
    max_turns: 50              # Maximum conversation turns per agent session
    max_tool_calls: 200        # Maximum tool invocations per agent session
    max_active_duration: 7200  # Maximum runtime in seconds (2 hours)
    max_iterations: 5          # Maximum sleep→wake cycles for persistent agents
  
  # Per-role overrides (optional)
  roles:
    pm:
      max_turns: 15
      max_tool_calls: 25
      max_active_duration: 300   # 5 minutes
    pr-review:
      max_turns: 25
      max_tool_calls: 50
      max_active_duration: 1800  # 30 minutes
```

**Notes:**
- `max_iterations` limits how many times a persistent agent can sleep and wake. After reaching the limit, the agent is escalated to humans.
- Individual agents can override these settings in their frontmatter (see [Agent Configuration Reference](reference/agent-configuration.md)).

---

### `agent_roles` — Agent Role Configuration

Defines which agents exist and how they're triggered:

```yaml
agent_roles:
  feat-dev:
    triggers:
      - issue_labeled: "feature"
    lifecycle: persistent
  
  bug-fix:
    triggers:
      - issue_labeled: "bug"
    lifecycle: persistent
  
  security-review:
    triggers:
      - issue_labeled: "security"
      - pr_labeled: "security"
    lifecycle: persistent
  
  docs-dev:
    triggers:
      - issue_labeled: "documentation"
    lifecycle: persistent
  
  pr-review:
    triggers:
      - pr_opened
    lifecycle: persistent
  
  pm:
    triggers:
      - issue_opened
      - issue_edited
      - command  # @squadron-dev pm mention
    lifecycle: ephemeral
```

**Trigger types:**
- `issue_labeled: "label-name"` — fires when label is applied to an issue
- `pr_labeled: "label-name"` — fires when label is applied to a PR
- `pr_opened` — fires when a PR is opened
- `issue_opened` — fires when an issue is opened
- `issue_edited` — fires when an issue body/title is edited
- `command` — fires when an `@squadron-dev {role}` mention is detected

---

### `review_policy` — PR Approval Requirements

Controls how many approvals are needed before PRs can merge:

```yaml
review_policy:
  required_approvals: 1        # Number of approvals needed
  require_human_approval: false # Whether a human must be among the approvers
  auto_merge: true             # Auto-merge when approval threshold is met
```

---

### `skills` — Skill Definitions

Skills are collections of Markdown files injected into agent system prompts:

```yaml
skills:
  definitions:
    squadron-internals:
      description: "Internal Squadron codebase knowledge"
      path: ".squadron/skills/squadron-internals/"
    
    squadron-dev-guide:
      description: "Development patterns and conventions"
      path: ".squadron/skills/squadron-dev-guide/"
```

**Notes:**
- Skills are assigned to agents via the `skills:` field in agent frontmatter.
- The files in the skill directory are concatenated and appended to the agent's system prompt.
- This is how agents gain knowledge about the project's internals.

---

## Complete Example

```yaml
# .squadron/config.yaml
project:
  name: "my-awesome-project"
  owner: "my-github-org"
  repo: "my-repo"
  default_branch: main
  bot_username: "squadron-my-repo[bot]"

human_groups:
  maintainers: ["alice", "bob"]
  reviewers: ["charlie"]

labels:
  types: [feature, bug, security, documentation, infrastructure]
  priorities: [critical, high, medium, low]
  states: [needs-triage, in-progress, blocked, needs-human, needs-clarification]

branch_naming:
  feature: "feat/issue-{issue_number}"
  bugfix: "fix/issue-{issue_number}"
  security: "security/issue-{issue_number}"
  docs: "docs/issue-{issue_number}"
  infra: "infra/issue-{issue_number}"

circuit_breakers:
  defaults:
    max_turns: 50
    max_tool_calls: 200
    max_active_duration: 7200
    max_iterations: 5

review_policy:
  required_approvals: 1
  require_human_approval: false
  auto_merge: true
```

---

## Environment Variables

Squadron requires several environment variables. These are set as GitHub Actions secrets (for the deployment workflow) or in a local `.env` file.

### For the Deployed Service

```bash
# GitHub App credentials
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIIEvQ...
-----END PRIVATE KEY-----"
GITHUB_INSTALLATION_ID=78901234
GITHUB_WEBHOOK_SECRET=your-random-secret

# Copilot SDK authentication
# Fine-grained PAT from a Copilot-licensed GitHub account
COPILOT_GITHUB_TOKEN=github_pat_...

# Optional: Repository URL (for container to clone at startup)
SQUADRON_REPO_URL=https://github.com/your-org/your-repo
```

### For Local Development

Copy `.env.example` to `.env` and fill in:

```bash
# GitHub App credentials (dev app)
SQ_APP_ID_DEV=123456
SQ_APP_CLIENT_ID_DEV=Iv1.abc...
SQ_APP_CLIENT_SECRET_DEV=abc...
SQ_INSTALLATION_ID_DEV=78901234

# Private key — use the file path for local dev
SQ_APP_PRIVATE_KEY_FILE=squadron-dev.2026-01-01.private-key.pem

# Copilot SDK auth (optional locally if using `copilot auth login`)
# COPILOT_GITHUB_TOKEN=github_pat_...

# E2E test target
E2E_TEST_OWNER=your-github-username
E2E_TEST_REPO=squadron-e2e-test
```

> **Security:** Never commit `.env` or `.pem` files. Both are in `.gitignore`.
