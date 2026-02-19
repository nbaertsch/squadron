"""Regression tests for process count monitoring (Issue #86 / #87).

Before the fix:
- `_read_process_count`, `_get_nproc_limit`, `PROCESS_WARNING_PERCENT` did not exist
- `ResourceSnapshot` had no `process_count` field
- The /health endpoint omitted process_count from the resources dict

These tests fail on the original code and pass after the fix.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import fields
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# 1. Public API — constants and functions must be importable
# ---------------------------------------------------------------------------


def test_process_warning_percent_constant_exists():
    """PROCESS_WARNING_PERCENT must be importable from resource_monitor."""
    from squadron.resource_monitor import PROCESS_WARNING_PERCENT  # noqa: F401

    assert isinstance(PROCESS_WARNING_PERCENT, (int, float))
    assert 0 < PROCESS_WARNING_PERCENT < 100


def test_get_nproc_limit_importable():
    """_get_nproc_limit must be importable and callable."""
    from squadron.resource_monitor import _get_nproc_limit

    result = _get_nproc_limit()
    assert isinstance(result, int)
    assert result >= 0


def test_read_process_count_importable():
    """_read_process_count must be importable and callable."""
    from squadron.resource_monitor import _read_process_count

    result = _read_process_count()
    assert isinstance(result, int)
    assert result >= 0


# ---------------------------------------------------------------------------
# 2. _read_process_count — behaviour on Linux
# ---------------------------------------------------------------------------


def test_read_process_count_returns_positive_int_on_linux():
    """On Linux, _read_process_count() should return a positive integer."""
    if sys.platform != "linux":
        import pytest

        pytest.skip("Linux-only test")
    from squadron.resource_monitor import _read_process_count

    count = _read_process_count()
    assert count > 0, "Expected at least one user process in /proc"


def test_read_process_count_filters_by_uid():
    """_read_process_count() must only count processes owned by os.getuid()."""
    import os
    import squadron.resource_monitor as rm

    current_uid = os.getuid()
    other_uid = 0 if current_uid != 0 else 1

    # One entry owned by current user, one by another UID, one non-numeric
    e1 = MagicMock()
    e1.name = "1234"
    e1.stat.return_value = MagicMock(st_uid=current_uid)

    e2 = MagicMock()
    e2.name = "5678"
    e2.stat.return_value = MagicMock(st_uid=other_uid)

    e3 = MagicMock()
    e3.name = "net"  # non-numeric — should be ignored

    with patch("os.scandir", return_value=[e1, e2, e3]):
        original_platform = rm.sys.platform
        try:
            rm.sys.platform = "linux"
            count = rm._read_process_count()
        finally:
            rm.sys.platform = original_platform

    # Only e1 matches current_uid
    assert count == 1


def test_read_process_count_returns_zero_on_non_linux():
    """On non-Linux platforms, _read_process_count() must return 0."""
    import squadron.resource_monitor as rm

    original_platform = rm.sys.platform
    try:
        rm.sys.platform = "darwin"
        result = rm._read_process_count()
        assert result == 0
    finally:
        rm.sys.platform = original_platform


def test_read_process_count_handles_oserror_gracefully():
    """If /proc is unavailable, _read_process_count() must return 0."""
    import squadron.resource_monitor as rm

    original_platform = rm.sys.platform
    try:
        rm.sys.platform = "linux"
        with patch("os.scandir", side_effect=OSError("no /proc")):
            result = rm._read_process_count()
        assert result == 0
    finally:
        rm.sys.platform = original_platform


# ---------------------------------------------------------------------------
# 3. ResourceSnapshot — process_count field must exist
# ---------------------------------------------------------------------------


def test_resource_snapshot_has_process_count_field():
    """ResourceSnapshot must have a process_count field defaulting to 0."""
    from squadron.resource_monitor import ResourceSnapshot

    field_names = {f.name for f in fields(ResourceSnapshot)}
    assert "process_count" in field_names, "ResourceSnapshot missing process_count field"


def test_resource_snapshot_process_count_default_is_zero():
    """ResourceSnapshot().process_count must default to 0."""
    from squadron.resource_monitor import ResourceSnapshot

    snap = ResourceSnapshot()
    assert snap.process_count == 0


# ---------------------------------------------------------------------------
# 4. ResourceMonitor.snapshot() — populates process_count
# ---------------------------------------------------------------------------


def test_snapshot_populates_process_count():
    """ResourceMonitor.snapshot() must capture a non-zero process count on Linux."""
    if sys.platform != "linux":
        import pytest

        pytest.skip("Linux-only test")
    import asyncio
    from squadron.resource_monitor import ResourceMonitor

    monitor = ResourceMonitor(repo_root=Path("/tmp"))
    snap = asyncio.run(monitor.snapshot())
    assert snap.process_count > 0, "snapshot() should capture running processes on Linux"


# ---------------------------------------------------------------------------
# 5. _check_thresholds — warning fires at/above threshold, not below
# ---------------------------------------------------------------------------


def test_check_thresholds_warns_when_process_count_at_limit():
    """A warning must be logged when process count >= PROCESS_WARNING_PERCENT of nproc limit."""
    from squadron.resource_monitor import ResourceMonitor, ResourceSnapshot, PROCESS_WARNING_PERCENT

    monitor = ResourceMonitor(repo_root=Path("/tmp"))
    nproc_limit = 1000
    # Place count exactly at the threshold
    threshold_count = int(nproc_limit * PROCESS_WARNING_PERCENT / 100)
    snap = ResourceSnapshot(process_count=threshold_count)

    with patch("squadron.resource_monitor._get_nproc_limit", return_value=nproc_limit):
        with patch.object(
            logging.getLogger("squadron.resource_monitor"), "warning"
        ) as mock_warning:
            monitor._check_thresholds(snap)
            assert mock_warning.called, (
                f"Expected warning at process_count={threshold_count} / nproc_limit={nproc_limit} "
                f"(PROCESS_WARNING_PERCENT={PROCESS_WARNING_PERCENT}%)"
            )
            # Confirm the warning message references process issues
            warning_msg = str(mock_warning.call_args)
            assert "bash" in warning_msg.lower() or "process" in warning_msg.lower()


def test_check_thresholds_no_warn_when_below_threshold():
    """No process warning must be logged when process count is well below the nproc limit."""
    from squadron.resource_monitor import ResourceMonitor, ResourceSnapshot, PROCESS_WARNING_PERCENT

    monitor = ResourceMonitor(repo_root=Path("/tmp"))
    nproc_limit = 1000
    # Place count well below the threshold
    safe_count = int(nproc_limit * PROCESS_WARNING_PERCENT / 100) - 10
    snap = ResourceSnapshot(process_count=safe_count)

    with patch("squadron.resource_monitor._get_nproc_limit", return_value=nproc_limit):
        with patch.object(
            logging.getLogger("squadron.resource_monitor"), "warning"
        ) as mock_warning:
            monitor._check_thresholds(snap)
            # Filter to only process-related warnings
            process_warnings = [
                c
                for c in mock_warning.call_args_list
                if "bash" in str(c).lower() or "process count" in str(c).lower()
            ]
            assert not process_warnings, (
                f"Unexpected process warning at count={safe_count} / limit={nproc_limit}"
            )


def test_check_thresholds_skipped_when_nproc_limit_unavailable():
    """When _get_nproc_limit() returns 0 (RLIM_INFINITY / unavailable), no warning fires."""
    from squadron.resource_monitor import ResourceMonitor, ResourceSnapshot

    monitor = ResourceMonitor(repo_root=Path("/tmp"))
    snap = ResourceSnapshot(process_count=99999)  # arbitrarily high count

    with patch("squadron.resource_monitor._get_nproc_limit", return_value=0):
        with patch.object(
            logging.getLogger("squadron.resource_monitor"), "warning"
        ) as mock_warning:
            monitor._check_thresholds(snap)
            process_warnings = [
                c
                for c in mock_warning.call_args_list
                if "bash" in str(c).lower() or "process count" in str(c).lower()
            ]
            assert not process_warnings, "Should not warn when nproc limit is unknown (returns 0)"


# ---------------------------------------------------------------------------
# 6. /health endpoint — process_count must be present in response
# ---------------------------------------------------------------------------


def test_health_endpoint_includes_process_count():
    """The /health endpoint resources dict must include process_count."""
    import inspect
    import squadron.server as srv

    source = inspect.getsource(srv)
    assert "process_count" in source, (
        "server.py must include process_count in the /health endpoint resources dict"
    )
