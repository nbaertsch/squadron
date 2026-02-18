# `.squadron/` Configuration Reference

**Date:** 2026-02-14  
**Relates to:** AD-006, AD-009, AD-015, AD-017, AD-018  
**Status:** Complete — canonical reference for all per-project configuration

---

## Directory Structure

```
.squadron/
├── config.yaml                     # Global project configuration
├── workflows/
│   └── approval-flows.yaml         # Approval flow rules (per-branch, per-path)
└── agents/
    ├── pm.md                       # PM agent definition
    ├── feat-dev.md                 # Feature development agent
    ├── bug-fix.md                  # Bug fix agent
    ├── pr-review.md                # PR review agent
    └── security-review.md          # Security review agent
```

All paths are relative to the repository root. Squadron reads this config on startup and on pushes to the default branch.

---

## `config.yaml` — Global Configuration

```yaml
# .squadron/config.yaml

# ─────────────────────────────────────────────
# Project Identity
# ─────────────────────────────────────────────
project:
  name: "my-project"                     # Used in agent system prompts and logging
  default_branch: main                   # Branch to read config from; target for most PRs

# ─────────────────────────────────────────────
# Label Taxonomy
# ─────────────────────────────────────────────
# Labels used by the PM for issue classification.
# Squadron creates these labels on install if they don't exist.
labels:
  types:
    - feature
    - bug
    - security
    - docs
    - infrastructure
  priorities:
    - critical
    - high
    - medium
    - low
  states:
    - needs-triage
    - in-progress
    - blocked
    - needs-human
    - needs-clarification

# ─────────────────────────────────────────────
# Branch Naming Conventions
# ─────────────────────────────────────────────
# Template for branch names created by agents.
# {issue_number} is replaced with the GitHub issue number.
branch_naming:
  feature: "feat/issue-{issue_number}"
  bugfix: "fix/issue-{issue_number}"
  security: "security/issue-{issue_number}"
  hotfix: "hotfix/issue-{issue_number}"

# ─────────────────────────────────────────────
# Human Groups
# ─────────────────────────────────────────────
# Named groups of humans, referenced in approval flows and escalation.
# Values are GitHub usernames (with @) or GitHub team slugs.
human_groups:
  maintainers: ["@alice", "@bob"]
  security-team: ["@charlie", "@dave"]

# ─────────────────────────────────────────────
# Agent Roles
# ─────────────────────────────────────────────
# Maps agent roles to their definitions and triggering conditions.
agent_roles:
  pm:
    agent_definition: agents/pm.md
    singleton: true                      # Only one PM per repo

  feat-dev:
    agent_definition: agents/feat-dev.md
    assignable_labels: [feature]         # PM assigns this role to issues with these labels

  bug-fix:
    agent_definition: agents/bug-fix.md
    assignable_labels: [bug]

  pr-review:
    agent_definition: agents/pr-review.md
    trigger: approval_flow               # Triggered by approval flow, not issue assignment

  security-review:
    agent_definition: agents/security-review.md
    trigger: approval_flow

# ─────────────────────────────────────────────
# Circuit Breakers
# ─────────────────────────────────────────────
# Hard limits on agent execution. Proxy metrics for cost control.
# See AD-018 and research/circuit-breakers.md for enforcement details.
circuit_breakers:
  defaults:
    max_iterations: 5                    # Test-fix retry cycles
    max_tool_calls: 200                  # Total tool invocations
    max_turns: 50                        # LLM conversation turns
    max_active_duration: 7200            # Seconds in ACTIVE state (2 hours)
    max_sleep_duration: 86400            # Seconds in SLEEPING state (24 hours)
    warning_threshold: 0.80              # Warn agent at 80% of any limit

  # Per-role overrides (merged with defaults)
  roles:
    pm:
      max_tool_calls: 50
      max_turns: 10
      max_active_duration: 600           # 10 minutes per event batch
    pr-review:
      max_tool_calls: 100
      max_turns: 20
      max_active_duration: 1800          # 30 minutes
    security-review:
      max_tool_calls: 100
      max_turns: 20
      max_active_duration: 1800          # 30 minutes

# ─────────────────────────────────────────────
# Runtime / LLM Configuration
# ─────────────────────────────────────────────
runtime:
  # Default model for all agents (can be overridden per-role)
  default_model: "claude-sonnet-4.6"
  default_reasoning_effort: "medium"     # low | medium | high | xhigh

  # Per-role model overrides
  models:
    pm:
      model: "claude-sonnet-4.6"           # Fast triage
      reasoning_effort: "low"
    feat-dev:
      model: "claude-sonnet-4.6"
      reasoning_effort: "high"           # Complex implementation
    bug-fix:
      model: "claude-sonnet-4.6"
      reasoning_effort: "high"
    pr-review:
      model: "claude-sonnet-4.6"
      reasoning_effort: "medium"
    security-review:
      model: "claude-sonnet-4.6"
      reasoning_effort: "high"           # Security needs careful reasoning

  # BYOK provider configuration
  # API keys should be in environment variables, not in this file
  provider:
    type: "anthropic"                    # openai | anthropic | azure
    base_url: "https://api.anthropic.com"
    api_key_env: "ANTHROPIC_API_KEY"     # Environment variable name

  # Reconciliation loop interval
  reconciliation_interval: 300           # Seconds (5 minutes)

# ─────────────────────────────────────────────
# Escalation / Notifications
# ─────────────────────────────────────────────
escalation:
  # Default human group to notify when agents escalate
  default_notify: maintainers

  # Labels applied to escalation issues
  escalation_labels:
    - needs-human
    - escalation

  # Maximum issue creation depth (EC-010 mitigation)
  max_issue_depth: 3
```

### Field Reference

| Section | Field | Type | Required | Description |
|---|---|---|---|---|
| `project` | `name` | string | yes | Project name for agent context |
| `project` | `default_branch` | string | no | Default: `main` |
| `labels` | `types` | string[] | yes | Issue type labels |
| `labels` | `priorities` | string[] | yes | Priority labels |
| `labels` | `states` | string[] | yes | Workflow state labels |
| `branch_naming` | `feature` | string | no | Template with `{issue_number}` |
| `human_groups` | `{name}` | string[] | yes (≥1 group) | GitHub usernames or team slugs |
| `agent_roles` | `{role}.agent_definition` | string | yes | Path to agent definition file |
| `agent_roles` | `{role}.assignable_labels` | string[] | conditional | Labels that trigger assignment (for dev roles) |
| `agent_roles` | `{role}.trigger` | string | conditional | `approval_flow` for review roles |
| `agent_roles` | `{role}.singleton` | bool | no | Single instance per repo (PM only) |
| `circuit_breakers` | `defaults.*` | various | yes | Global limit defaults |
| `circuit_breakers` | `roles.{role}.*` | various | no | Per-role overrides |
| `runtime` | `default_model` | string | yes | LLM model identifier |
| `runtime` | `provider` | object | yes | BYOK provider configuration |
| `escalation` | `default_notify` | string | yes | Human group for escalations |

---

## `workflows/approval-flows.yaml` — Approval Flows

This file is fully specified in [Approval Flow Schema Research](research/approval-flow-schema.md) (AD-015). Summary of schema:

```yaml
# .squadron/workflows/approval-flows.yaml

default:
  required_reviews:
    - role: agent:pr-review           # agent:{role} or human_group:{name}
      required: true
      auto_assign: true
      # min_approvals: 1              # Only for human_group

  merge_policy:
    auto_merge: false                 # true = auto-merge when all conditions met
    require_ci_pass: true
    require_all_reviews: true
    delete_branch: true
    merge_method: squash              # squash | merge | rebase

  required_status_checks:
    - context: "squadron/pr-review"   # Maps to GitHub required_status_checks
    - context: "ci/tests"

  protected_paths:
    - pattern: ".squadron/**"
      additional_review: human_group:maintainers
      reason: "Config changes require human approval"

  escalation:
    on_ci_failure:
      notify: [human_group:maintainers]
      action: comment
      max_retries: 2
    on_review_rejection:
      notify: [agent:pm]
      action: comment
    on_timeout:
      timeout_hours: 48
      notify: [human_group:maintainers]
      action: label
      label: needs-attention

# Branch-specific overrides (fnmatch patterns, first match wins)
branch_rules:
  - match: main
    # ... (more restrictive rules for main)
  - match: "feat/*"
    # ... (lighter rules for feature branches)
  - match: "fix/*"
    # ...
  - match: "hotfix/*"
    # ... (human-only review for hotfixes)
```

See [research/approval-flow-schema.md](research/approval-flow-schema.md) for the complete field reference, GitHub API mapping, and flow resolution algorithm.

---

## `agents/*.md` — Agent Definition Files

Each agent definition is a Markdown file containing structured sections that the framework parses to configure a Copilot SDK session.

### Format Specification

```markdown
# Agent: {role-name}

## System Prompt

{Complete system prompt sent to the LLM. This IS the agent's identity,
instructions, and behavioral constraints. Supports template variables:
- {project_name} — from config.yaml
- {issue_number} — assigned issue
- {issue_title} — issue title
- {issue_body} — issue body text}

## Tools

{List of tools available to this agent. Framework tools (check_for_events,
report_blocked, etc.) are always included. This section lists ADDITIONAL
tools — both built-in Copilot tools and custom project-specific tools.}

- `read_file` — Read file contents
- `write_file` — Create or modify files
- `bash` — Execute shell commands (restricted by role)
- `git` — Version control operations
- `pytest` — Run Python test suite

## Tool Restrictions

{Allowlist/denylist for shell command access. Interpreted by the
on_pre_tool_use hook in the framework.}

### Allowed Shell Commands
- git *
- pytest *
- npm test
- cargo test
- pip install

### Denied Shell Commands
- curl, wget (no network access)
- rm -rf / (no destructive operations)
- docker * (no container manipulation)

## Sub-Agents

{Lightweight sub-agents invoked within this agent's SDK session.
These share the parent's context and are defined inline.}

- `code-search`: Search the codebase for relevant patterns before editing
- `test-writer`: Generate test cases for implemented features

## Constraints

{Instructions the agent should follow to self-regulate. These are
prompt-level soft limits — the framework enforces hard limits via
circuit breakers (AD-018).}

- Max retry attempts before asking for help: {max_iterations from config}
- Max time budget: {max_active_duration from config}
- Must run tests before opening a PR
- Must not merge without required approvals
- Must check for events between major work phases

## Wake Protocol

{Instructions for what the agent should do when resumed from SLEEPING
state. Addresses EC-003 (stale context after long sleep).}

1. Pull latest changes from the base branch
2. Attempt rebase of feature branch
3. If rebase conflicts: attempt resolution, escalate if unable
4. Re-read files relevant to assigned issue
5. Review any new comments on assigned issue
6. Assess what changed while sleeping
7. Continue implementation with updated understanding
```

### Template Variables

| Variable | Scope | Description |
|---|---|---|
| `{project_name}` | All agents | From `config.yaml → project.name` |
| `{issue_number}` | Dev agents | Assigned issue number |
| `{issue_title}` | Dev agents | Assigned issue title |
| `{issue_body}` | Dev agents | Assigned issue body (full text) |
| `{pr_number}` | Review agents | PR being reviewed |
| `{pr_diff}` | Review agents | PR diff content (injected at review time) |
| `{branch_name}` | Dev agents | Agent's working branch |
| `{base_branch}` | All agents | Target branch (usually `main`) |

### Parsing

The framework parses agent definitions by extracting H2 sections:
- `## System Prompt` → `system_message.content` in Copilot SDK `create_session()`
- `## Tools` → `tools` list in session config
- `## Tool Restrictions` → `on_pre_tool_use` hook allowlist/denylist
- `## Sub-Agents` → `customAgents` in session config (if SDK supports)
- `## Constraints` → Appended to system prompt as behavioral instructions
- `## Wake Protocol` → Appended to resume prompt on `resume_session()`

---

## Config Loading & Validation

### Load Order

```
1. Read .squadron/config.yaml                    → global config
2. Read .squadron/workflows/approval-flows.yaml  → approval flow rules
3. For each role in config.agent_roles:
   a. Read .squadron/agents/{role}.md            → agent definitions
   b. Validate: file exists, has required sections
4. Cross-validate:
   a. All roles referenced in approval flows exist in agent_roles
   b. All human_groups referenced in approval flows exist in config
   c. All labels in assignable_labels match labels.types
   d. Branch naming templates contain {issue_number}
```

### Validation Errors (Fail-Fast on Startup)

| Error | Detected at | Severity |
|---|---|---|
| Missing `config.yaml` | Load | Fatal — cannot start |
| Missing agent definition file | Load | Fatal — role is configured but file missing |
| Unknown role in approval flow | Cross-validate | Fatal — approval flow references undefined role |
| Unknown human_group in approval flow | Cross-validate | Fatal — flow references undefined group |
| Missing system prompt section | Agent parse | Fatal — agent has no instructions |
| Invalid YAML syntax | Parse | Fatal |
| Missing required fields | Validate | Fatal |

### Config Reload

Config is re-read when a push event is received on the default branch that modifies any file in `.squadron/`. The server validates the new config before switching. If validation fails, the old config remains active and a warning is posted as an issue comment.

---

## Relationship to Architecture Decisions

| AD | Config Section |
|---|---|
| AD-002 (Agent Identity) | `agent_roles` — role definitions |
| AD-004 (Branch Strategy) | `branch_naming` — templates |
| AD-006 (Branch Protection) | `workflows/approval-flows.yaml` — per-branch rules |
| AD-008 (Sequential PM) | `agent_roles.pm.singleton: true` |
| AD-009 (Approval Flows) | `workflows/approval-flows.yaml` — full schema |
| AD-015 (Approval Schema) | `workflows/approval-flows.yaml` — concrete implementation |
| AD-016 (Role Enforcement) | `agent_roles` + approval-flows `required_status_checks` |
| AD-017 (Runtime) | `runtime` — model, provider, reconciliation interval |
| AD-018 (Circuit Breakers) | `circuit_breakers` — limits and overrides |
