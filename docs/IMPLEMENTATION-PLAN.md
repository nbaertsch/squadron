# Squadron — Design Review & Implementation Plan

> **Purpose**: Requirements-first design review grounded in user stories.
> What should Squadron *do* for the people who use it? Does the current
> architecture serve those stories? Where does it fall short?
>
> Existing MVP code is a reference, not a constraint — rewrite, discard, or
> invert any feature as needed.

---

## Part 1: User Stories & Design Challenges

### Personas

| Persona | Description |
|---|---|
| **Repo Owner (Operator)** | Sets up Squadron on their repo. Configures agent roles, approval flows, and policies. Wants autonomous dev work with human guardrails. |
| **Human Developer** | Works alongside agents on the same GitHub repo. Creates issues, reviews PRs, resolves escalations. Shouldn't need to learn a new tool — GitHub is the interface. |
| **Maintainer / Approver** | Reviews agent-generated PRs, approves merges, handles `needs-human` escalations. Needs confidence that agent work meets quality and security standards. |
| **Agent (PM)** | Ephemeral coordinator. Triages incoming issues, routes work to dev agents, handles escalations. Stateless — GitHub is its memory. |
| **Agent (Dev)** | Persistent worker. Implements features or fixes across multiple turns. Sleeps when blocked, wakes on events. Creates branches, writes code, opens PRs. |
| **Agent (Reviewer)** | Ephemeral reviewer. Spawned by approval flows to review PRs. Approves, requests changes, or escalates. |

---

### User Stories

#### US-1: First-Time Setup (Operator)

> *"I want to drop a `.squadron/` directory into my repo, deploy a container,
> and have agents start responding to issues — in under 30 minutes."*

**What this requires:**
- A self-contained config format that doesn't need external services beyond GitHub App registration
- A deploy pipeline that works with minimal manual steps (ideally: register GitHub App → push config → deploy container → done)
- Sensible defaults: a working PM + feat-dev + pr-review setup out of the box, without writing 400 lines of YAML
- Fail-fast validation: if config is wrong, the server tells you immediately on startup, not silently when the first issue arrives

**Design challenge — Config complexity vs. power:**
The current config schema (config-schema.md) defines ~15 top-level sections, 5+ agent definitions, approval flows, circuit breakers, model overrides, provider config, label taxonomies, branch naming, human groups, escalation settings... This is powerful for a production deployment, but it's a wall of YAML for someone trying Squadron for the first time.

**Decision: Ship an example `.squadron/` directory, not a CLI scaffolding tool.**
The repository includes a complete, minimal, working example config that operators copy into their repo and edit. No `squadron init` command — just a well-documented starter template.

**Minimum viable `.squadron/` directory (two required fields, everything else has defaults):**
```
.squadron/
├── config.yaml          # project.name + human_groups.maintainers (required)
├── agents/
│   ├── pm.md            # PM agent definition (shipped in example)
│   ├── feat-dev.md      # Dev agent definition (shipped in example)
│   └── pr-review.md     # Review agent definition (shipped in example)
```

Required fields in `config.yaml`:
- `project.name` — identifies the repo
- `human_groups.maintainers` — at least one human to escalate to

Everything else (`agent_roles`, `approval_flows`, `circuit_breakers`, `model_config`, `labels`, `branch_naming`) has Pydantic defaults that produce a working PM + feat-dev + pr-review system. Operators add complexity incrementally as they need it.

---

#### US-2: Issue → Working Code (End-to-End Happy Path)

> *"I create a GitHub issue describing a feature. Within minutes, an agent picks
> it up, implements it, opens a PR with tests, the PR gets reviewed by another
> agent, and I can merge it."*

**What this requires (the "core loop"):**
1. Webhook → event router → trigger system evaluates all matching triggers
2. Matching trigger spawns agent(s) directly (async/parallel) or initiates a workflow (sync/sequential)
3. For a typical feature issue: PM trigger fires → PM triages → applies label → label event fires a dev agent trigger
4. Dev agent: creates branch → reads codebase → implements → writes tests → runs tests → opens PR
5. PR opened → review agent triggers fire (filtered by approval flow conditions) → review agents spawn in parallel
6. Review agent: reads diff → posts review → approves (or requests changes)
7. If changes requested: dev agent wakes, addresses feedback, pushes, re-triggers review
8. All approvals met + CI passes → ready to merge (human or auto, per config)

**Decision — Unified trigger system as the single spawn mechanism:**
The trigger system is the universal entry point for all agent activity. Every agent spawn — dev, review, PM, custom role — goes through the same trigger→spawn pipeline. No hardcoded spawn paths. Two spawn modes, both defined in YAML:

1. **Direct spawn (async):** A trigger fires and spawns one or more agents in parallel. This is the default. Multiple roles can have triggers on the same event — all matching triggers fire.
   ```yaml
   agent_roles:
     pm:
       triggers:
         - event: "issues.opened"     # PM spawns on every new issue
     feat-dev:
       triggers:
         - event: "issues.labeled"
           label: "feature"           # dev agent spawns on label application
     pr-review:
       triggers:
         - event: "pull_request.opened"
           condition:
             approval_flow_required: true  # only if approval flow selects this role
     security-review:
       triggers:
         - event: "pull_request.opened"
           condition:
             approval_flow_required: true
   ```

2. **Workflow initiation (sync):** A trigger initiates a named workflow, which runs stages sequentially. Each stage spawns an agent, waits for completion/approval, then advances.
   ```yaml
   workflows:
     release-pipeline:
       triggers:
         - event: "push"
           branch: "main"
       stages:
         - role: security-review
           on_approve: next
         - role: deploy-agent
           on_approve: complete
   ```

The approval flow config defines *which* roles review *which* branches. Triggers with `approval_flow_required: true` check this config to decide whether to fire. The approval flow is a filtering condition on triggers, not a separate spawn system.

**Decision — PM is an ephemeral classifier with rich context injection:**
The PM's two jobs (mechanical routing + intelligent coordination) are both served by an ephemeral model with aggressive context injection. Before each PM session, the framework injects the current registry state, recent triage history, and pending escalations. The PM doesn't need persistent memory — the registry and GitHub state *are* its memory. This is cheaper, simpler, and avoids the always-on session cost.

If PM quality proves insufficient in practice (poor coordination, missed context), the fallback is to make the PM persistent with periodic checkpoints. But the ephemeral model is the starting design.

**Decision — PR lifecycle: sleep/wake on review.**
When a dev agent opens a PR, it sleeps (commits + pushes, enters SLEEPING state). When a reviewer requests changes, the dev agent wakes with full context injection (the review comments, current diff state, test results). This avoids wasting compute while the PR is in review. The wake trigger is `pull_request_review.submitted` with `action: "changes_requested"`, which fires a wake event for the sleeping dev agent associated with that PR.

---

#### US-3: Agent Gets Stuck → Human Resolves → Agent Continues

> *"A dev agent hits a merge conflict it can't resolve. It escalates. I get
> notified via a `needs-human` issue. I fix the conflict, close the escalation
> issue, and the dev agent automatically picks back up where it left off."*

**What this requires:**
1. Dev agent detects it's stuck → calls `escalate_to_human` or `create_blocker_issue`
2. Framework creates a `needs-human` issue referencing the original
3. PM (or framework) assigns it to the right human group
4. Human resolves and closes the escalation issue
5. Framework detects closure → finds sleeping agents blocked by this issue → wakes them
6. Dev agent rehydrates, assesses changed branch state (wake protocol), continues

**Design challenge — What survives a restart?**
The agent's session data (Copilot SDK state), worktree (uncommitted code), and registry record (SQLite) are all on ephemeral container disk. If the container restarts between steps 2 and 5 — which could be hours or days — everything is lost. The agent can't resume because there's no session to resume from.

**Decision: Rebuild from GitHub (no persistent storage).**
GitHub is the durable layer. On container restart with an empty registry:
1. Scan GitHub Issues API for open issues with squadron-managed labels
2. Scan open PRs created by `squadron[bot]`
3. Reconstruct `AgentRecord` for each from issue metadata (role from labels, status from labels, branch from naming convention)
4. For in-progress agents: create a fresh session with full context injection ("you were working on issue #38, here's the current state of the branch and issue")
5. For sleeping/blocked agents: mark SLEEPING and let the reconciliation loop handle wake conditions

**Decision: Agents must commit + push before sleeping.**
A pre-sleep hook runs `git add -A && git commit -m "[squadron-wip]" && git push`. This makes branch state recoverable from GitHub after any restart. Uncommitted work is the only data that's truly lost on restart — this rule eliminates that risk.

Container restarts become a temporary interruption, not a catastrophic loss. Active agents lose their in-progress reasoning but re-orient from GitHub state. Sleeping agents lose nothing — they weren't running.

---

#### US-4: Operator Customizes Agent Behavior (New Role, Modified Policy)

> *"I want to add a 'docs-writer' agent that triggers on `docs` labels,
> has access to `write_file` but not `bash`, and uses a cheaper model.
> I should be able to do this with YAML + a markdown prompt file, no Python."*

**What this requires:**
- New role defined in `config.yaml` under `agent_roles`
- New agent definition file at `.squadron/agents/docs-writer.md`
- Tool assignment for this role (which Squadron tools + which SDK tools)
- Model/reasoning override for cost control
- Trigger conditions (which events spawn it)
- The framework must pick all of this up without code changes

**Design challenge — Tool boundaries are not enforced:**
The current architecture has two layers of tools:
1. **Squadron tools** (custom): `create_issue`, `open_pr`, `report_blocked`, etc. Currently split into PMTools and FrameworkTools, assigned by `stateless` flag, not by config.
2. **SDK built-in tools**: `read_file`, `write_file`, `bash`, `grep`, etc. Always available to every agent. No restriction mechanism is wired up.

A PM agent that's told "you do NOT write code" in its prompt still has `bash` and `write_file` available through the SDK. Prompt-based restriction is a suggestion, not enforcement. An operator who configures a `docs-writer` role with `excluded_sdk_tools: [bash]` expects that to be enforced — but this feature exists in the SDK interface (`available_tools`/`excluded_tools` params) without ever being wired up.

The story also exposes that "no Python changes" requires *every* behavioral axis to be config-controllable: tools, triggers, lifecycle, model, circuit breaker overrides. Today, some of those are config-driven and some aren't.

---

#### US-5: Multiple Agents Working Concurrently

> *"I have 3 open feature issues and 2 bug issues. I want agents working on all
> of them in parallel, each on its own branch, without stepping on each other."*

**What this requires:**
- Concurrent agent spawning (PM triages issues sequentially, but dev agents run in parallel)
- Branch-per-issue isolation (already designed: `feat/issue-{N}`)
- Concurrency controls: semaphore limits so a small container doesn't OOM from 10 simultaneous agents
- Resource awareness: each agent = 1 CopilotClient CLI subprocess (Node.js process). With 5 agents, that's 5+ Node processes + the Python server. What's the memory ceiling?
- Rate limit management: 5 agents sharing 5,000 API calls/hour

**Design challenge — Resource model is untested:**
The architecture assumes agents are cheap to run concurrently, but nobody has measured it. Each `CopilotClient` spawns a CLI binary subprocess. The memory and CPU overhead per agent is unknown. This directly affects whether the "5 agents on a small container" story is feasible or whether we need to queue agents.

**Decision — Sequential PM is acceptable; registry queries provide memory:**
AD-008 says PM processes issues one at a time. If 5 issues arrive in rapid succession, they queue. The PM triages #1, spawns a dev agent, then moves to #2, etc. This is fine — triage is fast (seconds, not minutes), and the PM's per-event freshness is a feature (no accumulated context drift).

For cross-issue awareness (e.g., issue #3 references issue #1): the PM's registry query tool (`check_registry`) gives it visibility into what's already being worked on. This is a tool call, not inherent knowledge — but the PM's agent definition instructs it to always check the registry before triaging. The framework injects the current registry snapshot into every PM session as part of context injection, so this information is available without a tool call too.

---

#### US-6: PR Review with Teeth (Maintainer Confidence)

> *"When an agent opens a PR to main, I need a security review + code review
> before I'll merge it. I want to see the review comments, understand the
> agent's reasoning, and have confidence the review was thorough."*

**What this requires:**
- Approval flows that enforce multi-stage review (security + code review)
- Review agents that post substantive, readable PR review comments (not just "LGTM")
- GitHub status checks that branch protection can require (`squadron/pr-review: approved`)
- Human can see the full trail: which agents reviewed, what they found, whether they approved
- Human retains the merge button (configurable: `auto_merge: false`)

**Design challenge — Agent review quality is an LLM problem, not an architecture problem:**
The framework can enforce *that* a review happens, but not *how good* it is. A review agent might rubber-stamp everything, or nitpick irrelevantly, or miss critical security issues. This isn't a bug in the architecture — it's a function of prompt engineering, model capability, and the agent definition.

However, the architecture can *support* quality by:
- Giving review agents access to the full diff, test results, and CI status
- Supporting multi-model configs (use a stronger model for security review)
- Allowing multiple independent review agents (they can disagree — disagreement is signal)
- Making it easy for humans to reject and re-trigger reviews

**Design challenge — Approval flows and workflows are separate systems that overlap:**
1. `config.yaml` → `approval_flows` section → `ApprovalFlowConfig` → consumed by trigger system
2. `.squadron/workflows/` → `WorkflowDefinition` → consumed by `workflow_engine.py`

**Decision: These are two distinct concepts, not a unification problem.**
- **Approval flows** define *which roles review which branches* — a declarative policy layer. They are a condition on triggers (`approval_flow_required: true`). They answer: "does this PR need a security review?" The trigger system evaluates this condition and spawns review agents in parallel (async).
- **Workflows** define *sequential multi-stage pipelines* — a procedural orchestration layer. They are triggered independently and run stages one-at-a-time (sync). They answer: "what sequence of operations does this event require?"

Both go through the trigger system. Both are defined in YAML. But they serve different purposes and don't need to be merged into one abstraction. Approval flows are parallel fan-out. Workflows are sequential pipelines.

---

#### US-7: Cost Control / Runaway Prevention (Operator)

> *"I don't want to wake up to a $500 bill because an agent got stuck in a
> retry loop overnight. I need circuit breakers and I need to trust them."*

**What this requires:**
- Per-agent limits: max iterations, max tool calls, max turns, max wall-clock time
- Per-role overrides (PM gets 10 min, dev gets 2 hours)
- Escalation on trip: agent stops, creates `needs-human` issue, enters ESCALATED state
- Visibility: operator can see current agent resource consumption (or at least post-mortem)

**Design challenge — Circuit breakers must be framework-enforced, not agent-reported:**
AD-018 specifies limits and says they're enforced via `on_pre_tool_use` SDK hook. But:
- The SDK hook may not fire for built-in tools — this needs research.
- An agent in a long `bash` call won't trigger the hook until after.
- Pure reasoning time burns tokens without any tool call.

**Decision: Background timer is the primary enforcement mechanism.** The agent manager runs an `asyncio` timer per agent. When `max_active_duration` is exceeded, the framework cancels the agent's task — regardless of what the agent is doing. This is the hard kill. The `on_pre_tool_use` hook (if it works for built-ins) is defense-in-depth for per-tool-call checks like `max_tool_calls`.

**Global budget is explicitly deferred to V2.** Per-agent circuit breakers are the V1 scope. Operators can control total cost by limiting `max_concurrent_agents` and setting per-role duration caps.

---

#### US-8: Human Takes Over Mid-Flight

> *"I see an agent is going down the wrong path on issue #38. I want to
> take over — reassign the issue to myself, and the agent should stop."*

**What this requires:**
- Agent detects reassignment (via `check_for_events` tool polling its inbox)
- Agent stops work, comments on the issue noting the handoff, enters COMPLETED or CANCELLED
- Agent's branch is preserved for human to pick up or discard
- Framework doesn't spawn a new agent for this issue (human is now assigned)

**Decision — Framework-level abort on reassignment:**
The agent doesn't need to detect reassignment itself. The agent manager receives the reassignment event via the event router and cancels the agent's `asyncio.Task` directly. The framework posts a comment noting the handoff, preserves the branch, and destroys the session. Immediate, not polling-dependent.

For SLEEPING agents: update the registry, destroy the session, remove from wake conditions. No task to cancel.

The `check_for_events` tool remains as a soft check for other event types (new comments, label changes), but reassignment is handled by the framework — the agent never needs to cooperate.

---

#### US-9: Transparent Audit Trail (Maintainer)

> *"I want to look at any issue or PR and understand exactly what happened —
> which agent worked on it, what decisions it made, why it escalated. All
> visible in GitHub without any external dashboard."*

**What this requires:**
- Every agent action is visible as a GitHub comment, commit, label change, or status check
- Role tags in comments: `[squadron:pm]`, `[squadron:feat-dev]`, `[squadron:security-review]`
- Agent reasoning visible in PR review comments (not just approve/reject)
- Escalation chain traceable through issue cross-references

**Design status:** This is well-designed in the project plan (AD-001, AD-002). The "GitHub is the orchestration layer" thesis means the audit trail is a natural byproduct. No major design gap here — it's more about execution quality (making agent comments clear and informative).

---

#### US-10: Config Change Without Redeploy (Operator)

> *"I pushed a change to `.squadron/config.yaml` on main. The running container
> should pick it up without me redeploying."*

**What this requires:**
- Server watches for push events on the default branch that modify `.squadron/**`
- On detected change: re-read config, validate, and hot-swap if valid
- If validation fails: keep old config, post a warning (issue comment or log)
- In-flight agents finish under the config they started with. New agents get new config.

**Decision:** Live reload via push event detection + `git pull` + config re-validation. If validation fails, keep old config and log the error. In-flight agents are not affected — they run to completion under their original config. Workflow versioning is deferred to V2.

---

## Part 2: Architecture Evaluation Against Stories

For each design challenge surfaced above, what's the current state and what needs to change?

### DC-1: Config Complexity vs. First-Time Setup (US-1)

**Problem:** The config schema is comprehensive but daunting. A first-time user
needs to write `config.yaml`, `approval-flows.yaml`, and 3-5 agent `.md` files
before anything works.

**Decision:**
- Ship an **example `.squadron/` directory** in the repository that operators copy into their repo. No `squadron init` CLI command — just a well-documented template.
- **Two required fields**: `project.name` and `human_groups.maintainers`. Everything else has Pydantic defaults that produce a working PM + feat-dev + pr-review system.
- **Validation with helpful errors**: on startup, if a required field is missing, tell the operator exactly what to add and where.
- **Layered defaults**: operators add complexity incrementally. The example config is ~30 lines. The full schema supports ~200+ lines for production deployments.

**Priority:** High — this is the acquisition funnel. If setup is hard, nobody tries it.

---

### DC-2: Unified Event→Agent Spawning (US-2, US-6)

**Problem:** Dev agents and review agents are spawned by different mechanisms:
- Dev agents: config triggers (event + label match → spawn)
- Review agents: hardcoded `_handle_pr_opened()` → approval flow lookup → spawn

This violates "YAML is truth" and makes the system harder to reason about.

**Decision: Triggers are the universal spawn mechanism. Two modes, one pipeline.**

All agent spawns go through the trigger system. The `_handle_pr_opened()` hardcoded path is eliminated. Approval flow lookups become a condition layer on triggers.

**Mode 1 — Direct agent spawn (async/parallel):**
Each role defines its triggers. When an event matches, the role's agent spawns. Multiple roles can match the same event — all fire in parallel.
```yaml
agent_roles:
  pr-review:
    triggers:
      - event: "pull_request.opened"
        condition:
          approval_flow_required: true   # spawn only if approval flow selects this role
  security-review:
    triggers:
      - event: "pull_request.opened"
        condition:
          approval_flow_required: true
```
The trigger system evaluates `approval_flow_required` by checking the `approval_flows` config for the target branch. If the branch rule says "require security-review", the security-review trigger fires. If not, it doesn't. One spawn pipeline, one evaluation path.

**Mode 2 — Workflow initiation (sync/sequential):**
A trigger can initiate a named workflow instead of spawning a single agent. The workflow runs stages sequentially. This is defined in `workflows/` YAML files with their own `triggers` section.
```yaml
workflows:
  release-pipeline:
    triggers:
      - event: "push"
        branch: "main"
    stages:
      - role: security-review
        on_approve: next
      - role: deploy-agent
        on_approve: complete
```

**Approval flows vs. workflows are distinct concepts:**
- Approval flows = parallel fan-out ("spawn these N reviewers simultaneously")
- Workflows = sequential pipeline ("do A, then B, then C")
- Both go through the trigger system. Both are YAML-defined. They don't merge.

**Priority:** High — this is architectural debt that grows with every new feature.

---

### DC-3: Agent Persistence Across Container Restarts (US-3)

**Problem:** Agents can sleep for hours/days waiting for humans. Container restarts
destroy all agent state (registry, sessions, worktrees).

**Decision: Rebuild from GitHub. No persistent storage.**

On startup with an empty registry:
1. Query GitHub Issues API for open issues with squadron-managed labels (`in-progress`, `blocked`, `needs-human`)
2. Query GitHub PRs for open PRs opened by `squadron[bot]`
3. For each, reconstruct an `AgentRecord` from issue metadata:
   - Role: inferred from labels (e.g., `feature` label → `feat-dev` role)
   - Status: inferred from labels (`blocked` → SLEEPING, `in-progress` → needs re-activation)
   - Branch: inferred from branch naming convention (`feat/issue-{N}`)
   - `blocked_by`: inferred from issue cross-references
4. For issues that were `in-progress`: create a new agent session with a context-injection prompt: "You were previously working on this issue. Here's the current state of the branch, the issue, and any PR." The agent starts fresh but with full external context.
5. For issues that were `blocked`: mark SLEEPING, let reconciliation loop check if blockers are resolved.

**Decision: Agents must push branches before sleeping.**
Pre-sleep hook: `git add -A && git commit -m "[squadron-wip]" && git push`.
This makes branch state recoverable. Uncommitted work is the only truly lost data — this rule eliminates that risk.

**Decision: Stale agents are FAILED, not SLEEPING.**
If the registry has an ACTIVE agent but no running session (detected on startup or by reconciliation), mark it FAILED and escalate — don't silently transition to SLEEPING, which implies a clean sleep.

**Priority:** High — without this, the sleep/wake lifecycle is fragile theater.

---

### DC-4: Tool Boundary Enforcement (US-4)

**Problem:** Tool assignment is hardcoded (`stateless` → PMTools, else FrameworkTools)
and SDK built-in tools are unrestricted. Operators can't control what tools a
role gets without editing Python.

**Decision: Three layers of tool control, all config-driven.**

1. **Squadron tools** (custom tools registered via `@define_tool`):
   Per-role tool list in `config.yaml`. Framework passes only the listed tools
   to the session.
   ```yaml
   agent_roles:
     pm:
       tools: [create_issue, label_issue, assign_issue, comment_on_issue, read_issue, check_registry]
     feat-dev:
       tools: [comment_on_issue, open_pr, report_blocked, report_complete, check_for_events, escalate_to_human]
   ```

2. **SDK built-in tools** (read_file, bash, grep, etc.):
   Per-role restriction via `available_tools` or `excluded_tools` in Copilot SDK
   SessionConfig. Whether this actually works needs SDK research.
   ```yaml
   agent_roles:
     pm:
       excluded_sdk_tools: [bash, write_file, edit_file]
   ```

3. **Shell command restrictions** (what `bash` can run):
   Per-role allowlist/denylist in the agent definition `.md` file, enforced via
   `on_pre_tool_use` hook.
   ```markdown
   ## Tool Restrictions
   ### Allowed: git *, pytest *, npm test
   ### Denied: curl, wget, rm -rf /, docker *
   ```

**Dependency:** Layer 2 requires SDK research to confirm `available_tools` /
`excluded_tools` behavior. Layers 1 and 3 can be implemented without it.

**Priority:** High — this is a trust/safety requirement. If the operator says
"PM can't write files" it must be enforced, not suggested.

---

### DC-5: PM Role Design (US-2, US-5)

**Problem:** The PM is simultaneously:
- A classifier (triage issue → apply label) — mechanical, stateless, fast
- A coordinator (manage blockers, handle escalations, route work) — requires project awareness

These jobs have conflicting lifecycle needs. The classifier is fine as an
ephemeral fresh-session-per-event agent. The coordinator needs awareness of
project state that builds over time.

**Decision: Ephemeral PM with aggressive context injection.**
Before each PM session, the framework injects:
- Current registry state (all active agents, their statuses, issue numbers)
- Recent issue activity summary (last N issues triaged by PM)
- Pending escalations
- Config-defined workflow hints (decision trees)

This gives an ephemeral PM enough working memory to coordinate without needing
persistent context. The registry is the PM's external brain.

If this proves insufficient (PM makes poor coordination decisions due to lack of
project-level context), the fallback is a persistent PM with periodic checkpoints.
But the ephemeral model is the V1 design — simpler, cheaper, and testable.

**Priority:** Medium — current design is workable. Revisit only if PM quality is
demonstrably poor in integration testing.

---

### DC-6: Resource Model / Concurrency Limits (US-5)

**Problem:** Each agent spawns a CopilotClient CLI subprocess. Nobody has
measured the overhead. Concurrency limits are configured but the actual resource
ceiling is unknown.

**Decision:**
- **Benchmark before tuning.** Create 1, 3, 5 concurrent agents. Measure memory,
  CPU, latency per agent. This determines whether the current architecture
  supports 5 concurrent agents on a 2-core / 4GB container or if queuing is needed.
- **Conservative defaults.** Set `max_concurrent_agents` to 3 until benchmarks
  say otherwise. Better to queue than to OOM.
- **Agent priority queue.** If queuing is needed: PM always runs first (coordinator).
  Review agents have medium priority (they unblock PRs). Dev agents queue FIFO.

**Priority:** Medium — affects production viability but not the core design.

---

### DC-7: Human Override Detection Latency (US-8)

**Problem:** An agent only notices reassignment when it calls `check_for_events`.
If it's mid-implementation, it might not check for 20+ minutes.

**Decision: Framework-level task cancellation on reassignment.**
- The agent manager receives reassignment events via the event router. On
  reassignment, it cancels the agent's `asyncio.Task`. The agent's session is
  destroyed, and the framework posts a comment: "Agent stopped — issue
  reassigned to {new_assignee}."
- For ACTIVE agents: cancel the task (agent loses in-progress reasoning but
  the branch is preserved for the human to pick up).
- For SLEEPING agents: update the registry, destroy the session, remove from
  wake conditions.
- Agent definitions still instruct agents to "check for events periodically"
  as a soft check for non-reassignment events — but the framework-level abort
  is the hard enforcement for takeover.

**Priority:** Medium — affects human UX but agents working on reassigned issues
aren't dangerous, just wasteful.

---

### DC-8: Circuit Breaker Enforcement Depth (US-7)

**Problem:** Circuit breakers are designed (AD-018) but the enforcement mechanism
needs validation. Does `on_pre_tool_use` fire for SDK built-in tools? What about
pure reasoning time with no tool calls?

**Decision:**
- **Background timer is the primary enforcement.** The agent manager runs a
  per-agent `asyncio` timer. When `max_active_duration` is exceeded, the
  framework cancels the agent task and escalates. This works regardless of
  whether the agent is in a tool call, reasoning, or stuck.
- **`on_pre_tool_use` is defense-in-depth.** If SDK research confirms it fires
  for built-in tools, use it for per-tool-call checks (`max_tool_calls`,
  `max_iterations`). If not, the background timer is sufficient.
- **Token/cost tracking.** If the SDK exposes token usage per turn, track it.
  If not, tool call count is the proxy metric (already designed).

**Priority:** High for the background timer (safety net). Medium for the SDK
research (confirms defense-in-depth coverage).

---

## Part 3: Incremental Implementation Plan

Each design challenge becomes a focused work item. Ordered by dependency and
impact, not by code proximity.

### Phase 1: Foundation — Make the Core Loop Work End-to-End

**Goal:** US-2 (Issue → Working Code) works reliably for a single issue.
This is the V1 MVP happy path. Everything else builds on it.

**Work items:**

| # | Item | Addresses | Notes |
|---|---|---|---|
| 1.1 | **Example `.squadron/` config** — ship a minimal, documented starter config (PM + feat-dev + pr-review) in the repo that operators copy into their project | DC-1, US-1 | Two required fields (`project.name`, `human_groups.maintainers`), everything else has defaults |
| 1.2 | **Validate the core loop end-to-end** — manually create issue, verify PM triage → dev agent spawn → implementation → PR → review → merge-ready | US-2 | This is the integration test. May expose bugs, prompt quality issues, and timing problems that unit tests miss. |
| 1.3 | **Fix known runtime bugs** — `role.value` crash, `reasoning_effort` in resume config, `SQUADRON_REPO_URL` in Bicep | — | Minimum viable correctness |
| 1.4 | **Background duration timer** — framework kills agents that exceed `max_active_duration`, independent of tool hooks | DC-8, US-7 | Safety net. Don't trust agent self-reporting for cost control. |

**Exit criteria:** A human creates an issue. A PM agent triages it. A dev agent
implements it and opens a PR. A review agent reviews the PR. The human can merge.
This works without crashing.

---

### Phase 2: Config-Driven Everything

**Goal:** US-4 (new agent role via YAML + markdown only). "YAML is truth" becomes real.

**Work items:**

| # | Item | Addresses | Notes |
|---|---|---|---|
| 2.1 | **Unified tool registry** — merge PMTools + FrameworkTools into a single `SquadronTools` class with per-tool registration | DC-4 | Each tool is a standalone function. `get_tools(names: list[str])` returns the requested subset. |
| 2.2 | **Config-driven tool assignment** — `tools: [list]` on `AgentRoleConfig` replaces the `stateless→PMTools` bifurcation | DC-4, US-4 | Operator defines exactly which Squadron tools each role gets |
| 2.3 | **Unified trigger system** — all agent spawns go through a single trigger→spawn pipeline. Triggers can spawn agents directly (async/parallel) or initiate workflows (sync/sequential). Kill `_handle_pr_opened()` hardcoded path. Approval flows become a condition layer on triggers. | DC-2, US-6 | This is the centerpiece. Two spawn modes, one evaluation pipeline, all YAML-defined. |
| 2.4 | **SDK tool restriction research** — test `available_tools`/`excluded_tools` with real sessions | DC-4 | Determines whether SDK built-in tool enforcement is possible. Gates 2.5. |
| 2.5 | **SDK built-in tool restriction** (if 2.4 confirms it works) — `excluded_sdk_tools: [bash, write_file]` in config, wired through session creation | DC-4 | Real enforcement, not prompt-based suggestion |
| 2.6 | **Lifecycle semantic rename** — `stateless: true/false` → `lifecycle: ephemeral | persistent` | — | Clarity. Allows future lifecycle modes. |
| 2.7 | **PR sleep/wake lifecycle** — dev agent sleeps after opening PR (commit + push), wakes on `pull_request_review.submitted` with changes requested. Framework handles the wake trigger. | US-2, DC-3 | Core dev↔review loop. Requires commit-before-sleep hook (2.8). |
| 2.8 | **PM context injection** — before each PM session, inject registry state, recent activity, pending escalations | DC-5, US-5 | Makes ephemeral PM effective as coordinator. Moved from Phase 4 — needed for the core loop to work well. |

**Exit criteria:** Operator adds a `docs-writer` role with custom tools and model
override by editing only YAML + markdown. Framework picks it up after config reload.

---

### Phase 3: Persistence & Resilience

**Goal:** US-3 (agent survives container restart). The system is self-healing.

**Work items:**

| # | Item | Addresses | Notes |
|---|---|---|---|
| 3.1 | **WIP commit + push before sleep** — pre-sleep hook: `git add -A && git commit -m "[squadron-wip]" && git push` | DC-3 | Makes branch state recoverable after container restart |
| 3.2 | **GitHub-based state reconstruction** — on startup with empty registry, scan GitHub for squadron-managed issues/PRs and rebuild AgentRecords | DC-3, US-3 | The "rebuild from GitHub" strategy. Agent gets a fresh session with full context injection. |
| 3.3 | **Recovery semantics fix** — stale agents (ACTIVE in registry but no running session) are marked FAILED, not SLEEPING | DC-3 | Current behavior creates zombie SLEEPING agents with no session data |
| 3.4 | **Reconciliation loop hardening** — verify it catches: missed webhooks, stale agents, resolved blockers, max sleep duration | US-3 | Safety net for the sleep/wake lifecycle |
| 3.5 | **Human override abort** — framework cancels agent task on issue reassignment, posts comment, preserves branch | DC-7, US-8 | Hard enforcement, not tool-polling |

**Exit criteria:** Deploy. Open 3 issues. Let agents work on them. Kill the
container. Restart. The system reconstructs state from GitHub and agents
resume (with fresh sessions but correct context).

---

### Phase 4: Operational Maturity

**Goal:** US-7 (cost control), US-9 (audit trail), US-10 (config reload). The system
is safe and transparent enough for production use.

**Work items:**

| # | Item | Addresses | Notes |
|---|---|---|---|
| 4.1 | **Concurrency resource benchmark** — measure CopilotClient memory/CPU per agent, determine safe `max_concurrent_agents` default | DC-6, US-5 | Data-driven, not guessed |
| 4.2 | **SDK circuit breaker research** — confirm `on_pre_tool_use` fires for built-in tools, confirm token usage visibility | DC-8 | Determines enforcement coverage |
| 4.3 | **Deploy pipeline cleanup** — align deploy workflow with Bicep (remove Azure Files refs, simplify to single job) | US-1 | Current workflow references nonexistent infrastructure |
| 4.4 | **Health endpoint enrichment** — active agents, queue depth, last event timestamp, registry stats | US-9 | Observable system |
| 4.5 | **Config hot-reload** — detect `.squadron/**` changes on push to default branch, validate + swap | US-10 | Operator doesn't need to redeploy for config changes |
| 4.6 | **Subagent behavior research** — what does `custom_agents` actually do in the SDK? | — | Determines if the `subagents` config field is meaningful |

**Exit criteria:** Operator has confidence in cost controls, can see system state
via health endpoint and GitHub trail, can update config without redeploy.

---

## Part 4: Design Decisions Summary

Decisions locked in by this review:

| # | Decision | Rationale |
|---|---|---|
| D-1 | **Single-tenant, self-hosted, one container per repo** | Simplifies everything. Multi-tenant is a different product. |
| D-2 | **GitHub App with webhook delivery** | Standard pattern, bot identity, ~1s latency. |
| D-3 | **Copilot SDK as agent runtime** | GitHub-native, BYOK, built-in persistence, infinite sessions. |
| D-4 | **GitHub is the durable state layer** | Registry/sessions are ephemeral. On loss, reconstruct from GitHub. |
| D-5 | **Config YAML is the single source of truth** | All routing, tools, triggers, and behavior derive from `.squadron/`. Zero Python changes to add a role. |
| D-6 | **Unified trigger system with two spawn modes** | Triggers are the universal entry point. Direct spawn (async/parallel) for approval flows. Workflow initiation (sync/sequential) for pipelines. One evaluation pipeline, all YAML-defined. |
| D-7 | **Tool boundaries are enforced, not suggested** | Config defines what tools each role gets. Framework enforces it. Prompt restrictions are defense-in-depth, not primary enforcement. |
| D-8 | **PM is ephemeral with rich context injection** | Stateless per-event sessions. Registry + GitHub state injected as context. Revisit if quality proves insufficient. |
| D-9 | **Agents commit + push before sleeping** | Makes work recoverable across container restarts without persistent storage. |
| D-10 | **Background timers for duration limits** | Don't rely on agent self-reporting for safety limits. Framework enforces via task cancellation. |
| D-11 | **PR lifecycle: sleep/wake on review** | Dev agent sleeps after opening PR. Wakes on changes-requested review. Avoids wasting compute during human review time. |
| D-12 | **Framework-level abort on reassignment** | Agent manager cancels agent task directly on reassignment events. No polling dependency. Immediate, not cooperative. |
| D-13 | **Example config, not CLI scaffolding** | Ship a documented starter `.squadron/` directory. Operators copy and edit. `squadron init` CLI is a V2 convenience, not a V1 requirement. |

---

## Part 5: Explicit Deferrals

| Feature | Why Not Now |
|---|---|
| Multi-tenant support | Different product (D-1) |
| `squadron init` CLI scaffolding | V2 convenience. Example config is sufficient for V1 (D-13). |
| Kanban / GitHub Projects management | V2 per roadmap |
| Global cross-agent budget tracking | V2. Per-agent circuit breakers are sufficient for V1. |
| Semantic conflict detection | V2 per roadmap |
| Workflow versioning / linting | V2 per roadmap |
| Container sandboxing / network isolation | Phase 3 (production hardening) per roadmap |
| Multi-repo support | V2 per roadmap |
| Persistent PM (always-on session) | Only if ephemeral PM proves insufficient in integration testing (DC-5) |
| Advanced escalation routing (expertise-based) | V2. Route to `human_groups` is sufficient for V1. |
| Agent-to-agent direct communication | Deliberately excluded by design (AD communication model) |
| Auto-merge | V1 default is `auto_merge: false`. Human retains merge button. Configurable in V2. |
