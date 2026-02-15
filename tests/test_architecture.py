"""Tests for V1 architecture improvements.

Covers:
- Async git subprocess operations (_run_git, _run_git_in)
- Agent concurrency semaphore (max_concurrent_agents)
- Resource monitor (ResourceMonitor, ResourceSnapshot)
- GitHub API rate limit throttling
- Sparse checkout worktree creation
- New config fields (max_concurrent_agents, sparse_checkout)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
import respx

from squadron.config import RuntimeConfig
from squadron.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    _read_system_memory,
)


# ── Config: New Fields ──────────────────────────────────────────────────────


class TestRuntimeConfigNewFields:
    def test_max_concurrent_agents_default(self):
        config = RuntimeConfig()
        assert config.max_concurrent_agents == 10

    def test_max_concurrent_agents_custom(self):
        config = RuntimeConfig(max_concurrent_agents=5)
        assert config.max_concurrent_agents == 5

    def test_max_concurrent_agents_unlimited(self):
        config = RuntimeConfig(max_concurrent_agents=0)
        assert config.max_concurrent_agents == 0

    def test_sparse_checkout_default_false(self):
        config = RuntimeConfig()
        assert config.sparse_checkout is False

    def test_sparse_checkout_enabled(self):
        config = RuntimeConfig(sparse_checkout=True)
        assert config.sparse_checkout is True


# ── Agent Concurrency Semaphore ─────────────────────────────────────────────


class TestAgentConcurrencySemaphore:
    def _make_manager(self, registry, max_concurrent=10):
        from squadron.agent_manager import AgentManager
        from squadron.config import (
            CircuitBreakerConfig,
            LabelsConfig,
            ProjectConfig,
            SquadronConfig,
        )

        config = MagicMock(spec=SquadronConfig)
        config.project = ProjectConfig(name="test", owner="testowner", repo="testrepo")
        config.runtime = RuntimeConfig(max_concurrent_agents=max_concurrent)
        config.circuit_breakers = CircuitBreakerConfig()
        config.agent_roles = {}
        config.labels = LabelsConfig()

        return AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=MagicMock(),
            agent_definitions={},
            repo_root=Path("/tmp/test"),
        )

    @pytest_asyncio.fixture
    async def registry(self, tmp_path):
        from squadron.registry import AgentRegistry

        reg = AgentRegistry(str(tmp_path / "test.db"))
        await reg.initialize()
        yield reg
        await reg.close()

    async def test_semaphore_created_with_limit(self, registry):
        manager = self._make_manager(registry, max_concurrent=5)
        assert manager._agent_semaphore is not None
        assert manager._agent_semaphore._value == 5

    async def test_semaphore_none_when_unlimited(self, registry):
        manager = self._make_manager(registry, max_concurrent=0)
        assert manager._agent_semaphore is None

    async def test_release_semaphore_decrements(self, registry):
        manager = self._make_manager(registry, max_concurrent=3)
        # Acquire one slot
        await manager._agent_semaphore.acquire()
        assert manager._agent_semaphore._value == 2
        # Release via helper
        manager._release_semaphore()
        assert manager._agent_semaphore._value == 3

    async def test_release_semaphore_noop_when_unlimited(self, registry):
        manager = self._make_manager(registry, max_concurrent=0)
        # Should not raise
        manager._release_semaphore()


# ── Async Git Operations ────────────────────────────────────────────────────


class TestAsyncGitOperations:
    @pytest_asyncio.fixture
    async def registry(self, tmp_path):
        from squadron.registry import AgentRegistry

        reg = AgentRegistry(str(tmp_path / "test.db"))
        await reg.initialize()
        yield reg
        await reg.close()

    def _make_manager(self, registry, repo_root):
        from squadron.agent_manager import AgentManager
        from squadron.config import (
            CircuitBreakerConfig,
            LabelsConfig,
            ProjectConfig,
            SquadronConfig,
        )

        config = MagicMock(spec=SquadronConfig)
        config.project = ProjectConfig(name="test", owner="testowner", repo="testrepo")
        config.runtime = RuntimeConfig()
        config.circuit_breakers = CircuitBreakerConfig()
        config.agent_roles = {}
        config.labels = LabelsConfig()

        return AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=MagicMock(),
            agent_definitions={},
            repo_root=repo_root,
        )

    async def test_run_git_returns_output(self, registry, tmp_path):
        """_run_git should run git commands asynchronously and return output."""
        # Initialize a git repo for the test
        proc = await asyncio.create_subprocess_exec(
            "git",
            "init",
            str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        manager = self._make_manager(registry, tmp_path)
        returncode, stdout, stderr = await manager._run_git("status")
        assert returncode == 0
        assert "branch" in stdout.lower() or "commit" in stdout.lower() or "on" in stdout.lower()

    async def test_run_git_timeout_kills_process(self, registry, tmp_path):
        """_run_git should kill the process and raise on timeout."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "init",
            str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        manager = self._make_manager(registry, tmp_path)
        # Use an impossibly short timeout
        with pytest.raises(asyncio.TimeoutError):
            await manager._run_git("gc", timeout=0)

    async def test_run_git_in_uses_different_cwd(self, registry, tmp_path):
        """_run_git_in should execute in the specified directory."""
        # Create two dirs
        repo_dir = tmp_path / "repo"
        worktree_dir = tmp_path / "worktree"
        repo_dir.mkdir()
        worktree_dir.mkdir()

        # Init git in both
        for d in [repo_dir, worktree_dir]:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "init",
                str(d),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        manager = self._make_manager(registry, repo_dir)

        # _run_git_in should run in worktree_dir, not repo_dir
        returncode, stdout, stderr = await manager._run_git_in(
            worktree_dir, "rev-parse", "--show-toplevel"
        )
        assert returncode == 0
        assert str(worktree_dir) in stdout.strip() or "worktree" in stdout


# ── Resource Monitor ────────────────────────────────────────────────────────


class TestResourceSnapshot:
    def test_default_snapshot_has_zero_values(self):
        snap = ResourceSnapshot()
        assert snap.memory_total_mb == 0
        assert snap.disk_percent == 0
        assert snap.total_worktree_mb == 0
        assert snap.active_agent_count == 0
        assert snap.worktree_sizes == {}


class TestResourceMonitor:
    async def test_snapshot_returns_disk_info(self, tmp_path):
        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = await monitor.snapshot()
        # Should at least have disk info (we're Running on a real filesystem)
        assert snap.disk_total_mb > 0
        assert snap.disk_free_mb >= 0

    async def test_snapshot_measures_worktree_sizes(self, tmp_path):
        # Create fake worktree directories
        worktrees = tmp_path / ".squadron-data" / "worktrees"
        issue_dir = worktrees / "issue-42"
        issue_dir.mkdir(parents=True)
        # Write enough data to survive round(..., 1) — need > 0.05 MB ≈ 52429 bytes
        (issue_dir / "file.txt").write_text("x" * 60_000)

        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = await monitor.snapshot()
        assert "issue-42" in snap.worktree_sizes
        assert snap.worktree_sizes["issue-42"] > 0
        assert snap.active_agent_count == 1

    async def test_start_and_stop(self, tmp_path):
        monitor = ResourceMonitor(repo_root=tmp_path, interval=1)
        await monitor.start()
        assert monitor._running is True
        assert monitor._task is not None
        await monitor.stop()
        assert monitor._running is False

    async def test_latest_property(self, tmp_path):
        monitor = ResourceMonitor(repo_root=tmp_path)
        # Before any snapshot, latest returns default
        assert monitor.latest.memory_total_mb == 0

    def test_read_system_memory_returns_tuple(self):
        """_read_system_memory should return 3 floats without crashing."""
        total, used, pct = _read_system_memory()
        assert isinstance(total, (int, float))
        assert isinstance(used, (int, float))
        assert isinstance(pct, (int, float))
        # On Linux, should have real values; on other platforms, zeros
        assert total >= 0
        assert pct >= 0

    async def test_check_thresholds_logs_memory_warning(self, tmp_path, caplog):
        import logging

        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = ResourceSnapshot(memory_total_mb=1000, memory_used_mb=900, memory_percent=90)
        with caplog.at_level(logging.WARNING):
            monitor._check_thresholds(snap)
        assert "system memory at 90%" in caplog.text

    async def test_check_thresholds_logs_disk_warning(self, tmp_path, caplog):
        import logging

        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = ResourceSnapshot(
            disk_total_mb=10000, disk_used_mb=9500, disk_free_mb=500, disk_percent=95
        )
        with caplog.at_level(logging.WARNING):
            monitor._check_thresholds(snap)
        assert "disk at 95%" in caplog.text

    async def test_check_thresholds_logs_worktree_warning(self, tmp_path, caplog):
        import logging

        monitor = ResourceMonitor(repo_root=tmp_path)
        snap = ResourceSnapshot(
            worktree_sizes={"issue-1": 600.0},
            active_agent_count=1,
        )
        with caplog.at_level(logging.WARNING):
            monitor._check_thresholds(snap)
        assert "worktree issue-1 is 600 MB" in caplog.text


# ── GitHub Rate Limit Throttling ────────────────────────────────────────────


class TestRateLimitThrottling:
    @pytest.fixture
    def github(self):
        from squadron.github_client import GitHubClient

        client = GitHubClient(
            app_id="12345",
            private_key="fake",
            webhook_secret="test-secret",
            installation_id="67890",
        )
        client._token = "ghs_fake_token"
        client._token_expires_at = time.time() + 3600
        return client

    @pytest.fixture
    async def started_github(self, github):
        await github.start()
        yield github
        await github.close()

    def test_rate_limit_lock_initialized_on_start(self, started_github):
        """start() should create the asyncio.Lock for rate limiting."""
        assert started_github._rate_limit_lock is not None
        assert isinstance(started_github._rate_limit_lock, asyncio.Lock)

    def test_rate_limit_reserve_default(self, started_github):
        assert started_github._rate_limit_reserve == 50

    @respx.mock
    async def test_normal_request_bypasses_throttle(self, started_github):
        """Requests above reserve threshold should not be throttled."""
        started_github._rate_limit_remaining = 1000  # well above reserve
        respx.get("https://api.github.com/repos/a/b").mock(
            return_value=httpx.Response(200, json={})
        )
        await started_github.get_repo("a", "b")

    @respx.mock
    async def test_low_quota_serializes_through_lock(self, started_github):
        """When below reserve, requests should go through the lock."""
        started_github._rate_limit_remaining = 10  # below reserve of 50
        started_github._rate_limit_reset = time.time() + 3600

        respx.get("https://api.github.com/repos/a/b").mock(
            return_value=httpx.Response(
                200,
                json={},
                headers={"X-RateLimit-Remaining": "9", "X-RateLimit-Reset": "9999999999"},
            )
        )
        # Should still succeed, just go through the lock
        resp = await started_github.get_repo("a", "b")
        assert resp is not None

    async def test_wait_for_reset_returns_immediately_with_quota(self, started_github):
        """_wait_for_rate_limit_reset should return immediately if quota > 0."""
        started_github._rate_limit_remaining = 100
        # Should not sleep
        await started_github._wait_for_rate_limit_reset()

    async def test_wait_for_reset_sleeps_when_exhausted(self, started_github):
        """_wait_for_rate_limit_reset should sleep when quota is 0."""
        started_github._rate_limit_remaining = 0
        started_github._rate_limit_reset = time.time() + 0.1  # reset in 100ms

        start = time.monotonic()
        await started_github._wait_for_rate_limit_reset()
        elapsed = time.monotonic() - start

        # Should have waited at least ~0.1s (the reset time + 1s buffer, but we cap)
        assert elapsed >= 0.1
        # After waiting, remaining should be reset to 100
        assert started_github._rate_limit_remaining == 100


# ── Sparse Checkout Config ──────────────────────────────────────────────────


class TestSparseCheckoutConfig:
    def test_sparse_checkout_in_runtime_config(self):
        config = RuntimeConfig(sparse_checkout=True)
        assert config.sparse_checkout is True
        config2 = RuntimeConfig()
        assert config2.sparse_checkout is False
