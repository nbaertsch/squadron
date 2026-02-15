# OR-002 Research: Event Architecture

**Date:** 2025-01-XX  
**Status:** Research Complete — Recommendation: **GitHub App (Webhook-Based)**

---

## The Core Constraint

With the Copilot SDK (OR-001), agents are **long-running CLI binary sessions** that persist state to disk. They can't run on ephemeral infrastructure. This means:

- Agents MUST run on **persistent infrastructure** (containers with mounted volumes, VMs, etc.)
- A **server already exists** — the question is purely how GitHub events reach it

This constraint eliminates any architecture that relies on GitHub Actions as the agent runtime.

---

## Architecture Options Evaluated

### Option A: GitHub App (Webhook-Based) ⭐ RECOMMENDED

**How it works:**
1. Register a GitHub App with event subscriptions (`issues`, `issue_comment`, `pull_request`, `pull_request_review`, `push`)
2. Install App on target repos/orgs
3. GitHub POSTs signed webhooks to Squadron's server endpoint
4. Server validates HMAC signature, responds 200 immediately, queues event for async processing
5. Event router dispatches to appropriate agent session(s)

**Auth model:** App generates JWT → exchanges for installation access token (scoped to installed repos, 1-hour TTL, 5000 req/hr per installation).

**Identity:** App appears as `squadron[bot]` in the GitHub UI — comments, commits, status checks all attributed to the bot identity.

**Pros:**
- Direct delivery — no middleman between GitHub and Squadron
- Fine-grained permissions (only subscribe to events you need, only request permissions you use)
- **Built-in bot identity** (`squadron[bot]`) — directly implements AD-002
- Delivery logs visible in App settings, manual redelivery via API
- No Actions minutes burned on dispatch overhead
- **Standard pattern** — Dependabot, CodeQL, Copilot, and every major GitHub integration use this exact model
- Installation access tokens auto-scoped to installed repos (principle of least privilege)
- Supports both org-wide and per-repo installation

**Cons:**
- Requires a publicly reachable endpoint (or smee.io / ngrok for local dev)
- Must respond to webhooks within 10 seconds (queue async processing)
- No automatic retry on failed delivery (can check delivery logs and redeliver via API)
- More complex initial setup than Actions (JWT key generation, App registration)

### Option B: Pure GitHub Actions — Agent Runtime on Runners

**How it works:** Workflows trigger on `issues`, `pull_request`, etc. Agent code executes inside the Actions runner.

**Verdict: NOT VIABLE**

Critical incompatibilities with Copilot SDK:
- **6-hour max runtime per job** — agent tasks can run for hours or days
- **Ephemeral runners** — session state lost when job ends (no persistent `~/.copilot/session-state/`)
- **Cold-start latency** — runner provisioning adds seconds to every wake
- **Can't maintain Copilot CLI sessions** — binary killed when job completes
- **Per-minute billing** on private repos for long-running agent work

### Option C: Actions as Thin Dispatch → External Server

**How it works:**
1. Workflow triggers on GitHub events
2. Workflow parses the event payload, sends HTTP POST to external Squadron server
3. Squadron server manages agent lifecycle

**Pros:**
- Zero infrastructure for event capture (GitHub manages)
- GitHub handles event detection reliability
- Workflow files serve as event documentation

**Cons:**
- **Extra latency:** runner provisioning (~15-45s) + HTTP POST. Webhook delivery is ~1s.
- **Burns Actions minutes** for dispatch overhead (wasteful)
- **Two systems to debug** — if an event doesn't reach Squadron, is it an Actions issue or an HTTP issue?
- Workflow files must exist on default branch (merge-to-main required to change routing)
- **You already have a server** (for agents) — adding Actions as a middleman adds complexity without benefit
- `GITHUB_TOKEN` in Actions has narrower permissions than installation access tokens

### Option D: Hybrid — GitHub App + Actions

**How it works:**
- App receives webhooks directly for event routing to agents
- App triggers Actions via `repository_dispatch` for CI-like tasks (test running, linting, build verification)
- Long-running agent work stays on persistent infra

**Assessment:** This is actually just Option A with the recognition that `repository_dispatch` enables Agent → Actions communication. It's not a separate architecture — it's an enhancement of Option A.

---

## Recommendation: GitHub App (Option A)

### Chain of Reasoning

1. **Agents need persistent infra** → eliminates Option B
2. **You already have a server** → Option C's "zero infra" advantage is moot
3. **GitHub App = bot identity** → `squadron[bot]` directly implements AD-002 without a dedicated bot user account
4. **Direct webhook delivery** → simpler, faster, fewer failure modes than routing through Actions
5. **Fine-grained permissions** → App requests exactly what it needs (issues: rw, PRs: rw, contents: rw, etc.)
6. **`repository_dispatch` for reverse path** → when agents need CI/tests, they call the API to trigger Actions workflows. Actions handles what it's good at (CI). Agents handle long-running work.
7. **This is THE standard pattern** → every major GitHub integration works this way

### Architecture

```
GitHub Repository
  │
  ├── issues.opened ──────────────┐
  ├── issue_comment.created ──────┤
  ├── pull_request.opened ────────┤  Webhooks (signed, ~1s delivery)
  ├── pull_request_review ────────┤
  ├── push ───────────────────────┤
  └── ... ────────────────────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │  Squadron Server     │
                       │                     │
                       │  POST /webhook      │ ← validate HMAC, respond 200
                       │       │             │
                       │       ▼             │
                       │  Event Queue        │ ← async processing (satisfies 10s rule)
                       │       │             │
                       │       ▼             │
                       │  Event Router       │ ← maps events → agents (OR-003)
                       │       │             │
                       │  ┌────┼────┐        │
                       │  ▼    ▼    ▼        │
                       │ PM   Dev  Review    │ ← Copilot SDK sessions
                       │ Agt  Agt  Agent     │
                       └─────────────────────┘
                              │
                    Installation Access Token
                              │
                              ▼
                  GitHub API (REST / GraphQL)
                (branches, comments, PRs, status checks)
```

### Agent → GitHub Communication (Reverse Path)

Agents use the installation access token to:
- **Comment on issues/PRs** — status updates, questions, handoff messages
- **Create branches** — `feat/issue-{N}` per AD-004
- **Open PRs** — when work is complete
- **Post status checks** — `squadron/security-review: approved` for branch protection (OR-006)
- **Trigger Actions via `repository_dispatch`** — for CI/test execution

```
Agent Session
    │
    ├── github.issues.create_comment(...)
    ├── github.repos.create_branch(...)
    ├── github.pulls.create(...)
    ├── github.repos.create_commit_status(...)
    └── github.repos.create_dispatch_event(
            event_type="run-tests",
            client_payload={"branch": "feat/issue-42", "agent_id": "dev-1"}
        )
            │
            ▼
        Actions Workflow (on: repository_dispatch)
            │
            └── Run CI → post status check → Squadron server sees status update
```

---

## Events Squadron Needs

| GitHub Event | Action Filter | Squadron Behavior |
|---|---|---|
| `issues` | `opened` | PM agent triages new issue |
| `issues` | `assigned` | Agent creates session, starts work |
| `issues` | `closed` | Wake blocked agents, cleanup resources |
| `issues` | `reopened` | PM re-evaluates, possibly reassign |
| `issues` | `labeled` | PM updates routing (e.g., `needs-human` label) |
| `issue_comment` | `created` | Process feedback, @-mentions, human directives |
| `pull_request` | `opened` | PR review agent triggers |
| `pull_request` | `synchronize` | Re-run reviews on new commits |
| `pull_request` | `closed` (merged) | Cleanup branch, notify dependents, close issue |
| `pull_request_review` | `submitted` | Process approval/rejection/changes-requested |
| `push` | — | Could trigger CI (usually handled via PR events) |
| `status` | — | CI results → agent can proceed or fix |

### App Permissions Required

| Permission | Access | Why |
|---|---|---|
| Issues | Read & Write | Create/close/comment on issues |
| Pull Requests | Read & Write | Create/review/merge PRs |
| Contents | Read & Write | Create branches, push commits |
| Commit statuses | Read & Write | Post status checks for branch protection |
| Metadata | Read | Required for all Apps |
| Actions | Read | Check workflow run status (optional) |

---

## Key Design Decisions Embedded in This Architecture

### 1. Webhook Secret + HMAC Validation
Every delivery is signed with `X-Hub-Signature-256`. Server validates before processing. Non-negotiable security requirement.

### 2. Async Event Processing (10-Second Rule)
GitHub requires webhook responses within 10 seconds. Squadron must:
1. Validate signature
2. Respond 200
3. Queue event for async processing

This naturally leads to an internal event queue (in-process queue for V1, Redis/SQS for V2 scale).

### 3. `X-GitHub-Delivery` for Idempotency
Each delivery has a unique UUID. Server stores recent delivery IDs and skips duplicates. This prevents double-processing from redeliveries or retries.

### 4. `repository_dispatch` for Agent → Actions
When an agent needs CI/tests/builds, it creates a `repository_dispatch` event via the API:
```json
{
  "event_type": "squadron-run-tests",
  "client_payload": {
    "branch": "feat/issue-42",
    "agent_id": "dev-1",
    "issue_number": 42
  }
}
```
Actions workflow listens for `on: repository_dispatch` and runs the appropriate CI pipeline. Results come back via status checks or `workflow_run` events.

Constraints:
- `client_payload` max 65,535 chars, max 10 top-level properties
- Workflow file must exist on default branch
- Only triggers workflows on default branch

### 5. Bot Identity = App Identity
`squadron[bot]` appears in all GitHub interactions. No separate bot user account needed. Commit signatures, issue comments, PR reviews — all attributed to the App.

---

## Operational Considerations

### Local Development
- Use [smee.io](https://smee.io) or `gh webhook forward` to tunnel webhooks to localhost
- GitHub CLI supports `gh api` for testing App authentication
- App can be registered as a "development" App with localhost callback URLs

### Webhook Reliability
- GitHub does NOT automatically retry failed deliveries
- Delivery logs available in App settings (GitHub UI) and via API
- Manual redelivery via API: `POST /app/hook/deliveries/{delivery_id}/attempts`
- For production: implement a dead-letter queue for failed event processing
- Consider a periodic reconciliation job that checks for missed events

### Scaling
- V1: Single server, in-process event queue (asyncio queue or similar)
- V2: Multiple server instances behind load balancer, shared event queue (Redis/SQS), sticky routing per agent session
- Installation access tokens: 5000 req/hr per installation (generous for V1)

### Security
- Store App private key securely (env var, secrets manager — never in repo)
- Webhook secret: generate a strong random secret, validate HMAC on every delivery
- Installation access tokens expire after 1 hour — implement token refresh logic
- Principle of least privilege: request only the permissions listed above

---

## Implications for Other Open Research Questions

| OR | Implication |
|---|---|
| **OR-003** (Event Routing) | The Event Router is a component inside the Squadron server. It maps incoming webhook events to agent sessions using a registry/dependency graph. |
| **OR-004** (Race Conditions) | `X-GitHub-Delivery` deduplication + per-issue event queues prevent double-processing. Optimistic concurrency on GitHub API calls (ETags). |
| **OR-005** (Approval Flow Schema) | The App can post status checks that branch protection requires. Approval flows map to combinations of status checks + review requirements. |
| **OR-006** (Role Enforcement) | App identity posts role-specific status checks (e.g., `squadron/security-review: approved`). Branch protection requires these checks. This is the bridge between framework-level roles and GitHub-native enforcement. |
| **OR-009** (State Storage) | Already resolved — Copilot SDK handles session state. The server only needs lightweight orchestrator state (agent registry, event queue, delivery ID dedup). |

---

## Open Questions Remaining

1. **Webhook endpoint hosting for V1** — where does the Squadron server run? (Local machine for dev, cloud VM/container for prod)
2. **Event queue implementation** — in-process (asyncio) vs. external (Redis) for V1
3. **App registration automation** — can we script the GitHub App creation, or is it manual?
4. **Multi-repo support** — single App installed on multiple repos. Does the event router need to be repo-aware?
5. **Rate limiting** — 5000 req/hr per installation. Is this sufficient for a busy repo with multiple agents?

---

## References

- [About Creating GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/about-creating-github-apps)
- [Webhook Best Practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)
- [GitHub App Authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app)
- [Events That Trigger Workflows](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows)
