"""Ring-Buffer Log Handler — in-memory capture with SSE pub/sub for remote log access.

Provides:
- RingBufferHandler: a logging.Handler that stores the last N log records
  in a collections.deque ring buffer (thread-safe, bounded).
- LogBuffer: query + subscribe interface for the dashboard API to expose
  logs via REST and SSE without needing container log access.

Design Notes:
- Ring buffer is fixed at ``maxlen`` entries (default 20,000). Oldest entries
  are silently discarded when the buffer is full — no disk I/O.
- Each captured record is stored as a structured dict for JSON serialization.
- Pub/sub uses the same asyncio.Queue pattern as ActivityLogger._subscribers
  so the SSE log stream works identically to the activity stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone


class LogRecord(dict):
    """Typed dict wrapper for a captured log record.

    Keys: timestamp, level, name, message, agent_id (optional).
    """

    pass


def _extract_agent_id(record: logging.LogRecord) -> str | None:
    """Best-effort extraction of agent_id from a log record.

    The agent_manager logger often includes the agent_id in the message or
    as an extra attribute. We check for an explicit ``agent_id`` attribute
    first, then fall back to None.
    """
    return getattr(record, "agent_id", None)


def _record_to_dict(record: logging.LogRecord) -> LogRecord:
    """Convert a stdlib LogRecord into a JSON-serializable dict."""
    return LogRecord(
        timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        level=record.levelname,
        name=record.name,
        message=record.getMessage(),
        agent_id=_extract_agent_id(record),
    )


class RingBufferHandler(logging.Handler):
    """A logging.Handler that pushes records into a LogBuffer ring buffer.

    Attach this to the root logger after ``logging.basicConfig()``::

        handler = RingBufferHandler(log_buffer)
        logging.getLogger().addHandler(handler)
    """

    def __init__(self, log_buffer: "LogBuffer", level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self._buffer = log_buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = _record_to_dict(record)
            self._buffer.push(entry)
        except Exception:
            # Never let logging break the application
            self.handleError(record)


class LogBuffer:
    """In-memory ring buffer for log records with query and pub/sub support.

    Parameters
    ----------
    maxlen:
        Maximum number of log entries to retain (default 20,000).
    """

    def __init__(self, maxlen: int = 20_000) -> None:
        self._buffer: deque[LogRecord] = deque(maxlen=maxlen)
        self._subscribers: list[asyncio.Queue[LogRecord]] = []
        self._lock = asyncio.Lock()

    # ── Write path (called from RingBufferHandler.emit) ──────────────────

    def push(self, entry: LogRecord) -> None:
        """Append a log entry to the ring buffer and notify subscribers.

        This is called from ``RingBufferHandler.emit`` which may run on any
        thread, so we use ``call_soon_threadsafe`` to schedule the async
        broadcast on the event loop.
        """
        self._buffer.append(entry)
        # Fire-and-forget broadcast to SSE subscribers
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(self._sync_broadcast, entry)
        except RuntimeError:
            # No running event loop (e.g. during shutdown) — skip broadcast
            pass

    def _sync_broadcast(self, entry: LogRecord) -> None:
        """Non-async broadcast called via call_soon_threadsafe."""
        dead: list[asyncio.Queue[LogRecord]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                dead.append(queue)
        for q in dead:
            self._subscribers.remove(q)

    # ── Query path (called from dashboard REST endpoint) ─────────────────

    def query(
        self,
        *,
        level: str | None = None,
        name: str | None = None,
        limit: int = 500,
    ) -> list[LogRecord]:
        """Return matching log entries from the ring buffer (newest first).

        Parameters
        ----------
        level:
            Minimum log level filter (e.g. ``"WARNING"``). Records at or
            above this level are returned.
        name:
            Logger name prefix filter (e.g. ``"squadron.agent_manager"``).
            Uses startswith matching.
        limit:
            Maximum number of entries to return (default 500).
        """
        level_num = getattr(logging, level.upper(), None) if level else None

        results: list[LogRecord] = []
        # Iterate newest-first
        for entry in reversed(self._buffer):
            if level_num is not None:
                entry_level = getattr(logging, entry.get("level", "DEBUG"), logging.DEBUG)
                if entry_level < level_num:
                    continue
            if name is not None and not entry.get("name", "").startswith(name):
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    # ── Subscription path (called from dashboard SSE endpoint) ───────────

    async def subscribe(self) -> asyncio.Queue[LogRecord]:
        """Subscribe to live log entries. Returns an asyncio.Queue."""
        queue: asyncio.Queue[LogRecord] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers.append(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[LogRecord]) -> None:
        """Unsubscribe from live log entries."""
        async with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    # ── Introspection ────────────────────────────────────────────────────

    @property
    def size(self) -> int:
        """Current number of entries in the buffer."""
        return len(self._buffer)

    @property
    def maxlen(self) -> int:
        """Maximum capacity of the buffer."""
        return self._buffer.maxlen or 0
