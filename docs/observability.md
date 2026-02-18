# Real-Time Agent Observability

Squadron provides comprehensive real-time observability into agent operations through activity logging, Server-Sent Events (SSE) streaming, and a web dashboard.

## Overview

The observability system captures and streams agent activity in real-time:

- **Activity Logging**: All agent lifecycle events and tool calls are persisted to SQLite
- **SSE Streaming**: Real-time event streams for individual agents or all agents
- **REST API**: Query historical activity and statistics
- **Dashboard UI**: Web interface for monitoring agent activity

## Architecture

```
                          ┌─────────────────┐
                          │  Dashboard UI   │
                          │  (HTML/JS)      │
                          └────────┬────────┘
                                   │
                          ┌────────▼────────┐
                          │   SSE Stream    │
                          │  /dashboard/    │
                          └────────┬────────┘
                                   │
┌──────────────┐         ┌────────▼────────┐
│ AgentManager │────────►│ ActivityLogger  │
│              │         │    (SQLite)     │
│ ┌──────────┐ │         └────────┬────────┘
│ │  Tools   │─┼────────►         │
│ └──────────┘ │                  │
│              │         ┌────────▼────────┐
│ ┌──────────┐ │         │   Subscribers   │
│ │  Hooks   │─┼────────►│  (async queues) │
│ └──────────┘ │         └─────────────────┘
└──────────────┘
```

## Event Types

### Agent Lifecycle
- `agent_spawned` - Agent created and started
- `agent_woke` - Sleeping agent resumed
- `agent_sleeping` - Agent blocked, waiting for resolution
- `agent_completed` - Agent finished successfully
- `agent_escalated` - Agent escalated to human
- `agent_failed` - Agent failed with error

### Tool Execution
- `tool_call_start` - Tool invocation started
- `tool_call_end` - Tool invocation completed (includes duration, success/failure)

### LLM Interaction
- `reasoning` - LLM output/thinking
- `user_message` - User input to agent

### GitHub Operations
- `github_comment` - Comment posted on issue/PR
- `github_pr_opened` - Pull request created
- `github_review` - PR review submitted
- `github_issue_created` - Issue created

### System Events
- `error` - Error occurred
- `warning` - Warning condition
- `info` - Informational message
- `circuit_breaker_warning` - Approaching limits
- `circuit_breaker_triggered` - Limit exceeded

## API Endpoints

### Dashboard UI

```
GET /dashboard/
```

Opens the web dashboard UI. No authentication required for the UI itself (auth is checked on API calls).

### SSE Streaming

#### Stream All Activity
```
GET /dashboard/stream?token=<api_key>
```

Connect with EventSource:
```javascript
const es = new EventSource('/dashboard/stream?token=YOUR_KEY');
es.addEventListener('activity', (e) => {
    const event = JSON.parse(e.data);
    console.log(event);
});
```

Event types:
- `connected` - Initial connection confirmed
- `activity` - Agent activity event
- `heartbeat` - Keep-alive ping (every 30s)

#### Stream Single Agent
```
GET /dashboard/agents/{agent_id}/stream?token=<api_key>
```

Same format as above, filtered to one agent.

### REST Queries

#### List Agents
```
GET /dashboard/agents
Authorization: Bearer <api_key>
```

Response:
```json
{
    "active_count": 2,
    "active_agents": [
        {
            "agent_id": "feat-dev-issue-42",
            "role": "feat-dev",
            "status": "active",
            "issue_number": 42,
            "pr_number": null,
            "branch": "feat/issue-42",
            "active_since": "2024-01-15T10:30:00Z",
            "tool_call_count": 15,
            "iteration_count": 0
        }
    ],
    "recent_agents": [...]
}
```

#### Agent Activity
```
GET /dashboard/agents/{agent_id}/activity?limit=100&offset=0&event_types=tool_call_start,tool_call_end
Authorization: Bearer <api_key>
```

Response:
```json
{
    "agent_id": "feat-dev-issue-42",
    "count": 50,
    "offset": 0,
    "events": [
        {
            "id": 123,
            "event_type": "tool_call_end",
            "timestamp": "2024-01-15T10:35:00Z",
            "tool_name": "bash",
            "tool_success": true,
            "tool_duration_ms": 150,
            "issue_number": 42
        }
    ]
}
```

#### Agent Statistics
```
GET /dashboard/agents/{agent_id}/stats
Authorization: Bearer <api_key>
```

Response:
```json
{
    "agent_id": "feat-dev-issue-42",
    "total_events": 150,
    "tool_calls": 45,
    "errors": 2,
    "avg_tool_duration_ms": 125.5,
    "first_activity": "2024-01-15T10:30:00Z",
    "last_activity": "2024-01-15T11:45:00Z"
}
```

#### Recent Activity (All Agents)
```
GET /dashboard/activity?limit=100&event_types=agent_spawned,agent_completed
Authorization: Bearer <api_key>
```

#### Status
```
GET /dashboard/status
Authorization: Bearer <api_key>
```

Response:
```json
{
    "status": "ok",
    "activity_logging": true,
    "registry": true,
    "security": {
        "authentication_required": true,
        "api_key_env_var": "SQUADRON_DASHBOARD_API_KEY",
        "api_key_configured": true
    }
}
```

## Security

### Authentication

Dashboard authentication is optional and controlled by the `SQUADRON_DASHBOARD_API_KEY` environment variable.

**When not set:**
- All dashboard endpoints are open (suitable for internal/trusted networks)

**When set:**
- REST endpoints require `Authorization: Bearer <api_key>` header
- SSE endpoints accept `?token=<api_key>` query parameter (for EventSource compatibility)
- Invalid or missing credentials return 401 Unauthorized

### Generating an API Key

```python
from squadron.dashboard_security import generate_api_key
key = generate_api_key()
print(key)  # e.g., "Abc123Xyz789..."
```

Then set in environment:
```bash
export SQUADRON_DASHBOARD_API_KEY="your-generated-key"
```

### Security Recommendations

1. **Production deployments**: Always set `SQUADRON_DASHBOARD_API_KEY`
2. **Use HTTPS**: Protects the API key in transit
3. **Network isolation**: Consider IP allowlisting for additional security
4. **Key rotation**: Generate new keys periodically

## Data Storage

Activity events are stored in a SQLite database at:
```
$SQUADRON_DATA_DIR/activity.db
```

Default location: `.squadron-data/activity.db` in the repository root.

### Data Retention

The `prune_old_activity()` method removes events older than a specified number of hours:

```python
# Prune events older than 72 hours
pruned_count = await activity_logger.prune_old_activity(hours=72)
```

Consider adding this to a periodic cleanup job.

## Integration Example

### Programmatic Access

```python
from squadron.activity import ActivityLogger, ActivityEvent, ActivityEventType

# Initialize
logger = ActivityLogger("/path/to/activity.db")
await logger.initialize()

# Log an event
event = ActivityEvent(
    agent_id="my-agent",
    event_type=ActivityEventType.INFO,
    content="Custom event",
    metadata={"custom_key": "value"}
)
await logger.log(event)

# Query history
events = await logger.get_agent_activity("my-agent", limit=50)
stats = await logger.get_agent_stats("my-agent")

# Subscribe to real-time events
queue = await logger.subscribe("my-agent")
while True:
    event = await queue.get()
    print(f"Event: {event.event_type}")

# Cleanup
await logger.close()
```

### Custom Dashboard Integration

The SSE stream can be consumed by any SSE-compatible client:

```javascript
// JavaScript
const eventSource = new EventSource('/dashboard/stream?token=KEY');
eventSource.addEventListener('activity', handleActivity);
eventSource.addEventListener('heartbeat', () => console.log('alive'));
eventSource.onerror = () => console.log('connection lost');
```

```python
# Python with sseclient
import sseclient

url = 'http://localhost:8000/dashboard/stream?token=KEY'
client = sseclient.SSEClient(url)
for event in client.events():
    if event.event == 'activity':
        data = json.loads(event.data)
        print(f"Agent {data['agent_id']}: {data['event_type']}")
```

## Troubleshooting

### No events appearing in dashboard
1. Verify server is running and activity_logger is configured
2. Check `GET /dashboard/status` returns `activity_logging: true`
3. Ensure agents are active and making progress

### 401 Unauthorized errors
1. Check if `SQUADRON_DASHBOARD_API_KEY` is set
2. Verify the correct key is being passed
3. For SSE, use `?token=` query parameter (headers don't work with EventSource)

### SSE connection drops
- Heartbeats are sent every 30 seconds to keep connections alive
- Check for proxy/load balancer timeouts
- Ensure `X-Accel-Buffering: no` header is respected (nginx)

### Missing tool call events
- Tool logging is integrated into SDK hooks
- Ensure agents are using the standard `_build_hooks` method
- Check for errors in hook execution

## Performance Considerations

- Activity events are written to SQLite with WAL mode for concurrency
- Large results/content are truncated in SSE to prevent memory issues
- Consider pruning old events regularly to manage database size
- SSE subscribers that fall behind are automatically removed
