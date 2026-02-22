# Architecture Decisions

Decisions made during ideation, with rationale.

---

## AD-001: GitHub as the Orchestration Layer

**Status:** Accepted  
**Decision:** Use GitHub's native primitives (Issues, PRs, Actions/Webhooks, branch protections) as the primary orchestration and state layer. No separate control plane dashboard.

**Rationale:**
- Human-readable audit trails for free — every agent action is a comment, commit, or status change.
- Humans and agents interact through the same interfaces.
- GitHub's existing permission model provides the baseline access control layer.
- Reduces infrastructure — no separate UI or state management database for project state.

---

## AD-002: Agent Identity — GitHub App Bot Identity with Role-Based Enforcement

**Status:** Accepted (updated by AD-012)  
**Decision:** All agents operate under a single **GitHub App identity** (`squadron[bot]`). The App's installation access tokens authenticate all GitHub API calls. Role-based restrictions (e.g., "dev agents can't merge," "security agents can approve") are enforced at the **framework level**, not at the GitHub identity level.

**Rationale:**
- The GitHub App (AD-012) provides a built-in bot identity — no separate bot user account or PAT needed.
- `squadron[bot]` appears as the author on all comments, commits, status checks, and PR reviews.
- Installation access tokens are auto-scoped to installed repos with fine-grained permissions.
- GitHub's native permission model is per-user, not per-role-within-a-bot — so framework-level enforcement is always needed for fine-grained role distinctions.

**Enforcement approach:**
- The framework maintains a role → allowed-actions mapping (defined in `.squadron/config.yaml` or workflows).
- Before an agent performs a privileged action (merge, approve, deploy), the framework checks the agent's role against the allowed actions.
- Actions are logged with the agent role clearly identified (e.g., commit messages, issue comments include role tags like `[squadron:dev-1]`, `[squadron:security-review]`).
- Agents post role-specific status checks (e.g., `squadron/security-review: approved`) that branch protection can require.

**Open question:** ~~Can GitHub branch protection rules be configured to require approvals from specific "contexts" or status checks rather than specific users? If so, we could map agent roles to required status checks (e.g., `security-review/approved` status check must pass before merge).~~ **Resolved:** Yes — required status checks with `app_id` scoping are the primary mechanism. See [AD-016](architecture-decisions.md#AD-016) and [OR-006](open-research.md#OR-006).

---

## AD-003: Agent Lifecycle — Serialized Context with Event-Driven Rehydration

**Status:** Accepted  
**Decision:** Agent instances are created per-assignment (one LLM client context per issue). When an agent is blocked (waiting on a dependency), its context is serialized/checkpointed to persistent storage. When the blocker resolves (event fires), the agent is rehydrated from the checkpoint.

**Rationale:**
- Keeping processes alive while waiting is expensive and wasteful.
- Serialization enables horizontal scaling — agents don't need sticky sessions.
- Event-driven rehydration aligns with the GitHub-native event model (issue closed → webhook → rehydrate blocked agent).

**SDK implications (resolved — see OR-001):**
- **Copilot SDK** (chosen runtime) handles session persistence natively. State persists to `~/.copilot/session-state/{sessionId}/` with checkpoints, planning state, and artifacts. `create_session()` / `resume_session()` map directly to sleep/wake lifecycle.
- No external database or custom serialization needed — mount the session state directory to persistent storage for containerized deployment.
- `get_messages()` returns `SessionEvent[]` for observability.
- Infinite sessions with auto-compaction prevent context overflow.

**Framework decision:** Copilot SDK. See [OR-001](open-research.md#OR-001) and [Copilot SDK Session Persistence](research/copilot-sdk-session-persistence.md).

---

## AD-004: Branch Strategy — Branch-Per-Issue

**Status:** Accepted  
**Decision:** Each assigned issue gets its own branch, following a naming convention: `feat/issue-{N}`, `fix/issue-{N}`, `security/issue-{N}`, etc.

**Rationale:**
- Clean isolation — each agent works in its own sandbox.
- Standard git workflow that humans are already familiar with.
- Easy to map branches back to issues for traceability.
- Merge conflicts are handled at PR time, not during development.

---

## AD-005: Merge Conflict Resolution — Agent-First, Human-Escalation Path

**Status:** Accepted  
**Decision:** When merge conflicts arise, the dev agent should first attempt to resolve them independently. If the agent cannot resolve (ambiguous conflicts, repeated failures), it follows the standard issue → assignment → closure flow to escalate to a human.

**Resolution flow:**
1. Agent attempts merge/rebase and encounters conflicts.
2. Agent attempts to resolve conflicts autonomously.
3. If resolution fails (or agent lacks confidence):
   a. Agent opens a new issue describing the conflict, referencing the original issue.
   b. PM agent tags the new issue with a `needs-human` label type.
   c. Framework automatically notifies designated human(s).
   d. Human resolves the conflict and closes the issue.
   e. PM agent detects closure, notifies the original dev agent.
   f. Dev agent rehydrates, assesses the current branch state (did the human finish the merge? make code changes? clean up?), completes remaining work, and closes its original issue.

**Open question:** Should there be a dedicated "merge conflict resolution" agent role, or is this always a dev-agent-then-human flow? → **Deferred to V2.** For V1, merge conflicts follow the dev-agent-then-human escalation flow. A dedicated merge resolution agent may be evaluated based on V1 experience. See [OR-007](open-research.md#OR-007).

---

## AD-006: Branch Protection — Configurable Per-Branch Approval Flows

**Status:** Accepted  
**Decision:** Branch protection and approval requirements are configurable per-branch in `.squadron/workflows/`. Different branches have different standards.

**Examples:**
- `main`: Requires security review agent approval + architecture review agent approval + test coverage agent approval + human PR review + human merge.
- `main` (minimal config): Agent PR review approval required, but human must manually merge.
- `feat/*` / `fix/*`: Lower bar — dev agent can self-merge after CI passes, or require only one agent review.

**Configuration lives in:** `.squadron/workflows/` (YAML or similar).

---

## AD-007: Collision Avoidance — No File Locking, Merge-Based Resolution

**Status:** Accepted  
**Decision:** Do NOT implement file-level locking or claim systems. Allow agents to work on overlapping files and rely on merge-based conflict resolution (AD-005) plus CI-based semantic validation.

**Rationale:**
- File locking is fragile and creates deadlock risks in a multi-agent system.
- Git's merge system is the established solution for concurrent file edits.
- CI/test suites catch semantic conflicts that merge can't.
- The escalation path to humans exists for truly ambiguous cases.

---

## AD-008: Issue Processing — Sequential, Not Parallel

**Status:** Accepted  
**Decision:** The PM agent processes issues one at a time. Speed of issue assignment is not a priority.

**Rationale:**
- Simplifies PM agent logic — no need for concurrent issue triage.
- Avoids race conditions on issue state — resolved by layered concurrency strategy (see [AD-014](architecture-decisions.md#AD-014) and [OR-004](open-research.md#OR-004)).
- Resource management is simpler — no need for queue management or overload protection.
- Can revisit for V2 if throughput becomes a bottleneck.

---

## AD-009: Approval Flows & Permissions — Fully Configurable

**Status:** Accepted  
**Decision:** All approval flows and permissions are configurable per-project:
- Who can approve agent PRs (agent-only, human-only, or both)
- What requires human sign-off
- Auto-merge rules (CI pass + approval → merge, or mandatory human merge)
- Escalation paths

**Configuration lives in:** `.squadron/workflows/` and `.squadron/config.yaml`.

**Schema:** Resolved — see [AD-015](architecture-decisions.md#AD-015) and [OR-005](open-research.md#OR-005) for the complete YAML schema definition.

**Open question:** ~~The specific schema and configuration format for approval flows needs to be designed.~~ **Resolved:** See [Approval Flow Schema](research/approval-flow-schema.md).

---

## AD-010: Context Window Management — SDK-Provided Solutions

**Status:** Accepted (hand-waved for now)  
**Decision:** Rely on SDK-provided context collapse/summary mechanisms. If we use a framework with fine-grained context control, we may implement something custom later, but this is not a priority for V1.

---

## AD-011: Kanban / GitHub Projects Management — V2

**Status:** Deferred to V2  
**Decision:** PM agent management of GitHub Projects (Kanban board) is a V2 feature. Focus on core issue→PR→merge flow first.

---

## AD-012: Event Architecture — GitHub App with Webhook-Based Event Delivery

**Status:** Accepted  
**Decision:** Use a registered GitHub App as the primary event delivery and identity mechanism. The App subscribes to webhook events, receives them at the Squadron server, and uses installation access tokens to interact with the GitHub API.

**Rationale:**
- Agents run on persistent infrastructure (Copilot SDK sessions). A server already exists. Webhooks deliver events directly to it (~1s latency) — no middleman needed.
- GitHub App provides a built-in bot identity (`squadron[bot]`), directly implementing AD-002 without requiring a separate bot user account, PAT, or service account.
- Installation access tokens are auto-scoped to installed repos with fine-grained permissions — principle of least privilege by design.
- This is the standard pattern used by Dependabot, CodeQL, Copilot, and every major GitHub integration.
- `repository_dispatch` API enables the reverse path: agents can trigger Actions workflows for CI/test execution without Actions being the primary event bus.

**Alternatives rejected:**
- **Pure GitHub Actions:** Not viable — 6-hour max runtime and ephemeral runners are incompatible with Copilot SDK's persistent session model.
- **Actions as thin dispatch:** Adds 15-45s latency (runner provisioning), burns Actions minutes, and introduces a second system to debug — all without benefit since the server already exists.

**Key constraints:**
- Must respond to webhooks within 10 seconds (async event queue required).
- `X-GitHub-Delivery` header used for idempotent event processing.
- Webhook secret + HMAC-SHA256 signature validation on every delivery.
- Installation access tokens expire after 1 hour — implement token refresh.

**Detailed research:** [Event Architecture Research](research/event-architecture.md)

---

## AD-013: Event Routing — Agent Registry with Subscription-Based Dependency Resolution

**Status:** Accepted  
**Decision:** Maintain a lightweight **Agent Registry** (SQLite for V1) that tracks all active agent sessions, their issue assignments, PR mappings, and blocker dependencies. The Event Router uses this registry for all dispatch decisions. Dependencies are registered at creation time (not discovered via GitHub API queries).

**Rationale:**
- The framework itself creates blocker relationships — it already knows the dependency graph at creation time. No GitHub API query or text parsing needed at dispatch time.
- A local SQLite store provides sub-millisecond lookups for event routing, zero external dependencies, and atomic writes.
- GitHub cross-references serve as human-readable audit trail and recovery mechanism, not primary routing source.
- The registry answers OR-009's remaining sub-question ("do we need additional state beyond Copilot SDK sessions?") — yes, but only this lightweight orchestrator metadata.

**Key operations:**
- **Direct routing:** `issue_number → agent_session_id` lookup
- **Dependency resolution:** `issues.closed → query agents WHERE blocked_by CONTAINS closed_issue → wake`
- **Cycle detection:** BFS through `blocked_by` edges before registering new dependencies
- **Feedback loop prevention:** Discard events where `sender.login == "squadron[bot]"`
- **Reconciliation:** Background loop every 5 minutes cross-checks SLEEPING agents' blockers against GitHub state

**Detailed research:** [Event Routing Research](research/event-routing.md)

---

## AD-014: Concurrency Strategy — Layered Defense Against Race Conditions

**Status:** Accepted  
**Decision:** Use a six-layer concurrency strategy that leverages existing architectural decisions plus two additional mechanisms: **re-read before write** (optimistic concurrency) and **last-write-wins with audit trail**.

**Rationale:**
- L1–L4 are already provided by existing decisions (sequential PM processing, per-issue event queues, webhook deduplication, bot self-event filtering).
- GitHub does NOT support conditional writes (no `If-Match` for PATCH/PUT), so full OCC is not implementable.
- Issue-level locking (via labels) was rejected — fragile, pollutes timeline, unnecessary with per-issue queues.
- The remaining gap (PM acts on stale state while human modifies issue) is closed by re-reading issue state before applying mutations.
- For truly simultaneous writes (agent closes, human reopens), last-write-wins is the correct behavior — human override should prevail.

**Detailed research:** [Concurrency Strategy](research/concurrency-strategy.md)

---

## AD-015: Approval Flow Schema — Declarative YAML Configuration

**Status:** Accepted  
**Decision:** Approval flows are defined in `.squadron/workflows/approval-flows.yaml` using a declarative schema with: `required_reviews[]` (role-based, with `agent:` and `human_group:` prefixes), `merge_policy`, `required_status_checks[]`, `protected_paths[]` (glob patterns for sensitive files), `escalation` rules, and `branch_rules[]` (per-branch overrides using `fnmatch`).

**Rationale:**
- Agent roles are enforced via required status checks (`squadron/{role}`), not PR reviews — because all agents share a single GitHub App identity (`squadron[bot]`).
- Human approvals use GitHub's standard required reviews mechanism.
- Two enforcement layers: GitHub branch protection (hard backstop that can't be bypassed) + framework-level orchestration (richer logic like protected paths, escalation timers, CI retries).
- `fnmatch` branch matching aligns with GitHub Rulesets patterns.
- Schema is the concrete implementation of AD-006 and AD-009.

**Detailed research:** [Approval Flow Schema](research/approval-flow-schema.md)

---

## AD-016: Role Enforcement — Dual-Layer Defense (GitHub + Framework)

**Status:** Accepted  
**Decision:** Enforce agent role constraints at **two layers**: (1) GitHub branch protection with required status checks per role (e.g., `squadron/security-review`), and (2) framework-level `SquadronGitHubClient` wrapper that validates role → action permissions before every API call.

**Rationale:**
- GitHub's `required_status_checks` with `app_id` scoping ensures only the Squadron App can satisfy its own required checks — this is the hard backstop.
- CODEOWNERS cannot distinguish agent roles (single `squadron[bot]` identity), so it's not useful for role-based enforcement.
- Framework-level enforcement covers actions GitHub can't gate (which agent pushes to which branch, which agent posts which status check context, which agent comments on which issue).
- Even if a framework bug allows unauthorized actions, GitHub branch protection still blocks merge without all required checks passing.
- V1 uses branch protection API; V2 can add GitHub Rulesets for richer configuration.

**Detailed research:** [Role Enforcement](research/role-enforcement.md)

---

## AD-017: Runtime Architecture — Single-Process Monolith with Per-Agent CopilotClient

**Status:** Accepted  
**Decision:** V1 runs as a **single Python asyncio process** (FastAPI + uvicorn) containing all components: webhook receiver, event queue, event router, agent manager, agent registry (SQLite), reconciliation loop, and GitHub client. Each agent gets its own `CopilotClient` instance, which spawns a dedicated Copilot CLI subprocess for process-level isolation.

**Key sub-decisions:**
- **PM agent:** Event-driven with fresh sessions per event batch + injected context (stateless — memory is the registry + GitHub).
- **Dev/Review agents:** Persistent sessions with sleep/wake via `resume_session()` (stateful — conversation memory is the value).
- **Agent-host communication:** Custom `@define_tool` functions (e.g., `check_for_events`, `report_blocked`, `report_complete`) are the ONLY bridge between agents and the framework.
- **Working directories:** `git worktree` provides each agent an isolated file tree with shared `.git` object store.
- **Crash recovery:** Mark stale ACTIVE agents as SLEEPING on restart; reconciliation loop (5 min) catches missed events.

**Rationale:**
- Simplest architecture for prototyping — one process, one codebase, one deployment unit.
- Copilot SDK CLI subprocesses provide crash isolation between agents even within a monolith.
- In-memory event queues and agent inboxes align with existing event-routing.md design.
- SQLite WAL mode fits single-writer monolith perfectly.
- Clean migration path to containers in Phase 3: agent code stays unchanged, only communication layer changes (in-process calls → HTTP).

**Tech stack:** FastAPI, uvicorn, Copilot SDK (Python), SQLite, httpx, PyYAML, Pydantic.

**Detailed research:** [Runtime Architecture](research/runtime-architecture.md)

---

## AD-018: Circuit Breakers — Multi-Limit Enforcement via SDK Hooks

**Status:** Accepted  
**Decision:** Enforce five hard limits per agent task: max iterations (5), max tool calls (200), max conversation turns (50), max active duration (2 hours), and max sleep duration (24 hours). Limits are configured in `.squadron/config.yaml` with global defaults and per-role overrides. Enforcement uses three layers: SDK `on_pre_tool_use` hook (tool calls, iterations, turns), asyncio timer (wall-clock), and reconciliation loop (sleep duration).

**Key sub-decisions:**
- **Warning at 80%** of any limit — injected via hook `additionalContext` so the agent can wrap up cleanly.
- **On trip:** Deny further tool calls → prompt agent for summary → mark ESCALATED → create needs-human issue.
- **Work preserved:** Branch, commits, and SDK session state always survive escalation.
- **Proxy metrics over exact cost:** Token/cost tracking is opaque in the Copilot SDK. Tool calls, turns, and wall-clock time are measurable proxies strongly correlated with cost. Exact cost tracking deferred to V2.
- **Iteration detection:** Heuristic — count test runner invocations (pytest, npm test, etc.) via `on_pre_tool_use` hook.

**Detailed research:** [Circuit Breaker Design](research/circuit-breakers.md)

---

## AD-019: Unified Pipeline System — Single Orchestration Primitive

**Status:** Proposed  
**Decision:** Replace the three parallel orchestration systems (config-driven triggers, Workflow Engine v2, and Review Policy) with a single **unified pipeline system** where pipelines are the sole orchestration primitive. All legacy orchestration code (`triggers`, `review_policy`, `workflows`, Workflow Engine v2) is **removed entirely** — no backward-compatibility shim, no auto-conversion, no deprecation period.

**Rationale:**
- Three independent systems (triggers, workflow engine, review policy) evolved separately and don't communicate — creating fragmented state, duplicated logic, and critical gaps.
- Human PR reviews are not tracked by the approval system (`_handle_pr_review_submitted` doesn't call `record_pr_approval()`).
- The `pr_approval` gate type is declared in the schema but never implemented in the engine.
- No post-review feedback loop exists: review requests changes → agent woken without structured context.
- Auto-merge is a callback, not a pipeline stage, making it hard to compose with gates and retries.
- Two separate registries (`AgentRegistry` + `WorkflowRegistryV2`) with no cross-references.
- Clean replacement is the correct pattern for refactors — maintaining parallel legacy code creates maintenance burden and confusion.

**Key design elements:**
- **Seven stage types** — `agent`, `gate`, `human`, `parallel`, `delay`, `action`, `webhook`. The `human` stage is a first-class primitive for human-in-the-loop interactions with notification lifecycle and reminders.
- **Sub-pipeline composition** — pipelines can invoke other pipelines via `type: pipeline` stages. Max nesting depth of 3. Cycle detection at config load.
- **Multi-PR pipelines** — `scope: multi-pr` enables cross-PR and cross-repo orchestration with per-PR gate checks.
- **Pluggable gate check registry** — built-in checks (`pr_approvals_met`, `ci_status`, `command`, `file_exists`, `label_present`, `no_changes_requested`, `human_approved`, `branch_up_to_date`) plus user-extensible via Python modules.
- **Reactive event subscriptions** — running pipelines react to events (e.g., `pull_request_review.submitted` re-evaluates gate conditions) instead of being fire-and-forget.
- **Framework-level approval recording** — both agent and human reviews are tracked, closing the human review tracking gap.
- **Unified `PipelineRegistry`** — single SQLite database for agents, pipeline runs, stages, gate checks, and PR approvals.
- **Pipeline versioning** — definition snapshotted on start; in-flight pipelines unaffected by config changes.
- **Configurable gate timeouts** — each gate independently specifies fail, escalate, extend, notify, or cancel behavior.

**Legacy code removed:**
- `agent_roles.<role>.triggers` config and dispatch logic
- `review_policy` config section and all related Pydantic models
- `_auto_merge_pr` callback and `_handle_merge_failure`
- `src/squadron/workflow/` directory (WorkflowEngine, WorkflowRegistryV2)
- Old PR approval SQL tables in `registry.py`

**Detailed design:** [Unified Pipeline System](../design/unified-pipeline-system.md)
