"""Tests for the sandboxed worktree execution and host-side auth broker (issue #85)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from squadron.sandbox.audit import SandboxAuditLogger, _sha256_hex
from squadron.sandbox.config import SandboxConfig
from squadron.sandbox.inspector import DiffInspector, InspectionResult, OutputInspector
from squadron.sandbox.namespace import (
    SandboxNamespace,
    _bpf_jump,
    _bpf_stmt,
)
from squadron.sandbox.worktree import EphemeralWorktree


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def sandbox_config(tmp_path: Path) -> SandboxConfig:
    return SandboxConfig(
        enabled=False,
        retention_path=str(tmp_path / "forensics"),
        retention_days=1,
        socket_dir=str(tmp_path / "sockets"),
    )


@pytest.fixture
def sandbox_config_enabled(tmp_path: Path) -> SandboxConfig:
    return SandboxConfig(
        enabled=True,
        retention_path=str(tmp_path / "forensics"),
        retention_days=1,
        socket_dir=str(tmp_path / "sockets"),
        use_overlayfs=False,
    )


# -- SandboxConfig ----------------------------------------------------------


class TestSandboxConfig:
    def test_defaults(self):
        cfg = SandboxConfig()
        assert cfg.enabled is False
        assert cfg.retention_path == "/mnt/squadron-data/forensics"
        assert cfg.retention_days == 1
        assert cfg.session_timeout == 7200
        assert cfg.max_tool_calls_per_session == 200
        assert cfg.seccomp_enabled is True
        assert cfg.diff_inspection_enabled is True
        assert cfg.output_inspection_enabled is True
        assert cfg.block_sensitive_path_changes is True

    def test_sensitive_paths_defaults(self):
        cfg = SandboxConfig()
        assert ".github/**" in cfg.sensitive_paths
        assert "Makefile" in cfg.sensitive_paths
        assert "*.sh" in cfg.sensitive_paths
        assert "pyproject.toml" in cfg.sensitive_paths
        assert "Dockerfile" in cfg.sensitive_paths

    def test_custom_config(self):
        cfg = SandboxConfig(enabled=True, retention_days=7, memory_limit_mb=4096)
        assert cfg.enabled is True
        assert cfg.retention_days == 7
        assert cfg.memory_limit_mb == 4096


# -- SandboxAuditLogger -----------------------------------------------------


class TestSandboxAuditLogger:
    @pytest.mark.asyncio
    async def test_log_and_verify(self, tmp_path: Path):
        audit = SandboxAuditLogger(tmp_path / "audit")
        await audit.start()
        token = secrets.token_bytes(32)
        await audit.log_tool_call(
            agent_id="test-agent-1",
            session_token=token,
            tool="read_issue",
            params={"issue_number": 42},
            response={"title": "Test issue"},
            status="ok",
        )
        await audit.log_tool_call(
            agent_id="test-agent-1",
            session_token=token,
            tool="comment_on_issue",
            params={"issue_number": 42, "body": "Hello"},
            response={"id": 123},
            status="ok",
        )
        ok, msg = audit.verify_chain()
        assert ok, f"Chain verification failed: {msg}"
        assert "2 entries" in msg

    @pytest.mark.asyncio
    async def test_chain_detects_tampering(self, tmp_path: Path):
        audit = SandboxAuditLogger(tmp_path / "audit")
        await audit.start()
        token = secrets.token_bytes(32)
        await audit.log_tool_call(
            agent_id="agent",
            session_token=token,
            tool="read_issue",
            params={},
            response={},
            status="ok",
        )
        log_file = audit._log_file()
        lines = log_file.read_text().strip().splitlines()
        entry = json.loads(lines[0])
        entry["tool"] = "tampered_tool"
        lines[0] = json.dumps(entry, sort_keys=True)
        log_file.write_text(chr(10).join(lines))
        ok, msg = audit.verify_chain(log_file)
        assert not ok, "Chain should fail after tampering"

    @pytest.mark.asyncio
    async def test_session_token_not_stored_raw(self, tmp_path: Path):
        audit = SandboxAuditLogger(tmp_path / "audit")
        await audit.start()
        token = secrets.token_bytes(32)
        await audit.log_tool_call(
            agent_id="agent",
            session_token=token,
            tool="read_issue",
            params={},
            response={},
            status="ok",
        )
        log_content = audit._log_file().read_text()
        assert token.hex() not in log_content, "Raw token must not appear in audit log"
        assert _sha256_hex(token) in log_content

    @pytest.mark.asyncio
    async def test_log_worktree_hash(self, tmp_path: Path):
        audit = SandboxAuditLogger(tmp_path / "audit")
        await audit.start()
        token = secrets.token_bytes(32)
        await audit.log_worktree_hash("agent", token, "abc" * 20)
        ok, msg = audit.verify_chain()
        assert ok, msg

    @pytest.mark.asyncio
    async def test_sequence_numbers_monotonic(self, tmp_path: Path):
        audit = SandboxAuditLogger(tmp_path / "audit")
        await audit.start()
        token = secrets.token_bytes(32)
        for i in range(5):
            await audit.log_tool_call(
                agent_id="agent",
                session_token=token,
                tool=f"tool_{i}",
                params={},
                response={},
                status="ok",
            )
        log_file = audit._log_file()
        seqs = [
            json.loads(line)["seq"]
            for line in log_file.read_text().strip().splitlines()
            if line.strip()
        ]
        assert seqs == list(range(1, 6))

    @pytest.mark.asyncio
    async def test_blocked_call_logged(self, tmp_path: Path):
        audit = SandboxAuditLogger(tmp_path / "audit")
        await audit.start()
        token = secrets.token_bytes(32)
        await audit.log_tool_call(
            agent_id="agent",
            session_token=token,
            tool="delete_repo",
            params={},
            response={"blocked_reason": "not in allowlist"},
            status="blocked",
        )
        log_content = audit._log_file().read_text()
        assert "blocked" in log_content
        assert "delete_repo" in log_content


# -- OutputInspector --------------------------------------------------------


class TestOutputInspector:
    def test_clean_params_pass(self, sandbox_config: SandboxConfig):
        oi = OutputInspector(sandbox_config)
        result = oi.inspect("comment_on_issue", {"body": "Hello world!", "issue_number": 42})
        assert result.passed

    def test_github_token_blocked(self, sandbox_config: SandboxConfig):
        oi = OutputInspector(sandbox_config)
        result = oi.inspect(
            "comment_on_issue",
            {"body": "Token is ghs_abcdefghijklmnopqrstuvwxyz1234567890"},
        )
        assert not result.passed
        assert result.flagged_patterns

    def test_aws_key_blocked(self, sandbox_config: SandboxConfig):
        oi = OutputInspector(sandbox_config)
        result = oi.inspect("comment_on_issue", {"body": "key: AKIAIOSFODNN7EXAMPLE"})
        assert not result.passed

    def test_private_key_pem_blocked(self, sandbox_config: SandboxConfig):
        oi = OutputInspector(sandbox_config)
        result = oi.inspect(
            "comment_on_pr",
            {"body": "-----BEGIN RSA PRIVATE KEY----- content"},
        )
        assert not result.passed

    def test_proc_environ_blocked(self, sandbox_config: SandboxConfig):
        oi = OutputInspector(sandbox_config)
        result = oi.inspect("comment_on_issue", {"body": "see /proc/12345/environ"})
        assert not result.passed

    def test_nested_params_inspected(self, sandbox_config: SandboxConfig):
        oi = OutputInspector(sandbox_config)
        result = oi.inspect(
            "open_pr",
            {"title": "Fix", "body": {"nested": "AKIAIOSFODNN7EXAMPLE"}},
        )
        assert not result.passed

    def test_inspection_disabled(self, tmp_path: Path):
        cfg = SandboxConfig(output_inspection_enabled=False)
        oi = OutputInspector(cfg)
        result = oi.inspect(
            "comment_on_issue",
            {"body": "ghs_abcdefghijklmnopqrstuvwxyz1234567890"},
        )
        assert result.passed, "Inspection disabled should pass everything"

    def test_extra_patterns_respected(self, tmp_path: Path):
        cfg = SandboxConfig(extra_sensitive_patterns=["SUPER_SECRET_[A-Z]+"])
        oi = OutputInspector(cfg)
        result = oi.inspect("comment_on_issue", {"body": "Look: SUPER_SECRET_KEYVALUE"})
        assert not result.passed


# -- DiffInspector ----------------------------------------------------------


class TestDiffInspector:
    def _make_diff(self, files: list[str]) -> str:
        """Create a minimal unified diff string for the given file paths."""
        lines = []
        for f in files:
            lines.append(f"diff --git a/{f} b/{f}")
            lines.append("--- a/" + f)
            lines.append("+++ b/" + f)
            lines.append("@@ -1 +1 @@")
            lines.append("+changed content")
        return chr(10).join(lines)

    def test_clean_diff_passes(self, sandbox_config: SandboxConfig):
        di = DiffInspector(sandbox_config)
        diff = self._make_diff(["src/main.py", "tests/test_main.py"])
        assert di.inspect_diff(diff).passed

    def test_github_workflow_blocked(self, sandbox_config: SandboxConfig):
        di = DiffInspector(sandbox_config)
        diff = self._make_diff([".github/workflows/ci.yml"])
        result = di.inspect_diff(diff)
        assert not result.passed
        assert ".github/workflows/ci.yml" in result.flagged_paths

    def test_makefile_blocked(self, sandbox_config: SandboxConfig):
        di = DiffInspector(sandbox_config)
        assert not di.inspect_diff(self._make_diff(["Makefile"])).passed

    def test_shell_script_blocked(self, sandbox_config: SandboxConfig):
        di = DiffInspector(sandbox_config)
        assert not di.inspect_diff(self._make_diff(["scripts/deploy.sh"])).passed

    def test_git_hook_always_blocked(self, sandbox_config: SandboxConfig):
        di = DiffInspector(sandbox_config)
        diff = "+++ b/.git/hooks/pre-commit" + chr(10) + "+malicious"
        result = di.inspect_diff(diff)
        assert not result.passed
        assert "git hook" in result.reason.lower()

    def test_non_blocking_sensitive_paths(self, tmp_path: Path):
        cfg = SandboxConfig(block_sensitive_path_changes=False)
        di = DiffInspector(cfg)
        result = di.inspect_diff(self._make_diff(["Makefile"]))
        assert result.passed
        assert "Makefile" in result.flagged_paths

    def test_inspection_disabled(self):
        cfg = SandboxConfig(diff_inspection_enabled=False)
        di = DiffInspector(cfg)
        diff = "+++ b/.git/hooks/pre-commit" + chr(10) + "malicious"
        assert di.inspect_diff(diff).passed, "Disabled inspection should pass"

    def test_binary_blob_heuristic(self, sandbox_config: SandboxConfig):
        di = DiffInspector(sandbox_config)
        long_line = "+" + "A" * 11000
        diff = "diff --git a/data.bin b/data.bin" + chr(10)
        diff += "--- a/data.bin" + chr(10)
        diff += "+++ b/data.bin" + chr(10) + long_line
        result = di.inspect_diff(diff)
        assert not result.passed
        assert "long" in result.reason.lower()

    def test_extract_changed_files(self):
        lines = [
            "diff --git a/src/foo.py b/src/foo.py",
            "--- a/src/foo.py",
            "+++ b/src/foo.py",
            "@@ -1 +1 @@",
            "+change",
            "diff --git a/tests/bar.py b/tests/bar.py",
            "--- a/tests/bar.py",
            "+++ b/tests/bar.py",
            "@@ -1 +1 @@",
            "+change",
        ]
        diff = chr(10).join(lines)
        files = DiffInspector._extract_changed_files(diff)
        assert "src/foo.py" in files
        assert "tests/bar.py" in files


# -- SandboxNamespace -------------------------------------------------------


class TestSandboxNamespace:
    def test_disabled_returns_original_command(self):
        cfg = SandboxConfig(enabled=False)
        ns = SandboxNamespace(cfg)
        cmd = ["python", "agent.py"]
        assert ns.wrap_command(cmd) == cmd

    def test_enabled_without_unshare_warns(self, caplog):
        cfg = SandboxConfig(enabled=True)
        ns = SandboxNamespace(cfg)
        ns._available = False  # force unavailable
        import logging

        with caplog.at_level(logging.WARNING):
            result = ns.wrap_command(["python", "agent.py"])
        assert result == ["python", "agent.py"]
        assert "unshare" in caplog.text.lower()

    def test_wrap_command_includes_namespace_flags(self):
        cfg = SandboxConfig(
            enabled=True,
            namespace_mount=True,
            namespace_pid=True,
            namespace_net=True,
            namespace_ipc=True,
            namespace_uts=True,
        )
        ns = SandboxNamespace(cfg)
        ns._available = True
        wrapped = ns.wrap_command(["python", "agent.py"])
        assert "unshare" in wrapped[0]
        assert "--mount" in wrapped
        assert "--pid" in wrapped
        assert "--fork" in wrapped
        assert "--net" in wrapped
        assert "--ipc" in wrapped
        assert "--uts" in wrapped
        assert "--map-root-user" in wrapped
        assert "python" in wrapped

    def test_wrap_command_selective_namespaces(self):
        cfg = SandboxConfig(
            enabled=True,
            namespace_mount=False,
            namespace_pid=False,
            namespace_net=True,
            namespace_ipc=False,
            namespace_uts=False,
        )
        ns = SandboxNamespace(cfg)
        ns._available = True
        wrapped = ns.wrap_command(["cmd"])
        assert "--net" in wrapped
        assert "--mount" not in wrapped
        assert "--pid" not in wrapped
        assert "--fork" not in wrapped

    def test_seccomp_disabled_when_sandbox_off(self):
        cfg = SandboxConfig(enabled=False)
        assert SandboxNamespace(cfg).apply_seccomp_filter() is False

    def test_seccomp_disabled_via_config(self):
        cfg = SandboxConfig(enabled=True, seccomp_enabled=False)
        assert SandboxNamespace(cfg).apply_seccomp_filter() is False

    def test_bpf_instructions_encode_to_8_bytes(self):
        assert len(_bpf_stmt(0x06, 0x7FFF0000)) == 8
        assert len(_bpf_jump(0x15, 1, 0, 0)) == 8


# -- EphemeralWorktree ------------------------------------------------------


class TestEphemeralWorktree:
    @pytest.mark.asyncio
    async def test_tmpfs_copy_created(self, tmp_path: Path):
        """When overlayfs is disabled, a tmpfs copy should be created."""
        cfg = SandboxConfig(
            enabled=True,
            use_overlayfs=False,
            retention_path=str(tmp_path / "forensics"),
        )
        wt = EphemeralWorktree(cfg, tmp_path / "sandboxes")

        git_worktree = tmp_path / "worktree"
        git_worktree.mkdir()
        (git_worktree / "src").mkdir()
        (git_worktree / "src" / "main.py").write_text("print(42)")
        (git_worktree / "README.md").write_text("# Test")

        agents_dir = tmp_path / ".squadron" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "feat-dev.md").write_text("---\nname: feat-dev\n---\nPrompt")

        info = await wt.create(
            agent_id="test-agent",
            repo_root=tmp_path,
            git_worktree=git_worktree,
            agents_dir=agents_dir,
        )

        assert info.is_active
        assert info.merged_dir.exists()
        assert (info.merged_dir / "README.md").exists()
        assert (info.merged_dir / "src" / "main.py").exists()

        await wt.wipe(info)
        assert not info.base_dir.exists()

    @pytest.mark.asyncio
    async def test_diff_collection_graceful_on_non_git(self, tmp_path: Path):
        """collect_diff returns empty string on non-git directory."""
        cfg = SandboxConfig(use_overlayfs=False, retention_path=str(tmp_path))
        wt = EphemeralWorktree(cfg, tmp_path / "sandboxes")
        from squadron.sandbox.worktree import WorktreeInfo

        info = WorktreeInfo(
            base_dir=tmp_path / "base",
            lower_dir=tmp_path / "lower",
            upper_dir=tmp_path / "upper",
            work_dir=tmp_path / "work",
            merged_dir=tmp_path / "nonexistent",
            agent_def_dir=tmp_path / "agents",
        )
        diff = await wt.collect_diff(info)
        assert diff == ""

    def test_hash_diff_is_sha256(self, tmp_path: Path):
        cfg = SandboxConfig(retention_path=str(tmp_path))
        wt = EphemeralWorktree(cfg, tmp_path)
        diff = "+ added line\n- removed line\n"
        h = wt.hash_diff(diff)
        assert h == hashlib.sha256(diff.encode()).hexdigest()

    @pytest.mark.asyncio
    async def test_forensic_preservation(self, tmp_path: Path):
        cfg = SandboxConfig(
            retention_path=str(tmp_path / "forensics"),
            retention_days=1,
            use_overlayfs=False,
        )
        wt = EphemeralWorktree(cfg, tmp_path / "sandboxes")
        from squadron.sandbox.worktree import WorktreeInfo

        merged_dir = tmp_path / "base" / "merged"
        merged_dir.mkdir(parents=True)
        (merged_dir / "evidence.txt").write_text("suspicious")
        info = WorktreeInfo(
            base_dir=tmp_path / "base",
            lower_dir=tmp_path / "lower",
            upper_dir=tmp_path / "upper",
            work_dir=tmp_path / "work",
            merged_dir=merged_dir,
            agent_def_dir=tmp_path / "agents",
            is_overlayfs=False,
            is_active=True,
        )
        preserved = await wt.preserve_for_forensics(info, "test-agent", "test failure")
        assert preserved.exists()
        assert (preserved / "evidence.txt").exists()
        assert (preserved / ".sandbox-exit-reason.txt").exists()
        reason_content = (preserved / ".sandbox-exit-reason.txt").read_text()
        assert "test-agent" in reason_content
        assert "test failure" in reason_content

    @pytest.mark.asyncio
    async def test_purge_stale_forensics(self, tmp_path: Path):
        import time

        cfg = SandboxConfig(retention_path=str(tmp_path / "forensics"), retention_days=0)
        wt = EphemeralWorktree(cfg, tmp_path / "sandboxes")
        retention_dir = Path(cfg.retention_path)
        retention_dir.mkdir(parents=True)
        stale = retention_dir / "old-agent-12345"
        stale.mkdir()
        old_mtime = time.time() - 90000  # 25 hours ago
        os.utime(str(stale), (old_mtime, old_mtime))
        purged = await wt.purge_stale_forensics()
        assert purged == 1
        assert not stale.exists()


# -- Config Integration -----------------------------------------------------


class TestConfigIntegration:
    def test_get_sandbox_config_from_dict(self):
        from squadron.config import SquadronConfig

        config = SquadronConfig(
            project={"name": "test"},
            sandbox={"enabled": True, "retention_days": 7},
        )
        sb = config.get_sandbox_config()
        assert sb.enabled is True
        assert sb.retention_days == 7

    def test_get_sandbox_config_defaults(self):
        from squadron.config import SquadronConfig

        config = SquadronConfig(project={"name": "test"}, sandbox={})
        sb = config.get_sandbox_config()
        assert sb.enabled is False
        assert sb.retention_path == "/mnt/squadron-data/forensics"

    def test_sandbox_env_override_enabled(self, tmp_path: Path, monkeypatch):
        import yaml

        monkeypatch.setenv("SQUADRON_SANDBOX_ENABLED", "true")
        sq = tmp_path / ".squadron"
        sq.mkdir()
        (sq / "config.yaml").write_text(
            yaml.dump(
                {
                    "project": {"name": "test"},
                    "sandbox": {"enabled": False},
                }
            )
        )
        from squadron.config import load_config

        config = load_config(sq)
        assert config.get_sandbox_config().enabled is True

    def test_sandbox_retention_path_env_override(self, tmp_path: Path, monkeypatch):
        import yaml

        monkeypatch.setenv("SQUADRON_SANDBOX_RETENTION_PATH", "/custom/forensics")
        sq = tmp_path / ".squadron"
        sq.mkdir()
        (sq / "config.yaml").write_text(yaml.dump({"project": {"name": "test"}}))
        from squadron.config import load_config

        config = load_config(sq)
        assert config.get_sandbox_config().retention_path == "/custom/forensics"


# -- AuthBroker -------------------------------------------------------------


class TestAuthBroker:
    @pytest.mark.asyncio
    async def test_session_registration(self):
        from squadron.sandbox.broker import AuthBroker

        mock_github = MagicMock()
        broker = AuthBroker(mock_github)
        token = secrets.token_bytes(32)
        broker.register_session("agent-1", token)
        assert broker.is_valid_session(token, "agent-1")
        assert not broker.is_valid_session(token, "agent-2")  # wrong agent
        broker.unregister_session(token)
        assert not broker.is_valid_session(token, "agent-1")

    @pytest.mark.asyncio
    async def test_invalid_session_rejected(self):
        from squadron.sandbox.broker import AuthBroker, BrokerRequest

        mock_github = MagicMock()
        broker = AuthBroker(mock_github)
        await broker.start()
        try:
            token = secrets.token_bytes(32)
            response_q: asyncio.Queue = asyncio.Queue(maxsize=1)
            req = BrokerRequest(
                agent_id="agent-1",
                session_token=token,
                tool="read_issue",
                params={"issue_number": 42},
                response_queue=response_q,
            )
            resp = await broker.submit(req)
            assert not resp.ok
            assert "invalid" in resp.error.lower() or "expired" in resp.error.lower()
        finally:
            await broker.stop()

    @pytest.mark.asyncio
    async def test_registered_session_dispatches(self):
        from squadron.sandbox.broker import AuthBroker, BrokerRequest

        mock_github = AsyncMock()
        mock_github.get_issue = AsyncMock(return_value={"number": 42, "title": "Test"})
        broker = AuthBroker(mock_github)
        await broker.start()
        try:
            token = secrets.token_bytes(32)
            broker.register_session("agent-1", token)
            response_q: asyncio.Queue = asyncio.Queue(maxsize=1)
            req = BrokerRequest(
                agent_id="agent-1",
                session_token=token,
                params={"issue_number": 42, "_owner": "org", "_repo": "repo"},
                tool="read_issue",
                response_queue=response_q,
            )
            resp = await broker.submit(req)
            assert resp.ok
            assert resp.data == {"number": 42, "title": "Test"}
            mock_github.get_issue.assert_called_once_with("org", "repo", 42)
        finally:
            await broker.stop()


# -- ToolProxy --------------------------------------------------------------


class TestToolProxy:
    @pytest.mark.asyncio
    async def test_allowlist_enforcement(self, tmp_path: Path):
        from squadron.sandbox.proxy import ToolProxy

        cfg = SandboxConfig(socket_dir=str(tmp_path / "sockets"))
        token = secrets.token_bytes(32)
        mock_audit = AsyncMock()
        mock_oi = MagicMock()
        mock_oi.inspect.return_value = MagicMock(passed=True)
        proxy = ToolProxy(
            agent_id="agent-1",
            issue_number=42,
            session_token=token,
            allowed_tools=["read_issue", "comment_on_issue"],
            broker=MagicMock(),
            audit=mock_audit,
            output_inspector=mock_oi,
            config=cfg,
            owner="org",
            repo="repo",
        )
        result = await proxy._process_request(
            {"token": token.hex(), "tool": "delete_repo", "params": {}}
        )
        assert not result["ok"]
        assert "not-permitted" in result["error"]
        call_kwargs = mock_audit.log_tool_call.call_args[1]
        assert call_kwargs["status"] == "blocked"

    @pytest.mark.asyncio
    async def test_scope_validation(self, tmp_path: Path):
        from squadron.sandbox.proxy import ToolProxy

        cfg = SandboxConfig(socket_dir=str(tmp_path / "sockets"))
        token = secrets.token_bytes(32)
        mock_oi = MagicMock()
        mock_oi.inspect.return_value = MagicMock(passed=True)
        proxy = ToolProxy(
            agent_id="agent-issue-42",
            issue_number=42,
            session_token=token,
            allowed_tools=["comment_on_issue"],
            broker=MagicMock(),
            audit=AsyncMock(),
            output_inspector=mock_oi,
            config=cfg,
            owner="org",
            repo="repo",
        )
        # Attempt to write to a different issue (cross-issue violation)
        result = await proxy._process_request(
            {
                "token": token.hex(),
                "tool": "comment_on_issue",
                "params": {"issue_number": 99, "body": "Hello"},
            }
        )
        assert not result["ok"]
        assert "not-permitted" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, tmp_path: Path):
        from squadron.sandbox.proxy import ToolProxy

        cfg = SandboxConfig(socket_dir=str(tmp_path / "sockets"))
        token = secrets.token_bytes(32)
        wrong_token = secrets.token_bytes(32)
        proxy = ToolProxy(
            agent_id="agent-1",
            issue_number=42,
            session_token=token,
            allowed_tools=["read_issue"],
            broker=MagicMock(),
            audit=AsyncMock(),
            output_inspector=MagicMock(),
            config=cfg,
            owner="org",
            repo="repo",
        )
        result = await proxy._process_request(
            {"token": wrong_token.hex(), "tool": "read_issue", "params": {}}
        )
        assert not result["ok"]
        assert "session token" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rate_limiting(self, tmp_path: Path):
        from squadron.sandbox.broker import BrokerResponse
        from squadron.sandbox.proxy import ToolProxy

        cfg = SandboxConfig(
            socket_dir=str(tmp_path / "sockets"),
            max_tool_calls_per_session=2,
            timing_floor_ms=0,
        )
        token = secrets.token_bytes(32)
        mock_broker = AsyncMock()
        mock_broker.submit = AsyncMock(return_value=BrokerResponse(ok=True, data={"ok": True}))
        mock_oi = MagicMock()
        mock_oi.inspect.return_value = MagicMock(passed=True)
        proxy = ToolProxy(
            agent_id="agent-1",
            issue_number=42,
            session_token=token,
            allowed_tools=["read_issue"],
            broker=mock_broker,
            audit=AsyncMock(),
            output_inspector=mock_oi,
            config=cfg,
            owner="org",
            repo="repo",
        )
        for _ in range(2):
            r = await proxy._process_request(
                {
                    "token": token.hex(),
                    "tool": "read_issue",
                    "params": {"issue_number": 42},
                }
            )
            assert r["ok"], f"Expected ok but got {r}"
        # Third call should hit rate limit
        result = await proxy._process_request(
            {
                "token": token.hex(),
                "tool": "read_issue",
                "params": {"issue_number": 42},
            }
        )
        assert not result["ok"]
        assert "rate limit" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_output_inspection_blocks(self, tmp_path: Path):
        from squadron.sandbox.proxy import ToolProxy

        cfg = SandboxConfig(socket_dir=str(tmp_path / "sockets"), timing_floor_ms=0)
        token = secrets.token_bytes(32)
        mock_oi = MagicMock()
        mock_oi.inspect.return_value = InspectionResult(
            passed=False, reason="sensitive token detected"
        )
        proxy = ToolProxy(
            agent_id="agent-1",
            issue_number=42,
            session_token=token,
            allowed_tools=["comment_on_issue"],
            broker=AsyncMock(),
            audit=AsyncMock(),
            output_inspector=mock_oi,
            config=cfg,
            owner="org",
            repo="repo",
        )
        result = await proxy._process_request(
            {
                "token": token.hex(),
                "tool": "comment_on_issue",
                "params": {"issue_number": 42, "body": "leaking secret"},
            }
        )
        assert not result["ok"]
        assert "sensitive token detected" in result["error"]


# -- SandboxCA (Issue #146) ------------------------------------------------


class TestSandboxCA:
    def test_generate_ca(self, tmp_path: Path):
        from squadron.sandbox.ca import SandboxCA

        ca = SandboxCA(str(tmp_path / "ca"), validity_days=1)
        ca.ensure_ca()
        assert ca.cert_path.exists()
        assert ca.key_path.exists()
        # Cert should be PEM-encoded
        cert_content = ca.cert_path.read_text()
        assert "BEGIN CERTIFICATE" in cert_content

    def test_load_existing_ca(self, tmp_path: Path):
        from squadron.sandbox.ca import SandboxCA

        ca = SandboxCA(str(tmp_path / "ca"), validity_days=1)
        ca.ensure_ca()
        # Load again — should reuse existing files
        ca2 = SandboxCA(str(tmp_path / "ca"), validity_days=1)
        ca2.ensure_ca()
        assert ca2.cert_path.read_bytes() == ca.cert_path.read_bytes()

    def test_sign_leaf_cert(self, tmp_path: Path):
        from squadron.sandbox.ca import SandboxCA

        ca = SandboxCA(str(tmp_path / "ca"), validity_days=1)
        ca.ensure_ca()
        cert_pem, key_pem = ca.sign_leaf("api.anthropic.com")
        assert b"BEGIN CERTIFICATE" in cert_pem
        assert b"BEGIN PRIVATE KEY" in key_pem

    def test_sign_leaf_without_init_raises(self, tmp_path: Path):
        from squadron.sandbox.ca import SandboxCA

        ca = SandboxCA(str(tmp_path / "ca"))
        with pytest.raises(RuntimeError, match="CA not initialised"):
            ca.sign_leaf("example.com")

    def test_ca_key_permissions(self, tmp_path: Path):
        from squadron.sandbox.ca import SandboxCA

        ca = SandboxCA(str(tmp_path / "ca"), validity_days=1)
        ca.ensure_ca()
        key_mode = oct(ca.key_path.stat().st_mode)[-3:]
        assert key_mode == "600", f"CA key should be owner-only, got {key_mode}"


# -- NetworkBridge (Issue #146) ---------------------------------------------


class TestNetworkBridge:
    def test_unavailable_on_non_linux(self, tmp_path: Path):
        from squadron.sandbox.net_bridge import NetworkBridge

        cfg = SandboxConfig(enabled=True)
        bridge = NetworkBridge(cfg)
        # On non-Linux (WSL/Windows test runner), bridge may or may not be available.
        # We test the interface contract: if unavailable, create_veth returns None.
        if not bridge.is_available:
            import asyncio

            result = asyncio.get_event_loop().run_until_complete(bridge.create_veth("agent-1", 1))
            assert result is None

    def test_wrap_command_in_netns_when_unavailable(self, tmp_path: Path):
        from squadron.sandbox.net_bridge import NetworkBridge, VethPair

        cfg = SandboxConfig(enabled=False)
        bridge = NetworkBridge(cfg)
        veth = VethPair(
            agent_id="test",
            agent_index=1,
            host_iface="sq-vh-test",
            agent_iface="sq-va-test",
            agent_ip="10.146.1.2",
            netns_name="sq-ns-test",
        )
        cmd = ["python", "agent.py"]
        # When unavailable, returns original command
        assert bridge.wrap_command_in_netns(veth, cmd) == cmd

    def test_wrap_command_in_netns_when_available(self, tmp_path: Path):
        from squadron.sandbox.net_bridge import NetworkBridge, VethPair

        cfg = SandboxConfig(enabled=True)
        bridge = NetworkBridge(cfg)
        bridge._available = True  # force available for test
        veth = VethPair(
            agent_id="test",
            agent_index=1,
            host_iface="sq-vh-test",
            agent_iface="sq-va-test",
            agent_ip="10.146.1.2",
            netns_name="sq-ns-test",
        )
        cmd = ["python", "agent.py"]
        wrapped = bridge.wrap_command_in_netns(veth, cmd)
        assert wrapped[:4] == ["ip", "netns", "exec", "sq-ns-test"]
        assert wrapped[4:] == cmd

    def test_allocate_index_monotonic(self, tmp_path: Path):
        from squadron.sandbox.net_bridge import NetworkBridge

        cfg = SandboxConfig(enabled=True)
        bridge = NetworkBridge(cfg)
        assert bridge.allocate_index() == 1
        assert bridge.allocate_index() == 2
        assert bridge.allocate_index() == 3


# -- Environment Scrubbing (Issue #146) ------------------------------------


class TestEnvScrubbing:
    def test_strips_static_secrets(self, monkeypatch, tmp_path: Path):
        from squadron.sandbox.env_scrub import build_sanitized_env

        cfg = SandboxConfig()
        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        monkeypatch.setenv("GITHUB_PRIVATE_KEY", "secret-key-data")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_abc123")
        monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "dashboard-key")
        monkeypatch.setenv("PATH", "/usr/bin")

        env = build_sanitized_env(cfg)
        assert "GITHUB_APP_ID" not in env
        assert "GITHUB_PRIVATE_KEY" not in env
        assert "COPILOT_GITHUB_TOKEN" not in env
        assert "SQUADRON_DASHBOARD_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"

    def test_strips_dynamic_byok_vars(self, monkeypatch, tmp_path: Path):
        from squadron.sandbox.env_scrub import build_sanitized_env

        cfg = SandboxConfig()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-xxx")
        monkeypatch.setenv("PATH", "/usr/bin")

        env = build_sanitized_env(cfg, extra_strip=["ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env

    def test_pattern_based_stripping(self, monkeypatch, tmp_path: Path):
        from squadron.sandbox.env_scrub import build_sanitized_env

        cfg = SandboxConfig()
        monkeypatch.setenv("CUSTOM_API_KEY", "some-value")
        monkeypatch.setenv("MY_SECRET_KEY", "hidden")
        monkeypatch.setenv("SAFE_VARIABLE", "visible")

        env = build_sanitized_env(cfg)
        assert "CUSTOM_API_KEY" not in env
        assert "MY_SECRET_KEY" not in env
        assert "SAFE_VARIABLE" in env

    def test_injects_ca_cert_path(self, tmp_path: Path):
        from squadron.sandbox.env_scrub import build_sanitized_env

        cfg = SandboxConfig()
        cert_path = tmp_path / "ca.crt"
        cert_path.write_text("cert data")

        env = build_sanitized_env(cfg, ca_cert_path=cert_path)
        assert env["SSL_CERT_FILE"] == str(cert_path)
        assert env["NODE_EXTRA_CA_CERTS"] == str(cert_path)
        assert env["REQUESTS_CA_BUNDLE"] == str(cert_path)

    def test_injects_socket_and_token(self, tmp_path: Path):
        from squadron.sandbox.env_scrub import build_sanitized_env

        cfg = SandboxConfig()
        socket_path = tmp_path / "agent.sock"

        env = build_sanitized_env(cfg, socket_path=socket_path, session_token_hex="deadbeef")
        assert env["SQUADRON_PROXY_SOCKET"] == str(socket_path)
        assert env["SQUADRON_SESSION_TOKEN"] == "deadbeef"

    def test_get_dynamic_byok_vars(self, monkeypatch):
        from squadron.sandbox.env_scrub import get_dynamic_byok_vars

        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.setenv("OPENAI_API_KEY", "key2")

        result = get_dynamic_byok_vars("MY_CUSTOM_KEY")
        assert "MY_CUSTOM_KEY" in result
        assert "ANTHROPIC_API_KEY" in result
        assert "OPENAI_API_KEY" in result

    def test_does_not_mutate_os_environ(self, monkeypatch, tmp_path: Path):
        from squadron.sandbox.env_scrub import build_sanitized_env

        cfg = SandboxConfig()
        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        original_id = os.environ.get("GITHUB_APP_ID")

        build_sanitized_env(cfg)
        # os.environ should be unchanged
        assert os.environ.get("GITHUB_APP_ID") == original_id


# -- InferenceProxy (Issue #146) -------------------------------------------


class TestInferenceProxy:
    def test_credential_injection_anthropic(self):
        from squadron.sandbox.inference_proxy import InferenceProxy

        cfg = SandboxConfig(enabled=True)
        mock_ca = MagicMock()
        proxy = InferenceProxy(
            config=cfg,
            ca=mock_ca,
            credentials={"anthropic_key": "sk-ant-test"},
        )
        headers = proxy._inject_credentials(
            "api.anthropic.com", {"host": "api.anthropic.com", "content-type": "application/json"}
        )
        assert headers["x-api-key"] == "sk-ant-test"
        assert "authorization" not in headers

    def test_credential_injection_openai(self):
        from squadron.sandbox.inference_proxy import InferenceProxy

        cfg = SandboxConfig(enabled=True)
        proxy = InferenceProxy(
            config=cfg, ca=MagicMock(), credentials={"openai_key": "sk-openai-test"}
        )
        headers = proxy._inject_credentials("api.openai.com", {"host": "api.openai.com"})
        assert headers["authorization"] == "Bearer sk-openai-test"

    def test_credential_injection_copilot(self):
        from squadron.sandbox.inference_proxy import InferenceProxy

        cfg = SandboxConfig(enabled=True)
        proxy = InferenceProxy(
            config=cfg, ca=MagicMock(), credentials={"copilot_token": "ghu_test"}
        )
        headers = proxy._inject_credentials(
            "api.githubcopilot.com", {"host": "api.githubcopilot.com"}
        )
        assert headers["authorization"] == "Bearer ghu_test"

    def test_strips_existing_auth_headers(self):
        from squadron.sandbox.inference_proxy import InferenceProxy

        cfg = SandboxConfig(enabled=True)
        proxy = InferenceProxy(config=cfg, ca=MagicMock(), credentials={})
        headers = proxy._inject_credentials(
            "unknown.host.com",
            {"authorization": "Bearer leaked-token", "x-api-key": "leaked-key"},
        )
        # Without credentials for this host, auth headers should be stripped
        assert "authorization" not in headers
        assert "x-api-key" not in headers

    def test_byok_fallback_injection(self):
        from squadron.sandbox.inference_proxy import InferenceProxy

        cfg = SandboxConfig(enabled=True)
        proxy = InferenceProxy(
            config=cfg, ca=MagicMock(), credentials={"byok_key": "sk-custom-123"}
        )
        headers = proxy._inject_credentials(
            "custom-provider.example.com", {"host": "custom-provider.example.com"}
        )
        assert headers["authorization"] == "Bearer sk-custom-123"

    def test_build_credentials_from_env(self, monkeypatch):
        from squadron.sandbox.inference_proxy import build_credentials_from_env

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_copilot")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")

        creds = build_credentials_from_env("copilot", "")
        assert creds["copilot_token"] == "ghu_copilot"
        assert creds["anthropic_key"] == "sk-ant-xxx"

    def test_build_credentials_byok_anthropic(self, monkeypatch):
        from squadron.sandbox.inference_proxy import build_credentials_from_env

        monkeypatch.setenv("MY_KEY", "sk-ant-custom")
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        creds = build_credentials_from_env("anthropic", "MY_KEY")
        assert creds["anthropic_key"] == "sk-ant-custom"
        assert "copilot_token" not in creds

    def test_build_credentials_byok_openai(self, monkeypatch):
        from squadron.sandbox.inference_proxy import build_credentials_from_env

        monkeypatch.setenv("MY_KEY", "sk-openai-custom")
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        creds = build_credentials_from_env("openai", "MY_KEY")
        assert creds["openai_key"] == "sk-openai-custom"


# -- SandboxNamespace (Issue #146 — bridge integration) --------------------


class TestSandboxNamespaceBridge:
    """Tests for the bridge_net flag added in Issue #146."""

    def test_bridge_net_omits_net_flag(self):
        cfg = SandboxConfig(
            enabled=True,
            namespace_net=True,
        )
        ns = SandboxNamespace(cfg, use_bridge_net=True)
        ns._available = True
        wrapped = ns.wrap_command(["python", "agent.py"])
        # --net should NOT be in the command when bridge handles networking
        assert "--net" not in wrapped
        # Other ns flags should still be present
        assert "--mount" in wrapped
        assert "--pid" in wrapped

    def test_no_bridge_net_includes_net_flag(self):
        cfg = SandboxConfig(
            enabled=True,
            namespace_net=True,
        )
        ns = SandboxNamespace(cfg, use_bridge_net=False)
        ns._available = True
        wrapped = ns.wrap_command(["python", "agent.py"])
        # --net SHOULD be present when bridge is not active
        assert "--net" in wrapped


# -- SandboxConfig (Issue #146 — new fields) --------------------------------


class TestSandboxConfigIssue146:
    def test_new_defaults(self):
        cfg = SandboxConfig()
        assert cfg.bridge_name == "sq-br0"
        assert cfg.bridge_subnet == "10.146.0.0/16"
        assert cfg.bridge_ip == "10.146.0.1"
        assert cfg.proxy_port == 8443
        assert cfg.ca_dir == "/tmp/squadron-ca"
        assert cfg.ca_validity_days == 1
        assert "GITHUB_APP_ID" in cfg.secret_env_vars
        assert "COPILOT_GITHUB_TOKEN" in cfg.secret_env_vars
        assert "SQUADRON_DASHBOARD_API_KEY" in cfg.secret_env_vars

    def test_custom_bridge_config(self):
        cfg = SandboxConfig(
            bridge_name="custom-br0",
            bridge_subnet="172.20.0.0/24",
            bridge_ip="172.20.0.1",
            proxy_port=9443,
        )
        assert cfg.bridge_name == "custom-br0"
        assert cfg.bridge_subnet == "172.20.0.0/24"
        assert cfg.proxy_port == 9443


# -- SandboxManager (Issue #146 — integration) -----------------------------


class TestSandboxManagerIssue146:
    @pytest.mark.asyncio
    async def test_session_creates_sanitized_env(self, tmp_path: Path, monkeypatch):
        """When sandbox is enabled, sessions should have sanitized env."""
        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_test")
        monkeypatch.setenv("PATH", "/usr/bin")

        cfg = SandboxConfig(
            enabled=True,
            retention_path=str(tmp_path / "forensics"),
            socket_dir=str(tmp_path / "sockets"),
            use_overlayfs=False,
            ca_dir=str(tmp_path / "ca"),
        )
        mock_github = MagicMock()
        from squadron.sandbox.manager import SandboxManager

        mgr = SandboxManager(
            config=cfg,
            github=mock_github,
            repo_root=tmp_path,
            owner="org",
            repo="repo",
            provider_type="copilot",
            provider_api_key_env="",
        )

        # Mock infrastructure so we don't need real Linux networking.
        mgr._bridge = MagicMock()
        mgr._bridge.is_available = False  # Skip bridge/proxy creation
        mgr._bridge.setup_bridge = AsyncMock(return_value=False)
        mgr._bridge.teardown_bridge = AsyncMock()

        # Start manager (CA will init; bridge/proxy are mocked).
        await mgr.start()
        try:
            # Mock the components that create_session calls internally.
            # ToolProxy — mock so it doesn't bind a real Unix socket.
            mock_proxy = MagicMock()
            mock_proxy.start = AsyncMock()
            mock_proxy.stop = AsyncMock()
            mock_proxy.socket_path = str(tmp_path / "sockets" / "test-agent-1.sock")
            monkeypatch.setattr(
                "squadron.sandbox.manager.ToolProxy",
                lambda **kwargs: mock_proxy,
            )

            # EphemeralWorktree — mock so it doesn't create real overlayfs.
            mgr._worktree_mgr = MagicMock()
            mgr._worktree_mgr.create = AsyncMock(return_value=None)
            mgr._worktree_mgr.wipe = AsyncMock()

            # AuthBroker — mock registration.
            mgr._broker = MagicMock()
            mgr._broker.register_session = MagicMock()
            mgr._broker.unregister_session = MagicMock()
            mgr._broker.stop = AsyncMock()

            worktree_dir = tmp_path / "worktree"
            worktree_dir.mkdir()
            agents_dir = tmp_path / "agents"
            agents_dir.mkdir()

            session = await mgr.create_session(
                agent_id="test-agent-1",
                issue_number=42,
                allowed_tools=["read_issue"],
                git_worktree=worktree_dir,
                agents_dir=agents_dir,
            )

            # Session should have sanitized env
            assert session.sanitized_env
            assert "GITHUB_APP_ID" not in session.sanitized_env
            assert "COPILOT_GITHUB_TOKEN" not in session.sanitized_env
            assert session.sanitized_env.get("PATH") == "/usr/bin"
            # CA cert should be injected
            assert "SSL_CERT_FILE" in session.sanitized_env

            # get_sanitized_env should return a copy
            env = mgr.get_sanitized_env("test-agent-1")
            assert env is not None
            assert "GITHUB_APP_ID" not in env
        finally:
            await mgr.teardown_session("test-agent-1")
            await mgr.stop()

    @pytest.mark.asyncio
    async def test_disabled_sandbox_no_env(self, tmp_path: Path):
        """When sandbox is disabled, sessions have empty sanitized env."""
        cfg = SandboxConfig(
            enabled=False,
            retention_path=str(tmp_path / "forensics"),
            socket_dir=str(tmp_path / "sockets"),
        )
        from squadron.sandbox.manager import SandboxManager

        mgr = SandboxManager(
            config=cfg,
            github=MagicMock(),
            repo_root=tmp_path,
            owner="org",
            repo="repo",
        )
        await mgr.start()
        try:
            session = await mgr.create_session(
                agent_id="test-agent-2",
                issue_number=42,
                allowed_tools=[],
                git_worktree=tmp_path,
                agents_dir=tmp_path,
            )
            # Disabled sandbox: empty sanitized_env (passthrough)
            assert session.sanitized_env == {}
        finally:
            await mgr.teardown_session("test-agent-2")
            await mgr.stop()


# -- CopilotAgent env parameter (Issue #146) --------------------------------


class TestCopilotAgentEnv:
    def test_env_parameter_stored(self):
        from unittest.mock import MagicMock

        from squadron.copilot import CopilotAgent

        mock_config = MagicMock()
        agent = CopilotAgent(
            runtime_config=mock_config,
            working_directory="/tmp/test",
            env={"PATH": "/usr/bin", "HOME": "/home/agent"},
        )
        assert agent._env is not None
        assert agent._env["PATH"] == "/usr/bin"
        assert "GITHUB_APP_ID" not in agent._env

    def test_env_none_by_default(self):
        from unittest.mock import MagicMock

        from squadron.copilot import CopilotAgent

        agent = CopilotAgent(
            runtime_config=MagicMock(),
            working_directory="/tmp/test",
        )
        assert agent._env is None

    def test_github_token_captured_from_host_env(self, monkeypatch):
        """CopilotAgent should capture COPILOT_GITHUB_TOKEN from host env."""
        from unittest.mock import MagicMock

        from squadron.copilot import CopilotAgent

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_test_token_123")
        agent = CopilotAgent(
            runtime_config=MagicMock(),
            working_directory="/tmp/test",
            env={"PATH": "/usr/bin"},  # sanitized env without token
        )
        # Token should be captured from host env, not from sanitized env
        assert agent._github_token == "ghu_test_token_123"
        # But should NOT be in the sanitized subprocess env
        assert "COPILOT_GITHUB_TOKEN" not in agent._env

    def test_github_token_none_when_unset(self, monkeypatch):
        """When COPILOT_GITHUB_TOKEN is not set, _github_token should be None."""
        from unittest.mock import MagicMock

        from squadron.copilot import CopilotAgent

        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        agent = CopilotAgent(
            runtime_config=MagicMock(),
            working_directory="/tmp/test",
        )
        assert agent._github_token is None


class TestStandaloneSanitizedEnv:
    """Test build_standalone_sanitized_env() for lightweight agents."""

    def test_returns_none_when_disabled(self, tmp_path: Path):
        from squadron.sandbox.manager import SandboxManager

        cfg = SandboxConfig(
            enabled=False,
            retention_path=str(tmp_path / "forensics"),
            socket_dir=str(tmp_path / "sockets"),
        )
        mgr = SandboxManager(
            config=cfg,
            github=MagicMock(),
            repo_root=tmp_path,
            owner="org",
            repo="repo",
        )
        assert mgr.build_standalone_sanitized_env() is None

    def test_returns_scrubbed_env_when_enabled(self, tmp_path: Path, monkeypatch):
        from squadron.sandbox.manager import SandboxManager

        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_secret")
        monkeypatch.setenv("PATH", "/usr/bin")

        cfg = SandboxConfig(
            enabled=True,
            retention_path=str(tmp_path / "forensics"),
            socket_dir=str(tmp_path / "sockets"),
            ca_dir=str(tmp_path / "ca"),
        )
        mgr = SandboxManager(
            config=cfg,
            github=MagicMock(),
            repo_root=tmp_path,
            owner="org",
            repo="repo",
            provider_type="copilot",
            provider_api_key_env="",
        )
        env = mgr.build_standalone_sanitized_env()
        assert env is not None
        assert "GITHUB_APP_ID" not in env
        assert "COPILOT_GITHUB_TOKEN" not in env
        assert env.get("PATH") == "/usr/bin"
