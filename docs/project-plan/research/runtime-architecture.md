# Runtime Architecture Design

**Date:** 2026-02-14  
**Relates to:** B1 (Pre-prototyping gap), AD-003, AD-012, AD-013  
**Status:** Design Complete — Decision: **Single-Process Monolith with Per-Agent CopilotClient Instances**

---

## Context & Problem Statement

Every architectural decision so far describes *what* the system does, but not *how it runs*. We've decided:
- Copilot SDK is the agent runtime (AD-003)
- GitHub App receives webhooks (AD-012)
- Agent Registry tracks state in SQLite (AD-013)
- Events route through an Event Router (event-routing.md)

But we haven't answered the fundamental question: **What is the host process that ties these together, and how does it manage agent lifecycles?**

This decision determines:
- How the prototype is structured (what code runs where)
- How agents are spawned, monitored, and cleaned up
- How the webhook receiver, event router, agent registry, and agent sessions share a process boundary
- What the migration path to production looks like

---

## Options Analyzed

### Option A: Single-Process Monolith

One Python asyncio process runs everything: webhook receiver, event queue, event router, agent registry, agent manager, reconciliation loop, and all CopilotClient instances.

```
┌──────────────────────────────────────────────────────────────┐
│  Squadron Server (Python, asyncio, FastAPI)                  │
│                                                              │
│  ┌─────────────┐  ┌──────────┐  ┌──────────┐                │
│  │ Webhook     │→ │ Event    │→ │ Event    │                │
│  │ Receiver    │  │ Queue    │  │ Router   │                │
│  └─────────────┘  └──────────┘  └────┬─────┘                │
│                                      │                       │
│  ┌──────────────┐              ┌─────┴──────┐                │
│  │ Agent        │◄─────────────│ Agent      │                │
│  │ Registry     │              │ Manager    │                │
│  │ (SQLite)     │              └────┬───────┘                │
│  └──────────────┘                   │                        │
│                          ┌──────────┼──────────┐             │
│                          ▼          ▼          ▼             │
│                    ┌──────────┐┌──────────┐┌──────────┐      │
│                    │CopilotCli││CopilotCli││CopilotCli│      │
│                    │(PM)      ││(Dev-1)   ││(Review)  │      │
│                    │→ CLI proc││→ CLI proc││→ CLI proc│      │
│                    └──────────┘└──────────┘└──────────┘      │
│                                                              │
│  ┌──────────────┐                                            │
│  │ Reconcil.    │ (asyncio.create_task, every 5 min)         │
│  │ Loop         │                                            │
│  └──────────────┘                                            │
└──────────────────────────────────────────────────────────────┘
```

Each CopilotClient spawns its own CLI binary subprocess. The Python process manages all of them.

**Pros:**
- Simplest architecture — one process, one codebase, one deployment unit
- In-memory event queue and agent inboxes (no serialization, no IPC)
- SQLite naturally fits single-writer model
- Everything shares one asyncio event loop — clean coordination
- Easiest to debug (one process, one log stream)
- Agent Manager has direct access to all components

**Cons:**
- Single point of failure — server crash loses all in-memory state
- All agents compete for resources on one machine
- No isolation between agents (a rogue CLI process could affect others)
- Scaling limited to vertical (one machine)

---

### Option B: Supervisor + Worker Subprocesses

The Squadron server is the supervisor. Each agent is a separate Python subprocess managing its own CopilotClient.

**Pros:**
- Process isolation (one agent crash doesn't kill others)
- Can set per-subprocess resource limits
- Multi-core utilization

**Cons:**
- IPC complexity (pipes, Unix sockets, or shared files for event delivery)
- Debugging across processes is harder
- Still single-machine
- More boilerplate code for process lifecycle management
- Agent inboxes need serialization for cross-process delivery

---

### Option C: Containerized Workers

Same as B but each agent runs in a Docker container. The supervisor communicates via HTTP/gRPC.

**Pros:**
- Full isolation (filesystem, network, resources)
- Security sandboxing (EC-009)
- Resource limits enforced by container runtime
- Closest to production architecture

**Cons:**
- Heavy for prototyping — container build/start overhead per agent
- Volume mounts needed for SDK session state persistence
- Container networking adds latency for agent ↔ supervisor communication
- Docker dependency adds complexity to dev setup
- Debugging is significantly harder (container logs, attach, etc.)

---

## Decision: Option A — Single-Process Monolith (V1)

### Rationale

**1. We're building a prototype, not production.**
The roadmap places containerization in Phase 3 (Production Hardening). Phase 1 (Single-Agent Loop) and Phase 2 (Multi-Agent Coordination) need fast iteration, not infrastructure engineering.

**2. The Copilot SDK already provides session-level isolation.**
Each CopilotClient spawns its own CLI binary subprocess. Even in a monolith, each agent is effectively in its own OS process for LLM execution. The Python server coordinates them but doesn't share their execution context.

**3. In-memory communication aligns with existing designs.**
The event-routing.md research doc already assumes "agents and router in same process" for V1. Agent inboxes, event queues, and the reconciliation loop are all designed for in-process access.

**4. SQLite + single process = no concurrency headaches.**
SQLite WAL mode provides serialized writes. With a single Python process, there's no risk of concurrent database access from separate processes.

**5. Crash recovery is acceptable.**
On server crash: SQLite persists (durability built-in), Copilot SDK sessions persist to disk (automatic checkpointing). Only in-memory event queues and agent inboxes are lost. The reconciliation loop (every 5 min) catches any missed events on restart. This is acceptable for a prototype.

**6. Clean migration path to containers.**
Agents interact with the framework only through:
- Custom `@define_tool` tools (can become HTTP calls)
- GitHub API (already external)
- Agent registry (already a database)
- SDK session state (already on disk)

Extracting agents into containers later requires no architectural change to agent code — only the communication layer changes from in-process function calls to HTTP/gRPC.

---

## Process Architecture

### Component Breakdown

```
Squadron Server Process
│
├── 1. Webhook Receiver (FastAPI endpoint)
│      POST /webhook
│      - HMAC-SHA256 signature validation
│      - Respond 200 immediately (< 10 seconds)
│      - Enqueue raw event to Event Queue
│
├── 2. Event Queue (asyncio.Queue)
│      - Single consumer (Event Router)
│      - Bounded size (backpressure if overloaded)
│      - Lost on crash (acceptable — reconciliation catches gaps)
│
├── 3. Event Router (async consumer loop)
│      - Dequeue from Event Queue
│      - Bot self-event filter (sender == "squadron[bot]")
│      - Webhook dedup (X-GitHub-Delivery UUID in SQLite seen_events table)
│      - Event type → handler dispatch
│      - Agent Registry queries for routing decisions
│      - Enqueue to PM Queue or agent inboxes
│
├── 4. Agent Manager
│      - Manages CopilotClient instances (one per active agent)
│      - Creates / resumes / destroys agent sessions
│      - Registers custom @define_tool functions per agent
│      - Monitors session.idle events for completion detection
│      - Agent inbox management (dict[agent_id → asyncio.Queue])
│
├── 5. Agent Registry (SQLite, WAL mode)
│      - Agent records: id, role, issue, session_id, status, blocked_by[], pr_number
│      - Seen events table (webhook dedup)
│      - CRUD + dependency queries + BFS cycle detection
│      - Single file: .squadron-data/registry.db
│
├── 6. Reconciliation Loop (asyncio.create_task, periodic)
│      - Every 5 minutes
│      - Check SLEEPING agents' blockers against GitHub state
│      - Detect stale ACTIVE agents exceeding max duration
│      - Cross-check registry state vs. GitHub issue/PR state
│
├── 7. GitHub Client (async HTTP)
│      - Installation access token management (refresh on expiry)
│      - Rate limit tracking (5,000 req/hr)
│      - Async HTTP client (httpx)
│      - Used by: Event Router, Agent Manager, Reconciliation Loop
│
└── 8. Config Loader
       - Read .squadron/ config from repo
       - Parse workflow definitions, agent role configs, global settings
       - Refresh on config change events (push to default branch)
```

### Startup Sequence

```
1. Load .squadron/ config from repo working directory
2. Initialize SQLite database (create tables if not exist)
3. Read agent registry — identify any agents that were ACTIVE at last shutdown
4. Mark stale ACTIVE agents as SLEEPING (safe recovery — reconciliation will re-evaluate)
5. Start FastAPI server (uvicorn)
6. Start Event Router consumer loop (asyncio.create_task)
7. Start Reconciliation Loop (asyncio.create_task)
8. Begin accepting webhook events
```

### Graceful Shutdown

```
1. Stop accepting new webhook events
2. Drain Event Queue (process remaining events)
3. For each active CopilotClient:
   a. Wait for current session.idle (with timeout)
   b. Call session.destroy() — persists state to disk
   c. Call client.stop() — stops CLI subprocess
4. Update agent registry: mark ACTIVE agents as SLEEPING
5. Close SQLite connection
6. Exit
```

---

## Agent Process Model

### One CopilotClient Per Agent

Each agent gets its own `CopilotClient` instance, which spawns its own Copilot CLI subprocess:

```
Python Process (asyncio)
│
├── CopilotClient (PM)         → CLI Binary (subprocess, stdio transport)
│   └── Session: squadron-pm-owner-repo
│
├── CopilotClient (Dev-1)      → CLI Binary (subprocess, stdio transport)
│   └── Session: squadron-feat-dev-issue-42
│
├── CopilotClient (Dev-2)      → CLI Binary (subprocess, stdio transport)
│   └── Session: squadron-bug-fix-issue-99
│
└── CopilotClient (PR Review)  → CLI Binary (subprocess, stdio transport)
    └── Session: squadron-pr-review-pr-15
```

**Why per-agent clients instead of shared?** 
- **Crash isolation:** If one CLI process crashes (bad tool call, OOM), only that agent is affected. Other agents continue working. The crashed client can auto-restart (`auto_restart: True`).
- **Clean resource boundaries:** Each CLI process has independent memory, CPU usage, and context window state.
- **Maps to containers:** In Phase 3, each container would have exactly one CopilotClient + CLI process. Building this abstraction now means zero agent-code changes when containerizing.
- **Session identity clarity:** One client = one session = one agent. No confusion about which session belongs to which agent.

**Resource cost:** Each CLI subprocess is primarily an I/O-bound process (waiting on LLM API responses). Memory overhead per CLI process is manageable (estimated ~50-100MB). With 5-10 concurrent agents (realistic for V1), total overhead is < 1GB.

### CopilotClient Configuration Per Agent Role

```python
# Agent Manager creates a client for each agent
async def create_agent_client(agent_record: AgentRecord) -> CopilotClient:
    client = CopilotClient({
        "cwd": get_agent_worktree_path(agent_record),  # Per-agent working directory
        "auto_restart": True,
        "log_level": "info",
    })
    await client.start()
    return client
```

---

## Agent Lifecycle Models

### PM Agent — Event-Driven, Context-Injected Sessions

The PM agent is fundamentally different from dev agents:

| Property | PM Agent | Dev/Review Agents |
|---|---|---|
| Cardinality | Singleton per repo | One per issue/PR |
| Memory model | Stateless — reads state from registry + GitHub | Stateful — accumulated conversation context |
| Trigger | External event (webhook) | Assignment or wake-up |
| Session lifetime | Short (minutes per event batch) | Long (hours to days across sleep/wake) |
| Value of history | Low — each triage is independent | High — remembers what it tried, coded, etc. |

**Design: Fresh session per event batch with injected context.**

When events arrive for the PM, the Agent Manager:
1. Collects queued PM events into a batch
2. Queries the agent registry for current project state (active agents, sleeping agents, blockers)
3. Creates a fresh PM session with injected context:

```python
async def invoke_pm(events: list[Event]):
    client = CopilotClient({"cwd": repo_worktree_path})
    await client.start()
    
    session = await client.create_session({
        "session_id": f"squadron-pm-{repo}-{batch_id}",
        "model": config.pm_model,
        "system_message": {"content": load_agent_definition("pm")},
        "provider": get_provider_config(),
        "tools": [
            create_issue_tool,
            assign_issue_tool,
            label_issue_tool,
            register_agent_tool,
            check_registry_tool,
        ],
        "hooks": {
            "on_pre_tool_use": make_permission_guard("pm"),
            "on_error_occurred": pm_error_handler,
        },
    })
    
    context = build_pm_context()  # Registry state, recent activity, config
    prompt = format_pm_prompt(events, context)
    
    done = asyncio.Event()
    session.on("session.idle", lambda _: done.set())
    await session.send({"prompt": prompt})
    await done.wait()
    
    await session.destroy()
    await client.stop()
```

**Why fresh sessions?**
- PM's "knowledge" is external (GitHub state + registry), not conversational memory
- Context window stays clean — no accumulation from triaging 100 issues
- Each triage decision is independent, producing better results
- No session state to manage, persist, or recover

**Alternative considered: Long-running PM session.**
Rejected because accumulated conversation context would fill the context window with irrelevant past triage decisions. Infinite session compaction would summarize away details the PM doesn't need anyway. Fresh session + injected state is simpler and produces higher quality decisions.

---

### Dev Agents — Persistent Sessions with Sleep/Wake

Dev agents (`feat-dev`, `bug-fix`) are the primary case for the Copilot SDK's session persistence.

**Session ID convention:** `squadron-{role}-issue-{N}` (e.g., `squadron-feat-dev-issue-42`)

**Lifecycle:**

```
1. CREATED — PM assigns issue
   └── Agent Manager: create CopilotClient + create_session()
       └── System prompt: agent definition + issue context + repo context
       └── Custom tools: check_for_events, report_blocked, report_complete, github_*
       └── Initial prompt: "Implement the feature described in issue #42."

2. ACTIVE — Agent works autonomously
   └── Agent: reads code, creates branch, implements, writes tests, pushes, opens PR
   └── Agent: periodically calls check_for_events() between major actions
   └── session.idle → Agent finished current work phase

3a. SLEEPING — Agent reports blocked or awaiting review
    └── Agent called report_blocked(blocker_issue) or opened a PR
    └── Agent Manager: session.destroy() (state saved to disk), client.stop()
    └── Registry: status = SLEEPING
    └── CopilotClient resources freed — no running process

3b. COMPLETED — Agent reports task done
    └── Agent called report_complete(summary)
    └── Agent Manager: session.destroy(), client.delete_session(), client.stop()
    └── Registry: status = COMPLETED
    └── Worktree cleaned up (if PR merged)

4. WAKE — Blocker resolved or PR review received
   └── Agent Manager: create new CopilotClient, resume_session(session_id)
   └── Re-inject BYOK credentials (required on every resume)
   └── Re-register custom tools and hooks
   └── Prompt: "Your blocker #42 was resolved." or "PR review received: [feedback]"
   └── Agent continues from where it left off (full conversation history preserved)
   └── → Back to step 2 (ACTIVE)

5. ESCALATED — Circuit breaker tripped or unrecoverable error
   └── Agent exceeded max retries, max cost, or max time
   └── Agent Manager: session.destroy() (preserve state for debugging)
   └── Registry: status = ESCALATED
   └── PM notified → creates needs-human issue
```

**Key implementation detail:** When resuming a session, we must re-provide:
- BYOK credentials (security: never persisted to disk)
- Custom tool definitions (event handlers are in-process, not serializable)
- Hook handlers (same reason)

```python
async def wake_agent(agent_record: AgentRecord, trigger_event: Event):
    client = CopilotClient({"cwd": get_agent_worktree_path(agent_record)})
    await client.start()
    
    session = await client.resume_session(agent_record.session_id, {
        "provider": get_provider_config(),  # BYOK re-injection
        "tools": get_tools_for_role(agent_record.role),
    })
    
    # Re-register hooks (not persisted across sessions)
    # Note: hooks are set at create_session time via config dict,
    # but on resume they may need to be re-provided
    
    prompt = format_wake_prompt(agent_record, trigger_event)
    
    done = asyncio.Event()
    session.on("session.idle", lambda _: done.set())
    await session.send({"prompt": prompt})
    await done.wait()
    
    # Post-idle: check what the agent decided (via tool calls or registry state)
    await handle_agent_idle(agent_record, session, client)
```

---

### Review Agents — Per-PR Persistent Sessions

Review agents (`pr-review`, `security-review`) are invoked by the approval flow when a PR is opened or updated.

**Session ID convention:** `squadron-{role}-pr-{N}` (e.g., `squadron-pr-review-pr-15`)

**Lifecycle:**
1. PR opened → approval flow config says "need pr-review + security-review"
2. Agent Manager creates one review agent per required review role
3. Review agent receives PR diff, related issue context, review criteria
4. Agent reviews, posts comments, submits review (approve/request-changes)
5. If approved → session can be preserved (for re-review on subsequent pushes)
6. If changes requested → agent goes to SLEEPING, wakes when PR is updated (`pull_request.synchronize`)
7. On wake → resumes with updated diff, checks if previous feedback was addressed

**Why persistent sessions for review agents?**
Resuming gives the reviewer context about what it originally flagged. On re-review, it can verify fixes were made rather than repeating the full review from scratch. This produces better, faster reviews.

---

## Agent-Host Communication: Custom Tools

The bridge between agent LLM sessions and the framework is **custom tools** registered via the Copilot SDK's `@define_tool` decorator. These are the ONLY way the framework and agents interact during agent execution.

### Framework Tools (injected into every agent)

```python
from copilot import define_tool
from pydantic import BaseModel, Field

class CheckEventsParams(BaseModel):
    pass

@define_tool(description="Check for pending framework events (PR feedback, unblock notifications, human messages). Call this between major work phases.")
async def check_for_events(params: CheckEventsParams) -> str:
    agent_id = current_agent_context.agent_id
    events = agent_inboxes[agent_id].get_all()
    if not events:
        return "No pending events."
    return format_events_for_agent(events)


class ReportBlockedParams(BaseModel):
    blocker_issue: int = Field(description="The GitHub issue number that blocks this work")
    reason: str = Field(description="Why this issue blocks your current task")

@define_tool(description="Report that you are blocked on another issue. Your session will be saved and you'll be woken when the blocker is resolved.")
async def report_blocked(params: ReportBlockedParams) -> str:
    agent = current_agent_context.agent_record
    register_blocker(agent.agent_id, params.blocker_issue)
    # Post comment on the agent's issue
    await github.comment_on_issue(
        agent.issue_number,
        f"[squadron:{agent.role}] Blocked by #{params.blocker_issue}: {params.reason}. Going to sleep."
    )
    return "Blocker registered. Your session will be saved. You will be resumed when the blocker is resolved."


class ReportCompleteParams(BaseModel):
    summary: str = Field(description="Summary of what was accomplished")

@define_tool(description="Report that your task is complete. Call after your PR is merged or the issue is resolved.")
async def report_complete(params: ReportCompleteParams) -> str:
    agent = current_agent_context.agent_record
    agent.status = AgentStatus.COMPLETED
    registry.update(agent)
    await github.comment_on_issue(
        agent.issue_number,
        f"[squadron:{agent.role}] Task complete: {params.summary}"
    )
    return "Task marked complete. Session will be cleaned up."


class CreateBlockerIssueParams(BaseModel):
    title: str = Field(description="Issue title")
    body: str = Field(description="Issue body describing the blocker")
    labels: list[str] = Field(default=[], description="Labels to apply")

@define_tool(description="Create a new GitHub issue for a blocker you discovered. The PM will triage it. You will be blocked until it's resolved.")
async def create_blocker_issue(params: CreateBlockerIssueParams) -> str:
    agent = current_agent_context.agent_record
    new_issue = await github.create_issue(
        title=params.title,
        body=f"{params.body}\n\n_Blocking #{agent.issue_number}_",
        labels=params.labels,
    )
    register_blocker(agent.agent_id, new_issue.number)
    return f"Created issue #{new_issue.number}. You are now blocked on it. Going to sleep."
```

### GitHub Tools (role-dependent)

Different roles get different GitHub tool sets:

| Tool | PM | feat-dev | bug-fix | pr-review | security-review |
|---|---|---|---|---|---|
| `create_issue` | ✅ | ✅ (blockers only) | ✅ | ❌ | ❌ |
| `assign_issue` | ✅ | ❌ | ❌ | ❌ | ❌ |
| `label_issue` | ✅ | ❌ | ❌ | ❌ | ❌ |
| `comment_on_issue` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `create_branch` | ❌ | ✅ | ✅ | ❌ | ❌ |
| `push_commits` | ❌ | ✅ | ✅ | ❌ | ❌ |
| `open_pr` | ❌ | ✅ | ✅ | ❌ | ❌ |
| `submit_pr_review` | ❌ | ❌ | ❌ | ✅ | ✅ |
| `merge_pr` | ✅ | ❌ | ❌ | ✅ (if configured) | ❌ |
| `post_status_check` | ✅ | ❌ | ❌ | ✅ | ✅ |

**Enforcement:** The `on_pre_tool_use` hook validates every tool call against the agent's role before execution. This implements the framework layer of AD-016's dual-layer enforcement.

### Built-In Copilot Tools

Agents also have access to the Copilot CLI's built-in tools, filtered by role:

| Tool | Dev agents | Review agents | PM |
|---|---|---|---|
| `read_file` | ✅ | ✅ | ✅ |
| `write_file` | ✅ | ❌ | ❌ |
| `bash` (shell) | ✅ (restricted) | ❌ | ❌ |
| `grep` / `ripgrep` | ✅ | ✅ | ✅ |
| `view` (images) | ✅ | ✅ | ❌ |

Shell access for dev agents is restricted via the `on_pre_tool_use` hook:
- Allowed: `git`, `pytest`, `npm test`, `cargo test`, package managers
- Denied: `curl`, `wget`, network tools, `rm -rf /`, destructive commands
- Pattern: allowlist of command prefixes, deny everything else

---

## Working Directory Strategy: git worktree

Multiple agents work on different branches concurrently. They need isolated working directories.

### Approach: git worktree

```
/workspace/                     # Server's working directory
├── .squadron-data/             # Framework state (not in git)
│   ├── registry.db             # SQLite agent registry
│   └── logs/                   # Agent execution logs
│
├── main/                       # Primary clone (main branch)
│   ├── .squadron/              # Config directory (read from here)
│   └── src/...                 # Project source
│
└── worktrees/                  # One per active agent
    ├── issue-42/               # git worktree → feat/issue-42 branch
    │   └── (full working tree)
    ├── issue-99/               # git worktree → fix/issue-99 branch
    │   └── (full working tree)
    └── pr-15/                  # git worktree → (PR's source branch)
        └── (full working tree, read-only for reviewers)
```

**Why git worktree?**
- **Shared .git directory:** All worktrees share one `.git` object store. Efficient disk usage.
- **Isolated files:** Each agent has its own file tree. No conflicts during concurrent edits.
- **Standard git:** Push, pull, rebase all work normally from a worktree.
- **Simple cleanup:** `git worktree remove issue-42` cleans up everything.

**Agent worktree lifecycle:**

```python
async def create_agent_worktree(agent_record: AgentRecord) -> str:
    branch = f"{role_prefix(agent_record.role)}/issue-{agent_record.issue_number}"
    worktree_path = f"/workspace/worktrees/issue-{agent_record.issue_number}"
    
    # Create branch from main
    await run_git("branch", branch, "origin/main", cwd="/workspace/main")
    
    # Create worktree
    await run_git("worktree", "add", worktree_path, branch, cwd="/workspace/main")
    
    return worktree_path

async def cleanup_agent_worktree(agent_record: AgentRecord):
    worktree_path = f"/workspace/worktrees/issue-{agent_record.issue_number}"
    await run_git("worktree", "remove", worktree_path, cwd="/workspace/main")
```

**CopilotClient `cwd`:** Each agent's CopilotClient is created with `cwd` set to its worktree path. The CLI process operates in that directory, and the agent's tools (file read/write, git, shell) all execute relative to the worktree.

---

## Server Crash Recovery

On unexpected server termination, the following state survives:

| Component | Survives? | Recovery |
|---|---|---|
| Agent Registry (SQLite) | ✅ Yes | Read on startup, intact |
| SDK session state (disk) | ✅ Yes | `resume_session()` works |
| Event Queue (memory) | ❌ No | Reconciliation loop catches missed events |
| Agent Inboxes (memory) | ❌ No | Agents re-check GitHub state on wake |
| Active CopilotClients | ❌ No | CLI subprocesses die with parent |
| git worktrees | ✅ Yes | On disk, intact |

**Recovery procedure:**

```
1. Server starts up
2. Load config from .squadron/ in main worktree
3. Open SQLite database
4. Query agents WHERE status = ACTIVE
   └── These were mid-execution when crash occurred
   └── Mark all as SLEEPING (conservative — let reconciliation re-evaluate)
   └── Their session state is on disk — can be resumed when needed
5. Start Event Router + Reconciliation Loop
6. Reconciliation loop (first run within 5 min):
   └── Checks all SLEEPING agents' blockers against GitHub
   └── Wakes any whose blockers are resolved
   └── Detects any issues that changed state during downtime
```

This is acceptable for a prototype. No data is permanently lost — the reconciliation loop is the catch-all safety net.

---

## Technology Stack

| Component | Technology | Rationale |
|---|---|---|
| **Web framework** | FastAPI + uvicorn | Async-native, excellent request validation, background task support, ASGI |
| **Agent runtime** | Copilot SDK Python client | Chosen in AD-003 |
| **Database** | SQLite (WAL mode) | Embedded, zero-dependency, single-writer (perfect for monolith) |
| **HTTP client** | httpx (async) | Modern async HTTP, connection pooling, retry support |
| **Config parsing** | PyYAML + Pydantic | YAML config files validated by Pydantic models |
| **Process model** | asyncio | Single event loop, cooperative multitasking, native to SDK |

**No external infrastructure dependencies for V1.** No Redis, no Postgres, no message broker, no container runtime.

### Python Package Dependencies

```
# Core
fastapi>=0.100
uvicorn[standard]>=0.20
copilot-sdk  # GitHub Copilot SDK Python client
httpx>=0.24
pyyaml>=6.0
pydantic>=2.0

# Standard library (no install needed)
# sqlite3, asyncio, json, hashlib, hmac, logging
```

---

## Migration Path to Containers (Phase 3)

The monolith is designed for clean decomposition:

```
V1 (Monolith)                          Phase 3 (Containerized)
─────────────                          ─────────────────────────

┌─────────────────────┐                ┌─────────────────────┐
│ Squadron Server     │                │ Squadron Orchestrator│
│ (all components)    │                │ (Webhook, Router,    │
│                     │                │  Registry, GH Client)│
│ ┌─────┐ ┌─────┐    │                └──────────┬───────────┘
│ │Dev-1│ │Dev-2│    │                           │ HTTP/gRPC
│ └─────┘ └─────┘    │                ┌──────────┼──────────┐
└─────────────────────┘                ▼          ▼          ▼
                                 ┌─────────┐┌─────────┐┌─────────┐
                                 │ Agent   ││ Agent   ││ Agent   │
                                 │ Container│ Container│ Container
                                 │ (Dev-1) ││ (Dev-2) ││ (Review)│
                                 └─────────┘└─────────┘└─────────┘
```

**What changes:**
1. Agent inboxes: in-memory dict → HTTP endpoint on each container
2. Custom tools: in-process function calls → HTTP calls to orchestrator
3. Agent Manager: direct CopilotClient creation → Docker API / k8s pod creation
4. Session state: local disk → shared volume (NFS/Azure Files)
5. Agent registry: SQLite → PostgreSQL (multi-writer support)

**What stays the same:**
- Agent code (system prompts, tool definitions, workflow logic)
- Event routing logic
- Agent registry schema and queries
- GitHub API interactions
- Approval flow evaluation

---

## Summary of Decisions

| Decision | Choice | Key Rationale |
|---|---|---|
| V1 architecture | Single-process monolith | Simplest for prototyping; clean migration path |
| Agent isolation | One CopilotClient per agent | Crash isolation via CLI subprocesses; maps to containers |
| PM lifecycle | Fresh session per event batch | PM is stateless; context is external (registry + GitHub) |
| Dev/Review lifecycle | Persistent sessions with sleep/wake | Conversation memory is the value; SDK handles persistence |
| Agent-host communication | Custom `@define_tool` tools | SDK's native extensibility; type-safe with Pydantic |
| Working directories | git worktree | Concurrent branch isolation; shared .git objects |
| Web framework | FastAPI + uvicorn | Async-native; fits Python SDK ecosystem |
| Database | SQLite WAL | Embedded; single-writer; zero infrastructure |
| Crash recovery | Mark ACTIVE → SLEEPING, reconcile | Conservative; no data loss; acceptable for prototype |

---

## Open Questions (Minor — resolve during prototyping)

1. **PM event batching window:** How long should the PM wait to batch events before processing? Immediate (no batching) vs. short delay (e.g., 2 seconds to collect related events)?
2. **Agent worktree pre-creation:** Should worktrees be created lazily (on first branch access) or eagerly (at agent registration)?
3. **Copilot CLI binary distribution:** How is the CLI binary deployed to the server? System install, bundled, or downloaded at startup?
4. **Log aggregation:** Per-agent log files or centralized structured logging? (Structured logging with agent_id tags is probably better.)
5. **Health check endpoint:** Should the server expose a `/health` endpoint for monitoring? (Yes, trivially — FastAPI makes this easy.)
