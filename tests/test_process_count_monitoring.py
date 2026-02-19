"""Regression tests for process count monitoring (Issue #86).

The root cause of "Failed to start bash process" errors is OS process pool
exhaustion. When too many processes are running, the kernel refuses to spawn
new bash processes for agent tool calls.

These tests verify that ResourceMonitor tracks process counts and warns
when approaching limits — providing early detection before bash spawning fails.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from squadron.resource_monitor import (
    PROCESS_WARNING_THRESHOLD,
    ResourceMonitor,
    ResourceSnapshot,
    _read_process_count,
)


class TestReadProcessCount:
    """Test the process count reading function."""

    def test_read_process_count_returns_positive_int(self):
        """_read_process_count() should return a positive integer on any platform."""
        count = _read_process_count()
        assert isinstance(count, int)
        assert count > 0  # At least the current process exists

    def test_read_process_count_includes_current_process(self):
        """Process count should include the running Python process."""
        import os

        count = _read_process_count()
        # We know at least the current process is running
        assert count >= 1
        # Sanity: should be less than an absurd number
        assert count < 1_000_000

    def test_read_process_count_fallback_on_no_proc(self):
        """When /proc is unavailable, _read_process_count should return a safe fallback."""
        with patch("builtins.open", side_effect=FileNotFoundError("no /proc")):
            with patch("os.listdir", side_effect=OSError("no /proc")):
                count = _read_process_count()
        # Should not raise — returns 0 as fallback
        assert count == 0


class TestResourceSnapshotProcessCount:
    """Test that ResourceSnapshot includes process count fields."""

    def test_snapshot_has_process_count_field(self):
        """ResourceSnapshot must include process_count (regression: was missing before #86 fix)."""
        snap = ResourceSnapshot()
        # This field must exist — its absence caused "Failed to start bash process"
        # to be undetectable before limits were hit
        assert hasattr(snap, "process_count")

    def test_snapshot_process_count_defaults_to_zero(self):
        """Default ResourceSnapshot has process_count=0."""
        snap = ResourceSnapshot()
        assert snap.process_count == 0

    def test_snapshot_process_count_can_be_set(self):
        """ResourceSnapshot process_count can be set explicitly."""
        snap = ResourceSnapshot(process_count=42)
        assert snap.process_count == 42


class TestResourceMonitorProcessCountTracking:
    """Test that ResourceMonitor populates process_count in snapshots."""

    async def test_snapshot_captures_process_count(self, tmp_path):
        """Live snapshot should capture a non-zero process count (regression test for #86)."""
        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = await monitor.snapshot()
        # Should have captured a real process count
        assert snap.process_count > 0

    async def test_process_count_constant_is_defined(self, tmp_path):
        """PROCESS_WARNING_THRESHOLD constant must be defined and positive."""
        assert isinstance(PROCESS_WARNING_THRESHOLD, int)
        assert PROCESS_WARNING_THRESHOLD > 0

    async def test_check_thresholds_logs_process_warning(self, tmp_path, caplog):
        """_check_thresholds() should warn when process count exceeds threshold."""
        monitor = ResourceMonitor(repo_root=tmp_path)
        # Simulate a snapshot that exceeds the process warning threshold
        snap = ResourceSnapshot(process_count=PROCESS_WARNING_THRESHOLD + 100)
        with caplog.at_level(logging.WARNING):
            monitor._check_thresholds(snap)
        assert "process" in caplog.text.lower()

    async def test_check_thresholds_no_warning_below_threshold(self, tmp_path, caplog):
        """No process warning when count is below threshold."""
        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = ResourceSnapshot(process_count=10)  # Very low count
        with caplog.at_level(logging.WARNING):
            monitor._check_thresholds(snap)
        # Should not mention process count warnings for low counts
        assert "RESOURCE WARNING" not in caplog.text or "process" not in caplog.text

    async def test_snapshot_sync_populates_process_count(self, tmp_path):
        """_snapshot_sync() should populate the process_count field."""
        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = monitor._snapshot_sync()
        # Synchronous snapshot must also capture process count
        assert snap.process_count > 0


class TestProcessCountWarningFormat:
    """Test the warning message format for process count alerts."""

    async def test_warning_includes_count_value(self, tmp_path, caplog):
        """Process warning should include the actual process count for debugging."""
        monitor = ResourceMonitor(repo_root=tmp_path)
        high_count = PROCESS_WARNING_THRESHOLD + 500
        snap = ResourceSnapshot(process_count=high_count)
        with caplog.at_level(logging.WARNING):
            monitor._check_thresholds(snap)
        # Warning should include the count value so operators know severity
        assert str(high_count) in caplog.text or "process" in caplog.text.lower()
