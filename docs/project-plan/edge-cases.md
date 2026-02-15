# Edge Cases

Known edge cases, failure modes, and mitigation strategies.

---

## EC-001: Circular Blocker Dependencies

**Scenario:** Issue A blocks Issue B, and Issue B blocks Issue A. Both dev agents enter SLEEPING state, waiting on each other indefinitely.

**Severity:** 🔴 Critical — causes permanent deadlock.

**Mitigation:**
- PM agent must perform **cycle detection** when processing blocker relationships.
- When a new blocker cross-reference is created, the PM should traverse the dependency graph and check for cycles.
- If a cycle is detected: PM escalates to human with a clear description of the cycle.

**Status:** ✅ Resolved. Cycle detection designed in AD-013 — agent registry performs BFS cycle check on every new blocker relationship. Cycles trigger immediate PM escalation to human. See [Event Routing Research](research/event-routing.md).

---

## EC-002: Agent Hallucination — False Fix

**Scenario:** Dev agent "believes" it fixed a bug, writes a test that passes (but the test is wrong / doesn't actually test the fix), opens a PR. The real bug persists.

**Severity:** 🟡 Important — CI is the safety net but may not catch subtle logical errors.

**Mitigation:**
- PR review agent should independently verify test quality — not just "do tests pass" but "do these tests actually cover the claimed fix."
- Require minimum code coverage thresholds in CI.
- For critical paths: require human review (configured via approval flows).
- Agent retry policy: if CI fails after a fix attempt, the agent retries with the failure context. After N failures, escalate.

**Circuit breakers:**
- Max retry attempts (configurable, e.g., 5).
- Max tokens/cost spent per issue (configurable).
- Max wall-clock time per issue (configurable, e.g., 2 hours).
- On circuit breaker trip → agent enters ESCALATED state → human notified.

**Status:** ✅ Resolved. Circuit breaker design complete — see AD-018 and [Circuit Breaker Design](research/circuit-breakers.md). Limits: max iterations (5), max tool calls (200), max turns (50), max active duration (2h). Enforced via SDK `on_pre_tool_use` hook with 80% warning threshold. On trip → ESCALATED → needs-human issue.

---

## EC-003: Stale Context After Long Sleep

**Scenario:** Dev-1 is blocked for 6 hours waiting on a dependency. During that time, `main` has 15 new commits from other agents and humans. Dev-1 rehydrates with a context checkpoint that reflects a very different repo state.

**Severity:** 🟡 Important — can cause confusion, bad code, or merge conflicts.

**Mitigation:**
- On rehydration, the agent's first action should be: **assess current state**.
  1. Pull latest changes from the base branch.
  2. Attempt rebase/merge of its feature branch.
  3. Re-read relevant files to update its understanding.
  4. Compare current state against its checkpointed context.
  5. If the delta is too large → summarize changes and adjust plan.
- The agent definition should include explicit instructions for the "wake up" protocol.
- Consider limiting sleep duration — if blocked for more than X hours, escalate.

**Status:** ✅ Resolved. Wake protocols specified in agent definition files (`.squadron/agents/feat-dev.md`, `bug-fix.md`, `pr-review.md`). Each includes explicit rehydration steps: pull latest, rebase/merge, re-read changed files, reassess plan. Circuit breaker AD-018 enforces max sleep duration (24h).

---

## EC-004: Cost Runaway — Infinite Fix Loop

**Scenario:** Agent enters a cycle: fix code → run tests → tests fail → fix code → tests fail → ... consuming tokens and compute indefinitely.

**Severity:** 🔴 Critical — unbounded cost.

**Mitigation:**
- **Circuit breakers** (same as EC-002):
  - Max iterations per task.
  - Max tokens/cost per task.
  - Max wall-clock time per task.
- After breaker trips, agent enters ESCALATED state and creates a detailed summary of what it tried and why it failed.
- Framework-level budget tracking across all agents (daily/weekly spend limits).

**Status:** ✅ Resolved. Same circuit breaker system as EC-002 — see AD-018. Max iterations, max tool calls, max time enforced at framework level via SDK hooks. Global cross-agent budget tracking deferred to V2.

---

## EC-005: Human Override Mid-Flight

**Scenario:** A human reassigns issue #38 from Dev-1 to a different agent (or to themselves) while Dev-1 is actively working on it.

**Severity:** 🟡 Important — agent may continue working on a branch that's no longer "its" task.

**Mitigation:**
- Agent should check issue assignment before each major action (commit, PR open, etc.).
- If the agent detects it's been unassigned: stop work, serialize context, comment on the issue noting the interruption, and enter COMPLETED (or a new CANCELLED state).
- The agent's feature branch remains for the new assignee to pick up or discard.
- PM agent should detect the reassignment event and notify the original agent.

**Status:** Partially resolved. The `check_for_events` custom tool (AD-017) returns assignment changes as events the agent can act on. Exact check-before-commit pattern to be finalized during prototyping.

---

## EC-006: Concurrent PR Reviews — Conflicting Feedback

**Scenario:** Security review agent and PR review agent both review a PR simultaneously. Security agent requests removal of a code block; PR review agent approves the same code block as correct.

**Severity:** 🟢 Low — this is normal in human code review too.

**Mitigation:**
- This is expected behavior. Conflicting reviews are resolved by:
  1. The dev agent addresses all "request changes" reviews (security takes priority).
  2. Re-request reviews from all reviewers.
  3. If conflict persists after 2 rounds → escalate to human.
- Approval flow config can define review priority ordering.

**Status:** Acceptable as-is. No special handling needed in V1.

---

## EC-007: GitHub API Rate Limits

**Scenario:** Multiple agents making frequent API calls (reading issues, posting comments, creating branches, etc.) hit GitHub's API rate limit (5,000 requests/hour for authenticated users).

**Severity:** 🟡 Important — could stall all agents simultaneously.

**Mitigation:**
- **Installation access tokens** (AD-012): GitHub App installation tokens provide 5,000 requests/hour per installation — this is the auth model Squadron uses.
- Framework-level rate limit tracking and request queuing.
- Agents should batch API calls where possible.
- Use conditional requests (ETags) to reduce unnecessary API consumption.
- Prioritize PM agent API access over dev agents (PM is the coordinator).

**Status:** Must be considered in infrastructure design. Rate limit is 5,000 req/hr per installation (confirmed via AD-012).

---

## EC-008: Webhook Delivery Failure

**Scenario:** GitHub sends a webhook for "issue #42 closed" but the Squadron server is temporarily down. The event is lost. Dev-1 remains in SLEEPING state forever, waiting for a notification that never comes.

**Severity:** 🔴 Critical — can cause permanent agent stalls.

**Mitigation:**
- **Webhook delivery logs:** GitHub does NOT automatically retry failed webhook deliveries. However, delivery logs are available in the App settings UI and via API. Failed deliveries can be manually redelivered via `POST /app/hook/deliveries/{delivery_id}/attempts`.
- **Polling fallback (reconciliation loop):** A background process periodically queries GitHub for state changes and reconciles against known agent states. "Is any SLEEPING agent's blocker already resolved?" This is the primary safety net for missed webhooks.
- **Heartbeat/watchdog:** SLEEPING agents have a max sleep duration. If exceeded, the framework wakes them up for a state check regardless of whether an event was received.
- **Dead-letter queue:** Failed event processing is logged to a dead-letter queue for investigation and replay.
- **Idempotent event processing:** Events must be idempotent — `X-GitHub-Delivery` UUID used to dedup duplicate/replayed deliveries.

**Status:** ✅ Resolved. 5-minute reconciliation loop designed in AD-013 — background process queries agent registry for SLEEPING agents whose blockers are resolved, and for stale ACTIVE agents exceeding max duration. Dead-letter queue and `X-GitHub-Delivery` dedup also designed. See [Event Routing Research](research/event-routing.md) and [Concurrency Strategy](research/concurrency-strategy.md).

---

## EC-009: Agent Creates Malicious or Dangerous Code

**Scenario:** An agent (due to prompt injection, model error, or adversarial issue content) generates code that is malicious (e.g., data exfiltration, backdoors, destructive operations).

**Severity:** 🔴 Critical — security concern.

**Mitigation:**
- Agents run in **sandboxed containers** with no network access beyond GitHub and the package registry.
- Security review agent is required in the approval flow for main branch merges.
- Agents cannot access production secrets, deployment credentials, or sensitive infrastructure.
- Agent tool permissions are limited — they can only interact with the repo and GitHub issues.
- Human review required for merges to protected branches (configurable).
- Consider static analysis / SAST tools in CI that flag suspicious patterns.

**Status:** Deferred to Phase 3. V1 runs as a single-process monolith (AD-017) with tool-level restrictions in agent definitions. Full container sandboxing designed in Phase 3 (see [Roadmap](roadmap.md)).

---

## EC-010: Issue Spam / Infinite Issue Creation Loop

**Scenario:** An agent creates a new issue (e.g., a blocker bug report). The PM triages it and assigns it to another agent. That agent also creates a new issue. This creates an unbounded chain of issue creation.

**Severity:** 🟡 Important — could flood the issue tracker.

**Mitigation:**
- **Depth limit:** Track issue creation depth (original issue → blocker → blocker of blocker). If depth exceeds a threshold (e.g., 3), escalate to human.
- **Rate limit:** Max issues created per agent per time window.
- **PM awareness:** PM agent should recognize when issue creation is cascading and intervene.

**Status:** Partially resolved. OR-003 resolved — agent registry (AD-013) tracks blocker relationships and can infer depth. Explicit depth-limit enforcement (max issue creation depth per chain) not yet implemented but straightforward to add as a registry query. Deferred to prototyping.
