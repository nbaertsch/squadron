# OR-003 Research: Event Routing & Agent Registry

**Date:** 2025-02-XX  
**Status:** Research Complete — Recommendation: **Agent Registry + Subscription Model**

---

## The Two Problems

OR-003 is actually two interrelated problems disguised as one:

### Problem 1: Direct Routing
When a webhook event fires, which agent session should receive it?

Example: `issue_comment.created` on issue #38 → Dev-1 is working on #38 → route to Dev-1.

This is a **lookup**: `issue_number → agent_session_id`.

### Problem 2: Cross-Issue Dependency Resolution
When an event fires on one issue, which agents working on *other* issues should be notified?

Example: `issues.closed` on #42 → Dev-1 working on #38 is blocked by #42 → wake Dev-1.

This requires a **dependency graph**: `issue_42.closed → who has blocked_by: [42]?`

---

## The Key Insight: The Framework Creates the Dependencies

When Dev-1 discovers it's blocked and asks the PM to create blocker issue #42, the interaction flows through the framework:

```
Dev-1 working on #38
    │
    ├── Dev-1: "@squadron-pm I found a bug blocking my work. [description]"
    │
    ├── PM: reads comment, creates issue #42, references #38
    │      └── Framework ALREADY KNOWS: #38 is blocked by #42
    │
    ├── PM: comments on #38: "Created blocker #42. Waiting for resolution."
    │
    └── Framework: registers subscription
        agent="dev-1", issue=38, blocked_by=[42], status=SLEEPING
```

The dependency relationship is created **by the framework itself** — not discovered after the fact. This means:
- **No GitHub API query needed at dispatch time**
- **No text parsing of `Blocked by #42` comments**
- **No cross-reference traversal**
- The Agent Registry is the single source of truth for routing

---

## The Agent Registry

The Agent Registry is a lightweight data store that tracks all active agent sessions and their relationships.

### Data Model

```
┌─────────────────────────────────────────────────────────────┐
│ AgentRecord                                                 │
├─────────────────────────────────────────────────────────────┤
│ agent_id       : str        # "dev-1", "security-review-3" │
│ role           : str        # "feat-dev", "bug-fix", etc.  │
│ issue_number   : int        # The issue this agent owns    │
│ session_id     : str        # Copilot SDK session ID       │
│ status         : enum       # CREATED | ACTIVE | SLEEPING  │
│                             # | COMPLETED | ESCALATED      │
│                             # | CANCELLED                  │
│ branch         : str?       # "feat/issue-38"              │
│ pr_number      : int?       # Set when PR is opened        │
│ blocked_by     : [int]      # Issue numbers blocking this  │
│ created_at     : datetime   #                              │
│ updated_at     : datetime   #                              │
│ repo           : str        # "owner/repo" (multi-repo V2) │
└─────────────────────────────────────────────────────────────┘
```

### Storage

| Version | Backend | Why |
|---|---|---|
| V1 | SQLite file | Single-server, embedded, queryable, zero-dependency, persistent |
| V2 | PostgreSQL | Multi-server, concurrent access, production-grade |

The registry is **small** — one row per active agent. Even with 50 concurrent agents (unlikely in V1), this is trivially manageable in SQLite.

This is distinct from Copilot SDK session state (which stores conversation context in `~/.copilot/session-state/`). The Agent Registry is **orchestrator-level metadata** — it knows *who* is working on *what* and *what they're waiting for*, but not the agent's conversation history.

---

## Event Routing Logic

### The Event Router

```
Webhook Event (from Event Queue)
    │
    ├── 1. Extract key fields:
    │      event_type, action, issue_number, pr_number,
    │      comment_body, actor, branch
    │
    ├── 2. Is this from our own bot? (squadron[bot])
    │      └── YES → skip (prevent feedback loops)
    │
    ├── 3. Route by event type + action:
    │
    │   ┌── issues.opened ──────────────────── → PM Agent (always)
    │   │
    │   ├── issues.assigned ─────────────────── → Create new agent session
    │   │   └── Only if assignee is squadron[bot]
    │   │       Register in Agent Registry, set status=CREATED
    │   │
    │   ├── issues.closed ──────────────────── → Two actions:
    │   │   ├── a. Query Registry: agents WHERE blocked_by CONTAINS issue_number
    │   │   │      For each: remove from blocked_by, if empty → wake (SLEEPING→ACTIVE)
    │   │   └── b. Agent working on this issue → set status=COMPLETED, cleanup
    │   │
    │   ├── issues.reopened ────────────────── → PM Agent (re-evaluate)
    │   │
    │   ├── issues.labeled ─────────────────── → PM Agent (routing change?)
    │   │
    │   ├── issues.unassigned ──────────────── → Agent for this issue
    │   │   └── Cancel agent: set status=CANCELLED, comment, serialize
    │   │
    │   ├── issue_comment.created ──────────── → Route by content:
    │   │   ├── Contains "@squadron-pm" → PM Agent
    │   │   ├── On issue with active agent → that agent
    │   │   └── From human on agent-assigned issue → that agent (feedback)
    │   │
    │   ├── pull_request.opened ────────────── → Two actions:
    │   │   ├── a. Map PR → issue (parse body for "Fixes #N" or branch name)
    │   │   │      Update Agent Registry: set pr_number
    │   │   └── b. Trigger review agents per approval flow config
    │   │
    │   ├── pull_request.synchronize ──────── → Re-trigger reviews
    │   │
    │   ├── pull_request.closed (merged) ───── → Three actions:
    │   │   ├── a. Close linked issue (if not already)
    │   │   ├── b. Mark agent COMPLETED, cleanup branch
    │   │   └── c. Check: did this unblock anyone? (issue closure event handles it)
    │   │
    │   ├── pull_request_review.submitted ──── → Route by review state:
    │   │   ├── approved → check if all required approvals met → auto-merge?
    │   │   ├── changes_requested → dev agent for this PR's issue
    │   │   └── commented → dev agent (informational)
    │   │
    │   ├── status / check_run ─────────────── → Map via commit → branch → agent
    │   │   ├── success → agent can proceed (or auto-merge if all approvals met)
    │   │   └── failure → dev agent gets notified to fix
    │   │
    │   └── (unrecognized) ─────────────────── → Log and discard
    │
    └── 4. Dispatch to agent:
           ├── If status=ACTIVE → send event to running session
           ├── If status=SLEEPING → resume_session(), send event
           └── If status=COMPLETED/CANCELLED → skip (stale event)
```

### Feedback Loop Prevention

Critical: the App's own actions (commenting, labeling, assigning) trigger webhook events. Without protection, this creates infinite loops:

```
Agent comments on issue → issues.commented webhook → Event Router → Agent processes comment → Agent comments again → ...
```

**Solution:** Check the webhook payload's `sender` field:
```python
if event.sender.login == "squadron[bot]":
    return  # Skip events from our own bot
```

Additionally, check `performed_via_github_app.id` to catch App-generated events even if the sender field differs.

### The PM Agent as Default Router

Many events route to the PM agent. The PM agent is special:
- It's a **long-lived singleton** (one per repo/installation)
- It doesn't have a specific issue_number — it's a coordinator
- Its session ID is well-known: `pm-{repo_owner}-{repo_name}`
- It processes events sequentially (AD-008)

The PM agent has its own event queue (or the main Event Router simply routes PM-bound events to a PM-specific queue).

---

## Dependency Graph Operations

### Register a Dependency

When an agent reports a blocker:

```python
def register_blocker(agent_id: str, blocker_issue: int):
    record = registry.get(agent_id)
    
    # Cycle detection before registration
    if creates_cycle(record.issue_number, blocker_issue):
        raise CyclicDependencyError(
            f"Adding {blocker_issue} as blocker of {record.issue_number} "
            f"creates a dependency cycle"
        )
    
    record.blocked_by.append(blocker_issue)
    record.status = AgentStatus.SLEEPING
    record.updated_at = now()
    registry.update(record)
```

### Resolve a Dependency

When a blocker issue closes:

```python
def on_issue_closed(issue_number: int):
    # Find all agents blocked by this issue
    blocked_agents = registry.query(
        "SELECT * FROM agents WHERE ? = ANY(blocked_by)",
        issue_number
    )
    
    for agent in blocked_agents:
        agent.blocked_by.remove(issue_number)
        
        if len(agent.blocked_by) == 0:
            # All blockers resolved → wake the agent
            agent.status = AgentStatus.ACTIVE
            dispatch_wake_event(agent)
        
        agent.updated_at = now()
        registry.update(agent)
```

### Cycle Detection

Before adding a dependency edge `A blocked_by B`, check if B is transitively blocked by A:

```python
def creates_cycle(source_issue: int, target_issue: int) -> bool:
    """Check if adding 'source blocked_by target' creates a cycle."""
    visited = set()
    queue = [target_issue]
    
    while queue:
        current = queue.pop(0)
        if current == source_issue:
            return True  # Cycle detected!
        if current in visited:
            continue
        visited.add(current)
        
        # Find the agent working on 'current', check what IT is blocked by
        agent = registry.get_by_issue(current)
        if agent:
            queue.extend(agent.blocked_by)
    
    return False
```

### Transitive Dependency Query

"What issues are transitively blocking #38?"

```python
def transitive_blockers(issue_number: int) -> set[int]:
    """Return all issues transitively blocking the given issue."""
    result = set()
    queue = list(registry.get_by_issue(issue_number).blocked_by)
    
    while queue:
        current = queue.pop(0)
        if current in result:
            continue
        result.add(current)
        agent = registry.get_by_issue(current)
        if agent:
            queue.extend(agent.blocked_by)
    
    return result
```

---

## PR → Issue Mapping

When a PR is opened/updated, the Event Router needs to map it back to the originating issue and agent. Three mapping strategies (used in priority order):

### 1. Agent Registry Lookup (Primary)
The agent that opens the PR updates the registry with `pr_number`. Reverse lookup: `pr_number → agent_id → issue_number`.

### 2. Branch Name Convention (Fallback)
Branch names follow AD-004: `feat/issue-{N}`, `fix/issue-{N}`. Parse the issue number from the branch name.

```python
import re
match = re.match(r"(?:feat|fix|security|hotfix)/issue-(\d+)", branch_name)
if match:
    issue_number = int(match.group(1))
```

### 3. PR Body Cross-Reference (Fallback)
Parse `Fixes #N` / `Closes #N` / `Resolves #N` from the PR body. GitHub itself recognizes these keywords.

```python
import re
refs = re.findall(r"(?:fixes|closes|resolves)\s+#(\d+)", pr_body, re.IGNORECASE)
```

---

## GitHub Cross-References: Secondary Role

GitHub's cross-reference system (when issue #38 mentions #42, GitHub shows it in #42's timeline) is valuable but NOT the primary routing mechanism.

### Why not primary?
- Requires API calls at dispatch time (adds latency)
- Text-based parsing of `Blocked by #42` from comments is fragile
- Cross-references are bidirectional in GitHub UI but not queryable as a directed graph
- The framework already knows the deps when they're created

### Role in Squadron
- **Human visibility**: Cross-references appear in GitHub's sidebar, helping humans understand relationships
- **Audit trail**: Part of AD-001's "human-readable audit trail for free"
- **Recovery/reconciliation**: If the Agent Registry is lost, cross-references + issue state can help reconstruct it
- **Reconciliation loop verification**: Periodic job can cross-check registry state against GitHub cross-references

---

## Reconciliation Loop

A background process that runs periodically (e.g., every 5 minutes) to catch missed events:

```python
async def reconciliation_loop():
    while True:
        sleeping_agents = registry.query(status=SLEEPING)
        
        for agent in sleeping_agents:
            for blocker_issue in agent.blocked_by:
                # Check if blocker is actually still open
                issue = github_api.get_issue(blocker_issue)
                if issue.state == "closed":
                    # Missed the closure event! Resolve now.
                    on_issue_closed(blocker_issue)
                    log.warning(f"Reconciliation caught missed closure of #{blocker_issue}")
        
        # Also check for stale agents (sleeping too long)
        stale_agents = registry.query(
            status=SLEEPING,
            updated_at__lt=now() - MAX_SLEEP_DURATION
        )
        for agent in stale_agents:
            escalate_to_human(agent, reason="exceeded max sleep duration")
        
        await asyncio.sleep(300)  # 5 minutes
```

This is the safety net for EC-008 (webhook delivery failure).

---

## Event Dispatch: Running vs. Sleeping Agents

### Dispatching to a SLEEPING Agent (Wake)

```python
async def dispatch_wake_event(agent: AgentRecord):
    """Resume a sleeping agent's Copilot SDK session."""
    session = copilot_sdk.resume_session(
        session_id=agent.session_id,
        # Inject context about what changed while sleeping
    )
    
    # Agent's first action on wake: assess current state
    # (pull latest changes, check branch, rebase if needed)
    # This is built into the agent definition's "wake protocol"
    
    agent.status = AgentStatus.ACTIVE
    registry.update(agent)
```

### Dispatching to an ACTIVE Agent

Active agents are running Copilot SDK sessions. The Event Router needs to send events to them.

**Challenge:** Copilot SDK sessions are interactive CLI processes. You can't "inject" a message into a running session from outside.

**Solutions:**
1. **File-based event inbox**: Write event to a file the agent monitors. Agent's custom tool checks the inbox.
2. **Agent tool polling**: Agent has a `@define_tool` custom tool `check_for_events()` that queries the registry/event queue.
3. **Session interruption**: Terminate the current session, then resume with the new context (inelegant but guaranteed to work).

**Recommended for V1:** Option 2 — agent periodically calls `check_for_events()` tool between major actions. The tool reads from an in-memory or file-based event inbox for that agent.

```python
@define_tool
def check_for_events(agent_id: str) -> list[Event]:
    """Check for pending events (PR review feedback, unblocked notification, etc.)."""
    events = event_inbox.get_pending(agent_id)
    event_inbox.mark_read(agent_id)
    return events
```

---

## Full Architecture Diagram

```
                    GitHub Repository
                         │
            Webhook Events (signed)
                         │
                         ▼
            ┌────────────────────────┐
            │   Webhook Receiver     │
            │   (HMAC validation)    │
            │   (dedup via           │
            │    X-GitHub-Delivery)  │
            │   (respond 200)        │
            └────────┬───────────────┘
                     │
                     ▼
            ┌────────────────────────┐
            │   Event Queue          │
            │   (asyncio.Queue V1)   │
            │   (Redis V2)           │
            └────────┬───────────────┘
                     │
                     ▼
            ┌────────────────────────┐
            │   Event Router         │
            │                        │
            │   ├── Bot self-check   │
            │   ├── Event type       │
            │   │   routing table    │
            │   ├── Agent Registry   │◄──── SQLite (V1) / Postgres (V2)
            │   │   lookups          │
            │   └── Dependency       │
            │       resolution       │
            └────┬──────┬──────┬─────┘
                 │      │      │
        ┌────────┘      │      └────────┐
        ▼               ▼               ▼
   ┌─────────┐    ┌──────────┐    ┌──────────┐
   │ PM Queue │    │ Agent    │    │ Agent    │
   │          │    │ Inbox    │    │ Inbox    │
   │          │    │ (dev-1)  │    │ (dev-2)  │
   └────┬─────┘    └────┬─────┘    └────┬─────┘
        │               │               │
        ▼               ▼               ▼
   ┌─────────┐    ┌──────────┐    ┌──────────┐
   │ PM Agent │    │ Dev-1    │    │ Dev-2    │
   │ (always  │    │ Session  │    │ Session  │
   │  active) │    │ (Copilot │    │ (Copilot │
   │          │    │  SDK)    │    │  SDK)    │
   └──────────┘    └──────────┘    └──────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │  Reconciliation     │
              │  Loop (5 min)       │
              │  - Check sleeping   │
              │    agents' blockers │
              │  - Stale agent      │
              │    detection        │
              │  - Registry ↔ GH    │
              │    state sync       │
              └─────────────────────┘
```

---

## Implications for Other ORs

| OR | Implication |
|---|---|
| **OR-004** (Race Conditions) | Agent Registry operations must be atomic. SQLite with WAL mode provides serialized writes for V1. Per-issue event ordering guaranteed by sequential queue processing. |
| **OR-005** (Approval Flow Schema) | Approval flows are another routing rule in the Event Router: `pull_request.opened → lookup approval config → assign review agents`. |
| **EC-001** (Circular Dependencies) | Cycle detection algorithm defined above. Runs at dependency registration time, not dispatch time. |
| **EC-008** (Missed Webhooks) | Reconciliation loop defined above. Periodic polling catches missed events. |
| **EC-010** (Issue Spam) | Agent Registry can track `depth` — how deep the issue creation chain goes. Router refuses to create agents beyond depth limit. |

---

## Open Questions

1. **Agent inbox implementation** — file-based, in-memory queue, or shared memory? V1 probably in-memory (agents and router in same process).
2. **PM agent lifecycle** — is the PM always running, or does it wake per-event? If always running, it needs its own event loop. If per-event, it needs fast resume.
3. **Multi-repo routing** — for V2, the Agent Registry needs a `repo` field and events must be scoped per-installation.
4. **Agent ID generation** — sequential (`dev-1`, `dev-2`) or UUID-based? Sequential is more human-readable.
5. **Registry persistence on crash** — SQLite with WAL mode + periodic checkpoints ensures durability. But what about in-flight events in the asyncio queue?

---

## References

- [REST API: Issue Events](https://docs.github.com/en/rest/issues/events)
- [REST API: Issue Timeline Events](https://docs.github.com/en/rest/issues/timeline)
- [Issue Event Types](https://docs.github.com/en/webhooks-and-events/events/issue-event-types)
- [Event Architecture Research (OR-002)](event-architecture.md)
