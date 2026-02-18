"""Dashboard API — SSE streaming and REST endpoints for real-time observability.

Endpoints:
    UI:
    - GET /dashboard/ - Dashboard HTML UI

    SSE Streaming:
    - GET /dashboard/agents/{agent_id}/stream - Real-time activity stream for one agent
    - GET /dashboard/stream - Real-time activity stream for all agents (global)

    REST Queries:
    - GET /dashboard/agents/{agent_id}/activity - Historical activity for one agent
    - GET /dashboard/agents/{agent_id}/stats - Summary statistics for one agent
    - GET /dashboard/activity - Recent activity across all agents
    - GET /dashboard/agents - List all active agents with status

    Status:
    - GET /dashboard/status - Server and security status

Security:
    All endpoints respect SQUADRON_DASHBOARD_API_KEY when configured.
    SSE streams accept token via query parameter (?token=...) for EventSource compatibility.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from squadron.activity import ActivityEventType
from squadron.dashboard_security import require_api_key, validate_sse_token, get_security_config

if TYPE_CHECKING:
    from squadron.activity import ActivityLogger
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Path to static dashboard HTML
_STATIC_DIR = Path(__file__).parent / "static"

# Module-level references (configured at startup)
_activity_logger: "ActivityLogger | None" = None
_registry: "AgentRegistry | None" = None


def configure(activity_logger: "ActivityLogger", registry: "AgentRegistry") -> None:
    """Configure the dashboard router with required dependencies."""
    global _activity_logger, _registry
    _activity_logger = activity_logger
    _registry = registry
    logger.info("Dashboard router configured")


# ── Dashboard UI ──────────────────────────────────────────────────────────────


@router.get("/", include_in_schema=False)
async def dashboard_ui():
    """Serve the dashboard HTML UI."""
    html_path = _STATIC_DIR / "dashboard.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard UI not found")
    return FileResponse(html_path, media_type="text/html")


# ── SSE Streaming ─────────────────────────────────────────────────────────────


async def _sse_generator(agent_id: str | None = None):
    """Generate SSE events for activity stream.

    Args:
        agent_id: If set, only stream events for this agent. If None, stream all events.
    """
    if _activity_logger is None:
        yield 'event: error\ndata: {"error": "Activity logger not configured"}\n\n'
        return

    # Subscribe to activity events
    queue = await _activity_logger.subscribe(agent_id)

    try:
        # Send initial heartbeat
        yield 'event: connected\ndata: {"status": "connected"}\n\n'

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
    event_types: str | None = Query(
        default=None,
        description="Comma-separated event types to filter",
    ),
    _: bool = Depends(require_api_key),
):
    """Get recent activity events across all agents.

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

    events = await _activity_logger.get_recent_activity(limit=limit, event_types=type_filter)

    return {
        "count": len(events),
        "events": [
            {
                "id": e.id,
                "agent_id": e.agent_id,
                "event_type": e.event_type.value,
                "timestamp": e.timestamp.isoformat(),
                "tool_name": e.tool_name,
                "tool_success": e.tool_success,
                "tool_duration_ms": e.tool_duration_ms,
                "content": e.content[:500] if e.content and len(e.content) > 500 else e.content,
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
        "security": security,
        "client_ip": request.client.host if request.client else None,
    }
