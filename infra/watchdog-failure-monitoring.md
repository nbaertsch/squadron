# Watchdog Failure Monitoring Implementation

## Problem Analysis

**Issue #53**: Agent `pr-review-issue-51` exceeded max active duration of 1800s by 217s.
**Root Cause**: Primary watchdog (layer 1) failed to fire, reconciliation loop (layer 3) caught the timeout.

## Evidence from Code Analysis

1. **Timeout Configuration**: pr-review agents have `max_active_duration: 1800` (30 minutes)
2. **Watchdog Logic**: `_duration_watchdog()` uses `asyncio.sleep(max_seconds)` then cancels agent
3. **Failure Mode**: Watchdog timer fires but agent cleanup operations exceed the 30s `CLEANUP_TIMEOUT`

## Infrastructure Fixes Required

### 1. Enhanced Watchdog Failure Detection
Add monitoring to detect when reconciliation catches timeouts vs primary watchdog:

```python
# In reconciliation.py - track timeout enforcement layer
if overage > 60:  # Significant overage indicates watchdog failure
    escalation_labels.append("watchdog-failure")
    logger.warning("Primary watchdog failed - overage %ds indicates layer 1 timeout missed", overage)
```

### 2. Watchdog Health Monitoring
Track watchdog task health and failures:

```python  
# In agent_manager.py - monitor watchdog task state
async def _monitor_watchdog_health(self):
    for agent_id, watchdog_task in self._watchdog_tasks.items():
        if watchdog_task.done():
            exception = watchdog_task.exception()
            if exception:
                logger.error("Watchdog task failed for %s: %s", agent_id, exception)
```

### 3. Improved Cleanup Timeout Handling
For pr-review agents specifically, extend cleanup timeout due to complex Git operations:

```python
# Role-specific cleanup timeouts
CLEANUP_TIMEOUTS = {
    "pr-review": 60,     # Git operations can take time
    "infra-dev": 90,     # Complex infrastructure operations  
    "default": 30
}
```

### 4. Watchdog Task Recovery
Implement automatic watchdog restart if tasks fail:

```python
async def _ensure_watchdog_health(self, agent_id: str, role: str):
    watchdog = self._watchdog_tasks.get(agent_id)
    if watchdog and watchdog.done() and watchdog.exception():
        logger.warning("Restarting failed watchdog for %s", agent_id) 
        self._start_watchdog(agent_id, role)
```

## Implementation Priority

1. **Immediate**: Add watchdog failure detection to reconciliation loop
2. **Short-term**: Implement role-specific cleanup timeouts
3. **Medium-term**: Add watchdog health monitoring and recovery
4. **Long-term**: Enhanced observability and alerting

## Expected Outcomes

- Reduced watchdog failures for pr-review agents
- Better visibility into timeout enforcement layers
- Automatic recovery from transient watchdog issues  
- More reliable agent lifecycle management
