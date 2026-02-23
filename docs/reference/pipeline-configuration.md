# Pipeline Configuration Reference

Squadron uses a YAML-driven **unified pipeline system** to orchestrate all agent spawning, review automation, approval gates, and merge workflows. Pipelines are defined in the `pipelines:` section of `.squadron/config.yaml` and are the single orchestration primitive for all event-driven automation.

## Overview

A pipeline defines:
1. **When** to run (trigger event + conditions)
2. **What** to do (ordered list of stages)
3. **How** to react to events during execution (reactive event handlers)
4. **What** to do when it completes or fails (lifecycle hooks)

```yaml
# .squadron/config.yaml
pipelines:
  my-pipeline:
    description: "Human-readable description"
    trigger:
      event: issues.labeled
      conditions:
        label: feature
    scope: issue
    stages:
      - id: first-stage
        type: agent
        agent: feat-dev
        action: "Implement this feature"
        timeout: 2h
    on_complete:
      - type: comment
        body: "Pipeline complete."
    on_error:
      - type: label
        add: needs-human
```

---

## Pipeline-Level Fields

### `description` (string)
Human-readable description of the pipeline's purpose. Shown in dashboard and CLI output.

### `trigger` (object)
Defines the GitHub event that activates this pipeline.

| Field | Type | Description |
|-------|------|-------------|
| `event` | string | GitHub webhook event (e.g. `issues.opened`, `pull_request.opened`, `issues.labeled`) |
| `conditions` | object | Additional matching conditions |

**Supported condition keys:**
- `label` — matches the `label.name` from a `*.labeled` event
- `base_branch` — matches the PR's base branch ref

```yaml
trigger:
  event: issues.labeled
  conditions:
    label: bug
```

Pipelines without a `trigger` are **sub-pipelines** — they can only be invoked by other pipelines via `type: pipeline` stages.

### `scope` (string)
Controls how the pipeline tracks its target resource. Defaults to `single-pr`.

| Value | Description |
|-------|-------------|
| `single-pr` | Pipeline tracks a single PR |
| `multi-pr` | Pipeline can track multiple PRs (e.g. feature dev that opens a PR) |
| `issue` | Pipeline tracks an issue |

### `on_events` (object)
Reactive event handlers that fire when GitHub events occur **during** an active pipeline run. Keys are GitHub event types.

```yaml
on_events:
  pull_request_review.submitted:
    action: reevaluate_gates
  pull_request.synchronize:
    action: invalidate_and_restart
    invalidate:
      - security-review
      - code-review
    restart_from: test-coverage
  pull_request.closed:
    action: cancel
```

**Available reactive actions:**

| Action | Description |
|--------|-------------|
| `reevaluate_gates` | Re-check all gate conditions on the current gate stage |
| `invalidate_and_restart` | Mark specified stages as invalid and restart from a given stage |
| `cancel` | Cancel the pipeline run |
| `notify` | Send a notification (uses `notify:` config) |
| `wake_agent` | Wake the currently sleeping agent in the pipeline |

**Reactive event config fields:**
- `action` — the reactive action to take
- `invalidate` — list of stage IDs to invalidate (for `invalidate_and_restart`)
- `restart_from` — stage ID to restart execution from (for `invalidate_and_restart`)
- `notify` — notification config (for `notify` action)
- `context` — additional context to inject

### `context` (object)
Static key-value pairs available to all stages via template expressions. Useful for pipeline-wide configuration.

```yaml
context:
  require_tests: true
  test_coverage_threshold: 80
```

Access in templates: `{{ context.require_tests }}`

### `on_complete` (list)
Actions to execute when the pipeline completes successfully.

```yaml
on_complete:
  - type: comment
    body: "Pipeline complete — PR merged."
  - type: label
    add: deployed
```

### `on_error` (list)
Actions to execute when the pipeline encounters an error.

```yaml
on_error:
  - type: label
    add: needs-human
  - type: comment
    body: "Pipeline failed. Human review required."
```

---

## Stage Types

Every stage requires `id` (unique within the pipeline) and `type`. Stage IDs must match the pattern `^[a-zA-Z][a-zA-Z0-9_-]*$`.

### `agent` — Spawn an LLM Agent

Spawns an agent to perform work. The agent runs asynchronously; the pipeline waits for it to complete.

```yaml
- id: implement
  type: agent
  agent: feat-dev
  action: "Implement this feature and open a PR"
  timeout: 2h
  on_complete: quality-gate
  on_error:
    retry: 1
    then: escalate
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `agent` | string | yes | Agent role name (must match an `agent_roles` entry) |
| `action` | string | no | Action prompt passed to the agent |
| `continue_session` | bool | no | Resume existing agent session instead of creating new (default: false) |
| `timeout` | string | no | Max execution time (e.g. `30m`, `2h`) |
| `expected_outputs` | list | no | Output keys the agent must produce for validation |

### `gate` — Check Conditions

Evaluates one or more gate conditions. All conditions must pass (AND logic) unless `any_of` is used (OR logic).

```yaml
- id: approval-gate
  type: gate
  conditions:
    - check: pr_approvals_met
      count: 1
    - check: no_changes_requested
    - check: ci_status
      workflows: ["CI"]
  on_pass: auto-merge
  on_fail:
    goto: code-review
    max_iterations: 3
    then: escalate
  timeout: 48h
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `conditions` | list | yes* | Gate conditions (AND — all must pass) |
| `any_of` | list | yes* | Gate conditions (OR — any must pass) |
| `on_pass` | string | no | Stage ID to go to when gates pass |
| `on_fail` | object/string | no | Stage ID or config for gate failure |
| `timeout` | string | no | Max time to wait for gates to pass |
| `on_timeout` | object | no | Timeout behavior (notify, then, extend) |

*One of `conditions` or `any_of` is required.

**`on_fail` config (when object):**
- `goto` — stage ID to transition to
- `max_iterations` — max retry cycles before escalating
- `then` — what to do after max iterations (`escalate`, `fail`, `cancel`)

#### Built-in Gate Checks

| Check Name | Description | Config Fields |
|------------|-------------|---------------|
| `command` | Run a shell command and check exit code | `run`, `expect` |
| `file_exists` | Check if a file exists in the repo | `paths` |
| `pr_approvals_met` | Check PR has required number of approvals | `count` |
| `ci_status` | Check GitHub CI/Actions status | `workflows`, `scope` |
| `label_present` | Check if a label is present on the PR/issue | `label` |
| `no_changes_requested` | Check no reviewers have requested changes | — |
| `human_approved` | Check human stage was completed | — |
| `branch_up_to_date` | Check branch is up to date with base | — |

**Examples:**

```yaml
# Shell command gate
- check: command
  run: python -m pytest tests/
  expect: "exit_code == 0"

# CI status gate
- check: ci_status
  workflows: ["CI", "lint"]

# PR approval gate
- check: pr_approvals_met
  count: 2

# Label gate
- check: label_present
  label: approved
```

#### Custom Gate Checks

Define custom gate checks by providing a Python module path in your config. The module must export a class that subclasses `GateCheck`:

```yaml
# In pipeline_settings or custom_gates config
custom_gates:
  my_check: "mypackage.gates.MyCheck"
```

The class must implement:
- `check_name` — property returning the check name
- `evaluate(context, config)` — async method returning `GateCheckResult`

### `human` — Wait for Human Action

Pauses the pipeline until a human performs a specified action (approval, comment, label, or dismiss).

```yaml
- id: human-approval
  type: human
  human:
    description: "Review and approve deployment to production"
    wait_for: approval
    from: maintainers
    count: 1
    auto_assign: true
    notify:
      on_enter: "Pipeline is waiting for your approval."
      reminder:
        interval: 24h
        message: "Reminder: deployment approval is still pending."
        max_reminders: 3
  timeout: 72h
  on_timeout:
    notify:
      target: maintainers
      message: "Human approval timed out after 72h"
    then: escalate
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `human.description` | string | no | Description shown to the human |
| `human.wait_for` | string | no | Action type: `approval`, `comment`, `label`, `dismiss` (default: `approval`) |
| `human.from` | string | no | Human group name (from `human_groups` config) |
| `human.count` | int | no | Number of completions required (default: 1) |
| `human.auto_assign` | bool | no | Auto-assign users from the group (default: true) |
| `human.notify` | object | no | Notification config |

### `parallel` — Fan-Out Execution

Runs multiple branches concurrently with a configurable join strategy.

```yaml
- id: parallel-reviews
  type: parallel
  join: all  # or "any"
  branches:
    - id: security-review
      type: agent
      agent: security-review
      action: "Review security aspects"
    - id: code-review
      type: agent
      agent: pr-review
      action: "Review code quality"
    - id: notify-deploy
      type: pipeline
      pipeline: deploy-notification
  on_complete: approval-gate
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `join` | string | no | Join strategy: `all` (wait for all, default) or `any` (first completion) |
| `branches` | list | yes | Branch definitions |

**Branch fields:**
- `id` — unique branch ID
- `type` — stage type for this branch (`agent`, `pipeline`, `action`)
- `agent` / `pipeline` / `action` — type-specific config
- `condition` — optional condition for branch execution
- `timeout` — branch timeout
- `config` — config object (for action branches)
- `context` — extra context (for sub-pipeline branches)

### `delay` — Timed Wait

Pauses pipeline execution for a specified duration, optionally polling a condition.

```yaml
- id: cool-down
  type: delay
  duration: 5m
  poll:
    check: ci_status
    interval: 30s
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `duration` | string | yes | Wait duration (e.g. `30s`, `5m`, `2h`, `1d`) |
| `poll` | object | no | Optional polling condition |

### `action` — Execute Built-in Action

Executes a built-in action against the GitHub API.

```yaml
- id: auto-merge
  type: action
  action: merge_pr
  config:
    method: squash
    delete_branch: true
  on_error:
    retry: 1
    then: escalate
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | string | yes | Action name (see table below) |
| `config` | object | no | Action-specific configuration |

**Built-in actions:**

| Action | Description | Config Fields |
|--------|-------------|---------------|
| `merge_pr` | Merge the PR | `method` (merge/squash/rebase), `delete_branch` (bool) |
| `close_pr` | Close the PR | — |
| `add_label` | Add a label to the PR/issue | `label` |
| `remove_label` | Remove a label from the PR/issue | `label` |
| `comment` | Post a comment on the PR/issue | `message` |

### `webhook` — HTTP Request

Sends an HTTP request and validates the response.

```yaml
- id: notify-slack
  type: webhook
  request:
    url: "https://hooks.slack.com/services/{{ context.slack_webhook }}"
    method: POST
    headers:
      Content-Type: application/json
    body:
      text: "Pipeline {{ context.pipeline_name }} completed"
  expect:
    status: 200
  on_error:
    retry: 2
    then: fail
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request.url` | string | yes | Target URL (supports templates) |
| `request.method` | string | no | HTTP method (default: POST) |
| `request.headers` | object | no | Request headers (supports templates) |
| `request.body` | object | no | Request body (supports templates) |
| `expect` | object | no | Response validation (e.g. `status: 200`) |
| `expected_outputs` | list | no | Output keys to validate from response |

### `pipeline` — Invoke Sub-Pipeline

Invokes another pipeline definition as a child pipeline. The child pipeline runs independently and the parent waits for it to complete.

```yaml
- id: deploy
  type: pipeline
  pipeline: deploy-to-staging
  context:
    environment: staging
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `pipeline` | string | yes | Name of the pipeline to invoke (must exist in `pipelines:` config) |
| `context` | object | no | Additional context passed to the child pipeline |

Sub-pipelines:
- Support nesting up to 5 levels deep
- Inherit the parent's PR/issue context
- Can be cancelled by cancelling the parent (cascading cancel)
- Must be defined in the same `pipelines:` config section (without a `trigger`)

---

## Stage Transitions

Stages advance through explicit transitions or sequential fallthrough:

```yaml
stages:
  - id: review
    type: agent
    agent: pr-review
    action: "Review this PR"
    on_complete: approval-gate   # Explicit transition

  - id: approval-gate
    type: gate
    conditions:
      - check: pr_approvals_met
        count: 1
    on_pass: merge               # Explicit on gate pass
    on_fail:
      goto: review               # Loop back on failure
      max_iterations: 3
      then: escalate

  - id: merge
    type: action
    action: merge_pr
    # No on_complete → pipeline completes
```

### Transition Fields (all stage types)

| Field | Type | Description |
|-------|------|-------------|
| `on_complete` | string/object | Next stage after successful completion |
| `on_pass` | string/object | Next stage when gate passes |
| `on_fail` | string/object | Next stage when gate fails |
| `on_success` | string/object | Next stage on action success |
| `on_error` | object | Error handling config |
| `on_conflict` | string | Stage to go to on merge conflict |
| `on_timeout` | object | Timeout behavior config |
| `skip_to` | string | Stage to jump to when condition is not met |
| `timeout` | string | Max execution time |

### `on_error` Config

```yaml
on_error:
  retry: 2          # Number of retries before giving up
  then: escalate    # What to do after retries: stage ID, "escalate", or "fail"
```

### `on_timeout` Config (gate stages)

```yaml
on_timeout:
  notify:
    target: maintainers
    message: "Gate timed out"
  then: escalate          # "fail", "escalate", or "cancel"
  extend: 24h             # Extend the timeout
  max_extensions: 2       # Max number of extensions
```

---

## Conditional Execution

Stages can be conditionally skipped using the `condition` field:

```yaml
- id: security-review
  type: agent
  agent: security-review
  action: "Review security aspects"
  condition:
    any:
      - labels_include: security
      - paths_match:
          - "src/**/auth/**"
          - "src/**/crypto/**"
  skip_to: code-review   # Stage to jump to if condition is not met
```

Supported condition operators:
- `labels_include` — check if a label is present
- `paths_match` — check if changed files match glob patterns
- `any` — OR logic (any sub-condition must be true)
- `all` — AND logic (all sub-conditions must be true)

---

## Template Expressions

Pipeline YAML values can contain `{{ expression }}` template expressions that are resolved at runtime against the pipeline's context.

### Syntax

```yaml
action: "Fix bug #{{ context.issue_number }}"
url: "https://api.example.com/pr/{{ context.pr_number }}"
body:
  pipeline: "{{ context.pipeline_name }}"
  status: "{{ context.status }}"
```

### Available Namespaces

| Namespace | Description |
|-----------|-------------|
| `context` | Pipeline run context (pr_number, issue_number, custom values) |
| `trigger` | Original trigger event payload |
| `stages` | Output data from completed stages |
| `branches` | Output data from parallel branches |

### Filters

Expressions support filter functions with the `|` operator:

```yaml
value: "{{ context.pr_number | str }}"        # Convert to string
value: "{{ context.count | int }}"             # Convert to integer
value: "{{ context.name | default('unknown') }}"  # Default value
```

### Comparisons

```yaml
condition: "{{ context.pr_number != null }}"
condition: "{{ context.environment == 'production' }}"
```

### Type Preservation

When a template expression is the **entire** string value, the resolved value preserves its original type. For example, `"{{ context.count }}"` resolves to an integer if `context.count` is an integer. Mixed strings (text + expressions) always resolve to strings.

---

## Complete Example: PR Lifecycle Pipeline

This example shows a full PR review, approval, and auto-merge pipeline:

```yaml
pipelines:
  pr-lifecycle:
    description: "PR review, approval gates, and auto-merge"
    scope: single-pr
    trigger:
      event: pull_request.opened
      conditions:
        base_branch: main
    on_events:
      pull_request.synchronize:
        action: invalidate_and_restart
        invalidate:
          - security-review
          - code-review
        restart_from: test-coverage
      pull_request_review.submitted:
        action: reevaluate_gates
      check_suite.completed:
        action: reevaluate_gates
      pull_request.closed:
        action: cancel
    stages:
      - id: test-coverage
        type: agent
        agent: test-coverage
        action: "Review test coverage for this PR"
        timeout: 30m
        on_complete: security-review
        on_error:
          retry: 1
          then: escalate

      - id: security-review
        type: agent
        agent: security-review
        action: "Perform security review"
        condition:
          any:
            - labels_include: security
            - paths_match:
                - "src/**/auth/**"
                - "src/**/crypto/**"
        skip_to: code-review
        timeout: 30m
        on_complete: code-review

      - id: code-review
        type: agent
        agent: pr-review
        action: "Perform code review"
        timeout: 30m
        on_complete: approval-gate

      - id: approval-gate
        type: gate
        conditions:
          - check: pr_approvals_met
            count: 1
          - check: no_changes_requested
        on_pass: auto-merge
        on_fail:
          goto: code-review
          max_iterations: 3
          then: escalate
        timeout: 48h

      - id: auto-merge
        type: action
        action: merge_pr
        config:
          method: squash
          delete_branch: true

    on_complete:
      - type: comment
        body: "Pipeline complete — PR merged."
    on_error:
      - type: label
        add: needs-human
      - type: comment
        body: "PR lifecycle pipeline encountered an error."
```

---

## Complete Example: Issue-to-Merge Lifecycle

This example shows a full development lifecycle from issue label to working code:

```yaml
pipelines:
  feature-dev-lifecycle:
    description: "Full feature development from issue to code"
    scope: issue
    trigger:
      event: issues.labeled
      conditions:
        label: feature
    on_events:
      pull_request_review.submitted:
        action: wake_agent
      issue_comment.created:
        action: wake_agent
      pull_request.closed:
        action: wake_agent
    stages:
      - id: implement
        type: agent
        agent: feat-dev
        action: "Implement this feature and open a PR"
        timeout: 2h
        on_error:
          retry: 1
          then: escalate
```

---

## Duration Format

All timeout and duration values use the format `<number><unit>`:

| Unit | Description | Example |
|------|-------------|---------|
| `s` | Seconds | `30s` |
| `m` | Minutes | `5m` |
| `h` | Hours | `2h` |
| `d` | Days | `1d` |

---

## Validation

Pipeline definitions are validated at server startup. Validation checks include:

- All stage IDs are unique within a pipeline
- Stage ID format matches `^[a-zA-Z][a-zA-Z0-9_-]*$`
- Required fields per stage type are present (e.g. `agent` for agent stages, `conditions` for gate stages)
- All `on_complete`, `on_pass`, `on_fail`, `skip_to` references point to existing stage IDs
- Sub-pipeline references resolve to existing pipeline definitions
- No circular sub-pipeline references

Validation errors are logged at startup and accessible via `PipelineEngine.validate_all_pipelines()`.
