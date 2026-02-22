# Agent Lifecycle

## State Machine

**Defined in:** `src/squadron/models.py`, class `AgentStatus` (line 15)

```
CREATED → ACTIVE → SLEEPING → ACTIVE → ...
                ↓
            COMPLETED
                ↓
            FAILED (unhandled exception or timeout)
                ↓
            ESCALATED (human intervention needed)
```

## States

| State | Meaning | Who sets it |
|-------|---------|-------------|
| `CREATED` | Agent record created, not yet running | `create_agent()` in `agent_manager.py` |
| `ACTIVE` | Agent is running (LLM session active) | `create_agent()`, `wake_agent()` |
| `SLEEPING` | Agent called `report_blocked` — session preserved | `_handle_report_blocked()` tool |
| `COMPLETED` | Agent called `report_complete` — session destroyed | `_handle_report_complete()` tool |
| `FAILED` | Unhandled exception or timeout | Watchdog / exception handler in `_run_agent()` |
| `ESCALATED` | Agent called `escalate_to_human` | `escalate_to_human` tool |

## AgentRecord (`src/squadron/models.py`, line 34)

Fields stored in SQLite registry:
- `agent_id`: `"{role}-issue-{number}"` (e.g. `feat-dev-issue-42`)
- `role`: agent role name
- `issue_number`: the GitHub issue being worked on
- `session_id`: SDK session ID (`"squadron-{agent_id}"`)
- `status`: current `AgentStatus`
- `branch`: git branch name
- `worktree_path`: filesystem path to git worktree (None for ephemeral agents)
- `pr_number`: associated PR number (set after PR opened)
- `active_since`, `sleeping_since`: timestamps

## Transitions

### CREATED → ACTIVE
- Triggered by: `create_agent()` in `agent_manager.py`
- Actions: creates git worktree, starts `CopilotAgent` subprocess, starts `_run_agent` task

### ACTIVE → SLEEPING
- Triggered by: agent calling `report_blocked` tool
- Actions: marks agent as sleeping, suspends SDK session (state preserved by SDK), removes from active tasks
- Wake condition: the blocker issue is resolved and triggers a wake event

### SLEEPING → ACTIVE
- Triggered by: `wake_agent()` in `agent_manager.py`
- Actions: recreates `CopilotAgent` if needed (server restart), resumes SDK session

### ACTIVE → COMPLETED
- Triggered by: agent calling `report_complete` tool
- Actions: destroys SDK session, frees worktree (for stateful agents), removes from registry

### ACTIVE → FAILED
- Triggered by: unhandled exception in `_run_agent()`, or circuit breaker timeout (watchdog)
- Actions: marks agent failed, logs error, notifies escalation target

### ACTIVE → ESCALATED
- Triggered by: agent calling `escalate_to_human` tool
- Actions: adds `needs-human` label, posts comment to issue, preserves agent state

## Lifecycle Types

Defined in `AgentRoleConfig.lifecycle`:
- `persistent` (default): agent stays alive across events, uses worktrees
- `ephemeral`: one-shot execution, no worktree, unique ID with timestamp suffix
- `stateful`: synonym for persistent (kept for config compatibility)

## Circuit Breakers

Enforced by the watchdog started in `_start_watchdog()`:
- `max_active_duration`: seconds before forceful stop (default 7200)
- `max_iterations`: sleep→wake cycles allowed (default 5)
- `max_tool_calls`: total SDK tool calls (default 200, Layer 1 hook counting)
- `max_turns`: conversation turns (default 50)

Per-role overrides in `circuit_breakers.roles` in `config.yaml`.
