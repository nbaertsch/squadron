"""Agent Activity Logging — Real-time observability for agent operations.

Provides:
- ActivityEvent model for structured event data
- ActivityLogger for SQLite persistence
- SSE broadcast support for real-time streaming
- Query methods for historical activity

Event Types:
- agent_spawned, agent_woke, agent_sleeping, agent_completed, agent_escalated
- tool_call_start, tool_call_end
- reasoning (LLM output)
- github_comment, github_pr_opened, github_review
- error, warning
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiosqlite
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Event Types ──────────────────────────────────────────────────────────────


class ActivityEventType(str, enum.Enum):
    """Types of activity events that can be logged."""

    # Agent lifecycle
    AGENT_SPAWNED = "agent_spawned"
    AGENT_WOKE = "agent_woke"
    AGENT_SLEEPING = "agent_sleeping"
    AGENT_COMPLETED = "agent_completed"
    AGENT_ESCALATED = "agent_escalated"
    AGENT_FAILED = "agent_failed"

    # Tool execution
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"

    # LLM interaction
    REASONING = "reasoning"
    USER_MESSAGE = "user_message"

    # GitHub operations
    GITHUB_COMMENT = "github_comment"
    GITHUB_PR_OPENED = "github_pr_opened"
    GITHUB_REVIEW = "github_review"
    GITHUB_ISSUE_CREATED = "github_issue_created"

    # System events
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    # Agent session lifecycle (fills diagnostic blind spot between spawn and first tool call)
    SESSION_CREATED = "session_created"
    PROMPT_READY = "prompt_ready"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_REQUEST_COMPLETED = "model_request_completed"
    AGENT_HEARTBEAT = "agent_heartbeat"

    # Circuit breaker
    CIRCUIT_BREAKER_WARNING = "circuit_breaker_warning"
    CIRCUIT_BREAKER_TRIGGERED = "circuit_breaker_triggered"


# ── Event Model ──────────────────────────────────────────────────────────────


class ActivityEvent(BaseModel):
    """A single activity event from an agent."""

    id: int | None = None
    agent_id: str
    event_type: ActivityEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Event-specific data
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    tool_success: bool | None = None
    tool_duration_ms: int | None = None

    content: str | None = None  # For reasoning, messages, errors
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Context
    issue_number: int | None = None
    pr_number: int | None = None

    def to_sse_data(self) -> str:
        """Format for Server-Sent Events."""
        data = {
            "id": self.id,
            "agent_id": self.agent_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
        }

        if self.tool_name:
            data["tool_name"] = self.tool_name
        if self.tool_args:
            data["tool_args"] = self.tool_args
        if self.tool_result is not None:
            # Truncate large results for SSE
            result = self.tool_result
            if len(result) > 1000:
                result = result[:1000] + "... (truncated)"
            data["tool_result"] = result
        if self.tool_success is not None:
            data["tool_success"] = self.tool_success
        if self.tool_duration_ms is not None:
            data["tool_duration_ms"] = self.tool_duration_ms
        if self.content:
            # Truncate large content for SSE
            content = self.content
            if len(content) > 2000:
                content = content[:2000] + "... (truncated)"
            data["content"] = content
        if self.metadata:
            data["metadata"] = self.metadata
        if self.issue_number:
            data["issue_number"] = self.issue_number
        if self.pr_number:
            data["pr_number"] = self.pr_number

        return json.dumps(data)


# ── Database Schema ──────────────────────────────────────────────────────────


ACTIVITY_SCHEMA = """
-- Agent activity log for real-time observability
CREATE TABLE IF NOT EXISTS agent_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,

    -- Tool execution details
    tool_name TEXT,
    tool_args TEXT,          -- JSON
    tool_result TEXT,
    tool_success INTEGER,    -- 0/1 boolean
    tool_duration_ms INTEGER,

    -- Content (reasoning, messages, errors)
    content TEXT,

    -- Additional metadata as JSON
    metadata TEXT DEFAULT '{}',

    -- Context
    issue_number INTEGER,
    pr_number INTEGER
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_activity_agent ON agent_activity(agent_id);
CREATE INDEX IF NOT EXISTS idx_activity_agent_time ON agent_activity(agent_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_type ON agent_activity(event_type);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON agent_activity(timestamp DESC);
"""


# ── Activity Logger ──────────────────────────────────────────────────────────


class ActivityLogger:
    """SQLite-backed activity logger with SSE broadcast support."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        # Per-agent broadcast queues for SSE streaming
        self._subscribers: dict[str, list[asyncio.Queue[ActivityEvent]]] = {}
        # Global broadcast for dashboard (all agents)
        self._global_subscribers: list[asyncio.Queue[ActivityEvent]] = []
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Open database and create tables."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(ACTIVITY_SCHEMA)
        await self._db.commit()
        logger.info("Activity logger initialized: %s", self.db_path)

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ActivityLogger not initialized")
        return self._db

    # ── Logging ──────────────────────────────────────────────────────────────

    async def log(self, event: ActivityEvent) -> ActivityEvent:
        """Log an activity event and broadcast to subscribers."""
        # Persist to database
        cursor = await self.db.execute(
            """INSERT INTO agent_activity
               (agent_id, event_type, timestamp, tool_name, tool_args, tool_result,
                tool_success, tool_duration_ms, content, metadata, issue_number, pr_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.agent_id,
                event.event_type.value,
                event.timestamp.isoformat(),
                event.tool_name,
                json.dumps(event.tool_args) if event.tool_args else None,
                event.tool_result,
                1 if event.tool_success else (0 if event.tool_success is False else None),
                event.tool_duration_ms,
                event.content,
                json.dumps(event.metadata),
                event.issue_number,
                event.pr_number,
            ),
        )
        await self.db.commit()
        event.id = cursor.lastrowid

        # Broadcast to subscribers (non-blocking)
        await self._broadcast(event)

        return event

    async def _broadcast(self, event: ActivityEvent) -> None:
        """Broadcast event to all subscribers."""
        async with self._lock:
            # Per-agent subscribers
            if event.agent_id in self._subscribers:
                dead_queues = []
                for queue in self._subscribers[event.agent_id]:
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        dead_queues.append(queue)
                # Remove full queues (client disconnected or too slow)
                for q in dead_queues:
                    self._subscribers[event.agent_id].remove(q)

            # Global subscribers (dashboard)
            dead_global = []
            for queue in self._global_subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    dead_global.append(queue)
            for q in dead_global:
                self._global_subscribers.remove(q)

    # ── Subscription ─────────────────────────────────────────────────────────

    async def subscribe(self, agent_id: str | None = None) -> asyncio.Queue[ActivityEvent]:
        """Subscribe to activity events for an agent (or all agents if None)."""
        queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            if agent_id:
                if agent_id not in self._subscribers:
                    self._subscribers[agent_id] = []
                self._subscribers[agent_id].append(queue)
            else:
                self._global_subscribers.append(queue)
        return queue

    async def unsubscribe(
        self, queue: asyncio.Queue[ActivityEvent], agent_id: str | None = None
    ) -> None:
        """Unsubscribe from activity events."""
        async with self._lock:
            if agent_id and agent_id in self._subscribers:
                if queue in self._subscribers[agent_id]:
                    self._subscribers[agent_id].remove(queue)
            elif queue in self._global_subscribers:
                self._global_subscribers.remove(queue)

    # ── Queries ──────────────────────────────────────────────────────────────

    async def get_agent_activity(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
        event_types: list[ActivityEventType] | None = None,
    ) -> list[ActivityEvent]:
        """Get activity events for a specific agent."""
        query = "SELECT * FROM agent_activity WHERE agent_id = ?"
        params: list[Any] = [agent_id]

        if event_types:
            placeholders = ",".join("?" * len(event_types))
            query += f" AND event_type IN ({placeholders})"
            params.extend(et.value for et in event_types)

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_event(row) for row in rows]

    async def get_recent_activity(
        self,
        limit: int = 100,
        offset: int = 0,
        agent_id: str | None = None,
        event_types: list[ActivityEventType] | None = None,
    ) -> list[ActivityEvent]:
        """Get recent activity across all agents (or filtered by agent)."""
        query = "SELECT * FROM agent_activity"
        params: list[Any] = []
        conditions = []

        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)

        if event_types:
            placeholders = ",".join("?" * len(event_types))
            conditions.append(f"event_type IN ({placeholders})")
            params.extend(et.value for et in event_types)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_event(row) for row in rows]

    async def get_agent_stats(self, agent_id: str) -> dict[str, Any]:
        """Get summary statistics for an agent."""
        async with self.db.execute(
            """SELECT
                COUNT(*) as total_events,
                COUNT(CASE WHEN event_type = 'tool_call_end' THEN 1 END) as tool_calls,
                COUNT(CASE WHEN event_type = 'error' THEN 1 END) as errors,
                AVG(CASE WHEN tool_duration_ms IS NOT NULL THEN tool_duration_ms END) as avg_tool_duration_ms,
                MIN(timestamp) as first_activity,
                MAX(timestamp) as last_activity
               FROM agent_activity WHERE agent_id = ?""",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "agent_id": agent_id,
                    "total_events": row["total_events"],
                    "tool_calls": row["tool_calls"],
                    "errors": row["errors"],
                    "avg_tool_duration_ms": (
                        round(row["avg_tool_duration_ms"], 2)
                        if row["avg_tool_duration_ms"]
                        else None
                    ),
                    "first_activity": row["first_activity"],
                    "last_activity": row["last_activity"],
                }
            return {"agent_id": agent_id, "total_events": 0}

    async def prune_old_activity(self, hours: int = 72) -> int:
        """Delete activity events older than specified hours."""
        cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=hours)
        cursor = await self.db.execute(
            "DELETE FROM agent_activity WHERE timestamp < ?",
            (cutoff.isoformat(),),
        )
        await self.db.commit()
        return cursor.rowcount

    def _row_to_event(self, row: aiosqlite.Row) -> ActivityEvent:
        """Convert database row to ActivityEvent."""
        return ActivityEvent(
            id=row["id"],
            agent_id=row["agent_id"],
            event_type=ActivityEventType(row["event_type"]),
            timestamp=datetime.fromisoformat(row["timestamp"]),
            tool_name=row["tool_name"],
            tool_args=json.loads(row["tool_args"]) if row["tool_args"] else None,
            tool_result=row["tool_result"],
            tool_success=(bool(row["tool_success"]) if row["tool_success"] is not None else None),
            tool_duration_ms=row["tool_duration_ms"],
            content=row["content"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            issue_number=row["issue_number"],
            pr_number=row["pr_number"],
        )


# ── Helper Functions ─────────────────────────────────────────────────────────


def create_lifecycle_event(
    agent_id: str,
    event_type: ActivityEventType,
    issue_number: int | None = None,
    pr_number: int | None = None,
    content: str | None = None,
    **metadata: Any,
) -> ActivityEvent:
    """Create an agent lifecycle event."""
    return ActivityEvent(
        agent_id=agent_id,
        event_type=event_type,
        issue_number=issue_number,
        pr_number=pr_number,
        content=content,
        metadata=metadata,
    )


def create_tool_start_event(
    agent_id: str,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    issue_number: int | None = None,
) -> ActivityEvent:
    """Create a tool call start event."""
    return ActivityEvent(
        agent_id=agent_id,
        event_type=ActivityEventType.TOOL_CALL_START,
        tool_name=tool_name,
        tool_args=tool_args,
        issue_number=issue_number,
    )


def create_tool_end_event(
    agent_id: str,
    tool_name: str,
    success: bool,
    duration_ms: int,
    result: str | None = None,
    issue_number: int | None = None,
) -> ActivityEvent:
    """Create a tool call end event."""
    return ActivityEvent(
        agent_id=agent_id,
        event_type=ActivityEventType.TOOL_CALL_END,
        tool_name=tool_name,
        tool_success=success,
        tool_duration_ms=duration_ms,
        tool_result=result,
        issue_number=issue_number,
    )


def create_reasoning_event(
    agent_id: str,
    content: str,
    issue_number: int | None = None,
) -> ActivityEvent:
    """Create a reasoning/LLM output event."""
    return ActivityEvent(
        agent_id=agent_id,
        event_type=ActivityEventType.REASONING,
        content=content,
        issue_number=issue_number,
    )


def create_error_event(
    agent_id: str,
    error_message: str,
    issue_number: int | None = None,
    **metadata: Any,
) -> ActivityEvent:
    """Create an error event."""
    return ActivityEvent(
        agent_id=agent_id,
        event_type=ActivityEventType.ERROR,
        content=error_message,
        issue_number=issue_number,
        metadata=metadata,
    )
