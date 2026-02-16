# Squadron Systems Design Review Plan

> **⚠️ Superseded**: This document was the research/design phase. All decisions
> have been resolved and implemented via [ACTION-PLAN.md](ACTION-PLAN.md).
> Kept for historical reference only.

> **Purpose**: Top-down design review — requirements first, architecture second.
> Not a bug list. This plan asks "what should Squadron *be*?" before asking
> "is the code correct?" Every section opens with user intent, then derives
> system design from that intent. Refactoring and re-scoping are expected outcomes.

---

## 0 — Methodology

Each section follows the same structure:

1. **Intent** — What does the user/operator actually want?
2. **Current State** — What does the code do today? (with specific file refs)
3. **Gaps & Contradictions** — Where does current state diverge from intent?
4. **Design Questions** — Open questions that need answers before implementation.
5. **Proposed Direction** — Candidate solution (to be validated during review).

Work through sections in order. Each section's decisions cascade into later ones.

---

## 1 — Core Product Identity

### Intent
Squadron is a GitHub-native, multi-agent autonomous development framework.
A repo owner drops a `.squadron/` directory into their project, deploys to a
cloud container, and from then on GitHub issues/PRs trigger AI agents that
triage, implement, review, and merge code — autonomously.

### Design Questions
- **Single-tenant only, or multi-tenant?** Current design is firmly single-tenant
  (one container = one repo). Is that the forever model? If yes, the architecture
  can stay simple. If multi-tenant is on the roadmap, almost everything changes.
- **Self-hosting only, or managed service?** This affects whether config must be
  baked into the container or loaded at runtime from an external source.
- **What is "autonomous"?** Full auto-merge? PR creation only? Human-in-the-loop
  default with opt-out? This defines the default approval flow posture.

### Proposed Direction
Lock in: **single-tenant, self-hosted, one container per repo**. This simplifies
everything and matches the current `.squadron/`-in-repo model. Multi-tenant is a
different product.

---

## 2 — Configuration Philosophy

### Intent
The user said: *"ALL EVENT→AGENT routing MUST be defined in yaml! The PM agent
is NOT special — it's just another agent."* Config is the single source of truth
for the entire system's behavior. Adding a new agent role should require only
YAML + a markdown prompt — zero Python changes.

### Current State
- Config schema: 19 Pydantic models in `config.py` (546 lines).
- `config.yaml` defines triggers, labels, models, circuit breakers, approval flows.
- Agent definitions: `.squadron/agents/*.md` with YAML frontmatter (tools, description,
  mcp_servers, custom_agents, infer).

### Gaps & Contradictions
1. **`tools` frontmatter is informational, not enforced.** Agent `.md` files list
   tools like `read_file`, `write_file`, `bash`, `git`, `grep` — but these are
   Copilot SDK built-in tools, not Squadron-registered tools. The actual tool
   delivery is decided by the `stateless` flag in Python code (`PMTools` vs
   `FrameworkTools`). The frontmatter `tools` list is aspirational documentation.
2. **`trigger: approval_flow` is a separate scalar field.** Roles like `pr-review`
   and `security-review` have both `triggers: []` and `trigger: approval_flow`.
   These are two unrelated mechanisms coexisting confusingly.
3. **`assignable_labels` marked DEPRECATED** but still in the Pydantic models.
4. **`subagents` are config-driven but behavior is SDK-opaque.** The config says
   `subagents: [feat-dev, bug-fix, ...]` and the code builds `CustomAgentConfig`
   dicts for the SDK — but it's unclear when/how the SDK actually invokes subagents.
5. **`stateless` flag determines tool set in Python** — this is behavioral config
   that should be derivable from the YAML, but the mapping (stateless → PMTools,
   stateful → FrameworkTools) is hardcoded.

### Design Questions
- Should tool gating be defined in config? E.g., `tools: [create_issue, label_issue]`
  in `agent_roles.pm` determines exactly which Squadron tools that role gets?
- Should `trigger: approval_flow` become a trigger type within the `triggers[]`
  array? E.g., `{event: "pull_request.opened", via: workflow_engine}`?
- Should `stateless` be replaced by a more descriptive concept like `lifecycle: ephemeral | persistent`?
- What does `infer: true` in agent `.md` frontmatter do? Is it used anywhere?

### Proposed Direction
- **Remove `assignable_labels`** — dead code.
- **Unify `trigger` and `triggers`** — everything goes through `triggers[]`.
  Approval flow becomes a trigger source, not a separate field.
- **Make tool sets explicit in config** — `tools: [create_issue, comment_on_issue]`
  on the role, and the code builds the tool list from that. Eliminates the
  stateless→PMTools/stateful→FrameworkTools bifurcation.
- **Audit `infer` frontmatter** — remove if unused.

---

## 3 — Event Routing Architecture

### Intent
GitHub webhook → internal event → match against config triggers → spawn or wake
the right agent. The routing layer should be a dumb dispatcher with zero domain
knowledge — all intelligence lives in the config.

### Current State
- `event_router.py` (235 lines): Async consumer loop. `EVENT_MAP` maps
  `"action.type"` → `SquadronEventType`. `_dispatch()` calls registered handlers.
  Bot self-event filtering via `_BOT_ALLOWED_EVENTS` whitelist.
- `agent_manager.py`: `_register_trigger_handlers()` scans config at startup,
  registers `_handle_config_trigger()` for each event type. Hardcoded lifecycle
  handlers still registered separately (`ISSUE_CLOSED`, `PR_OPENED`, `PR_CLOSED`,
  `PR_REVIEW_SUBMITTED`, `PR_SYNCHRONIZED`).

### Gaps & Contradictions
1. **Lifecycle handlers are still hardcoded.** `_handle_issue_closed`,
   `_handle_pr_opened`, `_handle_pr_closed`, `_handle_pr_review_received`,
   `_handle_pr_updated` are registered with `router.on()` directly — not through
   config triggers. These handle "wake sleeping agent on PR feedback" and
   "complete agent on issue close" — framework-level concerns, not user-configurable.
2. **PR routing is implicit.** When a PR is opened, how does the system know which
   agent owns it? Currently through `_extract_issue_number()` from the PR body
   and registry lookup. This works but is fragile.
3. **`_BOT_ALLOWED_EVENTS`** is a hardcoded whitelist in `event_router.py` — this
   should arguably be config-driven (per-trigger `filter_bot` already exists).
4. **No negative triggers.** You can trigger on `issues.labeled` with label `bug`,
   but you can't say "trigger on `issues.opened` UNLESS label X is present."

### Design Questions
- Are lifecycle handlers (issue close → agent complete, PR feedback → agent wake)
  fundamentally different from user-defined triggers, or should they also be
  config-expressible?
- Is the PR→agent ownership model (parse issue number from PR body) good enough,
  or does it need a first-class `pr_number → agent_id` mapping in the registry?
- Should `_BOT_ALLOWED_EVENTS` be replaced entirely by per-trigger `filter_bot`?

### Proposed Direction
- **Keep lifecycle handlers hardcoded but clearly separated.** They're framework
  invariants, not user policy. Document this distinction.
- **Add `pr_number` as a first-class field on `AgentRecord`** (it already exists
  in the model — verify it's used consistently for PR ownership lookup).
- **Move `_BOT_ALLOWED_EVENTS` logic into config** — the per-trigger `filter_bot`
  field already exists, just needs to be the *only* mechanism.

---

## 4 — Agent Lifecycle Model

### Intent
Agents are autonomous workers. Some are ephemeral (PM: triage an issue and done),
others are persistent (feat-dev: implement across multiple turns, sleep waiting for
PR feedback, wake and iterate). The lifecycle should be clean and predictable.

### Current State
Two lifecycle modes, determined by `stateless` flag:
- **Stateless** (`stateless: true`): Fresh session per event. Timestamp-suffixed ID.
  No worktree. PMTools. Auto-destroy after completion. (Used by PM.)
- **Stateful** (default): Persistent session with sleep/wake. Git worktree for
  branch isolation. FrameworkTools. Session preserved across turns. Can be resumed.

State machine: `CREATED → ACTIVE → {SLEEPING, COMPLETED, ESCALATED}`.
Sleeping agents can be woken → ACTIVE again.

### Gaps & Contradictions
1. **Sessions persist to `/tmp`** — but `/tmp` is ephemeral container storage.
   If the container restarts, all Copilot SDK session state is lost. The recovery
   code marks stale ACTIVE agents as SLEEPING, but there's no session data to
   resume from. This is a fundamental contradiction.
2. **Worktrees also on ephemeral disk** — if a stateful agent is mid-implementation
   and the container restarts, its git worktree (with uncommitted changes) is gone.
3. **`duplicate guard` inconsistency** — `_handle_config_trigger` checks
   `get_agents_for_issue()` but `create_agent` also has its own duplicate check
   via `get_agent_by_issue()`. These use different registry methods and may behave
   differently (one returns all agents for an issue, the other returns the first).
4. **Turn counting** — `turn_count` is incremented in the post-turn state machine
   but `iteration_count` is incremented on wake. Two different counters tracking
   similar concepts.
5. **Concurrency semaphore** — acquired in `create_agent` and `wake_agent` but
   released in `_cleanup_agent` and on SLEEPING transition. If an agent errors
   before reaching cleanup, the slot may leak.

### Design Questions
- **Is session persistence across container restarts a requirement?** If yes,
  sessions need durable storage (which contradicts removing Azure Files). If no,
  the recovery code should destroy orphaned agents instead of marking them SLEEPING.
- **Should stateful agents commit WIP before sleeping?** This would make worktree
  loss recoverable (agent can re-clone and continue from last commit).
- **Is the dual duplicate-guard intentional?** Should there be a single point of
  truth for "does this agent already exist?"

### Proposed Direction
- **Accept ephemeral sessions.** Container restarts = agents restart from scratch.
  Recovery should mark stale agents as FAILED/DESTROYED, not SLEEPING. Document
  this as a design decision.
- **Agents should commit WIP before sleeping.** Add a pre-sleep hook that commits
  any uncommitted changes with a `[squadron-wip]` message. On wake, the agent can
  pick up from the last WIP commit even if the worktree was recreated.
- **Unify duplicate guards** — single method, single call site.
- **Consolidate `turn_count` and `iteration_count`** into one counter, or clearly
  define their distinct semantics.

---

## 5 — Tool Architecture

### Intent
Each agent role gets a specific set of tools. A PM can create/label/comment on
issues but can NOT read/write files. A feat-dev can read/write/bash/git but can
NOT create new issues (it should escalate instead). Tool boundaries enforce the
separation of concerns between roles.

### Current State
Two tool classes:
- `PMTools` (198 lines): `create_issue`, `assign_issue`, `label_issue`,
  `comment_on_issue`, `check_registry`, `read_issue`.
- `FrameworkTools` (430 lines): `check_for_events`, `report_blocked`,
  `report_complete`, `create_blocker_issue`, `escalate_to_human`,
  `comment_on_issue`, `submit_pr_review`, `open_pr`.

Additionally, the Copilot SDK provides built-in tools (`read_file`, `write_file`,
`bash`, `git`, `grep`, etc.) that are always available to all agents. The agent
`.md` frontmatter lists these as `tools:` but Squadron has no mechanism to
restrict them.

### Gaps & Contradictions
1. **SDK built-in tools are unrestricted.** A PM agent has access to `bash` and
   `write_file` through the SDK even though its prompt says "you do NOT write code."
   Prompt-based restriction is not enforcement.
2. **`comment_on_issue` exists in BOTH PMTools AND FrameworkTools.** Duplication.
3. **Tool selection is binary** — stateless gets PMTools, stateful gets FrameworkTools.
   There's no mixing. A stateful agent that needs both `open_pr` (FrameworkTools)
   and `create_issue` (PMTools) can't have both unless we refactor.
4. **Agent `.md` `tools` frontmatter is purely aesthetic.** The SDK may support
   `available_tools` / `excluded_tools` in `SessionConfig` — `copilot.py` already
   accepts these parameters but they're never populated.

### Design Questions
- **Can the SDK enforce tool restrictions?** The `available_tools` and
  `excluded_tools` fields in `SessionConfig` suggest yes. Need to verify SDK behavior.
- **Should PMTools and FrameworkTools merge into a single `SquadronTools` class**
  with role-based filtering? Or should they stay separate but composable?
- **Should tool sets be defined in config.yaml?** E.g.:
  ```yaml
  agent_roles:
    pm:
      squadron_tools: [create_issue, label_issue, comment_on_issue, read_issue]
    feat-dev:
      squadron_tools: [comment_on_issue, open_pr, report_blocked, report_complete]
  ```

### Proposed Direction
- **Merge PMTools and FrameworkTools** into a single `SquadronTools` class. Each
  tool is registered independently. Per-role tool set is defined in config.yaml.
- **Use `available_tools` / `excluded_tools`** in SessionConfig to restrict SDK
  built-in tools per role. Define these in the agent `.md` frontmatter or in
  config.yaml.
- **Kill `comment_on_issue` duplication** — single implementation, available to
  any role that lists it.

---

## 6 — Infrastructure & Deployment

### Intent
Deploy Squadron to a cloud container with one command. Infrastructure should be
minimal, reproducible, and match what the application actually needs. No unused
resources.

### Current State

#### Bicep (`infra/main.bicep`, 238 lines)
Provisions:
- Log Analytics workspace
- Container App Environment
- **Storage Account + Azure Files share + volume mount at `/data`**
- Container App with `--repo-root /data`, volume mounts, probes

#### Application (`server.py`)
- Clones repo to `/tmp/squadron-repo`
- SQLite DB at `/tmp/squadron-data/registry.db`
- Worktrees at `$SQUADRON_WORKTREE_DIR` (defaults to `/tmp/squadron-worktrees`)

#### Deploy Workflow (`squadron-deploy.yml`, 515 lines)
- `detect` job: diff HEAD~1 for code vs config changes
- `deploy` job: downloads Bicep from `main` branch, deploys via `az deployment group create`
- `sync-config` job: uploads `.squadron/` to Azure Files, restarts container
- `destroy` job: deletes resource group
- Post-deploy: deactivate old revisions, clean stale DB from Azure Files,
  sync config to Azure Files, restart container

### Gaps & Contradictions — CRITICAL
1. **Bicep still provisions Azure Files.** Storage account, file share, volume
   mount — all still present. This directly contradicts the decision to remove
   Azure Files (the cause of SQLite "database is locked" crashloop).
2. **Container args say `--repo-root /data`** (Azure Files mount) but the server
   clones to `/tmp/squadron-repo` and ignores `/data` entirely. The `--repo-root`
   flag is accepted but then overridden by the clone path.
3. **Deploy workflow still syncs config to Azure Files.** The `sync-config` job
   uploads `.squadron/` to Azure Files — but the container clones the repo and
   reads config from the clone. The Azure Files config is never read.
4. **`detect` job logic is fragile.** Uses `git diff HEAD~1` which fails on
   force-pushes, merge commits, and first commits. Also unclear why config-only
   changes need a separate path — a full deploy is fast enough.
5. **Workflow downloads Bicep from `main` branch via curl.** If you're deploying
   from a feature branch, you get the `main` Bicep template, not the one in your
   branch. This makes Bicep changes untestable before merge.
6. **`az` CLI used extensively in the workflow.** User rule: "Never use `az` CLI
   directly" — this was for interactive use, but the workflow is full of `az`
   commands that could be replaced with Bicep-native or `az containerapp` actions.

### Design Questions
- **Is Azure Files needed for ANYTHING?** If config comes from the git clone and
  DB is on local disk, Azure Files serves no purpose. Can we delete the storage
  account entirely?
- **Should `sync-config` exist?** If the container clones the repo at startup,
  config changes are picked up by restarting the container. Syncing config to a
  volume is unnecessary overhead.
- **Should `detect` be removed?** Every push to main could just do a full deploy.
  Container Apps handles zero-downtime revision swaps natively.
- **Where should Bicep come from?** The checked-out repo (matching the commit
  being deployed) seems correct, not a curl from `main`.

### Proposed Direction
- **Delete Azure Files from Bicep entirely.** Remove: `storageAccount`, `fileService`,
  `fileShare`, `envStorage`. Remove volume mounts from container. Remove `repoUrl`
  parameter. Change container args to `--repo-root /tmp/squadron-repo`.
- **Delete `sync-config` job and `detect` job.** Every push = full deploy. Simple.
- **Use checked-out Bicep**, not curl from main.
- **Clean up deploy workflow** to ~100 lines: checkout → login → deploy Bicep →
  health check. That's it.

---

## 7 — Data Model & Registry

### Intent
Track which agents exist, their status, what issue they're working on, and their
lifecycle state. This is the framework's memory.

### Current State
- `AgentRecord` Pydantic model with ~20 fields
- `AgentRegistry` wraps aiosqlite with CRUD operations
- SQLite DB at `/tmp/squadron-data/registry.db`
- Lost on container restart (ephemeral disk)

### Gaps & Contradictions
1. **DB is ephemeral but treated as persistent.** Recovery code tries to resume
   agents from DB, but if the container restarted, the DB was recreated empty.
   The recovery code is dead code in the container environment.
2. **No migration story.** Schema changes require destroying the DB. No `ALTER TABLE`
   or migration framework.
3. **`get_agent_by_issue()` vs `get_agents_for_issue()`** — two similar methods
   with different semantics (first-match vs all-matches). Used inconsistently.

### Design Questions
- **Is SQLite the right choice for ephemeral state?** If state doesn't survive
  restarts, would an in-memory dict suffice? SQLite adds complexity (async wrapper,
  schema init, connection pooling) for no durability benefit.
- **Should agent state be reconstructable from GitHub?** If we lose the DB, can
  we reconstruct agent state by scanning open issues/PRs with squadron labels?
  This would make the system truly stateless.

### Proposed Direction
- **Keep SQLite for now** — the async wrapper is already built and battle-tested.
  In-memory would lose state on *any* crash, not just container restarts.
  SQLite survives Python exceptions and `SIGTERM` graceful shutdown.
- **Add a "reconstruct from GitHub" startup path** — if DB is empty on startup,
  scan GitHub issues/PRs for squadron-managed agents and rebuild registry state.
  This makes the system resilient to DB loss.
- **Unify the duplicate-check methods** — single `get_agent(role, issue_number)`
  method used everywhere.

---

## 8 — Copilot SDK Integration

### Intent
The Copilot SDK is the LLM execution engine. Squadron provides the orchestration
layer — the SDK provides the AI. The integration should be clean, well-understood,
and leverage SDK features properly.

### Current State
- `copilot.py` (271 lines): Wraps `CopilotClient` and `CopilotSession`.
- One `CopilotClient` per agent (one CLI subprocess each).
- Sessions support create, resume, destroy, list.
- `build_session_config()` accepts tools, hooks, custom_agents, mcp_servers,
  skill_directories, available_tools, excluded_tools.
- `build_resume_config()` for sleep→wake (re-sends model, system message, tools).
- BYOK support via `_build_provider_dict()`.
- `infinite_sessions` with compaction enabled.

### Gaps & Contradictions
1. **`available_tools` / `excluded_tools` never used.** Both are accepted as
   parameters but never populated. This is the mechanism for tool restriction
   but it's dormant.
2. **`custom_agents` (subagents) behavior is opaque.** We build `CustomAgentConfig`
   dicts and pass them to the SDK, but there's no documentation of when/how the
   SDK invokes subagents. Are they automatically available? On-demand? This needs
   SDK research.
3. **`skill_directories` and `mcp_servers` accepted but rarely used.** The agent
   definition schema supports MCP servers but no current agents configure them.
4. **`reasoning_effort` included in `build_resume_config`** even when `None` —
   this may cause SDK errors for models that don't support it (the create path
   correctly guards with `if reasoning`).
5. **One CLI subprocess per agent.** If 10 agents are active, that's 10 node.js
   processes. What's the memory/CPU overhead? Is there a shared-client mode?

### Design Questions
- **What exactly does `available_tools` do in the SDK?** Does it restrict SDK
  built-in tools? Or only custom tools? This is critical for the tool architecture.
- **How do subagents work?** When a PM session is created with `custom_agents: [feat-dev, bug-fix]`,
  does the SDK let the PM "call" those agents? Or does it just make their
  definitions available for reference?
- **Is one CopilotClient per agent the right pattern?** Or can we share a client
  across agents and just create separate sessions?
- **What happens to sessions on process death?** Are they automatically persisted?
  Is there a session directory we should be aware of?

### Proposed Direction
- **Research `available_tools` / `excluded_tools` behavior** — if they restrict
  SDK built-ins, use them to enforce tool boundaries per role.
- **Research subagent invocation** — understand how the SDK handles `custom_agents`
  to determine if the `subagents` config field is meaningful or decorative.
- **Fix `reasoning_effort` in `build_resume_config`** — add the same `if reasoning`
  guard as `build_session_config`.
- **Benchmark CopilotClient overhead** — determine if aggressive client reuse
  is needed.

---

## 9 — Testing & Quality

### Current State
- 280 tests across 20 files.
- Unit tests mock the SDK, registry, and GitHub client.
- E2E tests use real config loading and full component wiring.
- All tests pass post-refactor.

### Gaps
1. **No integration test that actually calls the SDK.** All LLM interactions are
   mocked. There's no test that verifies an agent can actually create a session,
   send a prompt, and get a response.
2. **No test for the deploy workflow.** Bicep template is never validated.
3. **No test for container startup.** The clone→config→DB→start sequence is
   untested outside of manual deployment.
4. **Config trigger matching has sparse coverage.** The new `_handle_config_trigger`
   logic handles label matching, bot filtering, duplicate guards — each branch
   needs explicit tests.

### Proposed Direction
- **Add a "smoke test" that creates a real SDK session** (can use a mock model
  or cheap API). Verifies the full create→send→wait→destroy cycle.
- **Add Bicep validation** (`az bicep build --file main.bicep`) as a CI step.
- **Expand trigger matching tests** — label match, bot filter, duplicate guard,
  stateless multi-spawn.

---

## 10 — Execution Plan

Priority order based on impact and dependency:

### Phase 1: Infrastructure Cleanup (HIGH PRIORITY)
> Fixes production contradictions. No architectural decisions needed — just delete
> dead code to match the actual runtime model.

1. **Remove Azure Files from Bicep** — delete storage account, file share, env
   storage, volume mounts. Change container args to `--repo-root /tmp/squadron-repo`.
2. **Simplify deploy workflow** — delete `detect` job, `sync-config` job, Azure
   Files cleanup steps. Use checked-out Bicep, not curl. Target ~100-150 lines.
3. **Deploy and verify** — full deploy, health check, open a test issue.

### Phase 2: Configuration Consolidation
> Clean up config schema to match "YAML is truth" principle.

4. **Delete `assignable_labels`** from Pydantic models.
5. **Unify `trigger` and `triggers`** — remove the scalar `trigger: approval_flow`
   field, integrate approval flow into the `triggers[]` array.
6. **Audit `infer` frontmatter** — remove if unused.
7. **Remove dead recovery code** — if DB is ephemeral, recovery should destroy
   orphaned agents, not mark them SLEEPING.

### Phase 3: Tool Architecture Refactor
> Make tool boundaries real, not prompt-based.

8. **Research SDK `available_tools` / `excluded_tools`** behavior.
9. **Merge PMTools + FrameworkTools** into `SquadronTools`.
10. **Define per-role tool sets in config.yaml** — replace the stateless→PMTools
    bifurcation with explicit config.
11. **Wire `available_tools` / `excluded_tools`** for SDK built-in tool restriction.

### Phase 4: Lifecycle Hardening
> Make the agent lifecycle predictable and recoverable.

12. **Unify duplicate guards** — single check, single call site.
13. **Consolidate `turn_count` / `iteration_count`**.
14. **Add WIP commit before sleep** — makes worktree loss recoverable.
15. **Fix semaphore leak** on error paths.
16. **Fix `reasoning_effort` in `build_resume_config`**.

### Phase 5: Advanced (lower priority)
17. **GitHub-based state reconstruction** — rebuild registry from GitHub on empty DB.
18. **Subagent behavior research** — understand SDK `custom_agents` semantics.
19. **Bicep validation in CI** — `az bicep build` step.
20. **SDK integration smoke test** — real session create/send/destroy cycle.

---

## Appendix: Known Contradictions (Quick Reference)

| # | Contradiction | Files | Severity |
|---|---------------|-------|----------|
| 1 | Bicep provisions Azure Files; app uses `/tmp` | `main.bicep`, `server.py` | **CRITICAL** |
| 2 | Container arg `--repo-root /data`; server clones to `/tmp/squadron-repo` | `main.bicep`, `server.py` | **CRITICAL** |
| 3 | Deploy syncs config to Azure Files; container reads from git clone | `squadron-deploy.yml`, `server.py` | **HIGH** |
| 4 | Recovery marks orphans SLEEPING; no session data to resume | `server.py`, `agent_manager.py` | **MEDIUM** |
| 5 | Agent `.md` tools frontmatter not enforced | `agents/*.md`, `agent_manager.py` | **MEDIUM** |
| 6 | `comment_on_issue` duplicated in PMTools and FrameworkTools | `pm_tools.py`, `framework.py` | **LOW** |
| 7 | `assignable_labels` deprecated but in schema | `config.py` | **LOW** |
| 8 | `trigger` (scalar) vs `triggers` (array) coexist | `config.yaml`, `config.py` | **LOW** |
| 9 | `reasoning_effort` unconditionally set in `build_resume_config` | `copilot.py` | **LOW** |
| 10 | Dual duplicate guards in trigger handler and create_agent | `agent_manager.py` | **LOW** |
