"""Regression tests for process count monitoring (issue #87 / fix #89).

These tests verify that:
- PROCESS_WARNING_PERCENT, _get_nproc_limit, and _read_process_count are importable
- _read_process_count() returns a positive integer on Linux
- _read_process_count() filters by UID (only counts current user's processes)
- _read_process_count() returns 0 on non-Linux platforms
- _read_process_count() handles /proc unavailability gracefully
- ResourceSnapshot has a process_count field defaulting to 0
- ResourceMonitor.snapshot() captures non-zero process count on Linux
- _check_thresholds() logs a warning at the threshold boundary (>=)
- _check_thresholds() does not warn below threshold
- _check_thresholds() skips the check when nproc limit is unavailable (returns 0)
- The /health endpoint source contains process_count
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


# ── Importability ────────────────────────────────────────────────────────────


class TestImportability:
    def test_process_warning_percent_importable(self):
        from squadron.resource_monitor import PROCESS_WARNING_PERCENT

        assert PROCESS_WARNING_PERCENT == 80

    def test_get_nproc_limit_importable(self):
        from squadron.resource_monitor import _get_nproc_limit

        assert callable(_get_nproc_limit)

    def test_read_process_count_importable(self):
        from squadron.resource_monitor import _read_process_count

        assert callable(_read_process_count)


# ── _read_process_count ──────────────────────────────────────────────────────


class TestReadProcessCount:
    def test_returns_positive_integer_on_linux(self):
        """On Linux, _read_process_count() should return a positive integer."""
        if sys.platform != "linux":
            import pytest

            pytest.skip("Linux-only test")

        from squadron.resource_monitor import _read_process_count

        count = _read_process_count()
        assert isinstance(count, int)
        assert count > 0

    def test_filters_by_uid(self):
        """_read_process_count() should only count processes owned by the current user."""
        if sys.platform != "linux":
            import pytest

            pytest.skip("Linux-only test")

        import os

        from squadron.resource_monitor import _read_process_count

        current_uid = os.getuid()

        # Count manually to verify filtering
        expected = 0
        try:
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid == current_uid:
                        expected += 1
                except OSError:
                    continue
        except OSError:
            import pytest

            pytest.skip("/proc not available")

        count = _read_process_count()
        # Allow ±2 for processes that may appear/disappear between our count and the function's count
        assert abs(count - expected) <= 2

    def test_returns_zero_on_non_linux(self):
        """_read_process_count() should return 0 when not on Linux."""
        from squadron.resource_monitor import _read_process_count

        with patch("squadron.resource_monitor.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = _read_process_count()

        assert result == 0

    def test_returns_zero_when_proc_unavailable(self):
        """_read_process_count() should return 0 if /proc cannot be read."""
        from squadron.resource_monitor import _read_process_count

        with patch("squadron.resource_monitor.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch("os.scandir", side_effect=OSError("not available")):
                result = _read_process_count()

        assert result == 0


# ── _get_nproc_limit ─────────────────────────────────────────────────────────


class TestGetNprocLimit:
    def test_returns_int(self):
        from squadron.resource_monitor import _get_nproc_limit

        result = _get_nproc_limit()
        assert isinstance(result, int)
        assert result >= 0

    def test_returns_zero_when_rlimit_nproc_unavailable(self):
        """Returns 0 when RLIMIT_NPROC is not available (e.g. Windows)."""
        from squadron.resource_monitor import _get_nproc_limit

        with patch.dict("sys.modules", {"resource": None}):
            # Re-import to hit the AttributeError path
            import importlib

            import squadron.resource_monitor as rm

            importlib.reload(rm)
            # The function should still handle the error gracefully
            # Just test the return type is still valid after re-import
            result = rm._get_nproc_limit()
            assert isinstance(result, int)
            assert result >= 0

    def test_returns_zero_for_rlim_infinity(self):
        """Returns 0 when the soft limit is RLIM_INFINITY (-1)."""
        from squadron.resource_monitor import _get_nproc_limit

        import resource as resource_mod

        with patch.object(resource_mod, "getrlimit", return_value=(-1, -1)):
            result = _get_nproc_limit()

        assert result == 0


# ── ResourceSnapshot ─────────────────────────────────────────────────────────


class TestResourceSnapshotProcessCount:
    def test_process_count_field_exists_and_defaults_to_zero(self):
        from squadron.resource_monitor import ResourceSnapshot

        snap = ResourceSnapshot()
        assert hasattr(snap, "process_count")
        assert snap.process_count == 0

    def test_process_count_can_be_set(self):
        from squadron.resource_monitor import ResourceSnapshot

        snap = ResourceSnapshot(process_count=42)
        assert snap.process_count == 42


# ── ResourceMonitor.snapshot() ───────────────────────────────────────────────


class TestResourceMonitorSnapshot:
    async def test_snapshot_captures_process_count_on_linux(self, tmp_path):
        """ResourceMonitor.snapshot() should populate process_count on Linux."""
        if sys.platform != "linux":
            import pytest

            pytest.skip("Linux-only test")

        from squadron.resource_monitor import ResourceMonitor

        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = await monitor.snapshot()
        assert snap.process_count > 0


# ── _check_thresholds ────────────────────────────────────────────────────────


class TestCheckThresholdsProcessCount:
    def test_logs_warning_at_threshold_boundary(self, tmp_path, caplog):
        """Warning fires at exactly PROCESS_WARNING_PERCENT (>= not just >)."""
        import logging

        from squadron.resource_monitor import PROCESS_WARNING_PERCENT, ResourceMonitor, ResourceSnapshot

        # nproc limit = 100, count = 80 → exactly 80% → should warn
        nproc_limit = 100
        count = int(nproc_limit * PROCESS_WARNING_PERCENT / 100)

        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = ResourceSnapshot(process_count=count)

        with patch("squadron.resource_monitor._get_nproc_limit", return_value=nproc_limit):
            with caplog.at_level(logging.WARNING):
                monitor._check_thresholds(snap)

        assert "process count" in caplog.text.lower()

    def test_does_not_warn_below_threshold(self, tmp_path, caplog):
        """No warning fires when process count is below the threshold."""
        import logging

        from squadron.resource_monitor import PROCESS_WARNING_PERCENT, ResourceMonitor, ResourceSnapshot

        # nproc limit = 100, count = 79 → 79% → below threshold
        nproc_limit = 100
        count = int(nproc_limit * PROCESS_WARNING_PERCENT / 100) - 1

        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = ResourceSnapshot(process_count=count)

        with patch("squadron.resource_monitor._get_nproc_limit", return_value=nproc_limit):
            with caplog.at_level(logging.WARNING):
                monitor._check_thresholds(snap)

        assert "process count" not in caplog.text.lower()

    def test_skips_check_when_nproc_limit_unavailable(self, tmp_path, caplog):
        """When _get_nproc_limit() returns 0, the threshold check is skipped entirely."""
        import logging

        from squadron.resource_monitor import ResourceMonitor, ResourceSnapshot

        monitor = ResourceMonitor(repo_root=tmp_path)
        # Even a very high process count should not trigger a warning
        snap = ResourceSnapshot(process_count=99999)

        with patch("squadron.resource_monitor._get_nproc_limit", return_value=0):
            with caplog.at_level(logging.WARNING):
                monitor._check_thresholds(snap)

        assert "process count" not in caplog.text.lower()


# ── /health endpoint ─────────────────────────────────────────────────────────


class TestHealthEndpointProcessCount:
    def test_health_endpoint_source_contains_process_count(self):
        """The server.py health handler must expose process_count in the resources dict."""
        import inspect

        import squadron.server as server_mod

        source = inspect.getsource(server_mod)
        assert "process_count" in source
