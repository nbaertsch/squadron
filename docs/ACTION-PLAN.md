# Squadron V2 — Action Plan

> **Purpose**: Concrete gap analysis and work plan mapping the current codebase
> against the locked-down design document (IMPLEMENTATION-PLAN.md). Every task
> traces to a design decision (D-x) or design challenge (DC-x).
>
> **Ground rule**: The MVP code is a reference, not a constraint. We rewrite,
> discard, or invert any feature as needed.

> **Status (2026-02-16)**: All phases complete. 406 tests passing.
> - Phase 1 (1.1–1.4): ✅ Done
> - Phase 2 (2.1–2.8): ✅ Done
> - Phase 3 (3.1–3.5): ✅ Done
> - Phase 4: ✅ 4.3–4.6 Done | ⏸ 4.1–4.2 Deferred (require live Copilot SDK)

---

## Codebase Audit Summary

### What works and stays

| Component | File | Status | Notes |
|---|---|---|---|
| Event routing | `event_router.py` | **Solid** | Clean async consumer loop, no hardcoded routing, config-driven handler registration via `on()`. Keep as-is. |
| Webhook receiver | `webhook.py` | **Solid** | HMAC verification, installation ID check, repo scope check, rate limiting, immediate 200 response. Keep as-is. |
| GitHub client | `github_client.py` | **Solid** | JWT auth with retry, rate limit tracking with throttling, full Issue/PR/Review API surface. Keep as-is. |
| Agent registry | `registry.py` | **Solid** | SQLite/WAL, full CRUD, BFS cycle detection for blockers, workflow run management, webhook dedup. Minor fix needed. |
| Resource monitor | `resource_monitor.py` | **Solid** | Lightweight system metrics, no gaps relative to design. Keep as-is. |
| Config loading | `config.py` | **Good foundation** | 19 Pydantic models, YAML frontmatter parsing, agent definition loading. Needs field additions, not rewrite. |
| Reconciliation loop | `reconciliation.py` | **Good foundation** | Periodic blocker checks + stale agent detection. Needs hardening, not rewrite. |
| Copilot SDK wrapper | `copilot.py` | **Good foundation** | Session create/resume/delete. Has `available_tools`/`excluded_tools` params ready but unwired. One bug to fix. |
| Workflow engine | `workflow_engine.py` | **Functional** | Sequential pipelines work for happy path. One known bug (`on_reject: restart` doesn't re-spawn). Needs trigger unification. |
| Config-driven triggers | `agent_manager.py` | **Partial** | `_register_trigger_handlers()` and `_handle_config_trigger()` are a good foundation. Coexists with hardcoded handlers that must be removed. |

### What must be rewritten or significantly refactored

| Component | File | Problem | Decision |
|---|---|---|---|
| Hardcoded PR spawn logic | `agent_manager.py` `_handle_pr_opened()` | Bypasses trigger system entirely. Hardcodes approval flow spawning. This is the exact DC-2 dual-system problem. | **Delete.** Replace with trigger conditions (D-6). |
| Hardcoded role names | `agent_manager.py` `_handle_pr_closed()`, `_handle_pr_updated()`, `_branch_name()` | Hardcodes `feat-dev`, `bug-fix`, `pr-review`, `security-review` strings. Breaks when operator adds custom roles. | **Delete.** Replace with config-driven lookups. |
| Tool bifurcation | `agent_manager.py` + `tools/` | PMTools vs FrameworkTools split based on `stateless` flag. No per-role tool selection. | **Merge** into unified `SquadronTools` (D-7). |
| `squadron init` CLI | `__main__.py` | Entire init scaffolding with templates. D-13 says ship example config, not CLI. | **Remove.** Ship example `.squadron/` directory instead. |
| Stale agent recovery | `server.py` `_recover_stale_agents()` | Marks ACTIVE/SLEEPING as ESCALATED. D-3 says stale should be FAILED, and Phase 3 says rebuild from GitHub. | **Rewrite** for Phase 3.2. Short-term: fix status to FAILED. |

### Known bugs (fix immediately)

| # | Bug | Location | Impact |
|---|---|---|---|
| B-1 | `agent.role.value` crash | `tools/framework.py` lines ~165, ~200; `tools/pm_tools.py` line ~120 | `role` is `str`, not enum. `.value` raises `AttributeError` at runtime. Every `report_blocked()`, `report_complete()`, and `check_registry()` call crashes. |
| B-2 | `reasoning_effort` always sent in resume | `copilot.py` `build_resume_config()` line ~248 | Always includes `"reasoning_effort": reasoning` even when `None`. `build_session_config()` does it correctly (conditional). Causes SDK errors or unexpected behavior. |
| B-3 | `on_reject: restart` doesn't re-spawn | `workflow_engine.py` `_handle_rejection()` | Updates DB `current_stage_index` but doesn't create a new agent for the restarted stage. Pipeline stalls on rejection. |
| B-4 | Semaphore leak on `CancelledError` | `agent_manager.py` `create_agent()` | If `_run_agent()` is cancelled, the semaphore may not be released. Over time, this exhausts the concurrency limit. |

---

## Phase 1: Foundation — Make the Core Loop Work

**Goal**: US-2 (Issue → Working Code) works reliably for a single issue.

### 1.1 — Example `.squadron/` config directory

**Decision**: D-13 (example config, not CLI scaffolding)

**Current state**: `__main__.py` has a `squadron init` command with `_DEFAULT_CONFIG`, `_DEFAULT_PM`, `_DEFAULT_FEAT_DEV`, `_DEFAULT_BUG_FIX`, `_DEFAULT_PR_REVIEW`, `_DEFAULT_SECURITY_REVIEW` template strings (~200 lines of templates).

**Tasks**:
1. Create `examples/.squadron/config.yaml` — minimal working config with only `project.name` + `human_groups.maintainers` required, everything else using Pydantic defaults
2. Create `examples/.squadron/agents/pm.md`, `feat-dev.md`, `pr-review.md` — starter agent definitions extracted from `_DEFAULT_PM` etc.
3. Remove `_init_project()` function and `init` subparser from `__main__.py`
4. Remove all `_DEFAULT_*` template strings from `__main__.py`
5. Update `README.md` to reference example config instead of `squadron init`
6. Verify `config.py` Pydantic defaults produce a working system with only the two required fields

**Files touched**: `__main__.py`, `config.py` (verify defaults), `README.md`, new `examples/` directory

---

### 1.2 — Fix known runtime bugs

**Bugs**: B-1, B-2, B-3, B-4

**Tasks**:
1. **B-1**: In `tools/framework.py` and `tools/pm_tools.py`, replace all `agent.role.value` with `agent.role` (3 occurrences)
2. **B-2**: In `copilot.py` `build_resume_config()`, wrap `reasoning_effort` in a conditional:
   ```python
   if reasoning is not None:
       config["reasoning_effort"] = reasoning
   ```
3. **B-3**: In `workflow_engine.py` `_handle_rejection()`, after resetting `current_stage_index`, call `self.agent_manager.spawn_workflow_agent()` with the correct stage config
4. **B-4**: In `agent_manager.py` `create_agent()`, wrap the semaphore-guarded block in `try/finally` to ensure release on cancellation

**Files touched**: `tools/framework.py`, `tools/pm_tools.py`, `copilot.py`, `workflow_engine.py`, `agent_manager.py`

---

### 1.3 — Background duration timer

**Decision**: D-10 (background timers for duration limits)

**Current state**: `agent_manager.py` `_run_agent()` relies on `send_and_wait(timeout=max_duration)` — this is an SDK-level timeout, not framework-enforced. The reconciliation loop (`reconciliation.py`) polls every N minutes and marks stale agents ESCALATED. Neither provides real-time enforcement.

**Tasks**:
1. In `agent_manager.py`, when starting an agent task, create a companion `asyncio` timer task:
   ```python
   async def _duration_watchdog(self, agent_id: str, max_seconds: int):
       await asyncio.sleep(max_seconds)
       # Timer expired — cancel the agent task
       await self._cancel_agent(agent_id, reason="max_active_duration exceeded")
   ```
2. Store the watchdog task alongside the agent task in `_active_tasks`
3. Cancel the watchdog when the agent completes normally
4. On watchdog trigger: cancel agent task, mark ESCALATED, post `needs-human` issue
5. Keep reconciliation loop as second-layer catch (in case watchdog is lost on restart)

**Files touched**: `agent_manager.py`

---

### 1.4 — Validate the core loop end-to-end

**Tasks**:
1. Using the example config from 1.1, deploy locally and run through the full happy path manually:
   - Create a GitHub issue with `feature` label
   - Verify PM triggers, triages, applies label
   - Verify dev agent triggers on label, creates branch, implements, opens PR
   - Verify review agent triggers on PR, posts review
   - Verify the human can merge
2. Document any prompt quality issues, timing problems, or unhandled edge cases discovered
3. Write an integration test that simulates this flow with mocked GitHub API + mocked Copilot SDK

**Files touched**: `tests/` (new integration test), possibly agent definition `.md` files for prompt tuning

---

## Phase 2: Config-Driven Everything

**Goal**: US-4 — add a new agent role via YAML + markdown only, zero Python changes.

### 2.1 — Unified tool registry

**Decision**: D-7 (tool boundaries enforced, not suggested)

**Current state**: Tools are split across two classes:
- `FrameworkTools` (8 tools): `check_for_events`, `report_blocked`, `report_complete`, `create_blocker_issue`, `escalate_to_human`, `comment_on_issue`, `submit_pr_review`, `open_pr`
- `PMTools` (6 tools): `create_issue`, `assign_issue`, `label_issue`, `comment_on_issue`, `check_registry`, `read_issue`

`agent_manager.py` picks one class based on `stateless` flag. No per-role selection.

**Tasks**:
1. Create `tools/squadron_tools.py` — a single `SquadronTools` class that registers all 14 tools (deduping `comment_on_issue` which appears in both)
2. Each tool is registered by name in a `dict[str, Callable]`
3. Add `get_tools(names: list[str]) -> list[Tool]` that returns only the requested subset
4. If `names` is empty or `None`, return sensible defaults based on lifecycle type (backward compat during migration)
5. Delete `tools/framework.py` and `tools/pm_tools.py`
6. Update `agent_manager.py` to use `SquadronTools` everywhere

**Files touched**: New `tools/squadron_tools.py`, delete `tools/framework.py`, delete `tools/pm_tools.py`, `agent_manager.py`, `tools/__init__.py`

---

### 2.2 — Config-driven tool assignment

**Decision**: D-7

**Current state**: `AgentRoleConfig` has no `tools` field. `copilot.py` `build_session_config()` accepts `available_tools`/`excluded_tools` but they're never wired from config.

**Tasks**:
1. Add `tools: list[str] | None = None` to `AgentRoleConfig` in `config.py`
2. Add sensible defaults per role in the example config:
   ```yaml
   agent_roles:
     pm:
       tools: [create_issue, label_issue, assign_issue, comment_on_issue, read_issue, check_registry]
     feat-dev:
       tools: [comment_on_issue, open_pr, report_blocked, report_complete, check_for_events, escalate_to_human]
   ```
3. In `agent_manager.py`, pass the role's `tools` list to `SquadronTools.get_tools()` when building the session
4. If no `tools` list is configured, fall back to lifecycle-based defaults (ephemeral → PM-like tools, persistent → dev-like tools)

**Files touched**: `config.py`, `agent_manager.py`

---

### 2.3 — Unified trigger system

**Decision**: D-6 (unified trigger system with two spawn modes)

**Current state**: Two parallel spawn paths coexist:
1. **Config triggers** (`_register_trigger_handlers` → `_handle_config_trigger`): scans config, registers handlers for each event type, matches triggers by event+label+bot filter. This is the right design.
2. **Hardcoded handlers** (registered in `start()`): `_handle_pr_opened`, `_handle_pr_closed`, `_handle_pr_review_received`, `_handle_pr_updated` — these bypass the trigger system and hardcode role names + approval flow logic.

Additionally, `workflow_engine.py` `evaluate_event()` has its own trigger matching that runs in parallel with the event router.

**Tasks**:
1. **Add `condition` field to `AgentTrigger`** in `config.py`:
   ```python
   class AgentTrigger(BaseModel):
       event: str
       label: str | None = None
       filter_bot: bool = True
       condition: dict[str, Any] | None = None  # e.g., {"approval_flow_required": true}
   ```

2. **Implement condition evaluation** in `agent_manager.py` `_handle_config_trigger()`:
   - When a trigger has `condition.approval_flow_required: true`, check the `approval_flows` config to see if this role is required for the target branch
   - Pass/fail determines whether the trigger fires

3. **Delete hardcoded handlers**:
   - Remove `_handle_pr_opened()` entirely (replaced by triggers with conditions)
   - Remove `_handle_pr_closed()` — replace with config triggers on `pull_request.closed` that use lifecycle semantics (ephemeral agents → auto-complete on PR close)
   - Remove `_handle_pr_updated()` — replace with config trigger on `pull_request.synchronize` that wakes relevant sleeping agents

4. **Unify workflow triggers with event router**:
   - Workflows already have `triggers` in their YAML definition
   - In `_handle_config_trigger()`, add workflow initiation: if a trigger maps to a `workflow:` directive instead of a role, call `workflow_engine.create_run()`
   - Remove `evaluate_event()` from `workflow_engine.py` — the event router handles all dispatch

5. **Replace hardcoded role names** in `_branch_name()` with config-driven branch templates (already in `BranchNamingConfig`)

6. **Fix duplicate guard**: `create_agent()` uses `get_agent_by_issue()` (returns ONE agent). D-6 says multiple roles can trigger on the same event. Change to: check for existing agent *with the same role* for this issue, not any agent.

**Files touched**: `config.py`, `agent_manager.py` (major refactor), `workflow_engine.py`, `event_router.py` (minor)

**This is the largest single work item.** Consider splitting into sub-tasks:
- 2.3a: Add condition field + evaluation logic
- 2.3b: Delete `_handle_pr_opened()`, wire approval flows through triggers
- 2.3c: Delete `_handle_pr_closed()` + `_handle_pr_updated()`, replace with config triggers
- 2.3d: Unify workflow triggers
- 2.3e: Fix branch naming + duplicate guard

---

### 2.4 — SDK tool restriction research

**Decision**: D-7 (layer 2: SDK built-in tool control)

**Tasks**:
1. Create a test script that starts a Copilot SDK session with `excluded_tools: ["bash"]`
2. Verify the agent cannot invoke `bash`
3. Test with `available_tools` (allowlist mode)
4. Document findings: does it work? Are there edge cases? What's the exact param format?
5. If it works → proceed to 2.5
6. If it doesn't → document limitation, rely on `on_pre_tool_use` hook as fallback

**Files touched**: Research only, no production changes

---

### 2.5 — SDK built-in tool restriction (if 2.4 confirms)

**Tasks**:
1. Add `excluded_sdk_tools: list[str] | None = None` to `AgentRoleConfig` in `config.py`
2. In `agent_manager.py`, pass the role's `excluded_sdk_tools` to `copilot.py` `build_session_config()`
3. Wire through to the `excluded_tools` param that already exists in `build_session_config()`
4. Add to example config:
   ```yaml
   agent_roles:
     pm:
       excluded_sdk_tools: [bash, write_file, edit_file]
   ```

**Files touched**: `config.py`, `agent_manager.py`, `copilot.py` (already has the param, just needs wiring)

---

### 2.6 — Lifecycle semantic rename

**Current state**: `AgentRoleConfig` has `stateless: bool = False`.

**Tasks**:
1. Rename `stateless` to `lifecycle` with type `Literal["ephemeral", "persistent"]`, default `"persistent"`
2. Add Pydantic `model_validator` for backward compat: if `stateless: true` is found, convert to `lifecycle: "ephemeral"`
3. Update all references in `agent_manager.py` (currently checks `role_config.stateless`)
4. Update example config to use `lifecycle: ephemeral` for PM role

**Files touched**: `config.py`, `agent_manager.py`

---

### 2.7 — PR sleep/wake lifecycle

**Decision**: D-11 (sleep/wake on review)

**Current state**: `_handle_pr_review_received()` exists and wakes sleeping dev agents when any review is submitted. Partially correct — needs to filter to `changes_requested` only.

**Tasks**:
1. Refine wake trigger: only wake on `action: "changes_requested"`, not on approvals
2. When waking, inject review context: review body, comments, current diff state
3. After agent opens PR → framework transitions agent to SLEEPING (this should happen via a config trigger on `pull_request.opened` for the agent's own PR, not hardcoded)
4. Connect to pre-sleep commit+push hook (Phase 3.1)

**Files touched**: `agent_manager.py`

---

### 2.8 — PM context injection

**Decision**: D-8 (ephemeral PM with rich context injection)

**Current state**: `_build_stateless_prompt()` already injects registry state, event details, and available roles. This is partially done.

**Tasks**:
1. Enrich the injected context:
   - Recent issue triage history (last N issues PM handled — query from registry or GitHub)
   - Pending escalations (open `needs-human` issues)
   - Current agent workload summary
2. Format as structured markdown that the PM can parse
3. Ensure the PM agent definition instructs it to use this context
4. Test that PM makes good triage decisions with only injected context (no persistent memory)

**Files touched**: `agent_manager.py` (`_build_stateless_prompt`), PM agent definition `.md`

---

## Phase 3: Persistence & Resilience

**Goal**: US-3 — agents survive container restarts. The system is self-healing.

### 3.1 — WIP commit + push before sleep

**Decision**: D-9 (agents commit + push before sleeping)

**Current state**: No pre-sleep hook exists. When `report_blocked()` is called, the agent enters SLEEPING state but uncommitted work is lost on restart.

**Tasks**:
1. In `agent_manager.py`, add a `_pre_sleep_hook(agent: AgentRecord)` method:
   ```python
   async def _pre_sleep_hook(self, agent: AgentRecord):
       worktree = self._get_worktree_path(agent)
       await self._run_git(worktree, ["add", "-A"])
       await self._run_git(worktree, ["commit", "-m", "[squadron-wip] auto-save before sleep", "--allow-empty"])
       await self._run_git(worktree, ["push", "origin", agent.branch])
   ```
2. Call this hook in every sleep transition: `report_blocked()`, `report_complete()` (for PR-opening agents that sleep), and any framework-initiated sleep
3. Handle failures gracefully — if push fails (e.g., conflict), log and continue (the sleep is more important than the push)

**Files touched**: `agent_manager.py`, `tools/squadron_tools.py` (or equivalent)

---

### 3.2 — GitHub-based state reconstruction

**Decision**: D-4 (GitHub is the durable state layer)

**Current state**: `server.py` `_recover_stale_agents()` just marks ACTIVE/SLEEPING agents as ESCALATED. No attempt to reconstruct from GitHub.

**Tasks**:
1. Add `reconstruct_from_github()` method to `server.py` or a new `recovery.py`:
   - Query GitHub Issues API for open issues with squadron-managed labels (`squadron:in-progress`, `squadron:blocked`, `squadron:needs-human`)
   - Query GitHub PRs for open PRs by `squadron[bot]`
   - For each, reconstruct `AgentRecord`:
     - Role: from labels (e.g., `feature` → `feat-dev`)
     - Status: from labels (`blocked` → SLEEPING, `in-progress` → ACTIVE candidate)
     - Branch: from branch naming convention
     - `blocked_by`: from issue body cross-references
   - Insert reconstructed records into registry
2. For agents that were in-progress: spawn a new session with context injection ("you were previously working on this issue...")
3. For blocked agents: mark SLEEPING, let reconciliation handle wake
4. Call `reconstruct_from_github()` on startup *instead of* `_recover_stale_agents()`
5. Handle idempotency: if registry already has records (normal restart, not data loss), skip reconstruction

**Files touched**: `server.py`, new `recovery.py`, `agent_manager.py` (context injection for reconstructed agents)

---

### 3.3 — Recovery semantics: FAILED status

**Decision**: DC-3 (stale agents should be FAILED)

**Current state**: `AgentStatus` has CREATED, ACTIVE, SLEEPING, COMPLETED, ESCALATED. No FAILED state. `_recover_stale_agents()` marks stale as ESCALATED.

**Tasks**:
1. Add `FAILED = "failed"` to `AgentStatus` enum in `models.py`
2. Update `_recover_stale_agents()` (or the replacement reconstruction logic) to mark stale ACTIVE agents as FAILED
3. On FAILED: create a `needs-human` issue documenting what was lost
4. Update registry queries that filter by status to handle FAILED appropriately
5. Update health endpoint to report FAILED agent count

**Files touched**: `models.py`, `server.py` / `recovery.py`, `registry.py`, `server.py` (health endpoint)

---

### 3.4 — Reconciliation loop hardening

**Tasks**:
1. Verify `_check_sleeping_agents()` correctly catches:
   - Resolved blockers (blocker issue closed → wake agent)
   - Max sleep duration exceeded → ESCALATED
   - Orphaned agents (issue was closed while agent was sleeping)
2. Verify `_check_stale_active_agents()` correctly catches:
   - Active agents with no running task (framework bug or restart)
   - Active agents exceeding `max_active_duration` (backup for background timer)
3. Add reconciliation for:
   - PRs that were merged/closed while the dev agent was sleeping
   - Issues that were reassigned while the agent was sleeping (D-12)
4. Add structured logging for every reconciliation action

**Files touched**: `reconciliation.py`

---

### 3.5 — Framework-level abort on reassignment

**Decision**: D-12 (framework cancels agent on reassignment)

**Current state**: `ISSUE_ASSIGNED` is mapped in EVENT_MAP but no handler cancels agents. The agent's `check_for_events` tool is the only detection mechanism (polling, not reactive).

**Tasks**:
1. In `agent_manager.py` `start()`, register a handler for `ISSUE_ASSIGNED` events
2. Handler logic:
   - Get the new assignee from the event payload
   - If the new assignee is NOT `squadron[bot]` / `squadron-dev[bot]`:
     - Find active/sleeping agents for this issue
     - For ACTIVE: cancel the agent's `asyncio.Task`, post comment ("Agent stopped — issue reassigned to @{user}")
     - For SLEEPING: update registry, remove from wake conditions
     - Preserve the branch (don't delete — human may want it)
3. If the issue is reassigned *to* `squadron[bot]`, treat it as a spawn trigger (per existing trigger logic)

**Files touched**: `agent_manager.py`

---

## Phase 4: Operational Maturity

**Goal**: US-7, US-9, US-10 — cost control, observability, config reload.

### 4.1 — Concurrency resource benchmark

**Tasks**:
1. Spin up 1, 3, 5 concurrent CopilotClient sessions
2. Measure: memory per process, CPU utilization, latency per tool call
3. Determine safe `max_concurrent_agents` default for 2-core/4GB container
4. If agents are heavier than expected: implement priority queue (PM first, review medium, dev FIFO)
5. Document findings and update default config

---

### 4.2 — SDK circuit breaker research

**Tasks**:
1. Test if `on_pre_tool_use` fires for SDK built-in tools (bash, read_file, etc.)
2. Test if token usage is exposed per turn
3. Document enforcement coverage: which limits are framework-enforced (background timer) vs SDK-enforced (hook) vs unenforceable (pure reasoning)
4. If `on_pre_tool_use` doesn't fire for built-ins: document limitation and confirm background timer is sufficient

---

### 4.3 — Deploy pipeline cleanup

**Tasks**:
1. Audit `infra/main.bicep` — verify environment variables match real requirements
2. Audit `deploy/azure-container-apps/squadron-deploy.yml` — remove references to nonexistent infrastructure
3. Simplify to single-job deployment
4. Verify `SQUADRON_REPO_URL` works correctly in Bicep for git clone at startup

---

### 4.4 — Health endpoint enrichment

**Current state**: Health endpoint returns agent counts + resource snapshot.

**Tasks**:
1. Add: queue depth (event queue size), last event timestamp, last agent spawn timestamp
2. Add: per-status agent breakdown (ACTIVE: 3, SLEEPING: 2, FAILED: 1)
3. Add: registry stats (total agents, workflow runs)
4. Add: config validation status (is current config valid?)
5. Consider: prometheus-compatible metrics format

---

### 4.5 — Config hot-reload

**Decision**: D-5 (config reload via push event detection)

**Tasks**:
1. In `event_router.py`, detect push events to default branch that modify `.squadron/**` files
2. On detected change: `git pull`, re-read config, validate with Pydantic
3. If valid: swap config atomically. New agent spawns use new config. In-flight agents continue with old config.
4. If invalid: keep old config, log error, optionally create a GitHub issue warning the operator
5. Store config version (commit SHA) for debugging

**Files touched**: `event_router.py`, `server.py`, `config.py`

---

### 4.6 — Subagent behavior research

**Tasks**:
1. Investigate what `custom_agents` does in the Copilot SDK
2. Determine if `subagents` config field in `AgentDefinition` is meaningful
3. Document findings and either implement or remove the field

---

## Execution Order & Dependencies

```
Phase 1 (sequential — foundation must be solid before building on it):
  1.2 Bug fixes ──┐
  1.1 Example config ──┤
  1.3 Background timer ──┤
  1.4 E2E validation ◄───┘  (depends on 1.1-1.3 being done)

Phase 2 (partially parallelizable):
  2.1 Unified tool registry ──► 2.2 Config-driven tool assignment
  2.4 SDK tool research ──► 2.5 SDK tool restriction
  2.3 Unified trigger system (largest item, can start in parallel with 2.1)
  2.6 Lifecycle rename (independent)
  2.7 PR sleep/wake (depends on 2.3)
  2.8 PM context injection (independent)

Phase 3 (sequential — each builds on the previous):
  3.1 Commit before sleep
  3.2 GitHub state reconstruction (depends on 3.1)
  3.3 FAILED status (independent, but needed for 3.2)
  3.4 Reconciliation hardening (depends on 3.2, 3.3)
  3.5 Reassignment abort (independent)

Phase 4 (independent research + implementation):
  4.1-4.6 all independent, can be done in any order
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Copilot SDK `available_tools`/`excluded_tools` doesn't work | Medium | High (no SDK tool enforcement) | Research early (2.4). Fallback: `on_pre_tool_use` hook for all enforcement. |
| Unified trigger refactor (2.3) introduces regressions | High | High | Split into sub-tasks. Write tests for each condition type before refactoring. Keep existing handlers until new ones are proven. |
| CopilotClient memory overhead makes 5 agents infeasible | Medium | Medium | Benchmark early (4.1). Fallback: agent priority queue with lower default concurrency. |
| GitHub state reconstruction (3.2) is unreliable | Medium | Medium | Conservative reconstruction: if ambiguous, mark FAILED and escalate rather than guessing. |
| `on_reject: restart` fix (B-3) has edge cases | Low | Low | Test with the workflow engine's existing stage management. |

---

## Definition of Done per Phase

**Phase 1**: A human creates an issue. Agents triage, implement, and open a reviewed PR. No crashes. Background timer kills stuck agents.

**Phase 2**: An operator adds a `docs-writer` role by editing only `config.yaml` + creating `agents/docs-writer.md`. The framework picks it up. The new role has the correct tools and triggers. No Python changes.

**Phase 3**: Deploy. Open 3 issues. Kill the container. Restart. System reconstructs from GitHub. Agents resume with fresh sessions and correct context. Reassigning an issue immediately stops the agent.

**Phase 4**: Operator sees agent status in health endpoint. Config changes on `main` are picked up without redeploy. Cost controls are validated through benchmarks.
