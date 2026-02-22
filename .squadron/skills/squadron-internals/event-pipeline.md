# Event Pipeline

> **AD-019 MIGRATION NOTE:** The unified pipeline system (see `docs/design/unified-pipeline-system.md`) replaces the current trigger-based dispatch. After the refactor, the flow changes to: **GitHub Webhook → FastAPI Server → EventRouter → PipelineEngine → AgentManager**. The EventRouter will emit events to the PipelineEngine, which evaluates pipeline trigger conditions and advances pipeline stages. Direct `AgentManager._handle_*` trigger-matching methods will be deleted. The `AgentManager` becomes a pure agent lifecycle manager (create/wake/complete/sleep) invoked by pipeline stage execution, not by event dispatch.

## Overview

Squadron's event pipeline routes GitHub webhook events to the appropriate agents.
The flow is: **GitHub Webhook → FastAPI Server → EventRouter → AgentManager**.

## Entry Point: Webhook Handler

**File:** `src/squadron/webhook.py`

The FastAPI app exposes `POST /webhook` which:
1. Validates the GitHub HMAC signature (`X-Hub-Signature-256` header)
2. Parses the raw JSON payload into a `GitHubEvent` (from `src/squadron/models.py`)
3. Puts it onto the `EventRouter`'s internal asyncio queue

```python
# webhook.py — simplified
@app.post("/webhook")
async def github_webhook(request: Request):
    payload = await request.json()
    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event = GitHubEvent(event_type=event_type, payload=payload, delivery_id=delivery_id)
    await event_router.emit(event)
    return {"ok": True}
```

## EventRouter (`src/squadron/event_router.py`)

**Class:** `EventRouter` (line 66)

The `EventRouter`:
1. Maintains an `asyncio.Queue` of incoming `GitHubEvent` objects
2. Runs a `_consumer_loop` coroutine that drains the queue
3. For each event, calls `_route_event` which:
   - Converts `GitHubEvent` → `SquadronEvent` via `_to_squadron_event()` (line 157)
   - Checks if it's a command comment (`_is_command_comment`, line 221)
   - If command: calls `_handle_command()` (line 241)
   - Otherwise: calls `_dispatch()` (line 276) which invokes all registered handlers

### Registering Handlers

```python
router.on(SquadronEventType.ISSUE_LABELED, agent_manager.handle_issue_labeled)
```

`AgentManager` registers handlers in `start()`.

## SquadronEvent Types (`src/squadron/models.py`, line 203)

Key event types the router emits:
- `ISSUE_OPENED`, `ISSUE_LABELED`, `ISSUE_REOPENED`
- `PR_OPENED`, `PR_SYNCHRONIZED`, `PR_CLOSED`
- `PR_REVIEW_SUBMITTED`
- `ISSUE_COMMENT_CREATED`
- `COMMAND` (from `@squadron-dev <command>` mentions)

## AgentManager Dispatch (`src/squadron/agent_manager.py`)

> **LEGACY — will be removed by AD-019.** The trigger-matching dispatch below is replaced by the PipelineEngine. After the refactor, `AgentManager` no longer reads triggers from config or has `_handle_*` event methods. Instead, the PipelineEngine calls `AgentManager.create_agent()` / `wake_agent()` / etc. directly when pipeline stages execute.

The `AgentManager` handles events by:
1. Looking up matching `AgentRoleConfig` entries from `config.agent_roles`
2. For each matching role trigger, calling `create_agent()`, `wake_agent()`, `complete_agent()`, or `sleep_agent()`

The main dispatch logic is in `_handle_*` methods throughout `agent_manager.py`.

## Self-Event Filtering

Events from the bot's own GitHub App account are filtered out to prevent
infinite loops. The filter checks `event.sender == config.project.bot_username`
(configured as `squadron-dev[bot]`).
