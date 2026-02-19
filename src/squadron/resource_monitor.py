"""Resource monitoring for Squadron agents.

Tracks system resource usage and per-agent disk consumption.
Logs warnings when thresholds are exceeded and exposes metrics
via the /health endpoint.

This is a lightweight monitor — no external dependencies (psutil etc.).
It reads from /proc on Linux and uses shutil.disk_usage as fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Thresholds (configurable later via config.yaml)
MEMORY_WARNING_PERCENT = 85  # warn when system memory usage exceeds this %
DISK_WARNING_PERCENT = 90  # warn when disk usage exceeds this %
WORKTREE_SIZE_WARNING_MB = 500  # warn per worktree exceeding this size
PROCESS_WARNING_PERCENT = 80  # warn when process count reaches this % of nproc limit


@dataclass
class ResourceSnapshot:
    """Point-in-time resource metrics."""

    # System-level
    memory_total_mb: float = 0
    memory_used_mb: float = 0
    memory_percent: float = 0
    disk_total_mb: float = 0
    disk_used_mb: float = 0
    disk_free_mb: float = 0
    disk_percent: float = 0

    # Process count (Linux only; 0 on non-Linux)
    process_count: int = 0

    # Per-agent worktree sizes (agent_id → MB)
    worktree_sizes: dict[str, float] = field(default_factory=dict)

    # Aggregate
    total_worktree_mb: float = 0
    active_agent_count: int = 0


def _read_system_memory() -> tuple[float, float, float]:
    """Read system memory from /proc/meminfo (Linux) or fallback.

    Returns (total_mb, used_mb, percent_used).
    """
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    meminfo[key] = int(parts[1])  # kB

        total_kb = meminfo.get("MemTotal", 0)
        available_kb = meminfo.get("MemAvailable", 0)
        total_mb = total_kb / 1024
        used_mb = (total_kb - available_kb) / 1024
        pct = (used_mb / total_mb * 100) if total_mb > 0 else 0
        return total_mb, used_mb, pct
    except (FileNotFoundError, OSError):
        # Non-Linux or container without /proc — return zeros
        return 0, 0, 0


def _read_process_count() -> int:
    """Count OS processes owned by the current user.

    Reads /proc on Linux, filtering by UID to mirror the scope of
    RLIMIT_NPROC (which is enforced per-user). Kernel threads owned
    by root do not consume the current user's process budget.

    Returns 0 on non-Linux platforms or if /proc is unavailable.
    """
    if sys.platform != "linux":
        return 0

    current_uid = os.getuid()
    count = 0
    try:
        for entry in os.scandir("/proc"):
            # Only numeric directories are process entries
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid == current_uid:
                    count += 1
            except OSError:
                # Process may have exited between scandir and stat — skip it
                continue
    except OSError:
        return 0

    return count


def _get_nproc_limit() -> int:
    """Read the soft nproc limit for the current process at runtime.

    Uses resource.getrlimit(RLIMIT_NPROC) to get the actual enforced limit
    rather than assuming a Docker default.

    Returns 0 if the limit is unavailable (non-Linux, RLIM_INFINITY, or error).
    A return value of 0 signals that threshold checking should be skipped.
    """
    try:
        import resource  # stdlib — Linux/macOS only

        soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
        # resource.RLIM_INFINITY is typically -1; treat as "no limit"
        if soft <= 0:
            return 0
        return soft
    except (AttributeError, ImportError, ValueError, OSError):
        # RLIMIT_NPROC not available (e.g. Windows, or getrlimit failed)
        return 0


def _get_dir_size_mb(path: Path) -> float:
    """Get directory size in MB by walking the tree.

    Uses a fast os.scandir walk to avoid stat overhead.
    Silently skips unreadable entries.
    """
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += _get_dir_size_mb_bytes(entry.path)
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
    return total / (1024 * 1024)


def _get_dir_size_mb_bytes(path: str) -> int:
    """Recursive helper returning bytes."""
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += _get_dir_size_mb_bytes(entry.path)
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
    return total


class ResourceMonitor:
    """Monitors system and per-agent resource usage.

    Runs as a periodic background task within the agent manager's
    event loop.  Logs warnings when resources are under pressure.
    """

    def __init__(self, repo_root: Path, interval: int = 60, worktree_dir: Path | None = None):
        self.repo_root = repo_root
        self.interval = interval
        self._worktree_dir = worktree_dir
        self._task: asyncio.Task | None = None
        self._latest: ResourceSnapshot = ResourceSnapshot()
        self._running = False

    @property
    def latest(self) -> ResourceSnapshot:
        return self._latest

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(), name="resource-monitor")
        logger.info("Resource monitor started (interval=%ds)", self.interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Resource monitor stopped")

    async def snapshot(self) -> ResourceSnapshot:
        """Take a point-in-time resource snapshot.

        This runs blocking I/O in a thread executor to avoid blocking
        the event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._snapshot_sync)

    def _snapshot_sync(self) -> ResourceSnapshot:
        """Synchronous snapshot — runs in thread executor."""
        snap = ResourceSnapshot()

        # System memory
        snap.memory_total_mb, snap.memory_used_mb, snap.memory_percent = _read_system_memory()

        # Disk usage for the data directory
        data_dir = self.repo_root / ".squadron-data"
        try:
            usage = shutil.disk_usage(str(data_dir if data_dir.exists() else self.repo_root))
            snap.disk_total_mb = usage.total / (1024 * 1024)
            snap.disk_used_mb = usage.used / (1024 * 1024)
            snap.disk_free_mb = usage.free / (1024 * 1024)
            snap.disk_percent = (usage.used / usage.total * 100) if usage.total > 0 else 0
        except OSError:
            pass

        # Process count (Linux only; 0 on other platforms)
        snap.process_count = _read_process_count()

        # Per-agent worktree sizes
        worktrees_dir = self._worktree_dir or (data_dir / "worktrees")
        if worktrees_dir.exists():
            try:
                for entry in os.scandir(worktrees_dir):
                    if entry.is_dir():
                        size_mb = _get_dir_size_mb(Path(entry.path))
                        snap.worktree_sizes[entry.name] = round(size_mb, 1)
            except (PermissionError, OSError):
                pass

        snap.total_worktree_mb = sum(snap.worktree_sizes.values())
        snap.active_agent_count = len(snap.worktree_sizes)

        return snap

    async def _monitor_loop(self) -> None:
        """Periodic monitoring loop."""
        while self._running:
            try:
                snap = await self.snapshot()
                self._latest = snap
                self._check_thresholds(snap)
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Resource monitor error")
                await asyncio.sleep(self.interval)

    def _check_thresholds(self, snap: ResourceSnapshot) -> None:
        """Log warnings if resource usage exceeds thresholds."""
        if snap.memory_percent > MEMORY_WARNING_PERCENT:
            logger.warning(
                "RESOURCE WARNING — system memory at %.0f%% (%.0f/%.0f MB)",
                snap.memory_percent,
                snap.memory_used_mb,
                snap.memory_total_mb,
            )

        if snap.disk_percent > DISK_WARNING_PERCENT:
            logger.warning(
                "RESOURCE WARNING — disk at %.0f%% (%.0f MB free)",
                snap.disk_percent,
                snap.disk_free_mb,
            )

        for agent_id, size_mb in snap.worktree_sizes.items():
            if size_mb > WORKTREE_SIZE_WARNING_MB:
                logger.warning(
                    "RESOURCE WARNING — worktree %s is %.0f MB (threshold: %d MB)",
                    agent_id,
                    size_mb,
                    WORKTREE_SIZE_WARNING_MB,
                )

        # Process count threshold check — skipped when nproc limit is unavailable
        nproc_limit = _get_nproc_limit()
        if nproc_limit > 0 and snap.process_count > 0:
            pct = snap.process_count / nproc_limit * 100
            if pct >= PROCESS_WARNING_PERCENT:
                logger.warning(
                    "RESOURCE WARNING — process count at %.0f%% of nproc limit (%d/%d)",
                    pct,
                    snap.process_count,
                    nproc_limit,
                )

        # Periodic info log — only include process count on Linux where it's meaningful
        if snap.active_agent_count > 0:
            if snap.process_count > 0:
                logger.info(
                    "Resource snapshot — mem: %.0f%%, disk: %.0f%%, processes: %d, worktrees: %d (%.0f MB total)",
                    snap.memory_percent,
                    snap.disk_percent,
                    snap.process_count,
                    snap.active_agent_count,
                    snap.total_worktree_mb,
                )
            else:
                logger.info(
                    "Resource snapshot — mem: %.0f%%, disk: %.0f%%, worktrees: %d (%.0f MB total)",
                    snap.memory_percent,
                    snap.disk_percent,
                    snap.active_agent_count,
                    snap.total_worktree_mb,
                )
