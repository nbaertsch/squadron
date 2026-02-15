# Open Research & TODOs

Unresolved questions, research tasks, and architecture decisions that need further exploration.

**Priority Key:** 🔴 Critical (blocks V1) · 🟡 Important (should resolve before V1) · 🟢 Nice-to-have (can defer)

---

## OR-001: Agent Runtime Framework Selection 🔴

**Status:** Research Complete — Recommendation: **Copilot SDK**  
**Category:** Architecture  
**Blocks:** Implementation start

**Question:** Which LLM SDK / agent framework should Squadron use for its agent runtime?

### Critical Finding: Three-Tier SDK Distinction

Through research we discovered that "SDK" means very different things:

| Tier | Example | Architecture | Context Control |
|---|---|---|---|
| **Raw Client SDK** | `anthropic` pip package, `openai` pip package | Stateless HTTP wrapper → REST API | ✅ Full (you own `messages[]`) |
| **Agent SDK** | Copilot SDK (`github/copilot-sdk`), Claude Agent SDK | JSON-RPC → CLI binary subprocess | ❌ Opaque (CLI manages context) |
| **Extensions SDK** | Copilot Extensions | Webhook-based, GitHub sends history | ❌ Stateless |

Both the Copilot SDK and Claude Agent SDK are **wrappers around CLI binaries** (Copilot CLI, Claude Code CLI), not REST API clients. They communicate via JSON-RPC, and the CLI binary manages the conversation context, tool execution, and planning state internally.

### Candidate Comparison

| Capability | Copilot SDK | Claude Agent SDK | Raw API Client |
|---|---|---|---|
| GitHub-native | ✅ (cwd/repo/branch tracking, context_changed events) | ❌ | ❌ |
| Multi-model (BYOK) | ✅ (OpenAI, Azure, Anthropic, Ollama) | ❌ (Claude only) | ✅ (any provider) |
| Session persistence | ✅ Built-in file-based | ✅ Built-in opaque | DIY (serialize messages[]) |
| Context compaction | ✅ Infinite sessions (auto) | ✅ (agent manages) | DIY |
| Raw message control | ❌ Opaque | ❌ Opaque | ✅ Full |
| Session forking | ❌ | ✅ `fork_session` | ✅ Copy array |
| Built-in tools | ✅ (Read, Write, Bash, Glob, Grep, etc.) | ✅ (similar set) | ❌ |
| Hooks/lifecycle | ✅ Rich (pre/post tool, session start/end, error) | ✅ Similar | ❌ |
| Custom tools | ✅ `@define_tool` + Pydantic | ✅ Yes | ❌ (build yourself) |
| Maturity | Technical Preview (v0.1.23) | Newer | Stable |
| Lock-in risk | Medium (GitHub ecosystem) | Medium (Anthropic) | None |

### Recommendation: Copilot SDK

**Why:**
1. **GitHub-native** — Squadron IS a GitHub-native framework. Copilot SDK tracks cwd, repo, branch natively. `session.context_changed` event fires when agent switches branches.
2. **Multi-model via BYOK** — Not locked to one LLM provider. Different agent roles can use different models.
3. **Built-in session persistence** — Sleep/wake lifecycle (AD-003) maps directly to `create_session()` / `resume_session()`.
4. **Infinite sessions** — Auto-compaction prevents context overflow for long-running agent tasks.
5. **Rich hooks** — `onPreToolUse` for permission enforcement, `onSessionStart` for context injection on wake.
6. **Built-in tools** — File read/write/edit, bash, grep come free. Critical for dev agents.

**Risks:**
- Technical Preview status — API may change
- Opaque context — can't manipulate conversation history directly
- No session forking — can't branch conversations
- CLI binary dependency — deployment needs Copilot CLI installed

**Detailed research:** [Copilot SDK Session Persistence](research/copilot-sdk-session-persistence.md) | [Original SDK Comparison](research/context-serialization.md)

**TODO:**
- [ ] Prototype: create a session → do work → destroy → resume → verify context preserved
- [ ] Test BYOK with Anthropic provider (Claude via Copilot SDK)
- [ ] Measure CLI binary memory footprint per agent process
- [ ] Validate session persistence across container restarts
- [ ] **Decision gate:** Accept Copilot SDK or investigate alternatives further

---

## OR-002: Event Architecture — Actions vs. Webhooks vs. Hybrid 🔴

**Status:** Research Complete — Recommendation: **GitHub App (Webhook-Based)**  
**Category:** Architecture  
**Blocks:** Event-driven system design

**Question:** How does a GitHub event (issue created, comment posted, PR opened) reach the Squadron framework and trigger agent behavior?

### Key Constraint

Copilot SDK agents are long-running CLI sessions on persistent infrastructure. A server already exists for agents — the question is purely how events reach it.

### Options Evaluated

| Option | Verdict | Reason |
|---|---|---|
| **Pure GitHub Actions** | ❌ Not viable | 6-hour max, ephemeral runners, can't maintain Copilot SDK sessions |
| **Actions as thin dispatch** | ❌ Unnecessary | Adds latency (runner provisioning ~15-45s), burns Actions minutes, you already have a server |
| **GitHub App (webhooks)** | ✅ **Recommended** | Direct delivery (~1s), built-in bot identity, fine-grained permissions, standard pattern |
| **Hybrid (App + Actions)** | ✅ Enhancement of App | App receives events; agents trigger Actions via `repository_dispatch` for CI tasks |

### Recommendation: GitHub App

**Why:**
1. **Bot identity for free** — App appears as `squadron[bot]` in the UI, directly implementing AD-002 without a separate bot user account
2. **Direct webhook delivery** — ~1s latency vs. 15-45s for Actions runner provisioning
3. **Fine-grained permissions** — only subscribe to needed events, only request needed permissions
4. **Installation access tokens** — auto-scoped to installed repos, 5000 req/hr per installation
5. **Standard pattern** — Dependabot, CodeQL, Copilot all work this way
6. **`repository_dispatch` for reverse path** — agents trigger Actions for CI/tests when needed

**Architecture:** Server receives webhooks → validates HMAC → queues event → Event Router dispatches to agent sessions. Agents use installation access tokens to interact with GitHub API.

**Key design constraints:**
- Must respond to webhooks within 10 seconds (async queue required)
- `X-GitHub-Delivery` header for idempotent event processing
- Webhook secret + HMAC-SHA256 signature validation on every delivery
- Installation tokens expire after 1 hour (implement refresh logic)

**Detailed research:** [Event Architecture Research](research/event-architecture.md)

**TODO:**
- [ ] Prototype: Register a GitHub App, receive webhook on `issues.opened`, log event
- [ ] Test installation access token lifecycle (creation, refresh, scoping)
- [ ] Prototype: Agent triggers `repository_dispatch` → Actions workflow runs CI
- [ ] **Decision gate:** Accept GitHub App architecture or investigate alternatives further

---

## OR-003: Event Routing — Mapping Events to Agent Instances 🔴

**Status:** Research Complete — Recommendation: **Agent Registry + Subscription Model**  
**Category:** Architecture  
**Blocks:** Multi-agent coordination

**Question:** When an event fires (e.g., "issue #42 closed"), how does the system determine which agent instance(s) should be notified?

### Key Insight: The Framework Creates the Dependencies

When an agent discovers a blocker and the PM creates a new issue, **the framework already knows the relationship** at creation time. No GitHub API query or text parsing is needed at dispatch time.

### Solution: Agent Registry

A lightweight SQLite (V1) / PostgreSQL (V2) store tracks all active agent sessions:

```
AgentRecord:
  agent_id       : str        # "dev-1"
  role           : str        # "feat-dev"
  issue_number   : int        # The issue this agent owns
  session_id     : str        # Copilot SDK session ID
  status         : enum       # CREATED | ACTIVE | SLEEPING | COMPLETED | ESCALATED | CANCELLED
  branch         : str?       # "feat/issue-38"
  pr_number      : int?       # Set when PR is opened
  blocked_by     : [int]      # Issue numbers blocking this agent
```

### Routing Logic

| Event | Routing ||
|---|---|---|
| `issues.opened` | → PM Agent | Always |
| `issues.assigned` (to bot) | → Create new agent session | Register in Agent Registry |
| `issues.closed` | → Query: `WHERE blocked_by CONTAINS issue_number` | Wake blocked agents |
| `issue_comment.created` | → Agent for that issue, or PM if `@squadron-pm` | Lookup by issue_number |
| `pull_request.opened` | → Review agents per approval flow | Map PR→issue via registry/branch name |
| `pull_request_review.submitted` | → Dev agent for that PR's issue | Lookup by pr_number |
| Bot self-events | → Discard | `sender.login == "squadron[bot]"` |

### Dependency Resolution

- **Register**: Agent adds blocker to `blocked_by`, status → SLEEPING
- **Resolve**: On `issues.closed`, query blocked agents, remove from `blocked_by`, wake if empty
- **Cycle detection**: BFS from target issue through blocked_by edges before registration — reject if cycle found
- **Reconciliation loop**: Every 5 min, check each SLEEPING agent's blockers against GitHub state (catches missed webhooks)

### GitHub Cross-References: Secondary Role

GitHub's cross-reference system is for **human visibility and recovery**, not primary routing. Cross-refs appear in the sidebar (audit trail) and can reconstruct the registry if lost.

**Detailed research:** [Event Routing Research](research/event-routing.md)

**TODO:**
- [ ] Prototype: SQLite Agent Registry with CRUD operations
- [ ] Prototype: Event Router with routing table for 3-4 key event types
- [ ] Prototype: Cycle detection algorithm
- [ ] Design agent inbox for dispatching events to active sessions
- [ ] **Decision gate:** Accept Agent Registry architecture or investigate alternatives

---

## OR-004: Race Conditions on Issue State 🟡

**Status:** Research Complete — Recommendation: **Layered Concurrency Strategy**  
**Category:** Architecture  
**Blocks:** Robustness

**Question:** What happens when multiple actors (agents and/or humans) modify the same issue concurrently?

### Resolution: Six-Layer Defense

The existing architecture already handles most concurrency scenarios. The remaining gaps are covered by two additional mechanisms:

| Layer | Mechanism | Covers |
|---|---|---|
| L1 | Sequential PM processing (AD-008) | PM-to-PM races |
| L2 | Per-issue event queue partitioning | Multi-event races on same issue |
| L3 | `X-GitHub-Delivery` deduplication | Duplicate webhooks |
| L4 | Bot self-event filtering | Feedback loops |
| L5 | **Re-read before write (optimistic)** | Human-modifies-during-processing |
| L6 | **Last-write-wins + audit trail** | Unresolvable simultaneous mutations |

### Key Findings

- **GitHub does NOT support conditional writes** (`If-Match` for PATCH/PUT). Full OCC is not implementable at the API level.
- **Issue-level locking (via labels) was rejected** — fragile, unnecessary with per-issue queues, and pollutes the timeline.
- **Most "simultaneous" scenarios are non-issues:** Comments are append-only (no conflict). Issue state is a single field where last-write-wins is the correct behavior (human override should prevail).
- The **only real gap** was: PM makes a decision based on stale state. Solved by re-reading issue state before applying mutations.

**Detailed research:** [Concurrency Strategy](research/concurrency-strategy.md)

**TODO:**
- [ ] Implement per-issue event queue partitioning in Event Router prototype
- [ ] Implement re-read-before-write pattern in PM agent
- [ ] **Decision gate:** Accept layered concurrency strategy (AD-014)

---

## OR-005: Approval Flow Configuration Schema 🟡

**Status:** Research Complete — Recommendation: **Declarative YAML Schema**  
**Category:** Design  
**Blocks:** Workflow implementation

**Question:** What is the exact schema for configurable approval flows?

### Resolution: Declarative YAML in `.squadron/workflows/approval-flows.yaml`

A complete schema has been designed covering:

- **`required_reviews[]`** — Role-based review requirements with `agent:` and `human_group:` prefixes, `auto_assign`, `min_approvals`
- **`merge_policy`** — `auto_merge`, `require_ci_pass`, `merge_method` (squash/merge/rebase), `delete_branch`
- **`required_status_checks[]`** — Maps directly to GitHub branch protection API contexts
- **`protected_paths[]`** — Glob patterns triggering additional reviews (e.g., `.squadron/**` requires human, `*.lock` requires security)
- **`escalation`** — `on_ci_failure`, `on_review_rejection`, `on_timeout` with configurable notify/action
- **`branch_rules[]`** — Per-branch overrides using `fnmatch` patterns, merged with defaults (most restrictive wins)

### Key Design Decisions

1. **Agent roles use status checks** (`squadron/security-review`) for enforcement — not PR reviews (single bot identity limitation)
2. **Human approvals use required reviews** (`required_approving_review_count`) — standard GitHub mechanism
3. **Two enforcement layers:** GitHub branch protection (hard backstop) + Framework-level orchestration (richer logic)
4. **Protected paths are framework-evaluated** — PR file list is checked, additional reviewers auto-assigned

**Detailed research:** [Approval Flow Schema](research/approval-flow-schema.md)

**TODO:**
- [ ] Implement YAML schema parser with validation
- [ ] Implement flow resolution algorithm (default → branch override → protected path additions)
- [ ] Prototype: Branch protection rule setup from approval flow config
- [ ] **Decision gate:** Accept approval flow schema (AD-015)

---

## OR-006: Role-Based Action Enforcement via GitHub Mechanisms 🟡

**Status:** Research Complete — Recommendation: **Dual-Layer Enforcement**  
**Category:** Architecture  
**Blocks:** Permission model

**Question:** Can GitHub branch protection rules be configured to enforce agent role constraints natively, or must all enforcement be framework-level?

### Resolution: Both — Dual-Layer Enforcement

| Sub-Question | Answer |
|---|---|
| Can status checks serve as role-based gates? | **Yes** — Primary mechanism. `squadron/{role}` status checks + `required_status_checks` in branch protection. App-locked (`app_id` ensures only Squadron can satisfy its own checks). |
| Can CODEOWNERS enforce agent roles? | **No** — Single `squadron[bot]` identity can't distinguish roles. Use CODEOWNERS only for human code owners. |
| Can we differentiate roles via commit metadata? | **For audit only** — `[squadron:{role}]` tags in commit messages/comments. Not enforceable by GitHub. |

### Architecture: Two Layers

1. **GitHub-Native Layer (merge gates):** Required status checks per role (`squadron/security-review`, `squadron/pr-review`) + required reviews for humans. Cannot be bypassed even by framework bugs.
2. **Framework Layer (action permissions):** `SquadronGitHubClient` wrapper enforces role → action permission matrix before every API call. Validates branch ownership, status check context, etc.

### Key Finding: GitHub Rulesets

Modern alternative to branch protection — supports multiple simultaneous rulesets, `fnmatch` branch patterns, active/disabled/evaluate statuses, and layered aggregation (most restrictive wins). V1 uses branch protection for compatibility; V2 can add rulesets.

**Detailed research:** [Role Enforcement](research/role-enforcement.md)

**TODO:**
- [ ] Implement `SquadronGitHubClient` wrapper with role-based permission checks
- [ ] Prototype: Post status checks, configure branch protection to require them
- [ ] Test that only the Squadron App can satisfy its own required status checks (app_id scoping)
- [ ] **Decision gate:** Accept dual-layer enforcement (AD-016)

---

## OR-007: Dedicated Merge Conflict Resolution Agent? 🟢

**Status:** Deferred to V2  
**Category:** Design  

**Question:** Should there be a dedicated "merge conflict resolution" agent role, or should conflict resolution always follow the dev-agent-attempt → human-escalation path?

**Resolution:** Deferred. AD-005 (agent-first, human-escalation) is sufficient for V1. Merge conflicts require domain knowledge the original dev agent already has — a specialized agent adds complexity without clear benefit. Revisit in V2 if empirical data shows frequent conflict resolution failures.

---

## OR-008: Semantic Conflict Detection 🟢

**Status:** Deferred to V2  
**Category:** Research  

**Question:** How do we detect semantic conflicts — situations where two agents edit different files but introduce incompatible logic?

**Resolution:** Deferred. For V1, **CI is the primary safety net** — comprehensive test suites catch semantic conflicts at build/test time. The PR review agent's code review also serves as a secondary check. Sophisticated detection (static analysis, pre-merge integration testing, API surface change tracking) is a V2 optimization. AD-007 (no file locking, merge-based resolution) accepts that CI catches what git merge cannot.

---

## OR-009: Agent State Storage Backend 🔴

**Status:** Resolved — **Copilot SDK handles this natively**  
**Category:** Architecture  
**Blocks:** Agent lifecycle (sleep/wake)

**Question:** Where is serialized agent context stored?

**Resolution:** With the Copilot SDK (OR-001), session state is managed automatically by the CLI binary. State persists to `~/.copilot/session-state/{sessionId}/` with checkpoints, planning state, and artifacts. No external database or custom serialization format needed.

For containerized deployment, mount this path to persistent storage (Azure File Share, EBS, etc.). See [Copilot SDK Session Persistence](research/copilot-sdk-session-persistence.md) for deployment patterns.

**Remaining sub-question:** Do we need *additional* state beyond what the Copilot SDK persists? E.g., orchestrator-level state mapping (which agent is working on which issue, agent lifecycle status). This is lightweight metadata that could live in a simple SQLite file or even as GitHub issue labels.

---

## OR-010: Workflow Versioning 🟢

**Status:** Deferred to V2  
**Category:** Design  

**Question:** If a workflow definition changes while issues are in-flight, do in-progress issues follow the old workflow or the new one?

**Resolution:** Deferred. For V1, workflow changes apply immediately to all events (live reload). This is acceptable because: (1) workflow changes are infrequent, (2) most workflow changes are additive (new rules, not modified existing ones), (3) the PM can always re-triage. For V2, consider pinning workflow version at issue creation time (store version hash in Agent Registry).
