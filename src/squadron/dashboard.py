"""Dashboard API — SSE streaming and REST endpoints for real-time observability.

Endpoints:
    UI:
    - GET /dashboard/ - Dashboard HTML UI

    SSE Streaming:
    - GET /dashboard/agents/{agent_id}/stream - Real-time activity stream for one agent
    - GET /dashboard/stream - Real-time activity stream for all agents (global)
    - GET /dashboard/logs/stream - Real-time log stream (filtered by level/name)

    REST Queries:
    - GET /dashboard/agents/{agent_id}/activity - Historical activity for one agent
    - GET /dashboard/agents/{agent_id}/stats - Summary statistics for one agent
    - GET /dashboard/activity - Recent activity across all agents
    - GET /dashboard/agents - List all active agents with status
    - GET /dashboard/logs - Query in-memory log ring buffer

    Status:
    - GET /dashboard/status - Server and security status

Security:
    All endpoints respect SQUADRON_DASHBOARD_API_KEY when configured.
    SSE streams accept token via query parameter (?token=...) for EventSource compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from squadron.activity import ActivityEventType
from squadron.dashboard_security import require_api_key, validate_sse_token, get_security_config

if TYPE_CHECKING:
    from squadron.activity import ActivityLogger
    from squadron.log_buffer import LogBuffer
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Path to static dashboard HTML
_STATIC_DIR = Path(__file__).parent / "static"

# Module-level references (configured at startup)
_activity_logger: "ActivityLogger | None" = None
_registry: "AgentRegistry | None" = None
_log_buffer: "LogBuffer | None" = None


def configure(
    activity_logger: "ActivityLogger",
    registry: "AgentRegistry",
    log_buffer: "LogBuffer | None" = None,
) -> None:
    """Configure the dashboard router with required dependencies."""
    global _activity_logger, _registry, _log_buffer
    _activity_logger = activity_logger
    _registry = registry
    _log_buffer = log_buffer
    logger.info("Dashboard router configured (log_buffer=%s)", "yes" if log_buffer else "no")


# ── Dashboard UI ──────────────────────────────────────────────────────────────


@router.get("/", include_in_schema=False)
async def dashboard_ui():
    """Serve the dashboard HTML UI."""
    html_path = _STATIC_DIR / "dashboard.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard UI not found")
    return FileResponse(html_path, media_type="text/html")


# ── SSE Streaming ─────────────────────────────────────────────────────────────


# Number of history events to send on initial connection
_HYDRATION_LIMIT = 200


async def _sse_generator(agent_id: str | None = None):
    """Generate SSE events for activity stream.

    On connect, hydrates the client with recent history before streaming
    live events so the UI immediately shows context without a separate
    History fetch.

    Args:
        agent_id: If set, only stream events for this agent. If None, stream all events.
    """
    if _activity_logger is None:
        yield 'event: error\ndata: {"error": "Activity logger not configured"}\n\n'
        return

    # Subscribe BEFORE fetching history so we don't miss events that arrive
    # during the history query.
    queue = await _activity_logger.subscribe(agent_id)

    try:
        yield 'event: connected\ndata: {"status": "connected"}\n\n'

        # ── History hydration ────────────────────────────────────────────────
        # Fetch recent events (newest-first from DB) and send oldest-first so
        # the client sees them in chronological order.
        if agent_id:
            history = await _activity_logger.get_agent_activity(agent_id, limit=_HYDRATION_LIMIT)
        else:
            history = await _activity_logger.get_recent_activity(limit=_HYDRATION_LIMIT)

        for event in reversed(history):
            yield f"event: activity\ndata: {event.to_sse_data()}\n\n"

        # Signal that history hydration is complete; the client can mark all
        # previously received events as 'historical'.
        yield 'event: hydrated\ndata: {"status": "hydrated"}\n\n'

        # ── Live stream ──────────────────────────────────────────────────────
        while True:
            try:
                # Wait for next event with timeout (heartbeat every 30s)
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"event: activity\ndata: {event.to_sse_data()}\n\n"
            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                yield "event: heartbeat\ndata: {}\n\n"
            except asyncio.CancelledError:
                break
    finally:
        # Unsubscribe when client disconnects
        await _activity_logger.unsubscribe(queue, agent_id)


@router.get("/agents/{agent_id}/stream")
async def stream_agent_activity(
    agent_id: str,
    token: str | None = Query(default=None, description="API key for authentication"),
):
    """Stream real-time activity events for a specific agent via SSE.

    Connect with EventSource:
    ```javascript
    const es = new EventSource('/dashboard/agents/agent-123/stream?token=YOUR_KEY');
    es.addEventListener('activity', (e) => console.log(JSON.parse(e.data)));
    ```

    Event types:
    - connected: Initial connection confirmation
    - activity: Agent activity event (tool calls, lifecycle changes, etc.)
    - heartbeat: Keep-alive ping (every 30s)
    """
    validate_sse_token(token)

    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")

    # Verify agent exists
    agent = await _registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return StreamingResponse(
        _sse_generator(agent_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.get("/stream")
async def stream_all_activity(
    token: str | None = Query(default=None, description="API key for authentication"),
):
    """Stream real-time activity events for all agents via SSE.

    Connect with EventSource:
    ```javascript
    const es = new EventSource('/dashboard/stream?token=YOUR_KEY');
    es.addEventListener('activity', (e) => console.log(JSON.parse(e.data)));
    ```
    """
    validate_sse_token(token)

    return StreamingResponse(
        _sse_generator(None),  # None = global stream
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── REST Queries ──────────────────────────────────────────────────────────────


@router.get("/agents/{agent_id}/activity")
async def get_agent_activity(
    agent_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    event_types: str | None = Query(
        default=None,
        description="Comma-separated event types to filter (e.g., 'tool_call_start,tool_call_end')",
    ),
    _: bool = Depends(require_api_key),
):
    """Get historical activity events for a specific agent.

    Returns events in reverse chronological order (newest first).
    """
    if _activity_logger is None:
        raise HTTPException(status_code=503, detail="Activity logger not configured")

    # Parse event type filter
    type_filter = None
    if event_types:
        try:
            type_filter = [ActivityEventType(t.strip()) for t in event_types.split(",")]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid event type: {e}")

    events = await _activity_logger.get_agent_activity(
        agent_id, limit=limit, offset=offset, event_types=type_filter
    )

    return {
        "agent_id": agent_id,
        "count": len(events),
        "offset": offset,
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type.value,
                "timestamp": e.timestamp.isoformat(),
                "tool_name": e.tool_name,
                "tool_args": e.tool_args,
                "tool_result": e.tool_result[:500]
                if e.tool_result and len(e.tool_result) > 500
                else e.tool_result,
                "tool_success": e.tool_success,
                "tool_duration_ms": e.tool_duration_ms,
                "content": e.content[:1000] if e.content and len(e.content) > 1000 else e.content,
                "metadata": e.metadata,
                "issue_number": e.issue_number,
                "pr_number": e.pr_number,
            }
            for e in events
        ],
    }


@router.get("/agents/{agent_id}/stats")
async def get_agent_stats(
    agent_id: str,
    _: bool = Depends(require_api_key),
):
    """Get summary statistics for a specific agent."""
    if _activity_logger is None:
        raise HTTPException(status_code=503, detail="Activity logger not configured")

    stats = await _activity_logger.get_agent_stats(agent_id)
    return stats


@router.get("/activity")
async def get_recent_activity(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    agent_id: str | None = Query(
        default=None,
        description="Filter by agent ID",
    ),
    event_types: str | None = Query(
        default=None,
        description="Comma-separated event types to filter",
    ),
    _: bool = Depends(require_api_key),
):
    """Get recent activity events across all agents (or filtered by agent).

    Returns events in reverse chronological order (newest first).
    """
    if _activity_logger is None:
        raise HTTPException(status_code=503, detail="Activity logger not configured")

    # Parse event type filter
    type_filter = None
    if event_types:
        try:
            type_filter = [ActivityEventType(t.strip()) for t in event_types.split(",")]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid event type: {e}")

    events = await _activity_logger.get_recent_activity(
        limit=limit, offset=offset, agent_id=agent_id, event_types=type_filter
    )

    return {
        "count": len(events),
        "offset": offset,
        "agent_id": agent_id,
        "events": [
            {
                "id": e.id,
                "agent_id": e.agent_id,
                "event_type": e.event_type.value,
                "timestamp": e.timestamp.isoformat(),
                "tool_name": e.tool_name,
                "tool_args": e.tool_args,
                "tool_result": e.tool_result[:500]
                if e.tool_result and len(e.tool_result) > 500
                else e.tool_result,
                "tool_success": e.tool_success,
                "tool_duration_ms": e.tool_duration_ms,
                "content": e.content[:500] if e.content and len(e.content) > 500 else e.content,
                "metadata": e.metadata,
                "issue_number": e.issue_number,
                "pr_number": e.pr_number,
            }
            for e in events
        ],
    }


@router.get("/agents")
async def list_agents(
    _: bool = Depends(require_api_key),
):
    """List all agents with their current status."""
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")

    # Get all active agents
    active = await _registry.get_all_active_agents()

    # Get recent completed agents (for historical context)
    recent = await _registry.get_recent_agents(limit=20)

    return {
        "active_count": len(active),
        "active_agents": [
            {
                "agent_id": a.agent_id,
                "role": a.role,
                "status": a.status.value,
                "issue_number": a.issue_number,
                "pr_number": a.pr_number,
                "branch": a.branch,
                "active_since": a.active_since.isoformat() if a.active_since else None,
                "sleeping_since": a.sleeping_since.isoformat() if a.sleeping_since else None,
                "blocked_by": list(a.blocked_by) if a.blocked_by else [],
                "tool_call_count": a.tool_call_count,
                "iteration_count": a.iteration_count,
            }
            for a in active
        ],
        "recent_agents": [
            {
                "agent_id": a.agent_id,
                "role": a.role,
                "status": a.status.value,
                "issue_number": a.issue_number,
                "updated_at": a.updated_at.isoformat() if a.updated_at else None,
            }
            for a in recent
        ],
    }


@router.get("/status")
async def get_status(
    request: Request,
    _: bool = Depends(require_api_key),
):
    """Get dashboard and server status including security configuration."""
    security = get_security_config()

    return {
        "status": "ok",
        "activity_logging": _activity_logger is not None,
        "registry": _registry is not None,
        "log_buffer": _log_buffer is not None,
        "log_buffer_size": _log_buffer.size if _log_buffer else 0,
        "log_buffer_capacity": _log_buffer.maxlen if _log_buffer else 0,
        "security": security,
        "client_ip": request.client.host if request.client else None,
    }


# ── Log Buffer Endpoints ─────────────────────────────────────────────────────


@router.get("/logs")
async def get_logs(
    level: str | None = Query(
        default=None,
        description="Minimum log level filter (e.g. WARNING, ERROR)",
    ),
    name: str | None = Query(
        default=None,
        description="Logger name prefix filter (e.g. squadron.agent_manager)",
    ),
    limit: int = Query(default=500, ge=1, le=5000),
    _: bool = Depends(require_api_key),
):
    """Query the in-memory log ring buffer.

    Returns log entries from the ring buffer (newest first).
    The ring buffer holds the last 20,000 log lines — no disk I/O,
    no container log access required.

    Filters:
    - ``level``: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
      Records at or above this level are returned.
    - ``name``: Logger name prefix (e.g. ``squadron.agent_manager``).
      Uses startswith matching.
    """
    if _log_buffer is None:
        raise HTTPException(status_code=503, detail="Log buffer not configured")

    entries = _log_buffer.query(level=level, name=name, limit=limit)

    return {
        "count": len(entries),
        "buffer_size": _log_buffer.size,
        "buffer_capacity": _log_buffer.maxlen,
        "filters": {"level": level, "name": name},
        "entries": entries,
    }


async def _log_sse_generator(
    *,
    level: str | None = None,
    name: str | None = None,
):
    """Generate SSE events from the log ring buffer.

    Sends recent history first, then streams live log entries.
    Supports the same level/name filters as the REST endpoint.
    """
    if _log_buffer is None:
        yield 'event: error\ndata: {"error": "Log buffer not configured"}\n\n'
        return

    # Subscribe BEFORE fetching history (same pattern as activity SSE)
    queue = await _log_buffer.subscribe()

    try:
        yield 'event: connected\ndata: {"status": "connected"}\n\n'

        # ── History hydration (last 200 matching entries, oldest first) ──
        level_num = getattr(logging, level.upper(), None) if level else None
        history = _log_buffer.query(level=level, name=name, limit=200)
        for entry in reversed(history):  # oldest first
            yield f"event: log\ndata: {json.dumps(entry)}\n\n"

        yield 'event: hydrated\ndata: {"status": "hydrated"}\n\n'

        # ── Live stream ──────────────────────────────────────────────────
        while True:
            try:
                entry = await asyncio.wait_for(queue.get(), timeout=30.0)
                # Apply filters to live entries
                if level_num is not None:
                    entry_level = getattr(logging, entry.get("level", "DEBUG"), logging.DEBUG)
                    if entry_level < level_num:
                        continue
                if name is not None and not entry.get("name", "").startswith(name):
                    continue
                yield f"event: log\ndata: {json.dumps(entry)}\n\n"
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
            except asyncio.CancelledError:
                break
    finally:
        await _log_buffer.unsubscribe(queue)


@router.get("/logs/stream")
async def stream_logs(
    token: str | None = Query(default=None, description="API key for authentication"),
    level: str | None = Query(
        default=None,
        description="Minimum log level filter (e.g. WARNING, ERROR)",
    ),
    name: str | None = Query(
        default=None,
        description="Logger name prefix filter (e.g. squadron.agent_manager)",
    ),
):
    """Stream live log entries via SSE.

    Connect with EventSource:
    ```javascript
    const es = new EventSource('/dashboard/logs/stream?token=KEY&level=WARNING');
    es.addEventListener('log', (e) => console.log(JSON.parse(e.data)));
    ```

    Event types:
    - connected: Initial connection confirmation
    - log: A log entry ``{timestamp, level, name, message, agent_id}``
    - hydrated: History replay complete
    - heartbeat: Keep-alive ping (every 30s)
    """
    validate_sse_token(token)

    return StreamingResponse(
        _log_sse_generator(level=level, name=name),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
