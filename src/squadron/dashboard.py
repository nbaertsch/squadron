"""Dashboard API — SSE streaming and REST endpoints for real-time observability.

Endpoints:
    UI:
    - GET /dashboard/ - Dashboard HTML UI

    SSE Streaming:
    - GET /dashboard/agents/{agent_id}/stream - Real-time activity stream for one agent
    - GET /dashboard/stream - Real-time activity stream for all agents (global)
    - GET /dashboard/logs/stream - Real-time log stream (filtered by level/name)
    - GET /dashboard/pipelines/stream - Real-time pipeline event stream

    REST Queries:
    - GET /dashboard/agents/{agent_id}/activity - Historical activity for one agent
    - GET /dashboard/agents/{agent_id}/stats - Summary statistics for one agent
    - GET /dashboard/activity - Recent activity across all agents
    - GET /dashboard/agents - List all active agents with status
    - GET /dashboard/logs - Query in-memory log ring buffer

    Pipeline Visibility (AD-019):
    - GET /dashboard/pipelines - List pipeline definitions
    - GET /dashboard/pipelines/runs - List pipeline runs (paginated, filterable)
    - GET /dashboard/pipelines/runs/{run_id} - Get run detail with stage runs
    - POST /dashboard/pipelines/runs/{run_id}/cancel - Cancel a pipeline run

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
    from squadron.pipeline.engine import PipelineEngine
    from squadron.pipeline.registry import PipelineRegistry
    from squadron.registry import AgentRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Path to static dashboard HTML
_STATIC_DIR = Path(__file__).parent / "static"

# Module-level references (configured at startup)
_activity_logger: "ActivityLogger | None" = None
_registry: "AgentRegistry | None" = None
_log_buffer: "LogBuffer | None" = None
_pipeline_engine: "PipelineEngine | None" = None
_pipeline_registry: "PipelineRegistry | None" = None

# Pipeline SSE subscribers (queues that receive pipeline events)
_pipeline_subscribers: list[asyncio.Queue] = []


def configure(
    activity_logger: "ActivityLogger",
    registry: "AgentRegistry",
    log_buffer: "LogBuffer | None" = None,
    pipeline_engine: "PipelineEngine | None" = None,
    pipeline_registry: "PipelineRegistry | None" = None,
) -> None:
    """Configure the dashboard router with required dependencies."""
    global _activity_logger, _registry, _log_buffer, _pipeline_engine, _pipeline_registry
    _activity_logger = activity_logger
    _registry = registry
    _log_buffer = log_buffer
    _pipeline_engine = pipeline_engine
    _pipeline_registry = pipeline_registry
    logger.info(
        "Dashboard router configured (log_buffer=%s, pipelines=%s)",
        "yes" if log_buffer else "no",
        "yes" if pipeline_engine else "no",
    )


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
        "pipeline_engine": _pipeline_engine is not None,
        "pipeline_registry": _pipeline_registry is not None,
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


# ── Pipeline Visibility Endpoints (AD-019) ──────────────────────────────────


def _publish_pipeline_event(event_type: str, data: dict) -> None:
    """Publish a pipeline event to all SSE subscribers."""
    payload = json.dumps({"event_type": event_type, **data})
    dead: list[asyncio.Queue] = []
    for q in _pipeline_subscribers:
        try:
            q.put_nowait({"event_type": event_type, "payload": payload})
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _pipeline_subscribers.remove(q)


def _pipeline_run_to_dict(run) -> dict:
    """Convert a PipelineRun to a JSON-serializable dict."""
    return {
        "run_id": run.run_id,
        "pipeline_name": run.pipeline_name,
        "status": run.status.value,
        "trigger_event": run.trigger_event,
        "issue_number": run.issue_number,
        "pr_number": run.pr_number,
        "scope": run.scope.value,
        "parent_run_id": run.parent_run_id,
        "current_stage_id": run.current_stage_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "error_message": run.error_message,
        "error_stage_id": run.error_stage_id,
    }


def _stage_run_to_dict(sr) -> dict:
    """Convert a StageRun to a JSON-serializable dict."""
    return {
        "id": sr.id,
        "run_id": sr.run_id,
        "stage_id": sr.stage_id,
        "status": sr.status.value,
        "agent_id": sr.agent_id,
        "branch_id": sr.branch_id,
        "parent_stage_id": sr.parent_stage_id,
        "child_pipeline_run_id": sr.child_pipeline_run_id,
        "outputs": sr.outputs,
        "error_message": sr.error_message,
        "attempt_number": sr.attempt_number,
        "max_attempts": sr.max_attempts,
        "started_at": sr.started_at.isoformat() if sr.started_at else None,
        "completed_at": sr.completed_at.isoformat() if sr.completed_at else None,
        "duration_seconds": sr.duration_seconds,
    }


@router.get("/pipelines")
async def list_pipelines(
    _: bool = Depends(require_api_key),
):
    """List all registered pipeline definitions.

    Returns pipeline names, descriptions, trigger info, and stage counts.
    """
    if _pipeline_engine is None:
        raise HTTPException(status_code=503, detail="Pipeline engine not configured")

    names = _pipeline_engine.list_pipelines()
    definitions = []
    for name in names:
        defn = _pipeline_engine.get_pipeline(name)
        if defn:
            trigger_info = None
            if defn.trigger:
                trigger_info = {
                    "event": defn.trigger.event,
                    "conditions": defn.trigger.conditions,
                }
            definitions.append(
                {
                    "name": name,
                    "description": defn.description,
                    "scope": defn.scope.value,
                    "trigger": trigger_info,
                    "stage_count": len(defn.stages),
                    "stages": [{"id": s.id, "type": s.type.value} for s in defn.stages],
                    "reactive_events": list(defn.on_events.keys()),
                }
            )

    return {
        "count": len(definitions),
        "pipelines": definitions,
    }


@router.get("/pipelines/runs")
async def list_pipeline_runs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(
        default=None,
        description="Filter by status (pending, running, completed, failed, cancelled, escalated)",
    ),
    pipeline_name: str | None = Query(
        default=None,
        description="Filter by pipeline name",
    ),
    pr_number: int | None = Query(default=None, description="Filter by PR number"),
    issue_number: int | None = Query(default=None, description="Filter by issue number"),
    _: bool = Depends(require_api_key),
):
    """List pipeline runs with pagination and filtering.

    Returns runs in reverse chronological order (newest first).
    """
    if _pipeline_registry is None:
        raise HTTPException(status_code=503, detail="Pipeline registry not configured")

    from squadron.pipeline.models import PipelineRunStatus

    # Parse status filter
    status_filter = None
    if status:
        try:
            status_filter = PipelineRunStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status: '{status}'. "
                f"Valid: {', '.join(s.value for s in PipelineRunStatus)}",
            )

    # PR/issue-specific queries use dedicated registry methods
    if pr_number is not None:
        runs = await _pipeline_registry.get_pipeline_runs_by_pr(pr_number, status=status_filter)
        # Apply pipeline_name filter client-side (registry method doesn't support it)
        if pipeline_name:
            runs = [r for r in runs if r.pipeline_name == pipeline_name]
        total = len(runs)
        runs = runs[offset : offset + limit]
    elif issue_number is not None:
        runs = await _pipeline_registry.get_pipeline_runs_by_issue(
            issue_number, status=status_filter
        )
        if pipeline_name:
            runs = [r for r in runs if r.pipeline_name == pipeline_name]
        total = len(runs)
        runs = runs[offset : offset + limit]
    else:
        runs = await _pipeline_registry.get_recent_pipeline_runs(
            limit=limit,
            offset=offset,
            status=status_filter,
            pipeline_name=pipeline_name,
        )
        total = await _pipeline_registry.count_pipeline_runs(
            status=status_filter,
            pipeline_name=pipeline_name,
        )

    return {
        "total": total,
        "count": len(runs),
        "offset": offset,
        "runs": [_pipeline_run_to_dict(r) for r in runs],
    }


@router.get("/pipelines/runs/{run_id}")
async def get_pipeline_run_detail(
    run_id: str,
    _: bool = Depends(require_api_key),
):
    """Get detailed information about a pipeline run including all stage runs.

    Returns the run metadata, all stage runs with their statuses, and
    child pipeline references.
    """
    if _pipeline_registry is None:
        raise HTTPException(status_code=503, detail="Pipeline registry not configured")

    run = await _pipeline_registry.get_pipeline_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Pipeline run {run_id} not found")

    stage_runs = await _pipeline_registry.get_stage_runs_for_pipeline(run_id)
    children = await _pipeline_registry.get_child_pipelines(run_id)

    # Parse definition snapshot for stage metadata
    definition_stages = []
    try:
        defn_data = json.loads(run.definition_snapshot) if run.definition_snapshot else {}
        for stage_data in defn_data.get("stages", []):
            definition_stages.append(
                {
                    "id": stage_data.get("id"),
                    "type": stage_data.get("type"),
                }
            )
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "run": _pipeline_run_to_dict(run),
        "definition_stages": definition_stages,
        "stage_runs": [_stage_run_to_dict(sr) for sr in stage_runs],
        "children": [_pipeline_run_to_dict(c) for c in children],
    }


@router.post("/pipelines/runs/{run_id}/cancel")
async def cancel_pipeline_run(
    run_id: str,
    _: bool = Depends(require_api_key),
):
    """Cancel a running or pending pipeline run.

    Cascades cancellation to child pipelines.
    """
    if _pipeline_engine is None:
        raise HTTPException(status_code=503, detail="Pipeline engine not configured")

    cancelled = await _pipeline_engine.cancel_pipeline(run_id)
    if not cancelled:
        # Check if run exists at all
        if _pipeline_registry:
            run = await _pipeline_registry.get_pipeline_run(run_id)
            if not run:
                raise HTTPException(status_code=404, detail=f"Pipeline run {run_id} not found")
            raise HTTPException(
                status_code=409,
                detail=f"Pipeline run {run_id} is {run.status.value} and cannot be cancelled",
            )
        raise HTTPException(status_code=404, detail=f"Pipeline run {run_id} not found")

    _publish_pipeline_event("pipeline_cancelled", {"run_id": run_id})

    return {"cancelled": True, "run_id": run_id}


async def _pipeline_sse_generator():
    """Generate SSE events for pipeline activity stream.

    Streams real-time pipeline and stage transition events.
    Hydrates with current active runs on connect.
    """
    if _pipeline_registry is None:
        yield 'event: error\ndata: {"error": "Pipeline registry not configured"}\n\n'
        return

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    _pipeline_subscribers.append(queue)

    try:
        yield 'event: connected\ndata: {"status": "connected"}\n\n'

        # Hydrate with active pipeline runs
        active_runs = await _pipeline_registry.get_active_pipeline_runs()
        for run in active_runs:
            data = json.dumps(_pipeline_run_to_dict(run))
            yield f"event: pipeline_run\ndata: {data}\n\n"

        yield 'event: hydrated\ndata: {"status": "hydrated"}\n\n'

        # Live stream
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"event: {event['event_type']}\ndata: {event['payload']}\n\n"
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
            except asyncio.CancelledError:
                break
    finally:
        if queue in _pipeline_subscribers:
            _pipeline_subscribers.remove(queue)


@router.get("/pipelines/stream")
async def stream_pipeline_events(
    token: str | None = Query(default=None, description="API key for authentication"),
):
    """Stream real-time pipeline events via SSE.

    Connect with EventSource:
    ```javascript
    const es = new EventSource('/dashboard/pipelines/stream?token=KEY');
    es.addEventListener('pipeline_run', (e) => console.log(JSON.parse(e.data)));
    ```

    Event types:
    - connected: Initial connection confirmation
    - pipeline_run: Active pipeline run state (hydration)
    - pipeline_cancelled: A pipeline was cancelled
    - hydrated: History replay complete
    - heartbeat: Keep-alive ping (every 30s)
    """
    validate_sse_token(token)

    return StreamingResponse(
        _pipeline_sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
