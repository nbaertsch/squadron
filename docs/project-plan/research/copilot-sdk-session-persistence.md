# Research: Copilot SDK Session Persistence

**Date:** 2026-02-14  
**Relates to:** OR-001 (Framework Selection), AD-003 (Sleep/Wake Lifecycle)  
**Sources:** [session-persistence.md](https://github.com/github/copilot-sdk/blob/main/docs/guides/session-persistence.md), [compatibility.md](https://github.com/github/copilot-sdk/blob/main/docs/compatibility.md), [Node.js README](https://github.com/github/copilot-sdk/blob/main/nodejs/README.md), [Python README](https://github.com/github/copilot-sdk/blob/main/python/README.md), [PR #427](https://github.com/github/copilot-sdk/pull/427)

---

## Executive Summary

The Copilot SDK has a **built-in, opaque, file-based session persistence system** managed entirely by the Copilot CLI binary. Sessions are automatically checkpointed to disk and can be resumed by ID across process restarts, container migrations, and client instances. The SDK exposes lifecycle controls but the checkpoint format is internal to the CLI — you cannot directly read, write, or manipulate conversation history at the message level.

**Key implication for Squadron:** The sleep/wake lifecycle (AD-003) maps cleanly onto `create_session()` → `destroy()` → `resume_session()`, but we do NOT get raw message array access. Context is a black box managed by the CLI.

---

## Architecture: How It Works

```
┌──────────────────────┐        JSON-RPC        ┌──────────────────────┐
│   Squadron Agent     │ ◄────────────────────► │  Copilot CLI Binary  │
│   (Python SDK)       │                         │  (server mode)       │
│                      │                         │                      │
│  create_session()    │ ──────────────────────► │  Allocates session   │
│  send_and_wait()     │ ──────────────────────► │  Runs agent loop     │
│  resume_session()    │ ──────────────────────► │  Loads from disk     │
│  get_messages()      │ ◄────────────────────── │  Returns events[]    │
│  destroy()           │ ──────────────────────► │  Saves final state   │
└──────────────────────┘                         └──────────┬───────────┘
                                                            │
                                                            ▼
                                                 ~/.copilot/session-state/
                                                 └── {sessionId}/
                                                     ├── checkpoints/
                                                     │   ├── 001.json
                                                     │   ├── 002.json
                                                     │   └── ...
                                                     ├── plan.md
                                                     └── files/
                                                         ├── analysis.md
                                                         └── notes.txt
```

---

## What Gets Persisted vs What Doesn't

| Data | Persisted? | Notes |
|---|---|---|
| Conversation history (full message thread) | ✅ Yes | Automatic, in `checkpoints/` |
| Tool call results | ✅ Yes | Cached for context continuity |
| Agent planning state | ✅ Yes | `plan.md` file |
| Session artifacts (files agent created) | ✅ Yes | In `files/` directory |
| Session context (cwd, repo, branch) | ✅ Yes | New in PR #427, available via `list_sessions()` |
| Provider / API keys | ❌ No | Security: must re-provide on resume |
| In-memory tool state | ❌ No | Tools must be stateless or self-persist |
| Custom hooks / event handlers | ❌ No | Re-register on resume |

---

## Session Lifecycle API

### Creating a Resumable Session

The key is providing a **named session ID**. Without one, a random ID is generated and the session cannot be resumed later.

```python
from copilot import CopilotClient

client = CopilotClient()
await client.start()

session = await client.create_session({
    "session_id": "squadron-dev-agent-issue-42",
    "model": "gpt-5",
    "system_message": {"content": "<agent_role>Feature Developer</agent_role>"},
})

await session.send_and_wait({"prompt": "Analyze the codebase for issue #42"})
# State is automatically persisted to disk
```

### Resuming a Session (Sleep → Wake)

```python
# Minutes, hours, or days later...
session = await client.resume_session("squadron-dev-agent-issue-42")

# Agent has full conversation history — continues where it left off
await session.send_and_wait({"prompt": "The blocking PR was merged. Continue implementation."})
```

### Resume with Reconfiguration

When resuming, you can change many settings. This is powerful for Squadron — e.g., switching the model, adjusting reasoning effort, or re-providing BYOK credentials:

```python
session = await client.resume_session("squadron-dev-agent-issue-42", {
    "model": "claude-sonnet-4",       # Switch model mid-workflow
    "reasoning_effort": "high",        # Increase reasoning 
    "provider": {                      # Re-provide BYOK credentials (required)
        "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": os.environ["ANTHROPIC_KEY"],
    },
})
```

Full list of resume-time reconfigurable settings:

| Setting | Description |
|---|---|
| `model` | Change the model |
| `systemMessage` | Override/extend the system prompt |
| `availableTools` / `excludedTools` | Restrict which tools are available |
| `provider` | Re-provide BYOK credentials (required for BYOK sessions) |
| `reasoningEffort` | Adjust reasoning effort (`low`, `medium`, `high`, `xhigh`) |
| `streaming` | Enable/disable streaming |
| `workingDirectory` | Change working directory |
| `mcpServers` | Configure MCP servers |
| `customAgents` | Configure custom agents |
| `infiniteSessions` | Configure compaction behavior |

---

## Retrieving Session History: `get_messages()`

```python
events = await session.get_messages()  # Returns SessionEvent[]
```

This returns **all events/messages** from the session as a list of `SessionEvent` objects. Event types include:

- `user.message` — User prompts sent
- `assistant.message` — Assistant responses
- `assistant.message_delta` — Streaming chunks
- `tool.execution_start` — Tool invocation started
- `tool.execution_complete` — Tool invocation completed
- `session.idle` — Session finished processing
- `session.compaction_start` / `session.compaction_complete` — Context compaction events
- `session.context_changed` — Working directory/branch changed (new in PR #427)

**Important:** This returns *events*, not raw LLM API messages. You can observe the conversation but cannot inject arbitrary messages into the history. The context is opaque — the CLI manages the actual message array sent to the LLM.

---

## Session Context & Filtering (NEW — PR #427, merged yesterday)

Sessions now track **working directory context** automatically:

```python
from copilot import SessionListFilter

# List all sessions
sessions = await client.list_sessions()

# Filter by repository
sessions = await client.list_sessions(SessionListFilter(repository="owner/repo"))

# Filter by branch
sessions = await client.list_sessions(SessionListFilter(branch="feat/issue-42"))

# Each session metadata includes:
for s in sessions:
    print(s.session_id)
    print(s.context.cwd)          # Working directory
    print(s.context.git_root)     # Git repo root
    print(s.context.repository)   # "owner/repo" format
    print(s.context.branch)       # Current branch
```

**`session.context_changed` event** — fires when the agent switches branches or changes cwd:

```python
session.on("session.context_changed", lambda event: 
    print(f"Context changed: {event.data}")
    # { cwd, gitRoot?, repository?, branch? }
)
```

**Squadron relevance:** This is extremely useful. We can:
- List all active agent sessions for a given repository
- Filter sessions by branch (each agent works on its own branch per AD-004)
- Detect when an agent switches branches (collision detection)

---

## Infinite Sessions & Context Compaction

By default, sessions use **infinite sessions** — the CLI automatically compacts context when the context window fills up, summarizing older conversation into a compact representation. This is critical for long-running agent tasks.

```python
session = await client.create_session({
    "session_id": "squadron-dev-agent-issue-42",
    "model": "gpt-5",
    "infinite_sessions": {
        "enabled": True,
        "background_compaction_threshold": 0.80,    # Start compacting at 80% context usage
        "buffer_exhaustion_threshold": 0.95,         # Block at 95% until compaction completes
    },
})

# The workspace path for checkpoints/files:
print(session.workspace_path)
# → ~/.copilot/session-state/squadron-dev-agent-issue-42/
```

Compaction events:
- `session.compaction_start` — Background compaction started
- `session.compaction_complete` — Compaction finished (includes token counts)

**Squadron relevance:** Agents working on large issues may have long conversations. Infinite sessions prevent context overflow without us building compaction logic.

---

## Session Management & Cleanup

```python
# List all sessions (with optional filtering)
sessions = await client.list_sessions()

# Delete a specific session
await client.delete_session("squadron-dev-agent-issue-42")

# Destroy active session (frees resources immediately)
await session.destroy()
```

**Auto-cleanup:** The CLI has a built-in **30-minute idle timeout**. Sessions without activity are automatically cleaned up.

**Session ID best practices** (from docs):
- Use structured IDs encoding ownership and purpose
- `squadron-{role}-{issue_number}` → e.g., `squadron-dev-issue-42`
- `squadron-{role}-{issue_number}-{timestamp}` → for time-based cleanup
- Parse agent role and issue from ID for auditing

---

## Deployment Patterns

### Pattern 1: One CLI Server Per Agent (Recommended for Squadron)

Each agent gets its own CLI server process. Strong isolation.

```
┌─ PM Agent ──────────┐     ┌─ Dev Agent 1 ────────┐     ┌─ Dev Agent 2 ────────┐
│  CopilotClient()    │     │  CopilotClient()      │     │  CopilotClient()      │
│  CLI Process A       │     │  CLI Process B         │     │  CLI Process C         │
│  Session: pm-main    │     │  Session: dev-issue-42 │     │  Session: dev-issue-99 │
└──────────────────────┘     └────────────────────────┘     └────────────────────────┘
```

Benefits: ✅ Complete isolation, ✅ Simple security, ✅ Easy scaling  
Cost: One CLI process per agent (memory overhead)

### Pattern 2: Shared CLI Server (Resource Efficient)

One CLI process, multiple sessions. Requires application-level access control.

```python
# Application-level access control
async def resume_agent_session(client, session_id, requesting_role):
    # Parse role from session ID
    role = session_id.split("-")[1]  # "squadron-dev-issue-42" → "dev"
    if role != requesting_role:
        raise PermissionError(f"Role {requesting_role} cannot access {role} session")
    return await client.resume_session(session_id)
```

### Containerized Deployment (Azure Dynamic Sessions)

Mount persistent storage for session state survival across container restarts:

```yaml
# Azure Container Instance
containers:
  - name: squadron-agent
    image: squadron-agent:latest
    volumeMounts:
      - name: session-storage
        mountPath: /home/app/.copilot/session-state
volumes:
  - name: session-storage
    azureFile:
      shareName: squadron-sessions
      storageAccountName: myaccount
```

---

## Concurrency: No Built-In Locking

**The SDK does NOT provide session locking.** If two processes try to resume the same session, behavior is undefined. The SDK docs suggest application-level locking (e.g., Redis `NX` locks), but:

**Squadron decision:** We do NOT need a session-level locking system. Each agent gets a unique session ID keyed by role + issue (e.g., `squadron-dev-issue-42`), so concurrent access to the *same* session shouldn't normally occur. The only realistic scenario is **accidental double-dispatch** — a webhook fires twice and two processes try to wake the same agent.

This is a **dispatch-level problem**, not a session-level problem. The fix is **idempotent event handling in the orchestrator** (PM agent or event router): check "is this agent already active for this issue?" before dispatching. No Redis locks needed for V1.

---

## Hooks: Session Lifecycle Interception

The hooks system provides deep interception points relevant to Squadron's orchestration:

| Hook | Fires When | Squadron Use |
|---|---|---|
| `onPreToolUse` | Before each tool execution | Permission enforcement: deny shell commands, restrict file access |
| `onPostToolUse` | After each tool execution | Audit logging, result validation |
| `onUserPromptSubmitted` | When a prompt is sent | Prompt injection of workflow context, guardrails |
| `onSessionStart` | Session starts or resumes | Log activation, inject fresh context about issue state |
| `onSessionEnd` | Session ends | Trigger cleanup, update issue status |
| `onErrorOccurred` | Error during processing | Retry/skip/abort strategies, escalation |

```python
session = await client.create_session({
    "session_id": "squadron-dev-issue-42",
    "model": "gpt-5",
    "hooks": {
        "on_pre_tool_use": pre_tool_handler,      # Permission guard
        "on_session_start": on_agent_wakeup,       # Inject fresh context
        "on_session_end": on_agent_sleep,           # Persist state to GitHub
        "on_error_occurred": on_agent_error,        # Escalation logic
    },
})
```

---

## What We CAN'T Do (Limitations)

| Limitation | Description | Workaround |
|---|---|---|
| **No raw message injection** | Cannot manually insert messages into conversation history | Use `systemMessage` or prompt engineering |
| **Opaque checkpoint format** | `checkpoints/*.json` files are CLI-internal, not a public schema | Use `get_messages()` to observe, but can't modify |
| **No session forking** | Cannot clone a session to create parallel branches of conversation | Create separate sessions, inject context via system message |
| **No cross-session context sharing** | Sessions are isolated; no built-in way to share context between agents | Use GitHub issues/PRs as the shared state layer (per AD-001) |
| **No programmatic session export** | `--share` / `--share-gist` are CLI-only | Use `get_messages()` to collect events manually |
| **30-min idle timeout** | Sessions without activity auto-cleanup | Keep-alive mechanism, or just resume by ID |
| **BYOK keys not persisted** | API keys must be re-provided on every resume | Store in secret manager, inject at resume time |

---

## Squadron Design Implications

### Sleep / Wake Lifecycle (AD-003) — SOLVED

```
Agent CREATED  →  create_session(session_id="squadron-{role}-{issue}")
Agent ACTIVE   →  send_and_wait() in a loop, processing issue work
Agent SLEEPING →  session automatically persisted (or explicit destroy())
Agent WAKING   →  resume_session("squadron-{role}-{issue}")
Agent COMPLETED→  destroy() + delete_session()
```

### Branch-Per-Issue (AD-004) — SUPPORTED

The `SessionContext` tracks `branch` and `repository`. We can:
- Set `workingDirectory` to the branch checkout
- Filter sessions by branch via `list_sessions(filter)`
- Detect branch changes via `session.context_changed` event

### Multi-Model Support — SUPPORTED via BYOK

```python
# Dev agent uses Claude for code generation
dev_session = await client.create_session({
    "session_id": "squadron-dev-issue-42",
    "model": "claude-sonnet-4",
    "provider": {"type": "anthropic", "base_url": "...", "api_key": "..."},
})

# Security agent uses GPT for review
sec_session = await client.create_session({
    "session_id": "squadron-security-issue-42",
    "model": "gpt-5",
    "provider": {"type": "openai", "base_url": "...", "api_key": "..."},
})
```

### What We Must Build Ourselves

1. **Idempotent dispatch** — Orchestrator checks "is this agent already active?" before waking/creating, preventing double-dispatch from duplicate webhooks
2. **Cross-agent communication** — Via GitHub issues/comments (per AD-001), not session sharing
3. **Orchestration logic** — PM agent decides when to create/resume/destroy agent sessions
4. **State mapping** — Map GitHub issue state ↔ agent session lifecycle
5. **Secrets management** — Store BYOK API keys, inject on session resume
6. **Monitoring/observability** — Collect `get_messages()` events for dashboards/audit trails

---

## Comparison: Copilot SDK vs Alternative Approaches

| Capability | Copilot SDK | Raw API (Anthropic/OpenAI) | LangGraph |
|---|---|---|---|
| Session persistence | Built-in, file-based | DIY (serialize messages[]) | Built-in (SQLite/Postgres) |
| Context compaction | Built-in (infinite sessions) | DIY | Manual (graph redesign) |
| Resume across restarts | ✅ `resume_session(id)` | ✅ Reload JSON | ✅ Checkpointer |
| Raw message control | ❌ Opaque | ✅ Full control | ✅ Full state access |
| Session forking | ❌ Not supported | ✅ Copy messages[] | ✅ Fork from checkpoint |
| Multi-model | ✅ BYOK | ✅ Any provider | ✅ Any provider |
| Built-in tools | ✅ (Read, Write, Bash, etc.) | ❌ Build yourself | ❌ Build yourself |
| Hooks/lifecycle | ✅ Rich hook system | ❌ Build yourself | ✅ Node callbacks |
| GitHub-native context | ✅ (cwd, repo, branch tracking) | ❌ | ❌ |
| Maturity | Technical Preview | Stable | Stable |

---

## Open Questions

1. **Checkpoint file format stability**: Is the `checkpoints/*.json` schema documented or versioned? If it changes between CLI versions, do resumed sessions break?
2. **Concurrent CLI processes**: Can multiple CLI server processes share the same `~/.copilot/session-state/` directory safely, or do they need separate state dirs?
3. **Session size limits**: Is there a maximum number of checkpoints or total session size on disk?
4. **Compaction visibility**: After infinite session compaction, is the compacted summary available via `get_messages()`, or does the event list get truncated?
5. **CLI binary licensing**: Is the Copilot CLI binary freely distributable for server-side deployment, or does it require a Copilot license per instance?
