# Unified Pipeline System — Design Document (AD-019)

**Status:** Proposed  
**Supersedes:** `workflow-system-v2.md`, `review_policy` config section, `agent_roles.<role>.triggers`  
**Extends:** AD-006, AD-009, AD-013, AD-015, AD-017, AD-018  
**Related research:** `approval-flow-schema.md`, `event-routing.md`, `event-architecture.md`

---

## Problem Statement

Squadron currently has **three parallel orchestration systems** that evolved independently and don't communicate:

1. **Config-driven triggers** (`agent_roles.<role>.triggers`) — Simple event-to-action mappings (spawn/wake/complete). Stateless and reactive. This is what production uses today.

2. **Workflow Engine v2** (`src/squadron/workflow/`) — Multi-stage pipelines with agent stages, gates, delays, and actions. Has its own SQLite persistence (`WorkflowRegistryV2`). Gate conditions only support `command` and `file_exists`. Only one workflow defined (`feature-dev-pipeline`). **Not connected to the PR lifecycle.**

3. **Review Policy** (`review_policy` in config.yaml, SQL tables in `registry.py`) — `ReviewPolicyConfig` with rules, sequences, auto-merge. `submit_pr_review` tool records approvals and triggers auto-merge. `_auto_merge_pr` is a callback, not an agent stage.

These three systems create fragmented state, duplicated logic, and critical gaps in the PR lifecycle.

---

## Critical Gaps in Current Implementation

### Gap 1: Human reviews are not tracked

`_handle_pr_review_submitted` in `agent_manager.py:2737` only queues inbox events — it does **not** call `record_pr_approval()`. Human GitHub reviews are invisible to the approval tracking system.

### Gap 2: `pr_approval` gate type declared but not implemented

`GateCondition` in `config.py:553` declares `pr_approval` as a valid gate check type. `_evaluate_condition()` in `workflow/engine.py:454-539` only implements `command` and `file_exists`. Any workflow using `pr_approval` gates silently fails.

### Gap 3: No post-review feedback loop

When a reviewer requests changes, the feat-dev agent is woken but receives no structured context about what to fix. There's no workflow stage that routes review feedback back to implementation with retry tracking.

### Gap 4: No merge agent

Auto-merge is a callback (`_auto_merge_pr` at `agent_manager.py:3638`), not an agent or workflow stage. Merge conflicts, CI failures after approval, and retry logic are handled ad-hoc.

### Gap 5: Workflow Engine disconnected from PR lifecycle

`agent_manager.py:735-739` contains a TODO acknowledging this. The workflow engine can't react to `pull_request.synchronize`, `pull_request_review.submitted`, or `check_suite.completed` events.

### Gap 6: Two separate registries

`AgentRegistry` (in `registry.py`) tracks agents, PR approvals, and sequence state. `WorkflowRegistryV2` (in `workflow/registry.py`) tracks workflow runs, stages, and gate checks. Neither knows about the other. An agent stage in a workflow has no link to its `AgentRecord`.

### Gap 7: Triggers can't express conditional workflows

The trigger system maps one event to one action (spawn/wake/complete). It cannot express: "after ALL reviews approved AND CI passes, THEN merge." Multi-condition logic requires a pipeline.

### Gap 8: No custom gate definitions in YAML

Users cannot define new gate check types without modifying Python source code.

---

## Design Proposal: Unified Pipelines

### Core Principle

**Pipelines are the single orchestration primitive.** All agent spawning, review orchestration, approval tracking, and merge automation are expressed as pipeline definitions. The current `triggers`, `review_policy`, and `workflows` config sections are **removed entirely** and replaced by `pipelines:`.

> **CRITICAL DESIGN DIRECTIVE:** This refactor removes ALL legacy orchestration code — triggers, review_policy, and the v2 workflow engine. There is no backward-compatibility shim, no auto-conversion, no deprecation period. The old code is deleted. This is the correct pattern for refactors: clean replacement, not parallel maintenance.

### Architecture Overview

```
                          ┌─────────────────────────┐
                          │     config.yaml          │
                          │                          │
                          │  pipelines:              │
                          │    pr-review-pipeline:   │
                          │      ...                 │
                          │    feature-dev:           │
                          │      ...                 │
                          └────────────┬────────────┘
                                       │ load & validate
                                       ▼
                          ┌─────────────────────────┐
                          │   Pipeline Registry      │
                          │   (single SQLite DB)     │
                          │                          │
                          │  - Pipeline definitions  │
                          │  - Pipeline runs         │
                          │  - Stage runs            │
                          │  - Gate check results    │
                          │  - Agent records         │
                          │  - PR approval state     │
                          └────────────┬────────────┘
                                       │
                          ┌────────────┴────────────┐
                          │    Pipeline Engine       │
                          │                          │
                          │  - Event subscription    │
                          │  - Stage execution       │
                          │  - Gate evaluation       │
                          │  - Transition logic      │
                          │  - Reactive re-evaluation│
                          │  - Sub-pipeline invoke   │
                          └─────────────────────────┘
```

---

## Pipeline Schema

### Top-Level Config

Pipelines are defined in `config.yaml` under a `pipelines:` key, or in separate YAML files under `.squadron/pipelines/`.

```yaml
# .squadron/config.yaml (inline)
pipelines:
  pr-lifecycle:
    # ... pipeline definition

# OR as separate files:
# .squadron/pipelines/pr-lifecycle.yaml
```

### Pipeline Definition

```yaml
pipelines:
  pr-lifecycle:
    description: "Full PR review, approval, and merge pipeline"
    
    # ─── TRIGGER ─────────────────────────────────────────────
    # When does this pipeline activate?
    trigger:
      event: pull_request.opened
      conditions:
        base_branch: squadron-dev    # optional filter
    
    # ─── REACTIVE EVENTS ────────────────────────────────────
    # Events that affect a RUNNING pipeline (not just trigger it)
    on_events:
      pull_request.synchronize:
        # Invalidate approvals and re-run from a stage
        action: invalidate_and_restart
        invalidate:
          - security-review
          - pr-review
        restart_from: test-coverage    # or "current" to restart current stage
      
      pull_request_review.submitted:
        # Re-evaluate gates when a review comes in
        action: reevaluate_gates
      
      check_suite.completed:
        # Re-evaluate gates when CI finishes
        action: reevaluate_gates
      
      pull_request.closed:
        action: cancel              # Cancel pipeline if PR is closed

    # ─── CONTEXT ─────────────────────────────────────────────
    context:
      pr_number: "{{ trigger.pull_request.number }}"
      base_branch: "{{ trigger.pull_request.base.ref }}"
      head_branch: "{{ trigger.pull_request.head.ref }}"
    
    # ─── STAGES ──────────────────────────────────────────────
    stages:
      # Stage 1: Sequential agent reviews
      - id: test-coverage
        type: agent
        agent: test-coverage
        action: review
        timeout: 30m
        on_complete: security-review
        on_error:
          retry: 1
          then: escalate
      
      - id: security-review
        type: agent
        agent: security-review
        action: review
        # Conditional — only for matching rules
        condition:
          any:
            - labels_include: security
            - paths_match:
                - "src/**/auth/**"
                - "src/**/crypto/**"
                - "**/security/**"
        skip_to: pr-review         # Skip if condition not met
        timeout: 30m
        on_complete: pr-review
      
      - id: pr-review
        type: agent
        agent: pr-review
        action: review
        timeout: 30m
        on_complete: approval-gate
      
      # Stage 2: Human approval
      - id: human-approval
        type: human
        description: "Maintainer approval required"
        wait_for: approval
        from: maintainers
        count: 1
        notify:
          on_enter: "PR #{{ context.pr_number }} ready for human review"
          reminder:
            interval: 24h
            message: "PR #{{ context.pr_number }} still awaiting approval"
            max_reminders: 3
        timeout: 72h
        on_timeout:
          label: needs-attention
          notify:
            target: maintainers
            message: "PR #{{ context.pr_number }} has been waiting 72h for approval"
          then: escalate
        on_complete: approval-gate
      
      # Stage 3: Approval gate — waits for all conditions
      - id: approval-gate
        type: gate
        conditions:
          - check: pr_approvals_met
            scope: agents
          - check: ci_status
            workflows: ["test", "lint"]
            expect: success
            optional: true
          - check: no_changes_requested
        
        on_pass: auto-merge
        on_fail:
          when:
            check_failed: no_changes_requested
          action: notify_and_wait
          notify:
            target: "{{ context.head_branch | agent_for_branch }}"
            message: "Changes requested on PR #{{ context.pr_number }}"
        timeout: 48h
        on_timeout:
          notify:
            target: maintainers
          label: needs-attention
      
      # Stage 4: Auto-merge
      - id: auto-merge
        type: action
        action: merge_pr
        config:
          method: squash
          delete_branch: true
        on_success: complete
        on_conflict:
          notify:
            target: maintainers
          label: merge-conflict
        on_ci_failure:
          goto: approval-gate
          max_iterations: 2
          then: escalate
    
    # ─── COMPLETION ──────────────────────────────────────────
    on_complete:
      - close_issue: "{{ context.pr_number | linked_issue }}"
      - comment:
          target: pr
          message: "Pipeline completed. PR merged to {{ context.base_branch }}."
    
    on_error:
      - label: needs-human
      - notify: maintainers
```

---

## Stage Types

The pipeline system supports **seven** stage types.

### 1. `agent` — Spawn or Wake an Agent

```yaml
- id: security-review
  type: agent
  agent: security-review        # Agent role from config
  action: review                # Passed to agent as context
  
  # Session management
  continue_session: false       # Default: false (fresh session)
  
  # Conditional execution
  condition:
    any:
      - labels_include: security
      - paths_match: ["src/**/auth/**"]
  skip_to: next-stage           # Stage to jump to if condition not met
  
  # Timeouts and retries
  timeout: 30m
  on_complete: next-stage
  on_error:
    retry: 2
    then: escalate
```

**Behavior:** The engine spawns (or wakes) the specified agent role for the pipeline's PR/issue. When the agent calls `report_complete` or `submit_pr_review`, the engine advances to the next stage.

### 2. `gate` — Condition-Based Gate

```yaml
- id: approval-gate
  type: gate
  
  # ALL conditions must pass (default conjunction)
  # Use "any_of" for disjunction
  conditions:
    - check: pr_approvals_met
      scope: agents              # agents | humans | all
    - check: ci_status
      workflows: ["test"]
      expect: success
    - check: no_changes_requested
    - check: label_present
      label: approved
  
  on_pass: next-stage
  on_fail:
    goto: previous-stage
    max_iterations: 3
    then: escalate
  timeout: 24h
  on_timeout:
    # Configurable per-gate — can fail, escalate, extend, notify, or cancel
    notify:
      target: maintainers
      message: "Gate timed out after 24h"
    then: escalate              # or: fail | extend: 24h | cancel
```

**Behavior:** Gate conditions are evaluated when the stage is entered and **re-evaluated on every relevant reactive event** (e.g., `pull_request_review.submitted` triggers re-evaluation of `pr_approvals_met`). Gates are non-blocking waiters — they don't spin-poll.

**Timeout behavior is configurable per-gate.** Each gate's `on_timeout` can independently specify: fail the stage, escalate the pipeline, extend the timeout (with max extensions), send notifications, or cancel the pipeline. This allows approval gates to wait patiently while CI gates fail fast.

### 3. `human` — Human-in-the-Loop Stage

```yaml
- id: maintainer-approval
  type: human
  description: "Maintainer must approve before merge"
  
  # What human action to wait for
  wait_for: approval            # approval | comment | label | dismiss
  
  # Who must act
  from: maintainers             # Human group from config.yaml
  count: 1                      # Number of actions needed
  
  # Notification lifecycle
  notify:
    on_enter: "PR #{{ context.pr_number }} is ready for your review"
    reminder:
      interval: 24h
      message: "Reminder: PR #{{ context.pr_number }} awaits your review"
      max_reminders: 3
  
  # Assignment
  auto_assign: true             # Assign from the human group on GitHub
  
  # Timeout
  timeout: 72h
  on_timeout:
    label: needs-attention
    notify:
      target: maintainers
    then: escalate
  
  on_complete: next-stage
```

**Behavior:** The `human` stage type is a first-class primitive for waiting on human actions. It handles:

- **Notification on entry** — posts a comment or sends a notification when the stage activates
- **Periodic reminders** — configurable interval, message, and max count
- **Auto-assignment** — optionally assigns reviewers from the human group on GitHub
- **Multiple wait types** — `approval` (PR approval), `comment` (any comment with optional pattern match), `label` (specific label added), `dismiss` (dismissal of a blocking review)
- **Reactive re-evaluation** — listens for `pull_request_review.submitted`, `issue_comment.created`, `pull_request.labeled` depending on `wait_for` type

This is distinct from `gate` because it models a single human interaction with built-in notification/reminder lifecycle, rather than a set of boolean conditions to evaluate.

### 4. `parallel` — Concurrent Agent Execution

```yaml
- id: review-pipeline
  type: parallel
  join: all                     # all | any | N-of-M
  
  branches:
    - id: test-review
      agent: test-coverage
      action: review
    - id: security-review
      agent: security-review
      action: review
      condition:
        labels_include: security
    - id: code-review
      agent: pr-review
      action: review
  
  on_complete: approval-gate
  on_any_reject:
    goto: implement
    context:
      review_feedback: "{{ branches.*.feedback }}"
```

### 5. `delay` — Timed Wait

```yaml
- id: wait-for-ci
  type: delay
  duration: 30s
  poll:
    interval: 5s
    condition: "{{ github.pr.mergeable != null }}"
    on_ready: skip_remaining_delay
```

### 6. `action` — Built-in Framework Action

```yaml
- id: merge
  type: action
  action: merge_pr              # merge_pr | close_issue | create_issue | label | notify
  config:
    method: squash
    delete_branch: true
  on_success: complete
  on_conflict: escalate
```

### 7. `webhook` — External Integration

```yaml
- id: external-check
  type: webhook
  request:
    url: "https://api.example.com/validate"
    method: POST
    body:
      pr_number: "{{ context.pr_number }}"
  expect:
    status: 200
  on_success: next-stage
  on_failure: escalate
```

---

## Sub-Pipeline Composition

Pipelines can invoke other pipelines as sub-pipelines. A stage can reference another pipeline by name; the sub-pipeline runs inline, and its completion (or failure) advances the parent stage.

### Schema

```yaml
pipelines:
  # Reusable review sub-pipeline
  standard-review:
    description: "Standard 3-agent review sequence"
    # No trigger — only invoked as sub-pipeline
    stages:
      - id: test-coverage
        type: agent
        agent: test-coverage
        action: review
        timeout: 30m
        on_complete: security-review
      - id: security-review
        type: agent
        agent: security-review
        action: review
        timeout: 30m
        on_complete: pr-review
      - id: pr-review
        type: agent
        agent: pr-review
        action: review
        timeout: 30m
        on_complete: complete

  # Parent pipeline references the sub-pipeline
  feature-dev:
    trigger:
      event: issues.labeled
      conditions:
        label: feature
    stages:
      - id: implement
        type: agent
        agent: feat-dev
        action: implement
        timeout: 2h
        on_complete: review
      
      - id: review
        type: pipeline
        pipeline: standard-review        # Invoke sub-pipeline by name
        # Context from parent is passed to child
        context:
          pr_number: "{{ context.pr_number }}"
        on_complete: human-approval
        on_error: escalate
      
      - id: human-approval
        type: human
        wait_for: approval
        from: maintainers
        count: 1
        timeout: 48h
        on_complete: merge
      
      - id: merge
        type: action
        action: merge_pr
        config:
          method: squash
          delete_branch: true
```

### Execution Semantics

- **Context propagation:** Parent context is merged into sub-pipeline context. Sub-pipeline outputs are available to the parent as `{{ stages.<stage_id>.outputs }}`.
- **Error propagation:** Sub-pipeline failure propagates to the parent stage's `on_error` handler.
- **Completion:** Sub-pipeline reaching its terminal `complete` state advances the parent stage to `on_complete`.
- **Cancellation:** Cancelling the parent pipeline cancels all running sub-pipelines.
- **Cycle detection:** The engine detects circular sub-pipeline references at config load time (BFS through `type: pipeline` stages). Config validation fails if a cycle is found.
- **Nesting depth limit:** Maximum sub-pipeline nesting depth of 3 (configurable). Prevents runaway recursion.

### Pipelines Without Triggers

A pipeline with no `trigger:` section can only be invoked as a sub-pipeline. This is the recommended pattern for reusable stage sequences.

---

## Multi-PR Pipelines

Pipelines can span multiple PRs. This enables coordinated changes across repos, feature branches that spawn sub-PRs, and cross-PR dependency tracking.

### Scope Configuration

```yaml
pipelines:
  cross-repo-feature:
    description: "Coordinated feature across API and frontend repos"
    scope: multi-pr                    # single-pr (default) | multi-pr | issue
    
    trigger:
      event: issues.labeled
      conditions:
        label: cross-repo
    
    context:
      issue_number: "{{ trigger.issue.number }}"
      prs: []                          # Populated as PRs are created
    
    stages:
      - id: api-changes
        type: agent
        agent: feat-dev
        action: implement
        config:
          repo: "nbaertsch/squadron-api"   # Target repo for this stage
          branch_prefix: "feat/issue-{{ context.issue_number }}"
        timeout: 2h
        on_complete: frontend-changes
      
      - id: frontend-changes
        type: agent
        agent: feat-dev
        action: implement
        config:
          repo: "nbaertsch/squadron-frontend"
          branch_prefix: "feat/issue-{{ context.issue_number }}"
        timeout: 2h
        on_complete: review-all
      
      - id: review-all
        type: parallel
        join: all
        branches:
          - id: api-review
            type: pipeline
            pipeline: standard-review
            context:
              pr_number: "{{ context.prs[0] }}"
          - id: frontend-review
            type: pipeline
            pipeline: standard-review
            context:
              pr_number: "{{ context.prs[1] }}"
        on_complete: merge-all
      
      - id: merge-all
        type: parallel
        join: all
        branches:
          - id: merge-api
            type: action
            action: merge_pr
            config:
              pr_number: "{{ context.prs[0] }}"
              method: squash
          - id: merge-frontend
            type: action
            action: merge_pr
            config:
              pr_number: "{{ context.prs[1] }}"
              method: squash
        on_complete: complete
```

### Cross-PR Context

Multi-PR pipelines maintain a `prs` list in context that is populated as agent stages create PRs. The pipeline registry tracks the association between a pipeline run and multiple PR numbers:

```sql
-- Pipeline-to-PR association (supports multi-PR)
CREATE TABLE pipeline_pr_associations (
    pipeline_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    pr_number INTEGER NOT NULL,
    repo TEXT NOT NULL,            -- "owner/repo" for cross-repo support
    stage_id TEXT,                 -- Stage that created/claimed this PR
    role TEXT,                     -- Role of the PR in the pipeline (e.g., "api", "frontend")
    
    PRIMARY KEY(pipeline_run_id, pr_number, repo)
);
```

### Cross-PR Gate Checks

Gate checks in multi-PR pipelines can reference specific PRs:

```yaml
- id: all-prs-approved
  type: gate
  conditions:
    - check: pr_approvals_met
      pr: "{{ context.prs[0] }}"    # Specific PR
      scope: all
    - check: pr_approvals_met
      pr: "{{ context.prs[1] }}"
      scope: all
    - check: ci_status
      pr: all                        # All associated PRs
      expect: success
```

### Reactive Events Across PRs

Multi-PR pipelines subscribe to events across all associated PRs. When a `pull_request_review.submitted` event fires for any PR in the pipeline's `prs` list, the pipeline engine routes it to the correct running pipeline.

---

## Pluggable Gate Check Registry

Gate checks are the core extensibility mechanism. Built-in checks cover common patterns; users can register custom checks via Python modules.

### Built-in Gate Checks

| Check Type | Description | Re-evaluates On |
|---|---|---|
| `pr_approvals_met` | All required agent/human reviews approved | `pull_request_review.submitted` |
| `ci_status` | CI workflow(s) passed | `check_suite.completed`, `status` |
| `command` | Shell command exits 0 | Manual / stage entry |
| `file_exists` | File(s) exist at path(s) | Manual / stage entry |
| `label_present` | PR/issue has specified label | `pull_request.labeled` |
| `no_changes_requested` | No outstanding "changes requested" | `pull_request_review.submitted`, `pull_request_review.dismissed` |
| `human_approved` | At least N humans from group approved | `pull_request_review.submitted` |
| `branch_up_to_date` | Head branch is up to date with base | `push`, `pull_request.synchronize` |

### Gate Check Interface

```python
# src/squadron/pipeline/gates.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

@dataclass
class GateCheckResult:
    """Result of a gate condition evaluation."""
    passed: bool
    message: str
    data: dict[str, Any] | None = None

class GateCheck(ABC):
    """Base class for all gate checks."""
    
    # Class-level: which events should trigger re-evaluation
    reactive_events: set[str] = set()
    
    @abstractmethod
    async def evaluate(
        self,
        config: dict[str, Any],
        context: "PipelineContext",
    ) -> GateCheckResult:
        """Evaluate the gate condition.
        
        Args:
            config: The check-specific configuration from YAML
                    (everything in the condition dict except 'check')
            context: Pipeline context with PR number, repo info, etc.
        
        Returns:
            GateCheckResult indicating pass/fail with details
        """
        ...


class PrApprovalsMetCheck(GateCheck):
    """Check that all required PR approvals have been recorded."""
    
    reactive_events = {
        "pull_request_review.submitted",
        "pull_request_review.dismissed",
    }
    
    async def evaluate(self, config, context) -> GateCheckResult:
        scope = config.get("scope", "all")  # agents | humans | all
        pr = config.get("pr", context.pr_number)  # Support multi-PR
        registry = context.registry
        
        ready, missing = await registry.check_pr_merge_ready(
            pr_number=pr,
            scope=scope,
        )
        
        if ready:
            return GateCheckResult(passed=True, message="All approvals met")
        return GateCheckResult(
            passed=False,
            message=f"Missing approvals: {', '.join(missing)}",
            data={"missing": missing},
        )


class CiStatusCheck(GateCheck):
    """Check that CI workflows have passed."""
    
    reactive_events = {"check_suite.completed", "status"}
    
    async def evaluate(self, config, context) -> GateCheckResult:
        workflows = config.get("workflows", [])
        expect = config.get("expect", "success")
        # ... query GitHub check runs API
        ...


class NoChangesRequestedCheck(GateCheck):
    """Check that no reviews have 'changes_requested' state."""
    
    reactive_events = {
        "pull_request_review.submitted",
        "pull_request_review.dismissed",
    }
    
    async def evaluate(self, config, context) -> GateCheckResult:
        # Query GitHub PR reviews, check none have
        # state == "CHANGES_REQUESTED"
        ...
```

### Custom Gate Registration

Users can extend the gate check registry by pointing to Python modules in config:

```yaml
# .squadron/config.yaml
pipeline_settings:
  custom_gates:
    - module: "squadron_custom.gates"
      checks:
        - name: "deployment_ready"
          class: "DeploymentReadyCheck"
        - name: "load_test_passed"
          class: "LoadTestCheck"
```

```python
# squadron_custom/gates.py (user-provided)
from squadron.pipeline.gates import GateCheck, GateCheckResult

class DeploymentReadyCheck(GateCheck):
    reactive_events = {"deployment_status"}
    
    async def evaluate(self, config, context) -> GateCheckResult:
        environment = config.get("environment", "staging")
        # Check deployment status via API
        ...
```

Usage in a pipeline:

```yaml
stages:
  - id: deploy-gate
    type: gate
    conditions:
      - check: deployment_ready
        environment: staging
      - check: load_test_passed
        threshold: 99.5
```

### Gate Registry Implementation

```python
# src/squadron/pipeline/gate_registry.py

class GateCheckRegistry:
    """Registry of all available gate check types."""
    
    def __init__(self):
        self._checks: dict[str, type[GateCheck]] = {}
        self._register_builtins()
    
    def _register_builtins(self):
        """Register all built-in gate checks."""
        self.register("pr_approvals_met", PrApprovalsMetCheck)
        self.register("ci_status", CiStatusCheck)
        self.register("command", CommandCheck)
        self.register("file_exists", FileExistsCheck)
        self.register("label_present", LabelPresentCheck)
        self.register("no_changes_requested", NoChangesRequestedCheck)
        self.register("human_approved", HumanApprovedCheck)
        self.register("branch_up_to_date", BranchUpToDateCheck)
    
    def register(self, name: str, check_class: type[GateCheck]):
        """Register a gate check type."""
        if name in self._checks:
            raise ValueError(f"Gate check '{name}' already registered")
        self._checks[name] = check_class
    
    def load_custom_gates(self, config: list[dict]):
        """Load custom gate checks from user-specified modules."""
        for entry in config:
            module = importlib.import_module(entry["module"])
            for check_def in entry["checks"]:
                cls = getattr(module, check_def["class"])
                if not issubclass(cls, GateCheck):
                    raise TypeError(
                        f"{check_def['class']} must subclass GateCheck"
                    )
                self.register(check_def["name"], cls)
    
    def get(self, name: str) -> type[GateCheck]:
        """Get a gate check class by name."""
        if name not in self._checks:
            raise KeyError(
                f"Unknown gate check '{name}'. "
                f"Available: {sorted(self._checks.keys())}"
            )
        return self._checks[name]
    
    def get_reactive_events(self) -> dict[str, set[str]]:
        """Map of event -> set of gate check names that react to it."""
        mapping: dict[str, set[str]] = {}
        for name, cls in self._checks.items():
            for event in cls.reactive_events:
                mapping.setdefault(event, set()).add(name)
        return mapping
```

---

## Reactive Event Subscriptions

Pipelines are **reactive** to events that occur while they're running. This is what closes Gap 3 (no post-review feedback loop) and Gap 5 (workflow disconnected from PR lifecycle).

### How Reactive Events Work

1. **Pipeline declares `on_events`** — a mapping of GitHub event types to pipeline actions.
2. **Event Router** checks running pipelines when an event arrives.
3. **Pipeline Engine** processes the reactive action:
   - `reevaluate_gates`: Re-run gate condition checks for any gate stage that's currently `WAITING`.
   - `invalidate_and_restart`: Mark specified stage approvals as stale, restart from a stage.
   - `cancel`: Terminate the pipeline.
   - `notify`: Send a notification without changing pipeline state.
   - `wake_agent`: Wake a specific agent in a stage with new context.

### Event Router Integration

```python
# Pseudocode for event dispatch — pipelines are the ONLY orchestration mechanism

async def route_event(event: SquadronEvent):
    # 1. Check trigger-based pipeline activation (new pipelines)
    for pipeline_def in config.pipelines:
        if pipeline_def.trigger and pipeline_def.trigger.matches(event):
            engine.start_pipeline(pipeline_def, event)
    
    # 2. Check reactive events on RUNNING pipelines
    running = registry.get_running_pipelines(
        pr_number=event.pr_number,
        issue_number=event.issue_number,
    )
    for pipeline_run in running:
        reactive_config = pipeline_run.definition.on_events.get(event.type)
        if reactive_config:
            await engine.handle_reactive_event(pipeline_run, reactive_config, event)
```

### Re-evaluation Flow

When a `pull_request_review.submitted` event arrives for a PR with a running pipeline:

```
Event: pull_request_review.submitted (PR #130)
  │
  ▼
Pipeline Engine: pr-lifecycle run for PR #130
  │
  ├── Current stage: approval-gate (type: gate, status: WAITING)
  │     │
  │     ├── Condition: pr_approvals_met
  │     │     └── reactive_events includes "pull_request_review.submitted" -> re-evaluate -> passed
  │     │
  │     ├── Condition: no_changes_requested
  │     │     └── reactive_events includes "pull_request_review.submitted" -> re-evaluate -> passed
  │     │
  │     ├── Condition: ci_status
  │     │     └── reactive_events does NOT include this event -> use cached result -> passed
  │     │
  │     └── All conditions passed -> transition to auto-merge stage
  │
  ▼
Stage: auto-merge (type: action) -> execute merge
```

---

## Framework-Level Approval Recording

**This closes Gap 1.** The framework itself records approvals from both agent tools AND GitHub webhook events.

### Current State (Broken)

```
Agent calls submit_pr_review -> record_pr_approval()   OK
Human submits review on GitHub -> _handle_pr_review_submitted() -> inbox event only   BROKEN
```

### Fixed Design

```python
# agent_manager.py — _handle_pr_review_submitted

async def _handle_pr_review_submitted(self, event: SquadronEvent):
    review = event.payload["review"]
    pr_number = event.payload["pull_request"]["number"]
    reviewer = review["user"]["login"]
    state = review["state"]  # "approved", "changes_requested", "commented"
    
    # 1. Record in approval tracking (closes Gap 1)
    if state == "approved":
        self.registry.record_pr_approval(
            pr_number=pr_number,
            role=f"human:{reviewer}",
            approved=True,
            review_id=review["id"],
        )
    elif state == "changes_requested":
        self.registry.record_pr_approval(
            pr_number=pr_number,
            role=f"human:{reviewer}",
            approved=False,
            review_id=review["id"],
        )
    
    # 2. Notify pipeline engine (re-evaluate gates)
    await self.pipeline_engine.handle_reactive_event(
        pr_number=pr_number,
        event_type="pull_request_review.submitted",
        payload=event.payload,
    )
    
    # 3. Queue inbox event for relevant agents
    await self._queue_inbox_event(event)
```

---

## Pipeline Versioning

**Decision: Snapshot on start.**

When a pipeline run begins, the engine snapshots the pipeline definition at that moment. The run completes with that original definition regardless of config changes made during execution.

- New pipeline definitions only apply to **newly triggered** pipeline runs.
- In-flight pipelines are never affected by config changes.
- The snapshotted definition is stored in the `pipeline_runs.definition_snapshot` column (JSON).

This is the simplest and most predictable behavior. It avoids mid-run inconsistencies where a stage that was valid at start time no longer exists in the new config.

```sql
-- Pipeline runs store their definition snapshot
CREATE TABLE pipeline_runs (
    -- ... other columns ...
    definition_snapshot TEXT NOT NULL,  -- JSON: full pipeline definition at start time
);
```

---

## Unified Registry

**This closes Gap 6.** A single registry manages all state: agents, pipeline runs, stages, gate checks, and PR approvals.

### Schema

```sql
-- ═══════════════════════════════════════════════════════════════
-- PIPELINE STATE
-- ═══════════════════════════════════════════════════════════════

-- Pipeline run instances
CREATE TABLE pipeline_runs (
    run_id TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    definition_snapshot TEXT NOT NULL,   -- JSON: frozen definition at start time
    
    -- Trigger context
    trigger_event TEXT,
    trigger_delivery_id TEXT UNIQUE,
    issue_number INTEGER,
    pr_number INTEGER,                   -- Primary PR (for single-PR pipelines)
    scope TEXT DEFAULT 'single-pr',      -- single-pr | multi-pr | issue
    
    -- Sub-pipeline support
    parent_run_id TEXT REFERENCES pipeline_runs(run_id),
    parent_stage_id TEXT,
    nesting_depth INTEGER DEFAULT 0,
    
    -- Execution state
    status TEXT DEFAULT 'pending',  -- pending | running | completed | failed | cancelled | escalated
    current_stage_id TEXT,
    
    -- Context (JSON)
    context TEXT DEFAULT '{}',
    
    -- Timestamps
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT,
    
    -- Error tracking
    error_message TEXT,
    error_stage_id TEXT
);

CREATE INDEX idx_pipeline_runs_pr ON pipeline_runs(pr_number, status);
CREATE INDEX idx_pipeline_runs_issue ON pipeline_runs(issue_number, status);
CREATE INDEX idx_pipeline_runs_parent ON pipeline_runs(parent_run_id);

-- Pipeline-to-PR association (supports multi-PR pipelines)
CREATE TABLE pipeline_pr_associations (
    pipeline_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    pr_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    stage_id TEXT,
    role TEXT,
    
    PRIMARY KEY(pipeline_run_id, pr_number, repo)
);

-- Stage run instances
CREATE TABLE pipeline_stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    
    -- Execution
    status TEXT DEFAULT 'pending',  -- pending | running | waiting | completed | failed | skipped | cancelled
    agent_id TEXT REFERENCES agents(agent_id),
    
    -- For parallel stages
    branch_id TEXT,
    parent_stage_id TEXT,
    
    -- For sub-pipeline stages
    child_pipeline_run_id TEXT REFERENCES pipeline_runs(run_id),
    
    -- Results (JSON)
    outputs TEXT DEFAULT '{}',
    error_message TEXT,
    
    -- Retry tracking
    attempt_number INTEGER DEFAULT 1,
    max_attempts INTEGER DEFAULT 1,
    
    -- Timing
    started_at TEXT,
    completed_at TEXT,
    
    UNIQUE(run_id, stage_id, attempt_number)
);

-- Gate condition check results
CREATE TABLE pipeline_gate_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_run_id INTEGER NOT NULL REFERENCES pipeline_stage_runs(id) ON DELETE CASCADE,
    check_type TEXT NOT NULL,
    check_config TEXT,
    
    -- Result
    passed BOOLEAN,
    message TEXT,
    result_data TEXT,
    
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Human stage state (notifications, reminders)
CREATE TABLE pipeline_human_stage_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_run_id INTEGER NOT NULL REFERENCES pipeline_stage_runs(id) ON DELETE CASCADE,
    
    -- Notification tracking
    entry_notified_at TEXT,
    last_reminder_at TEXT,
    reminder_count INTEGER DEFAULT 0,
    
    -- Assignment tracking
    assigned_users TEXT,           -- JSON array of GitHub usernames
    
    -- Completion tracking
    completed_by TEXT,             -- GitHub username who completed the action
    completed_action TEXT          -- "approved", "commented", "labeled", etc.
);

-- ═══════════════════════════════════════════════════════════════
-- PR APPROVAL STATE
-- ═══════════════════════════════════════════════════════════════

-- PR review requirements (generated from pipeline stages)
CREATE TABLE pr_review_requirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    role TEXT NOT NULL,
    required_count INTEGER DEFAULT 1,
    pipeline_run_id TEXT REFERENCES pipeline_runs(run_id),
    
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pr_number, role)
);

-- PR approvals (records from both agents AND humans)
CREATE TABLE pr_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    role TEXT NOT NULL,
    approved BOOLEAN NOT NULL,
    review_id TEXT,
    stale BOOLEAN DEFAULT FALSE,
    
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_pr_approvals_pr ON pr_approvals(pr_number, stale);

-- PR review sequence state (for ordered review pipelines)
CREATE TABLE pr_sequence_state (
    pr_number INTEGER NOT NULL,
    current_role TEXT NOT NULL,
    sequence_index INTEGER DEFAULT 0,
    pipeline_run_id TEXT REFERENCES pipeline_runs(run_id),
    
    PRIMARY KEY(pr_number)
);

-- ═══════════════════════════════════════════════════════════════
-- AGENT STATE (extended with pipeline linkage)
-- ═══════════════════════════════════════════════════════════════

-- agents table: add pipeline columns
-- ALTER TABLE agents ADD COLUMN pipeline_run_id TEXT REFERENCES pipeline_runs(run_id);
-- ALTER TABLE agents ADD COLUMN pipeline_stage_id TEXT;
```

### Registry Class

```python
class PipelineRegistry:
    """Unified registry for agents, pipelines, and PR state."""
    
    # ── Agent operations ──
    def register_agent(...) -> str: ...
    def get_agent(agent_id: str) -> AgentRecord: ...
    def update_agent_status(...): ...
    
    # ── Pipeline operations ──
    def create_pipeline_run(...) -> str: ...
    def get_pipeline_run(run_id: str) -> PipelineRun: ...
    def get_running_pipelines(pr_number: int) -> list[PipelineRun]: ...
    def get_running_pipelines_for_issue(issue_number: int) -> list[PipelineRun]: ...
    def update_stage_status(...): ...
    def record_gate_check(...): ...
    
    # ── Sub-pipeline operations ──
    def create_child_pipeline_run(parent_run_id: str, parent_stage_id: str, ...) -> str: ...
    def get_child_pipelines(parent_run_id: str) -> list[PipelineRun]: ...
    
    # ── Multi-PR operations ──
    def associate_pr(run_id: str, pr_number: int, repo: str, stage_id: str): ...
    def get_pipelines_for_pr(pr_number: int, repo: str) -> list[PipelineRun]: ...
    
    # ── Human stage operations ──
    def record_human_stage_notification(stage_run_id: int): ...
    def record_human_stage_reminder(stage_run_id: int): ...
    def complete_human_stage(stage_run_id: int, user: str, action: str): ...
    
    # ── PR approval operations ──
    def set_pr_requirements(...): ...
    def record_pr_approval(...): ...
    def invalidate_pr_approvals(pr_number: int): ...
    def check_pr_merge_ready(...) -> tuple[bool, list[str]]: ...
    
    # ── Cross-cutting queries ──
    def get_agent_for_stage(run_id: str, stage_id: str) -> AgentRecord | None: ...
    def get_pipeline_for_agent(agent_id: str) -> PipelineRun | None: ...
```

---

## Legacy Code Removal

> **DIRECTIVE:** This refactor removes ALL legacy orchestration systems. There is no deprecation period, no backward-compatibility shim. The old code is deleted and replaced cleanly.

### Code to Remove

| Component | Location | Replaced By |
|---|---|---|
| `agent_roles.<role>.triggers` config | `config.py`, `config.yaml` | `pipelines:` trigger definitions |
| `review_policy` config | `config.py:242-317`, `config.yaml:259-318` | Pipeline gate stages with `pr_approvals_met` |
| `ReviewPolicyConfig` model | `config.py:317` | Pipeline definition models |
| `ReviewRequirement` model | `config.py:269` | Pipeline stage definitions |
| `ReviewRule` model | `config.py:308` | Pipeline trigger conditions |
| `_auto_merge_pr` callback | `agent_manager.py:3638` | `type: action, action: merge_pr` stage |
| `_handle_merge_failure` | `agent_manager.py:3781` | Pipeline `on_conflict` / `on_error` handlers |
| `WorkflowEngine` class | `workflow/engine.py` | `PipelineEngine` |
| `WorkflowRegistryV2` | `workflow/registry.py` | `PipelineRegistry` |
| `WorkflowConfig` model | `config.py:471+` | Pipeline definition models |
| `StageDefinition` (old) | `config.py` | Pipeline stage models |
| `GateCondition` (old) | `config.py:553` | `GateCheck` ABC + registry |
| Trigger dispatch in event router | `event_router.py` | Pipeline trigger matching |
| `workflows:` config section | `config.yaml:337-377` | `pipelines:` config section |
| `set_pr_requirements` (old) | `registry.py:528` | `PipelineRegistry.set_pr_requirements` |
| `record_pr_approval` (old) | `registry.py:605` | `PipelineRegistry.record_pr_approval` |
| `check_pr_merge_ready` (old) | `registry.py:766` | `PipelineRegistry.check_pr_merge_ready` |
| `invalidate_pr_approvals` (old) | `registry.py:681` | `PipelineRegistry.invalidate_pr_approvals` |
| PR approval SQL tables (old) | `registry.py` | `PipelineRegistry` unified schema |

### Config Migration

The `config.yaml` structure changes from:

```yaml
# OLD — removed entirely
agent_roles:
  feat-dev:
    triggers:
      - event: issues.labeled
        label: feature
review_policy:
  enabled: true
  default_requirements: [...]
  rules: [...]
workflows:
  feature-dev-pipeline: [...]
```

To:

```yaml
# NEW — pipelines are the only orchestration config
agent_roles:
  feat-dev:
    agent_definition: agents/feat-dev.md
    lifecycle: stateful
    # NO triggers — pipelines handle all orchestration

pipelines:
  feature-dev:
    trigger:
      event: issues.labeled
      conditions:
        label: feature
    stages: [...]
  
  pr-lifecycle:
    trigger:
      event: pull_request.opened
    stages: [...]
```

Agent role definitions (`agent_definition`, `lifecycle`, model config, circuit breakers) remain in `agent_roles`. Only orchestration (triggers, review policy, workflows) moves to `pipelines`.

---

## Pipeline Engine Design

### Core Engine

```python
class PipelineEngine:
    """Executes pipeline definitions."""
    
    MAX_NESTING_DEPTH = 3
    
    def __init__(
        self,
        registry: PipelineRegistry,
        gate_registry: GateCheckRegistry,
        agent_manager: "AgentManager",
        github_client: "GitHubClient",
    ):
        self.registry = registry
        self.gate_registry = gate_registry
        self.agent_manager = agent_manager
        self.github = github_client
    
    async def start_pipeline(
        self,
        definition: PipelineDefinition,
        trigger_event: SquadronEvent,
        parent_run_id: str | None = None,
        parent_stage_id: str | None = None,
    ) -> str:
        """Start a new pipeline run."""
        # Check nesting depth
        nesting_depth = 0
        if parent_run_id:
            parent = self.registry.get_pipeline_run(parent_run_id)
            nesting_depth = parent.nesting_depth + 1
            if nesting_depth > self.MAX_NESTING_DEPTH:
                raise PipelineError(
                    f"Max sub-pipeline nesting depth ({self.MAX_NESTING_DEPTH}) exceeded"
                )
        
        # Snapshot definition
        run_id = self.registry.create_pipeline_run(
            pipeline_name=definition.name,
            definition_snapshot=definition.to_json(),
            trigger_event=trigger_event,
            context=definition.resolve_context(trigger_event),
            parent_run_id=parent_run_id,
            parent_stage_id=parent_stage_id,
            nesting_depth=nesting_depth,
        )
        
        # Start first stage
        first_stage = definition.stages[0]
        await self._execute_stage(run_id, first_stage)
        
        return run_id
    
    async def handle_reactive_event(
        self,
        pr_number: int,
        event_type: str,
        payload: dict,
    ):
        """Handle an event that affects running pipelines."""
        runs = self.registry.get_running_pipelines(pr_number=pr_number)
        
        for run in runs:
            definition = PipelineDefinition.from_json(run.definition_snapshot)
            reactive_config = definition.on_events.get(event_type)
            if not reactive_config:
                continue
            
            action = reactive_config["action"]
            
            if action == "reevaluate_gates":
                await self._reevaluate_gates(run, event_type)
            elif action == "invalidate_and_restart":
                await self._invalidate_and_restart(run, reactive_config)
            elif action == "cancel":
                await self._cancel_pipeline(run)
            elif action == "wake_agent":
                await self._wake_stage_agent(run, reactive_config, payload)
    
    async def _execute_stage(self, run_id: str, stage: StageDefinition):
        """Execute a pipeline stage."""
        if stage.type == "agent":
            await self._execute_agent_stage(run_id, stage)
        elif stage.type == "gate":
            await self._execute_gate_stage(run_id, stage)
        elif stage.type == "human":
            await self._execute_human_stage(run_id, stage)
        elif stage.type == "parallel":
            await self._execute_parallel_stage(run_id, stage)
        elif stage.type == "action":
            await self._execute_action_stage(run_id, stage)
        elif stage.type == "delay":
            await self._execute_delay_stage(run_id, stage)
        elif stage.type == "pipeline":
            await self._execute_sub_pipeline_stage(run_id, stage)
    
    async def _execute_gate_stage(self, run_id: str, stage: StageDefinition):
        """Execute a gate stage — evaluate conditions, wait if needed."""
        self.registry.update_stage_status(run_id, stage.id, "waiting")
        
        all_passed = True
        for condition in stage.conditions:
            check_cls = self.gate_registry.get(condition["check"])
            check = check_cls()
            context = self._build_gate_context(run_id)
            result = await check.evaluate(condition, context)
            
            self.registry.record_gate_check(
                run_id=run_id,
                stage_id=stage.id,
                check_type=condition["check"],
                passed=result.passed,
                message=result.message,
                result_data=result.data,
            )
            
            if not result.passed:
                all_passed = False
        
        if all_passed:
            await self._transition(run_id, stage, "pass")
        # else: remain in WAITING state — reactive events will re-trigger
    
    async def _execute_human_stage(self, run_id: str, stage: StageDefinition):
        """Execute a human stage — notify and wait for human action."""
        self.registry.update_stage_status(run_id, stage.id, "waiting")
        
        # Send entry notification
        if stage.notify and stage.notify.on_enter:
            context = self._build_context(run_id)
            message = self._resolve_template(stage.notify.on_enter, context)
            await self._send_notification(stage.from_group, message, run_id)
            self.registry.record_human_stage_notification(...)
        
        # Auto-assign if configured
        if stage.auto_assign:
            await self._assign_reviewers(run_id, stage)
        
        # Schedule reminder timer
        if stage.notify and stage.notify.reminder:
            await self._schedule_reminder(run_id, stage)
    
    async def _execute_sub_pipeline_stage(self, run_id: str, stage: StageDefinition):
        """Execute a sub-pipeline stage — start child pipeline."""
        self.registry.update_stage_status(run_id, stage.id, "running")
        
        child_definition = self._get_pipeline_definition(stage.pipeline)
        if not child_definition:
            raise PipelineError(f"Sub-pipeline '{stage.pipeline}' not found")
        
        # Merge parent context into child
        parent_context = self._build_context(run_id)
        child_context = {**parent_context, **(stage.context or {})}
        
        child_run_id = await self.start_pipeline(
            definition=child_definition,
            trigger_event=None,  # Sub-pipelines don't have trigger events
            parent_run_id=run_id,
            parent_stage_id=stage.id,
        )
        
        self.registry.update_stage_child_pipeline(
            run_id, stage.id, child_run_id
        )
    
    async def _on_child_pipeline_completed(self, child_run_id: str):
        """Called when a sub-pipeline completes — advance parent stage."""
        child_run = self.registry.get_pipeline_run(child_run_id)
        if not child_run.parent_run_id:
            return
        
        parent_stage = self.registry.get_stage_for_child(child_run_id)
        
        if child_run.status == "completed":
            await self._transition(child_run.parent_run_id, parent_stage, "pass")
        elif child_run.status in ("failed", "escalated"):
            await self._handle_stage_error(child_run.parent_run_id, parent_stage)
    
    async def _reevaluate_gates(self, run: PipelineRun, event_type: str):
        """Re-evaluate gate conditions when a reactive event fires."""
        current_stage = run.current_stage
        if current_stage.type not in ("gate", "human"):
            return
        
        if current_stage.type == "human":
            await self._check_human_stage_completion(run, event_type)
            return
        
        # Only re-evaluate checks that react to this event type
        reactive_mapping = self.gate_registry.get_reactive_events()
        relevant_checks = reactive_mapping.get(event_type, set())
        
        all_passed = True
        for condition in current_stage.conditions:
            check_name = condition["check"]
            
            if check_name in relevant_checks:
                # Re-evaluate this check
                check_cls = self.gate_registry.get(check_name)
                check = check_cls()
                context = self._build_gate_context(run.run_id)
                result = await check.evaluate(condition, context)
                self.registry.record_gate_check(...)
                if not result.passed:
                    all_passed = False
            else:
                # Use cached result
                cached = self.registry.get_latest_gate_check(
                    run.run_id, current_stage.id, check_name
                )
                if not cached or not cached.passed:
                    all_passed = False
        
        if all_passed:
            await self._transition(run.run_id, current_stage, "pass")
```

---

## Implementation Phases

### Phase 1: Foundation (2-3 weeks)

**Goal:** Pipeline engine exists and works for new pipeline definitions.

- [ ] Pipeline definition Pydantic models (all 7 stage types)
- [ ] `PipelineEngine` core: start, stage execution, transitions
- [ ] `GateCheckRegistry` with built-in checks: `command`, `file_exists`, `pr_approvals_met`, `ci_status`
- [ ] Unified `PipelineRegistry` with full SQL schema
- [ ] Agent stage execution (spawn/wake agent, detect completion)
- [ ] Gate stage execution with re-evaluation
- [ ] Action stage: `merge_pr`
- [ ] Event Router rewrite: pipeline trigger matching replaces legacy trigger dispatch
- [ ] Unit tests for engine, gate checks, and registry

### Phase 2: Human Stages + Approval Recording (1-2 weeks)

**Goal:** Close Gap 1 (human reviews not tracked), add human stage type.

- [ ] `human` stage type: notification, reminders, auto-assignment, completion detection
- [ ] Framework-level `record_pr_approval()` in `_handle_pr_review_submitted`
- [ ] Implement remaining gate checks: `no_changes_requested`, `human_approved`, `label_present`, `branch_up_to_date`
- [ ] Reactive event subscription: gate + human stage re-evaluation on review/CI events
- [ ] `on_events` reactive config support
- [ ] `invalidate_and_restart` action for `pull_request.synchronize`
- [ ] `pipeline_human_stage_state` table and reminder scheduling
- [ ] Integration tests with mock GitHub events

### Phase 3: Legacy Removal + Config Migration (2 weeks)

**Goal:** Remove all legacy orchestration code. Rewrite config.yaml to use pipelines.

- [ ] Delete `agent_roles.<role>.triggers` config support
- [ ] Delete `review_policy` config section and all related models
- [ ] Delete `ReviewPolicyConfig`, `ReviewRequirement`, `ReviewRule`, `MatchCondition`, `AutoMergeConfig`, `SynchronizeConfig` from `config.py`
- [ ] Delete `_auto_merge_pr` callback and `_handle_merge_failure` from `agent_manager.py`
- [ ] Delete `src/squadron/workflow/` directory entirely (engine.py, registry.py)
- [ ] Delete `WorkflowConfig`, `StageDefinition` (old), `GateCondition` (old) from `config.py`
- [ ] Delete legacy trigger dispatch from `event_router.py`
- [ ] Delete old PR approval tables from `registry.py` (migrated to `PipelineRegistry`)
- [ ] Delete `workflows:` config section support
- [ ] Rewrite `.squadron/config.yaml` with `pipelines:` replacing triggers, review_policy, and workflows
- [ ] Parallel stage execution (`type: parallel`, join strategies)
- [ ] Update all tests

### Phase 4: Sub-Pipelines + Multi-PR (2 weeks)

**Goal:** Pipeline composition and multi-PR support.

- [ ] `type: pipeline` stage type with sub-pipeline invocation
- [ ] Context propagation parent-to-child and child output collection
- [ ] Cycle detection at config load time (BFS through pipeline references)
- [ ] Nesting depth enforcement
- [ ] Child pipeline completion → parent stage advancement
- [ ] Multi-PR pipeline scope: `pipeline_pr_associations` table
- [ ] Cross-PR gate checks (PR-specific `pr_approvals_met`, `ci_status`)
- [ ] Cross-PR reactive event routing
- [ ] Pipeline versioning: definition snapshot on start

### Phase 5: Advanced Features (2 weeks)

**Goal:** Full feature set.

- [ ] Custom gate check plugin loading from Python modules
- [ ] `delay` stage type with poll conditions
- [ ] `webhook` stage type
- [ ] Conditional stage execution (`condition:` with `any`/`all` logic)
- [ ] Context propagation and template resolution (`{{ }}` expressions)
- [ ] Output validation on agent stages
- [ ] Pipeline visibility in dashboard
- [ ] Pipeline status CLI commands

---

## Relationship to Existing Design Documents

### `workflow-system-v2.md`

This document **supersedes and replaces** the workflow system v2 design. The pipeline system incorporates v2's concepts (stage types, state machine, context propagation, DB schema) but adds:

- Reactive event subscriptions (v2 workflows were fire-and-forget)
- Pluggable gate checks (v2 only had `command` and `file_exists`)
- Framework-level approval recording (v2 didn't address this)
- Sub-pipeline composition (v2 left this as an open question)
- Multi-PR pipeline scope (v2 was single-PR only)
- `human` stage type (v2 had no human-specific stage)
- Unified registry (v2 had a separate `WorkflowRegistryV2`)

The v2 workflow engine code is deleted as part of this refactor.

### `approval-flow-schema.md`

The approval flow schema research is **incorporated** into this design. The `pr_approvals_met`, `human_approved`, and `no_changes_requested` gate checks implement the approval flow concepts. The `protected_paths` and `escalation` sections from the approval schema can be expressed as pipeline stage conditions and `on_error` handlers.

The separate `.squadron/workflows/approval-flows.yaml` file proposed in that research is replaced by inline pipeline definitions.

### Architecture Decisions

- **AD-006** (configurable per-branch approvals): Implemented via pipeline `trigger.conditions.base_branch` and branch-specific pipeline definitions.
- **AD-009** (fully configurable approval flows): Implemented via pipeline gate conditions and `human` stages.
- **AD-013** (Agent Registry): Extended with pipeline state into unified `PipelineRegistry`.
- **AD-015** (approval flow schema): Subsumed into pipeline gate checks and human stages.
- **AD-017** (runtime architecture): Pipeline engine is a new component within the existing monolith.
- **AD-018** (circuit breakers): Agent stages respect circuit breaker limits. Pipeline-level timeouts are an additional layer.

---

## Resolved Design Decisions

These were originally open questions, now resolved:

1. **Pipeline versioning** — **Snapshot on start.** In-flight pipelines complete with the original definition. New config applies only to new runs.

2. **Multi-PR pipelines** — **Yes, supported.** Pipelines can declare `scope: multi-pr` and track multiple PRs via `pipeline_pr_associations`. Gate checks support per-PR evaluation.

3. **Pipeline composition** — **Yes, sub-pipelines supported.** A `type: pipeline` stage invokes another pipeline by name. Max nesting depth of 3. Cycle detection at config load. Pipelines without triggers are reusable sub-pipeline templates.

4. **Legacy trigger conflict** — **No conflict — legacy is removed.** All legacy orchestration (triggers, review_policy, workflows) is deleted. Pipelines are the only orchestration mechanism. No backward-compat shim.

5. **Gate timeout behavior** — **Configurable per-gate.** Each gate's `on_timeout` independently specifies: fail, escalate, extend (with max), notify, or cancel.

6. **Human-in-the-loop** — **First-class `human` stage type.** Dedicated stage with notification lifecycle, reminders, auto-assignment, and wait-for semantics. Distinct from `gate` because it models a single human interaction pattern.

---

## Summary

The unified pipeline system replaces three fragmented orchestration mechanisms with a single, coherent model:

| Current System | Unified Pipeline Equivalent |
|---|---|
| `triggers` (spawn/wake/complete) | Pipeline trigger + agent stages |
| `review_policy` (rules, sequences, auto-merge) | Pipeline gate + human + action stages |
| Workflow Engine v2 (stages, gates) | Pipeline stages and gate checks (native) |
| `_auto_merge_pr` callback | `type: action, action: merge_pr` stage |
| `record_pr_approval` (agent-only) | Framework-level recording (agent + human) |
| `AgentRegistry` + `WorkflowRegistryV2` | Single `PipelineRegistry` |
| No human stage | `type: human` with notifications + reminders |
| No sub-pipelines | `type: pipeline` for composition |
| Single-PR only | `scope: multi-pr` for cross-PR orchestration |

All legacy orchestration code is removed. Pipelines are the sole orchestration primitive.
