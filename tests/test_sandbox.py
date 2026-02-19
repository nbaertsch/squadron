"""Tests for the sandboxed worktree execution and host-side auth broker (issue #85)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from squadron.sandbox.audit import SandboxAuditLogger, _sha256_hex
from squadron.sandbox.config import SandboxConfig
from squadron.sandbox.inspector import DiffInspector, InspectionResult, OutputInspector
from squadron.sandbox.namespace import (
    SandboxNamespace,
    _bpf_jump,
    _bpf_stmt,
    is_linux,
    unshare_available,
)
from squadron.sandbox.worktree import EphemeralWorktree, _overlayfs_available


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
            agent_id="agent", session_token=token,
            tool="read_issue", params={}, response={}, status="ok",
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
            agent_id="agent", session_token=token,
            tool="read_issue", params={}, response={}, status="ok",
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
                agent_id="agent", session_token=token,
                tool=f"tool_{i}", params={}, response={}, status="ok",
            )
        log_file = audit._log_file()
        seqs = [json.loads(line)["seq"]
                for line in log_file.read_text().strip().splitlines()
                if line.strip()]
        assert seqs == list(range(1, 6))

    @pytest.mark.asyncio
    async def test_blocked_call_logged(self, tmp_path: Path):
        audit = SandboxAuditLogger(tmp_path / "audit")
        await audit.start()
        token = secrets.token_bytes(32)
        await audit.log_tool_call(
            agent_id="agent", session_token=token,
            tool="delete_repo",
            params={}, response={"blocked_reason": "not in allowlist"}, status="blocked",
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
            namespace_mount=True, namespace_pid=True, namespace_net=True,
            namespace_ipc=True, namespace_uts=True,
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
            namespace_mount=False, namespace_pid=False, namespace_net=True,
            namespace_ipc=False, namespace_uts=False,
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
            enabled=True, use_overlayfs=False,
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
        (sq / "config.yaml").write_text(yaml.dump({
            "project": {"name": "test"},
            "sandbox": {"enabled": False},
        }))
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
                agent_id="agent-1", session_token=token,
                tool="read_issue", params={"issue_number": 42},
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
                agent_id="agent-1", session_token=token,
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
            agent_id="agent-1", issue_number=42, session_token=token,
            allowed_tools=["read_issue", "comment_on_issue"],
            broker=MagicMock(), audit=mock_audit, output_inspector=mock_oi,
            config=cfg, owner="org", repo="repo",
        )
        result = await proxy._process_request({
            "token": token.hex(), "tool": "delete_repo", "params": {}
        })
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
            agent_id="agent-issue-42", issue_number=42, session_token=token,
            allowed_tools=["comment_on_issue"],
            broker=MagicMock(), audit=AsyncMock(), output_inspector=mock_oi,
            config=cfg, owner="org", repo="repo",
        )
        # Attempt to write to a different issue (cross-issue violation)
        result = await proxy._process_request({
            "token": token.hex(),
            "tool": "comment_on_issue",
            "params": {"issue_number": 99, "body": "Hello"},
        })
        assert not result["ok"]
        assert "not-permitted" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, tmp_path: Path):
        from squadron.sandbox.proxy import ToolProxy
        cfg = SandboxConfig(socket_dir=str(tmp_path / "sockets"))
        token = secrets.token_bytes(32)
        wrong_token = secrets.token_bytes(32)
        proxy = ToolProxy(
            agent_id="agent-1", issue_number=42, session_token=token,
            allowed_tools=["read_issue"],
            broker=MagicMock(), audit=AsyncMock(), output_inspector=MagicMock(),
            config=cfg, owner="org", repo="repo",
        )
        result = await proxy._process_request({
            "token": wrong_token.hex(), "tool": "read_issue", "params": {}
        })
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
        mock_broker.submit = AsyncMock(
            return_value=BrokerResponse(ok=True, data={"ok": True})
        )
        mock_oi = MagicMock()
        mock_oi.inspect.return_value = MagicMock(passed=True)
        proxy = ToolProxy(
            agent_id="agent-1", issue_number=42, session_token=token,
            allowed_tools=["read_issue"],
            broker=mock_broker, audit=AsyncMock(), output_inspector=mock_oi,
            config=cfg, owner="org", repo="repo",
        )
        for _ in range(2):
            r = await proxy._process_request({
                "token": token.hex(), "tool": "read_issue",
                "params": {"issue_number": 42},
            })
            assert r["ok"], f"Expected ok but got {r}"
        # Third call should hit rate limit
        result = await proxy._process_request({
            "token": token.hex(), "tool": "read_issue",
            "params": {"issue_number": 42},
        })
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
            agent_id="agent-1", issue_number=42, session_token=token,
            allowed_tools=["comment_on_issue"],
            broker=AsyncMock(), audit=AsyncMock(), output_inspector=mock_oi,
            config=cfg, owner="org", repo="repo",
        )
        result = await proxy._process_request({
            "token": token.hex(),
            "tool": "comment_on_issue",
            "params": {"issue_number": 42, "body": "leaking secret"},
        })
        assert not result["ok"]
        assert "sensitive token detected" in result["error"]
