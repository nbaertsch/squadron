# Async Patterns

## Core Pattern

Squadron is an async-first codebase. Almost all I/O-bound operations use `async/await`.

**Standard pattern:**
```python
async def my_function() -> SomeType:
    result = await some_async_operation()
    return result
```

## asyncio.Queue

Used for event routing and agent inboxes:
```python
queue: asyncio.Queue[SquadronEvent] = asyncio.Queue()
await queue.put(event)
event = await queue.get()
```

## asyncio.create_task

Long-running agent tasks are created with `create_task`:
```python
task = asyncio.create_task(
    self._run_agent(record, trigger_event),
    name=f"agent-{agent_id}",
)
self._agent_tasks[agent_id] = task
```

**Always name tasks** — makes debugging easier.

## asyncio.Semaphore

Used for agent concurrency control:
```python
self._agent_semaphore = asyncio.Semaphore(max_concurrent_agents)
await self._agent_semaphore.acquire()
# ... do work ...
self._agent_semaphore.release()
```

## Exception Handling in Tasks

Tasks that raise exceptions don't propagate unless awaited. Always wrap task bodies:
```python
async def _run_agent(self, record, event):
    try:
        await self._do_agent_work(record, event)
    except Exception as e:
        logger.exception("Agent %s failed: %s", record.agent_id, e)
        await self._mark_agent_failed(record.agent_id)
```

## Common Async Pitfalls

### 1. Forgetting `await`
```python
# BAD — coroutine is created but never awaited
result = some_async_function()

# GOOD
result = await some_async_function()
```

### 2. Blocking the event loop
Never call blocking I/O in async code:
```python
# BAD — blocks event loop
time.sleep(1)
data = open("file").read()

# GOOD
await asyncio.sleep(1)
async with aiofiles.open("file") as f:
    data = await f.read()
```

### 3. asyncio.gather vs sequential awaits
Use `gather` for concurrent independent operations:
```python
# Sequential (slow if independent)
a = await fetch_a()
b = await fetch_b()

# Concurrent (faster)
a, b = await asyncio.gather(fetch_a(), fetch_b())
```

### 4. Task cancellation
When stopping agents, cancel tasks gracefully:
```python
task.cancel()
try:
    await task
except asyncio.CancelledError:
    pass  # Expected
```

## Database Operations

All SQLite operations via `aiosqlite` are async:
```python
async with aiosqlite.connect(self.db_path) as db:
    await db.execute("INSERT INTO agents ...", values)
    await db.commit()
```
