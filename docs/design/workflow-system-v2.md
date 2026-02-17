# Workflow System v2 — Design Document

## Overview

The extended workflow system enables **deterministic multi-agent orchestration** while preserving the flexibility of prompt-driven autonomous agents. Workflows are optional — users can continue using agents with tools and prompts for maximum autonomy, or define strict workflows for predictable, auditable execution.

## Design Principles

1. **Optional, Not Enforced** — Workflows layer on top of existing trigger-based spawning
2. **Backwards Compatible** — Current `review_policy` and agent triggers continue working
3. **Deterministic When Needed** — Strict stage sequencing for compliance-sensitive flows
4. **Flexible Composition** — Sequential, parallel, and conditional stage execution
5. **Observable** — Clear state machine with audit trail in registry

---

## Workflow Types

```yaml
# Three workflow archetypes, all using the same schema
workflow_types:

  # Type 1: Review Pipeline (current capability, enhanced)
  # Triggers on PR events, orchestrates review agents
  review_pipeline:
    trigger: pull_request.opened
    stages: [test, security-review, code-review]

  # Type 2: Development Lifecycle (NEW)
  # Triggers on issue events, orchestrates full dev cycle
  development_lifecycle:
    trigger: issues.labeled
    stages: [research, implement, test, document, pr, review]

  # Type 3: Custom Orchestration (NEW)
  # User-defined flows for any purpose
  custom:
    trigger: <any supported event>
    stages: <user-defined>
```

---

## Schema Definition

### Workflow Definition

```yaml
# .squadron/workflows/feature-development.yaml

apiVersion: squadron.dev/v1
kind: Workflow
metadata:
  name: feature-development
  description: "Full feature development lifecycle with quality gates"

# ─────────────────────────────────────────────────────────────────
# TRIGGER: When does this workflow activate?
# ─────────────────────────────────────────────────────────────────
trigger:
  event: issues.labeled
  conditions:
    label: feature
    # Optional additional conditions:
    # assignee_is_bot: true
    # milestone: "v1.0"

# ─────────────────────────────────────────────────────────────────
# CONTEXT: What data is passed through the workflow?
# ─────────────────────────────────────────────────────────────────
context:
  # Automatically populated from trigger event
  issue_number: "{{ trigger.issue.number }}"
  issue_title: "{{ trigger.issue.title }}"
  # User-defined context variables
  base_branch: "{{ config.project.default_branch }}"

# ─────────────────────────────────────────────────────────────────
# STAGES: The execution pipeline
# ─────────────────────────────────────────────────────────────────
stages:
  # ── Stage 1: Research & Planning ──────────────────────────────
  - id: research
    name: "Research & Planning"
    type: agent                    # agent | gate | parallel | delay | webhook
    agent: feat-dev
    action: research               # Defined in agent prompt, informs behavior

    # What the agent should produce (validated before advancing)
    outputs:
      - name: implementation_plan
        type: comment              # comment | file | artifact | pr
        required: true

    # Transition rules
    on_complete: implement
    on_error: escalate
    timeout: 30m

  # ── Stage 2: Implementation ───────────────────────────────────
  - id: implement
    name: "Implementation"
    type: agent
    agent: feat-dev
    action: implement

    # Agent continues from previous stage (same session)
    continue_session: true

    outputs:
      - name: code_changes
        type: file
        pattern: "src/**/*.py"
      - name: tests
        type: file
        pattern: "tests/**/*.py"
        required: true

    on_complete: test
    on_error:
      retry: 2
      then: escalate
    timeout: 2h

  # ── Stage 3: Quality Gate (Testing) ───────────────────────────
  - id: test
    name: "Quality Gate: Tests"
    type: gate

    # Gate conditions (ALL must pass)
    conditions:
      - check: command
        run: "pytest tests/ -x"
        expect: exit_code == 0

      - check: command
        run: "ruff check src/"
        expect: exit_code == 0

      - check: coverage
        minimum: 80%
        # Optional: specific files
        # paths: ["src/squadron/**"]

    on_pass: documentation
    on_fail:
      # Return to implementation with failure context
      goto: implement
      context:
        failure_reason: "{{ gate.failed_checks }}"
      max_iterations: 3
      then: escalate

    timeout: 10m

  # ── Stage 4: Documentation (Optional) ─────────────────────────
  - id: documentation
    name: "Documentation"
    type: agent
    agent: docs-dev
    action: update_docs

    # Optional stage — skip if no docs needed
    condition: "{{ outputs.code_changes | has_public_api }}"
    skip_to: pull_request

    on_complete: pull_request
    timeout: 30m

  # ── Stage 5: Pull Request ─────────────────────────────────────
  - id: pull_request
    name: "Create Pull Request"
    type: agent
    agent: feat-dev
    action: open_pr

    outputs:
      - name: pr_number
        type: pr
        required: true

    on_complete:
      delay: 30s              # Wait for GitHub to propagate
      then: review_pipeline

    timeout: 10m

  # ── Stage 6: Review Pipeline (Parallel) ───────────────────────
  - id: review_pipeline
    name: "Code Review Pipeline"
    type: parallel

    # All branches must complete (join semantics)
    join: all                 # all | any | N-of-M

    branches:
      - id: test_review
        agent: test-coverage
        action: review

      - id: security_review
        agent: security-review
        action: review
        condition: "{{ context.labels | contains('security') }}"

      - id: code_review
        agent: pr-review
        action: review

    on_complete: human_approval
    on_any_reject:
      goto: implement
      context:
        review_feedback: "{{ branches.*.feedback }}"

    timeout: 1h

  # ── Stage 7: Human Approval Gate ──────────────────────────────
  - id: human_approval
    name: "Human Approval"
    type: gate

    conditions:
      - check: pr_approval
        from: maintainers        # Group from config.yaml
        count: 1

    # Notify humans that approval is needed
    on_enter:
      notify:
        target: maintainers
        message: "PR #{{ context.pr_number }} ready for final approval"

    on_pass: merge
    on_timeout:
      notify:
        target: maintainers
        message: "PR #{{ context.pr_number }} awaiting approval for 24h"
      extend: 24h              # Keep waiting
      max_extensions: 3
      then: escalate

    timeout: 24h

  # ── Stage 8: Merge ────────────────────────────────────────────
  - id: merge
    name: "Auto-Merge"
    type: action

    action: merge_pr
    method: squash
    delete_branch: true

    on_success: complete
    on_conflict:
      spawn: merge-conflict
      then: review_pipeline    # Re-review after conflict resolution
    on_ci_failure:
      goto: implement
      context:
        ci_failure: "{{ action.error }}"

# ─────────────────────────────────────────────────────────────────
# COMPLETION: What happens when workflow finishes?
# ─────────────────────────────────────────────────────────────────
on_complete:
  - close_issue: "{{ context.issue_number }}"
  - comment:
      target: issue
      message: |
        Workflow completed successfully.
        - PR: #{{ context.pr_number }}
        - Merged to: {{ context.base_branch }}

on_error:
  - label_issue: needs-human
  - notify: maintainers
```

---

## Stage Types

### 1. `agent` — Execute an Agent

```yaml
- id: implement
  type: agent
  agent: feat-dev              # Agent role from config
  action: implement            # Passed to agent prompt as context

  # Session management
  continue_session: true       # Continue from previous stage (default: false)

  # Expected outputs (validated before advancing)
  outputs:
    - name: code_changes
      type: file
      pattern: "src/**"
      required: true

  # Timeouts and retries
  timeout: 2h
  on_error:
    retry: 2
    then: escalate
```

### 2. `gate` — Quality/Approval Gate

```yaml
- id: quality_gate
  type: gate

  conditions:
    # Command execution check
    - check: command
      run: "pytest tests/"
      expect: exit_code == 0

    # File existence check
    - check: file_exists
      paths: ["README.md", "CHANGELOG.md"]

    # PR approval check
    - check: pr_approval
      from: [maintainers, security-team]
      count: 2

    # CI status check
    - check: ci_status
      workflows: ["test", "lint"]
      expect: success

    # Custom webhook check
    - check: webhook
      url: "https://api.example.com/validate"
      expect: status == 200

    # Coverage threshold
    - check: coverage
      minimum: 80%

  on_pass: next_stage
  on_fail:
    goto: previous_stage
    max_iterations: 3
```

### 3. `parallel` — Concurrent Execution

```yaml
- id: review_pipeline
  type: parallel

  # Join strategy
  join: all                    # all | any | 2-of-3

  branches:
    - id: branch_a
      agent: test-coverage
      action: review

    - id: branch_b
      agent: security-review
      action: review
      condition: "{{ has_security_label }}"  # Conditional branch

    - id: branch_c
      agent: pr-review
      action: review

  # Aggregate results
  on_complete: merge_stage
  on_any_reject: handle_rejection
  on_all_reject: escalate
```

### 4. `delay` — Timed Wait

```yaml
- id: wait_for_propagation
  type: delay
  duration: 30s

  # Optional: poll condition during wait
  poll:
    interval: 5s
    condition: "{{ github.pr.mergeable != null }}"
    on_ready: skip_remaining_delay
```

### 5. `action` — Built-in Actions

```yaml
- id: merge
  type: action
  action: merge_pr             # merge_pr | close_issue | create_issue | label | notify

  # Action-specific config
  method: squash
  delete_branch: true

  on_success: complete
  on_error: escalate
```

### 6. `webhook` — External Integration

```yaml
- id: external_validation
  type: webhook

  request:
    url: "https://api.example.com/validate"
    method: POST
    headers:
      Authorization: "Bearer {{ secrets.API_TOKEN }}"
    body:
      pr_number: "{{ context.pr_number }}"
      files: "{{ outputs.changed_files }}"

  expect:
    status: 200
    body:
      approved: true

  on_success: next_stage
  on_failure: escalate
```

---

## Execution Model

### State Machine

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         WORKFLOW EXECUTION MODEL                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  WORKFLOW RUN STATES                                                        │
│  ───────────────────                                                        │
│                                                                             │
│  ┌──────────┐   trigger    ┌──────────┐   all stages   ┌───────────┐       │
│  │ PENDING  │ ───────────▶ │ RUNNING  │ ─────────────▶ │ COMPLETED │       │
│  └──────────┘              └────┬─────┘                └───────────┘       │
│                                 │                                           │
│                    error/timeout│                                           │
│                                 ▼                                           │
│                           ┌───────────┐                                     │
│                           │  FAILED   │                                     │
│                           └─────┬─────┘                                     │
│                                 │ escalate                                  │
│                                 ▼                                           │
│                           ┌───────────┐                                     │
│                           │ ESCALATED │                                     │
│                           └───────────┘                                     │
│                                                                             │
│  STAGE STATES                                                               │
│  ────────────                                                               │
│                                                                             │
│  ┌─────────┐  start   ┌─────────┐  complete   ┌───────────┐                │
│  │ PENDING │ ───────▶ │ RUNNING │ ──────────▶ │ COMPLETED │                │
│  └─────────┘          └────┬────┘             └───────────┘                │
│       │                    │                                                │
│       │ skip               │ fail                                           │
│       ▼                    ▼                                                │
│  ┌─────────┐          ┌─────────┐                                          │
│  │ SKIPPED │          │ FAILED  │──retry──▶ RUNNING                        │
│  └─────────┘          └─────────┘                                          │
│                                                                             │
│  PARALLEL BRANCH STATES                                                     │
│  ──────────────────────                                                     │
│                                                                             │
│  All branches start simultaneously.                                         │
│  Join waits for branches per join strategy:                                │
│    - all: wait for ALL branches                                            │
│    - any: proceed when ANY branch completes                                │
│    - N-of-M: proceed when N branches complete                              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Context Propagation

```yaml
# Context flows through stages, accumulating outputs

# Stage 1 produces:
context:
  issue_number: 42
  outputs:
    research:
      implementation_plan: "comment:123456"

# Stage 2 adds:
context:
  issue_number: 42
  outputs:
    research: { ... }
    implement:
      code_changes: ["src/foo.py", "src/bar.py"]
      tests: ["tests/test_foo.py"]

# Stage 3 (gate) adds:
context:
  issue_number: 42
  outputs:
    research: { ... }
    implement: { ... }
    test:
      passed: true
      coverage: 87.5
      duration: "45s"

# And so on...
```

---

## Backwards Compatibility

### Current Triggers Still Work

```yaml
# config.yaml — existing trigger-based spawning unchanged
agent_roles:
  feat-dev:
    triggers:
      - event: "issues.labeled"
        label: feature
        # This still spawns feat-dev autonomously
        # No workflow involvement
```

### Current review_policy Still Works

```yaml
# config.yaml — review_policy is syntactic sugar for a workflow
review_policy:
  enabled: true
  default_requirements:
    - role: pr-review
      count: 1
  rules:
    - name: squadron-dev-pipeline
      match:
        base_branch: squadron-dev
      requirements:
        - role: test-coverage
        - role: security-review
        - role: pr-review
      sequence: [test-coverage, security-review, pr-review]

# Internally generates equivalent workflow:
# workflows:
#   - name: squadron-dev-pipeline (auto-generated)
#     trigger: { event: pull_request.opened, conditions: { base_branch: squadron-dev } }
#     stages:
#       - { id: test-coverage, type: agent, agent: test-coverage, action: review }
#       - { id: security-review, type: agent, agent: security-review, action: review }
#       - { id: pr-review, type: agent, agent: pr-review, action: review }
```

### Workflow Override

```yaml
# Users can opt-in to workflow control per-issue
# by adding a label or using a command

# Option 1: Label-based
# Adding "workflow:feature-development" label activates that workflow

# Option 2: Command-based
# @squadron-dev workflow:feature-development
# Explicitly starts the named workflow

# Option 3: Always-on for matching triggers
# If a workflow trigger matches, it takes precedence over raw triggers
# (configurable per-workflow with `override_triggers: true`)
```

---

## Implementation Phases

### Phase 1: Core Engine (Foundation)
- [ ] Workflow schema parsing and validation
- [ ] Stage state machine
- [ ] Sequential stage execution
- [ ] Context propagation
- [ ] `agent` stage type
- [ ] `gate` stage type (command checks only)
- [ ] Registry tables for workflow runs

### Phase 2: Quality Gates
- [ ] `gate` conditions: file_exists, pr_approval, ci_status
- [ ] Test output parsing (pytest, coverage)
- [ ] Retry logic with backoff
- [ ] Iteration tracking and limits

### Phase 3: Parallel Execution
- [ ] `parallel` stage type
- [ ] Branch execution and tracking
- [ ] Join strategies (all, any, N-of-M)
- [ ] Result aggregation

### Phase 4: Advanced Features
- [ ] `delay` stage type with polling
- [ ] `webhook` stage type
- [ ] `action` stage type (merge, close, label)
- [ ] Conditional stage execution
- [ ] Output validation

### Phase 5: Integration
- [ ] review_policy → workflow generation
- [ ] Workflow override for triggers
- [ ] UI/CLI for workflow status
- [ ] Workflow templates library

---

## Database Schema

```sql
-- Workflow runs (enhanced from current)
CREATE TABLE workflow_runs (
    run_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    workflow_version TEXT,

    -- Trigger context
    trigger_event TEXT,
    trigger_delivery_id TEXT,
    issue_number INTEGER,
    pr_number INTEGER,

    -- Execution state
    status TEXT DEFAULT 'pending',  -- pending, running, completed, failed, escalated
    current_stage_id TEXT,
    current_stage_index INTEGER DEFAULT 0,
    iteration_count INTEGER DEFAULT 0,

    -- Context (JSON blob)
    context TEXT DEFAULT '{}',
    outputs TEXT DEFAULT '{}',

    -- Timestamps
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT,

    -- Error tracking
    error_message TEXT,
    error_stage TEXT
);

-- Stage executions
CREATE TABLE workflow_stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
    stage_id TEXT NOT NULL,
    stage_index INTEGER NOT NULL,

    -- Execution
    status TEXT DEFAULT 'pending',  -- pending, running, completed, failed, skipped
    agent_id TEXT,

    -- For parallel stages
    branch_id TEXT,
    parent_stage_id TEXT,

    -- Results
    outputs TEXT DEFAULT '{}',
    error_message TEXT,

    -- Timing
    started_at TEXT,
    completed_at TEXT,
    duration_seconds REAL,

    -- Retry tracking
    attempt_number INTEGER DEFAULT 1,
    max_attempts INTEGER DEFAULT 1
);

-- Gate condition results
CREATE TABLE workflow_gate_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_run_id INTEGER NOT NULL REFERENCES workflow_stage_runs(id),
    check_type TEXT NOT NULL,
    check_config TEXT,

    -- Result
    passed BOOLEAN,
    result_data TEXT,
    error_message TEXT,

    checked_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## Example Workflows

### Example 1: Simple PR Review (Current Capability)

```yaml
apiVersion: squadron.dev/v1
kind: Workflow
metadata:
  name: simple-review

trigger:
  event: pull_request.opened

stages:
  - id: review
    type: agent
    agent: pr-review
    action: review
    on_complete: complete
```

### Example 2: Bug Fix with Regression Test

```yaml
apiVersion: squadron.dev/v1
kind: Workflow
metadata:
  name: bug-fix-workflow

trigger:
  event: issues.labeled
  conditions:
    label: bug

stages:
  - id: diagnose
    type: agent
    agent: bug-fix
    action: diagnose
    outputs:
      - name: root_cause
        type: comment
    on_complete: implement

  - id: implement
    type: agent
    agent: bug-fix
    action: fix
    continue_session: true
    outputs:
      - name: regression_test
        type: file
        pattern: "tests/**/test_*"
        required: true
    on_complete: verify

  - id: verify
    type: gate
    conditions:
      - check: command
        run: "pytest tests/ -x --tb=short"
        expect: exit_code == 0
    on_pass: open_pr
    on_fail:
      goto: implement
      max_iterations: 3

  - id: open_pr
    type: agent
    agent: bug-fix
    action: open_pr
    continue_session: true
    on_complete: complete
```

### Example 3: Security-Sensitive Feature

```yaml
apiVersion: squadron.dev/v1
kind: Workflow
metadata:
  name: security-feature

trigger:
  event: issues.labeled
  conditions:
    labels: [feature, security]

stages:
  - id: threat_model
    type: agent
    agent: security-review
    action: threat_model
    outputs:
      - name: threat_analysis
        type: comment
    on_complete: implement

  - id: implement
    type: agent
    agent: feat-dev
    action: implement
    on_complete: security_review

  - id: security_review
    type: parallel
    join: all
    branches:
      - id: code_review
        agent: pr-review
        action: review
      - id: security_audit
        agent: security-review
        action: audit
      - id: test_coverage
        agent: test-coverage
        action: review
    on_complete: human_approval

  - id: human_approval
    type: gate
    conditions:
      - check: pr_approval
        from: security-team
        count: 1
    on_pass: merge
    timeout: 48h

  - id: merge
    type: action
    action: merge_pr
    on_success: complete
```

---

## Open Questions

1. **Session continuity across stages** — Should agent sessions persist across stages, or should each stage get a fresh context?

2. **Workflow versioning** — How to handle workflow definition changes mid-execution?

3. **Partial execution** — Can workflows be started from an arbitrary stage for recovery?

4. **Workflow composition** — Can workflows call other workflows as sub-workflows?

5. **Concurrency limits** — How many workflow stages can run in parallel globally?

---

## Next Steps

1. Review and finalize schema
2. Implement Phase 1 (core engine)
3. Migrate existing review_policy to workflow format
4. Add workflow templates for common patterns
5. Build CLI for workflow management
