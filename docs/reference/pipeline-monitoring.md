# Pipeline Monitoring and Interaction

Squadron provides multiple interfaces for monitoring pipeline execution and interacting with running pipelines: a REST API dashboard, a CLI, and real-time SSE streaming. This guide covers all end-user interfaces for the pipeline system.

## Dashboard REST API

All dashboard endpoints are prefixed with `/dashboard/` and require Bearer token authentication (except SSE, which uses a query parameter token).

### Authentication

```bash
# REST endpoints
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://your-squadron-url/dashboard/pipelines

# SSE endpoint
curl "https://your-squadron-url/dashboard/pipelines/stream?token=YOUR_API_KEY"
```

### List Pipeline Definitions

Returns all registered pipeline definitions with their trigger configuration and stage summary.

```
GET /dashboard/pipelines
```

**Response:**
```json
{
  "count": 8,
  "pipelines": [
    {
      "name": "pr-lifecycle",
      "description": "PR review, approval gates, and auto-merge pipeline",
      "scope": "single-pr",
      "trigger": {
        "event": "pull_request.opened",
        "conditions": {"base_branch": "main"}
      },
      "stage_count": 5,
      "stages": [
        {"id": "test-coverage", "type": "agent"},
        {"id": "security-review", "type": "agent"},
        {"id": "code-review", "type": "agent"},
        {"id": "approval-gate", "type": "gate"},
        {"id": "auto-merge", "type": "action"}
      ],
      "reactive_events": [
        "pull_request.synchronize",
        "pull_request_review.submitted",
        "check_suite.completed",
        "pull_request.closed"
      ]
    }
  ]
}
```

### List Pipeline Runs

Returns pipeline runs with pagination and filtering. Results are ordered newest-first.

```
GET /dashboard/pipelines/runs
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | Results per page (1-500) |
| `offset` | int | 0 | Pagination offset |
| `status` | string | — | Filter: `pending`, `running`, `completed`, `failed`, `cancelled`, `escalated` |
| `pipeline_name` | string | — | Filter by pipeline definition name |
| `pr_number` | int | — | Filter by PR number |
| `issue_number` | int | — | Filter by issue number |

**Response:**
```json
{
  "total": 142,
  "count": 50,
  "offset": 0,
  "runs": [
    {
      "run_id": "run_abc123",
      "pipeline_name": "pr-lifecycle",
      "status": "running",
      "trigger_event": "pull_request.opened",
      "issue_number": null,
      "pr_number": 45,
      "scope": "single-pr",
      "parent_run_id": null,
      "current_stage_id": "code-review",
      "created_at": "2026-02-20T14:30:00Z",
      "started_at": "2026-02-20T14:30:01Z",
      "completed_at": null,
      "error_message": null,
      "error_stage_id": null
    }
  ]
}
```

### Get Pipeline Run Detail

Returns comprehensive detail for a single pipeline run, including all stage runs and child pipeline references.

```
GET /dashboard/pipelines/runs/{run_id}
```

**Response:**
```json
{
  "run": {
    "run_id": "run_abc123",
    "pipeline_name": "pr-lifecycle",
    "status": "running",
    "trigger_event": "pull_request.opened",
    "pr_number": 45,
    "scope": "single-pr",
    "current_stage_id": "code-review",
    "created_at": "2026-02-20T14:30:00Z",
    "started_at": "2026-02-20T14:30:01Z",
    "completed_at": null,
    "error_message": null,
    "error_stage_id": null
  },
  "definition_stages": [
    {"id": "test-coverage", "type": "agent"},
    {"id": "security-review", "type": "agent"},
    {"id": "code-review", "type": "agent"},
    {"id": "approval-gate", "type": "gate"},
    {"id": "auto-merge", "type": "action"}
  ],
  "stage_runs": [
    {
      "id": 1,
      "run_id": "run_abc123",
      "stage_id": "test-coverage",
      "status": "completed",
      "agent_id": "agent_xyz",
      "branch_id": null,
      "parent_stage_id": null,
      "child_pipeline_run_id": null,
      "outputs": {},
      "error_message": null,
      "attempt_number": 1,
      "max_attempts": 2,
      "started_at": "2026-02-20T14:30:01Z",
      "completed_at": "2026-02-20T14:35:00Z",
      "duration_seconds": 299.0
    },
    {
      "id": 2,
      "run_id": "run_abc123",
      "stage_id": "code-review",
      "status": "running",
      "agent_id": "agent_abc",
      "started_at": "2026-02-20T14:35:01Z",
      "completed_at": null,
      "duration_seconds": null
    }
  ],
  "children": []
}
```

### Cancel a Pipeline Run

Cancels a running or pending pipeline run. Cascades cancellation to all child pipelines.

```
POST /dashboard/pipelines/runs/{run_id}/cancel
```

**Response (success):**
```json
{
  "cancelled": true,
  "run_id": "run_abc123"
}
```

**Error responses:**
- `404` — run not found
- `409` — run is already in a terminal state (completed, failed, cancelled)
- `503` — pipeline engine not configured

### Real-Time SSE Stream

Streams real-time pipeline events via Server-Sent Events. On connection, the stream hydrates with all currently active pipeline runs before switching to live events.

```
GET /dashboard/pipelines/stream?token=YOUR_API_KEY
```

**Event types:**

| Event | Description |
|-------|-------------|
| `connected` | Initial connection confirmation |
| `pipeline_run` | Active pipeline run state (sent during hydration) |
| `pipeline_cancelled` | A pipeline run was cancelled |
| `hydrated` | History replay complete, switching to live events |
| `heartbeat` | Keep-alive ping (every 30 seconds) |

**JavaScript example:**
```javascript
const es = new EventSource('/dashboard/pipelines/stream?token=YOUR_KEY');

es.addEventListener('pipeline_run', (e) => {
  const run = JSON.parse(e.data);
  console.log(`Pipeline ${run.pipeline_name}: ${run.status}`);
});

es.addEventListener('pipeline_cancelled', (e) => {
  const data = JSON.parse(e.data);
  console.log(`Pipeline ${data.run_id} cancelled`);
});

es.addEventListener('hydrated', () => {
  console.log('Initial state loaded, now streaming live events');
});
```

---

## CLI Commands

The `squadron pipelines` command group provides pipeline visibility from the command line. All commands communicate with the dashboard API and require `--url` and `--api-key` arguments (or the equivalent environment variables).

### Common Arguments

| Argument | Environment Variable | Description |
|----------|---------------------|-------------|
| `--url` | `SQUADRON_URL` | Dashboard base URL |
| `--api-key` | `SQUADRON_API_KEY` | API authentication key |

### List Pipeline Definitions

```bash
squadron pipelines list --url https://your-url --api-key YOUR_KEY
```

Displays all registered pipeline definitions in a formatted table showing name, description, trigger event, and stage count.

**Example output:**
```
Pipelines (8 registered):
  pr-lifecycle
    PR review, approval gates, and auto-merge pipeline
    Trigger: pull_request.opened  |  Stages: 5

  bug-fix-lifecycle
    Full bug fix lifecycle from issue label to PR merge
    Trigger: issues.labeled  |  Stages: 1

  feature-dev-lifecycle
    Full feature development lifecycle from issue label to PR merge
    Trigger: issues.labeled  |  Stages: 1
```

### List Pipeline Runs

```bash
squadron pipelines runs --url https://your-url --api-key YOUR_KEY \
  [--limit 20] [--status running] [--pipeline pr-lifecycle]
```

| Option | Description |
|--------|-------------|
| `--limit` | Number of results (default: 20) |
| `--status` | Filter by status |
| `--pipeline` | Filter by pipeline name |

**Example output:**
```
Pipeline Runs (3 of 142):
  run_abc123  pr-lifecycle     running    PR #45   Started: 2026-02-20 14:30
  run_def456  bug-fix-lifecycle completed  #89     Started: 2026-02-20 12:00
  run_ghi789  issue-triage     completed  #92     Started: 2026-02-20 11:15
```

### Show Pipeline Run Detail

```bash
squadron pipelines run run_abc123 --url https://your-url --api-key YOUR_KEY
```

Shows full run detail including all stage runs, their statuses, durations, and any error messages.

**Example output:**
```
Pipeline Run: run_abc123
  Pipeline:  pr-lifecycle
  Status:    running
  PR:        #45
  Started:   2026-02-20 14:30:01

  Stages:
    [completed] test-coverage    agent:agent_xyz   299s
    [skipped]   security-review  (condition not met)
    [running]   code-review      agent:agent_abc
    [pending]   approval-gate
    [pending]   auto-merge
```

### Cancel a Pipeline Run

```bash
squadron pipelines cancel run_abc123 --url https://your-url --api-key YOUR_KEY
```

Cancels the specified pipeline run and all child pipelines.

---

## Human Interaction

Pipeline `human` stages pause execution and wait for human action. This enables human-in-the-loop workflows for approvals, reviews, and decisions.

### How Human Stages Work

1. Pipeline reaches a `human` stage and pauses execution
2. If `auto_assign` is enabled, users from the specified `from` group are assigned
3. An entry notification is posted (if configured)
4. Pipeline waits for the specified action (`approval`, `comment`, `label`, or `dismiss`)
5. Periodic reminders are sent (if configured)
6. Once the required number of completions is reached, the pipeline advances

### Configuring Human Stages

```yaml
- id: deploy-approval
  type: human
  human:
    description: "Approve deployment to production"
    wait_for: approval          # approval | comment | label | dismiss
    from: maintainers           # human_groups key
    count: 2                    # Number of approvals needed
    auto_assign: true           # Assign users from the group
    notify:
      on_enter: "Production deployment is ready for review."
      reminder:
        interval: 24h
        message: "Reminder: deployment approval is still pending."
        max_reminders: 3
  timeout: 72h
  on_timeout:
    notify:
      target: maintainers
      message: "Deployment approval timed out."
    then: escalate
```

### Human Wait Types

| Type | Completed When |
|------|---------------|
| `approval` | Required number of PR approvals submitted via GitHub review |
| `comment` | A comment is posted on the issue/PR |
| `label` | A specified label is added to the issue/PR |
| `dismiss` | A review dismissal occurs |

### Human Groups

Human groups are defined in `.squadron/config.yaml`:

```yaml
human_groups:
  maintainers:
    - "@alice"
    - "@bob"
  security-team:
    - "@carol"
    - "@dave"
```

When a human stage references `from: maintainers`, users from that group are notified and optionally assigned.

### Completing Human Stages

Human stages are completed through normal GitHub interactions:

- **Approval**: Submit a PR review with "Approve" status
- **Comment**: Post a comment on the issue or PR
- **Label**: Add the expected label via GitHub UI
- **Dismiss**: Dismiss a PR review

The pipeline engine monitors these events via reactive event handlers and advances the pipeline when the completion criteria are met.

---

## Notifications

The pipeline system can send notifications at various points during execution.

### Pipeline Lifecycle Hooks

```yaml
on_complete:
  - type: comment
    body: "Pipeline complete — all stages passed."
  - type: label
    add: deployed

on_error:
  - type: label
    add: needs-human
  - type: comment
    body: "Pipeline encountered an error. Human review required."
```

**Supported notification types in lifecycle hooks:**

| Type | Fields | Description |
|------|--------|-------------|
| `comment` | `body` | Post a comment on the PR/issue |
| `label` | `add` | Add a label to the PR/issue |

### Gate Timeout Notifications

```yaml
on_timeout:
  notify:
    target: maintainers
    message: "Approval gate timed out after 48h."
  then: escalate
```

### Escalation

When a stage's error handling specifies `then: escalate`, the pipeline:
1. Labels the issue/PR with the configured escalation labels (default: `needs-human`)
2. Notifies the configured escalation group (default: `maintainers`)
3. Posts a comment describing the failure
4. Marks the pipeline as `escalated`

---

## Pipeline Statuses

### Pipeline Run Statuses

| Status | Description |
|--------|-------------|
| `pending` | Run created but not yet started |
| `running` | Actively executing stages |
| `completed` | All stages finished successfully |
| `failed` | A stage failed without recovery |
| `cancelled` | Run was cancelled by user or reactive event |
| `escalated` | Run was escalated to humans |

### Stage Run Statuses

| Status | Description |
|--------|-------------|
| `pending` | Stage not yet started |
| `running` | Stage actively executing |
| `waiting` | Stage waiting for external input (gate conditions, human action, webhook response) |
| `completed` | Stage finished successfully |
| `failed` | Stage failed |
| `skipped` | Stage skipped (condition not met) |
| `cancelled` | Stage cancelled |

---

## Reactive Events

Running pipelines can react to GitHub events that occur during execution. This enables dynamic behavior like re-evaluating gates when a new review is submitted, or cancelling the pipeline when a PR is closed.

### Configuration

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
  issue_comment.created:
    action: wake_agent
```

### How Reactive Events Work

1. A GitHub webhook arrives while a pipeline is running
2. The pipeline engine checks if the running pipeline has a matching `on_events` handler
3. The configured action is executed:
   - **`reevaluate_gates`**: If the pipeline is currently on a gate stage, all gate conditions are re-evaluated immediately
   - **`invalidate_and_restart`**: The specified stage runs are marked as invalid, and execution restarts from the specified stage
   - **`cancel`**: The pipeline run is cancelled
   - **`wake_agent`**: The currently sleeping agent in the pipeline is woken with new event context
   - **`notify`**: A notification is sent without changing pipeline state

### Common Patterns

**PR push invalidates reviews:**
When a developer pushes new commits (`pull_request.synchronize`), security and code reviews are invalidated and the pipeline restarts from the test-coverage stage. This ensures reviews are always based on the latest code.

**Gate re-evaluation on review:**
When a reviewer submits a review (`pull_request_review.submitted`), the approval gate is immediately re-evaluated. If the new review satisfies the gate conditions, the pipeline advances to the merge stage without waiting for the next poll cycle.

**Auto-cancel on PR close:**
When a PR is closed or merged externally (`pull_request.closed`), the pipeline is automatically cancelled to avoid orphaned pipeline runs.

---

## Recovery

The pipeline engine automatically recovers active pipelines after a server restart. On startup, `PipelineEngine.recover_active_pipelines()` is called to resume any pipeline runs that were in `running` or `pending` status when the server stopped.

Gate stages are re-evaluated, agent stages check for completed agents, and the pipeline continues from where it left off.
