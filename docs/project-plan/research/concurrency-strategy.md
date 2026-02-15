# Research: Concurrency Strategy (OR-004)

How Squadron handles race conditions when multiple actors (agents and humans) modify the same issue concurrently.

---

## Problem Statement

Multiple events can arrive for the same issue simultaneously:
1. PM is processing issue #50 (about to assign it) while a human re-labels and reassigns it.
2. Two events fire nearly simultaneously for the same issue.
3. An agent closes an issue at the same moment a human reopens it.

---

## Analysis: What the Existing Architecture Already Solves

Before designing new concurrency mechanisms, the current architecture already addresses most scenarios:

### Layer 1: Sequential PM Processing (AD-008)

The PM agent processes issues **one at a time**. This eliminates the most dangerous class of race conditions — two PM decisions about the same issue happening concurrently. Events for the same issue queue up in the event router's per-issue queue.

### Layer 2: Per-Issue Event Queue (AD-013 — Agent Registry)

The Event Router (from OR-003) can trivially partition its internal queue by issue number. Events for issue #50 are processed sequentially even if events for issue #51 proceed in parallel. This is a standard partitioned queue pattern:

```python
class EventRouter:
    def __init__(self):
        self.issue_queues: dict[int, asyncio.Queue] = {}
        self.issue_locks: dict[int, asyncio.Lock] = {}
    
    async def dispatch(self, event: WebhookEvent):
        issue_num = self.extract_issue_number(event)
        if issue_num is None:
            # Non-issue events (e.g., push, deployment) — process immediately
            await self.handle_non_issue_event(event)
            return
        
        # Acquire per-issue lock — events for same issue are serialized
        lock = self.issue_locks.setdefault(issue_num, asyncio.Lock())
        async with lock:
            await self.route_to_agent(event, issue_num)
```

### Layer 3: Idempotent Event Processing (AD-012 — GitHub App)

`X-GitHub-Delivery` header provides a unique delivery ID per webhook. The Event Router deduplicates — if the same event arrives twice (e.g., due to retry or network hiccup), the second delivery is discarded:

```python
class EventRouter:
    def __init__(self):
        self.processed_deliveries: set[str] = set()  # TTL-evicted in production
    
    async def dispatch(self, event: WebhookEvent):
        delivery_id = event.headers["X-GitHub-Delivery"]
        if delivery_id in self.processed_deliveries:
            log.info(f"Duplicate delivery {delivery_id}, skipping")
            return
        self.processed_deliveries.add(delivery_id)
        # ... proceed with routing
```

### Layer 4: Bot Self-Event Filtering (AD-013)

`sender.login == "squadron[bot]"` filtering prevents infinite loops where the bot's own actions trigger new events.

---

## Remaining Scenarios Not Covered

After the four layers above, only a few scenarios remain:

### Scenario A: Human Modifies Issue While PM Is Processing

**Situation:** PM reads issue #50, starts LLM reasoning (~5-30s), decides to assign to `feat-dev`. Meanwhile, a human re-labels the issue as `won't-fix` and closes it.

**Resolution: Re-read before write (Optimistic Concurrency)**

The PM should re-read the issue state from GitHub API immediately before applying its decision. If the state has materially changed (closed, different labels, different assignee), the PM discards its stale decision and re-evaluates:

```python
async def pm_process_issue(issue_number: int):
    # 1. Read current state
    issue = await github.get_issue(issue_number)
    
    # 2. LLM reasoning (may take 5-30s)
    decision = await llm.classify_issue(issue)
    
    # 3. Re-read before write — check for drift
    current_issue = await github.get_issue(issue_number)
    if has_materially_changed(issue, current_issue):
        log.info(f"Issue #{issue_number} changed during processing, re-evaluating")
        # Re-queue for fresh processing
        await event_router.requeue(issue_number)
        return
    
    # 4. Apply decision
    await apply_decision(decision, current_issue)

def has_materially_changed(before, after) -> bool:
    """Check if issue state changed in ways that invalidate PM's decision."""
    return (
        before.state != after.state or           # opened → closed
        before.assignee != after.assignee or     # someone else assigned it
        set(before.labels) != set(after.labels)  # labels changed
    )
```

This is lightweight "optimistic concurrency" — no locks, no ETags, just a cheap GET before the mutating call.

### Scenario B: Agent and Human Both Write to the Same Issue Simultaneously

**Situation:** Agent posts a comment while human posts a comment at the same moment.

**Resolution: No conflict — GitHub handles this.** Issue comments are append-only. Two comments posted simultaneously both appear in the timeline. No data loss, no conflict. This is a non-issue.

### Scenario C: Agent Closes Issue While Human Reopens It

**Situation:** Agent determines issue is complete and closes it. A human (who disagrees) reopens it at the same instant.

**Resolution: Last-write-wins with audit trail.** GitHub's issue state is a single field (`open`/`closed`). The last API call wins. Both the close and reopen events appear in the issue timeline, providing a clear audit trail. The Event Router will receive both events sequentially (per Layer 2 above), so the PM can react appropriately to the resulting state.

**This is actually the correct behavior.** If a human disagrees with the agent's assessment, the human's action should prevail. The agent sees the `issues.reopened` event and re-enters the workflow.

### Scenario D: Two Agents Modify the Same Issue

**Situation:** Dev agent and Security agent both try to add labels to the same issue.

**Resolution: Not possible in Squadron's design.** Each issue has exactly one owning agent (tracked in the Agent Registry). The PM agent is the only entity that modifies issue metadata (labels, assignments). Dev agents modify code (branches, commits, PRs) but not issue metadata.

If this constraint is relaxed in V2, GitHub's label API is append-only — adding a label doesn't affect existing labels, so concurrent adds are safe.

---

## GitHub API Concurrency Features

### ETags for Conditional Requests

GitHub's REST API returns `ETag` headers on GET responses. You can use `If-None-Match` on subsequent GETs to check if data has changed (returns `304 Not Modified` if unchanged, doesn't count against rate limit).

**Useful for:** Polling to detect changes, cache invalidation.
**Not useful for:** Conditional writes (GitHub does NOT support `If-Match` for PUT/PATCH — there's no compare-and-swap).

### Rate Limiting Implications

- GitHub recommends: "Make requests serially instead of concurrently" and "Pause at least one second between mutative requests."
- This naturally serializes agent API calls, reducing the concurrency window.

---

## Decision: Layered Concurrency Strategy

| Layer | Mechanism | Covers |
|---|---|---|
| L1 | Sequential PM processing (AD-008) | PM-to-PM races |
| L2 | Per-issue event queue partitioning | Multi-event races on same issue |
| L3 | `X-GitHub-Delivery` deduplication | Duplicate webhooks |
| L4 | Bot self-event filtering | Feedback loops |
| L5 | Re-read before write (optimistic) | Human-modifies-during-processing |
| L6 | Last-write-wins + audit trail | Unresolvable simultaneous mutations |

### Why NOT Issue-Level Locking?

A label-based locking mechanism (`squadron:processing` label) was considered but rejected:

1. **Fragile** — If the PM crashes while holding the lock, the issue is permanently "locked" unless a cleanup process runs.
2. **Unnecessary** — Per-issue event queue serialization (L2) provides the same guarantee without the failure mode.
3. **Anti-pattern on GitHub** — Using labels as locks pollutes the issue timeline and confuses humans.
4. **Extra API calls** — Setting/removing labels costs 2 additional API calls per issue processing cycle.

### Why NOT Full Optimistic Concurrency Control (OCC)?

Full OCC (read version, write if version matches, retry on conflict) requires server-side support (compare-and-swap). GitHub's API does NOT support conditional writes (`If-Match` headers on PATCH/PUT). Therefore, OCC is not implementable at the GitHub API level.

The re-read-before-write pattern (L5) is the pragmatic equivalent — it doesn't prevent the race but detects it with high probability (the window between re-read and write is milliseconds, vs. seconds for the LLM reasoning window).

---

## Open Questions (Minor — Non-Blocking)

1. **Event queue persistence:** Should the per-issue event queue survive server restarts? For V1, in-memory is fine (webhooks can be replayed from delivery log). For V2, consider a persistent queue (Redis, SQLite WAL).
2. **Stale decision timeout:** How long should re-read-before-write wait before declaring a stale decision? Current approach: any material change triggers re-evaluation. Could add a TTL (e.g., decisions older than 60s are always re-evaluated).
3. **Human override protocol:** Should we formalize a "human is taking over" signal (e.g., `squadron:human-override` label) that tells the PM to stop processing an issue? Useful but not blocking for V1.
