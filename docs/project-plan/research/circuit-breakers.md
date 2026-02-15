# Circuit Breaker Design

**Date:** 2026-02-14  
**Relates to:** EC-002, EC-004, Roadmap #13, AD-017  
**Status:** Design Complete

---

## Problem

Without hard limits, an agent can enter infinite loops (fix → test → fail → fix → ...), consume unbounded LLM tokens, or run indefinitely. This is a critical V1 safety requirement.

---

## Limit Types

| Limit | What it prevents | Measured by | Default |
|---|---|---|---|
| **Max iterations** | Infinite retry loops (EC-004) | Count of test-then-fix cycles | 5 |
| **Max tool calls** | Runaway tool usage | `on_pre_tool_use` hook counter | 200 |
| **Max turns** | Unbounded conversation length | Count of `session.send()` calls | 50 |
| **Max active duration** | Time-based runaway | Wall-clock time in ACTIVE state (excludes SLEEPING) | 2 hours |
| **Max sleep duration** | Permanent stalls | Time in SLEEPING state | 24 hours |

### Why Not Track Token Count Directly?

The Copilot SDK's context is opaque — we can observe output tokens via `assistant.message` events but cannot reliably track input tokens (which include the full context window, system prompt, and compacted history). Proxy metrics (tool calls, turns, wall-clock time) are measurable, actionable, and strongly correlated with cost. Exact cost tracking via provider billing APIs is a Phase 3 / V2 enhancement.

---

## Configuration

### Hierarchy (most specific wins)

```
Global defaults (.squadron/config.yaml → circuit_breakers.defaults)
  └── Per-role overrides (.squadron/config.yaml → circuit_breakers.roles.{role})
```

Agent definition files (`.squadron/agents/*.md`) specify limits as **instructions** to the agent (prompt-level self-regulation). The config.yaml circuit breakers are **enforcement** (code-level, via SDK hooks). Both layers are needed — the agent should try to self-limit, but the framework enforces hard limits regardless.

### Configuration Schema

```yaml
# .squadron/config.yaml (circuit_breakers section)

circuit_breakers:
  defaults:
    max_iterations: 5         # test-fix retry cycles before escalation
    max_tool_calls: 200       # total tool invocations per task
    max_turns: 50             # LLM conversation turns per task
    max_active_duration: 7200 # seconds in ACTIVE state (2 hours)
    max_sleep_duration: 86400 # seconds in SLEEPING state (24 hours)
    warning_threshold: 0.80   # warn agent at 80% of any limit

  roles:
    pm:
      max_tool_calls: 50
      max_turns: 10
      max_active_duration: 600    # 10 minutes per event batch
    pr-review:
      max_tool_calls: 100
      max_turns: 20
      max_active_duration: 1800   # 30 minutes
    security-review:
      max_tool_calls: 100
      max_turns: 20
      max_active_duration: 1800   # 30 minutes
    feat-dev:
      # uses defaults
    bug-fix:
      # uses defaults
```

### Default Rationale

| Limit | Default | Reasoning |
|---|---|---|
| 5 iterations | A competent developer rarely needs more than 5 attempts. After 5 failed fix cycles, the approach is likely wrong — human judgment needed. |
| 200 tool calls | Typical coding task: ~10 file reads + ~20 edits + ~10 git ops + ~10 test runs + overhead = ~100 calls. 200 gives 2x headroom. |
| 50 turns | Each turn is a prompt/response cycle. 50 turns is a substantial conversation — enough for a complex feature. |
| 2 hours | A senior developer spending 2 hours on a single issue would reassess. Agents should too. |
| 24 hours | If a blocker hasn't been resolved in 24 hours, it's likely stuck and needs human escalation. |
| 80% warning | Gives agent ~20% of remaining budget to wrap up cleanly. |

---

## Enforcement Mechanism

### Layer 1: SDK Hook Enforcement (hard limits)

The `on_pre_tool_use` hook is the primary enforcement point. It fires before every tool call, giving the framework a chance to deny the call.

```python
class CircuitBreaker:
    """Per-agent circuit breaker tracking and enforcement."""
    
    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.tool_call_count = 0
        self.iteration_count = 0
        self.turn_count = 0
        self.start_time: float | None = None  # Set when agent enters ACTIVE
        self.tripped = False
        self.trip_reason: str | None = None
    
    def on_agent_activated(self):
        self.start_time = time.time()
    
    def on_turn(self):
        """Called when a new prompt is sent to the agent."""
        self.turn_count += 1
    
    def check_pre_tool(self, tool_name: str, tool_args: dict) -> HookResult:
        """Called by on_pre_tool_use hook. Returns allow/deny decision."""
        
        if self.tripped:
            return HookResult(
                permission="deny",
                context="CIRCUIT BREAKER TRIPPED. Please summarize your progress immediately."
            )
        
        self.tool_call_count += 1
        
        # Detect test-fix iterations (heuristic: bash/terminal running test commands)
        if self._is_test_execution(tool_name, tool_args):
            self.iteration_count += 1
        
        # Check all limits
        elapsed = time.time() - self.start_time if self.start_time else 0
        
        checks = [
            (self.tool_call_count, self.config.max_tool_calls, "max tool calls"),
            (self.iteration_count, self.config.max_iterations, "max retry iterations"),
            (self.turn_count, self.config.max_turns, "max conversation turns"),
            (elapsed, self.config.max_active_duration, "max active duration"),
        ]
        
        for current, maximum, name in checks:
            if current >= maximum:
                self.tripped = True
                self.trip_reason = f"{name} ({current}/{maximum})"
                return HookResult(
                    permission="deny",
                    context=f"CIRCUIT BREAKER: {self.trip_reason}. "
                            f"Summarize your progress and call report_complete or report_blocked."
                )
            
            # Warning at threshold
            ratio = current / maximum if maximum > 0 else 0
            if ratio >= self.config.warning_threshold:
                return HookResult(
                    permission="allow",
                    context=f"⚠️ Approaching limit: {name} at {current}/{maximum} "
                            f"({ratio:.0%}). Plan to wrap up soon."
                )
        
        return HookResult(permission="allow")
    
    def _is_test_execution(self, tool_name: str, tool_args: dict) -> bool:
        """Heuristic: detect test runner invocations."""
        if tool_name not in ("bash", "terminal", "run_command"):
            return False
        command = tool_args.get("command", "")
        test_patterns = ["pytest", "npm test", "cargo test", "go test", 
                         "dotnet test", "mvn test", "make test"]
        return any(p in command for p in test_patterns)
```

### Layer 2: Timer Enforcement (wall-clock)

An asyncio timer fires independently of tool calls, catching agents that are "thinking" but not calling tools.

```python
async def agent_timeout_watchdog(agent_record: AgentRecord, 
                                  breaker: CircuitBreaker,
                                  session: Session):
    """Background task that enforces max_active_duration."""
    await asyncio.sleep(breaker.config.max_active_duration)
    
    if agent_record.status != AgentStatus.ACTIVE:
        return  # Agent already finished
    
    # Time's up — send final prompt
    breaker.tripped = True
    breaker.trip_reason = f"max active duration ({breaker.config.max_active_duration}s)"
    
    await session.send({
        "prompt": "TIME LIMIT REACHED. You must stop working now. "
                  "Please summarize what you accomplished and what remains, "
                  "then call report_complete or report_blocked."
    })
    
    # Give agent 60 seconds to wrap up
    try:
        await asyncio.wait_for(wait_for_idle(session), timeout=60)
    except asyncio.TimeoutError:
        pass  # Agent didn't respond in time
    
    # Force escalation
    await escalate_agent(agent_record, breaker.trip_reason)
```

### Layer 3: Reconciliation Enforcement (sleep duration)

Already designed in AD-013 / event-routing.md. The reconciliation loop checks:

```python
# In reconciliation_loop():
stale_agents = registry.query(
    status=SLEEPING,
    updated_at__lt=now() - config.circuit_breakers.max_sleep_duration
)
for agent in stale_agents:
    await escalate_agent(agent, f"max sleep duration exceeded ({config.max_sleep_duration}s)")
```

---

## Escalation Flow on Trip

When any circuit breaker trips:

```
1. Agent denied further tool calls (with explanation context)
2. Agent prompted to summarize progress (via hook context or direct prompt)
3. Wait for agent response (60s timeout)
4. Framework actions:
   a. Mark agent ESCALATED in registry
   b. Post comment on agent's issue:
      "[squadron:{role}] ⚠️ Circuit breaker: {reason}
       Progress: {agent's summary or 'no summary provided'}
       Branch: {branch_name} — work preserved for human pickup."
   c. Post status check: squadron/{role} → failure (with details)
   d. PM agent notified → creates escalation issue:
      
      Title: [needs-human] Agent escalation: #{issue_number} — {reason}
      Body:
      - Original issue: #{issue_number}
      - Agent role: {role}
      - Circuit breaker: {which limit, current/max}
      - Agent summary: {what was accomplished}
      - Branch: {branch_name}
      - Session: preserved on disk ({session_id})
      Labels: needs-human, escalation
```

**Agent work is always preserved:**
- Feature branch with commits remains intact
- Copilot SDK session state is saved to disk
- A human (or fresh agent) can pick up from where the agent left off

---

## Interaction with Agent State Machine

```
                    ┌───────────────┐
                    │    ACTIVE     │
                    └───┬───────┬───┘
                        │       │
         Normal flow    │       │  Circuit breaker trip
         (complete/     │       │
          blocked)      │       │
                        ▼       ▼
                ┌──────────┐ ┌──────────┐
                │SLEEPING/ │ │ESCALATED │
                │COMPLETED │ │          │
                └──────────┘ └──────────┘
```

ESCALATED is a terminal state. The agent does not resume. A human must:
1. Resolve the original issue manually, OR
2. Close the escalation issue and re-assign the original to a fresh agent

---

## What This Does NOT Cover (V2 / Phase 3)

| Feature | Status | Notes |
|---|---|---|
| Exact token/cost tracking | Deferred | Requires provider billing API integration |
| Global cross-agent budget | Deferred | Daily/weekly spend caps across all agents |
| Per-issue cost attribution | Deferred | Roll up cost from all agents that touched an issue |
| Adaptive limits | Deferred | Automatically adjust limits based on task complexity |
| Cost dashboards | Deferred | UI for monitoring agent spending |

For V1, proxy metrics (tool calls, turns, time) provide sufficient cost control. The 80% warning + hard limit pattern catches runaways before they become expensive.

---

## Summary

| Aspect | Decision |
|---|---|
| Limits tracked | Iterations (5), tool calls (200), turns (50), active time (2h), sleep time (24h) |
| Configuration | `.squadron/config.yaml` with global defaults + per-role overrides |
| Enforcement | SDK `on_pre_tool_use` hook + asyncio timer + reconciliation loop |
| Warning | At 80% of any limit — injected via hook `additionalContext` |
| On trip | Deny tools → prompt summary → ESCALATED → needs-human issue |
| Work preservation | Branch, commits, and session state always preserved |
