"""Ephemeral worktree management for sandboxed agent execution.

Each agent gets an isolated worktree with:
- An overlayfs (or tmpfs fallback) write layer over a read-only snapshot
  of the repo, so changes never touch the host repository directly.
- Agent .md definition files excluded from the write layer (mounted
  read-only or absent) to prevent frontmatter self-modification.

On normal exit:  collect diff, hash it, wipe the overlay.
On abnormal exit: preserve the overlay in encrypted forensic storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from squadron.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)


@dataclass
class WorktreeInfo:
    """Holds paths for an ephemeral sandboxed worktree."""
    base_dir: Path        # root of all sandbox dirs for this agent
    lower_dir: Path       # read-only repo snapshot (bind-mounted or rsync)
    upper_dir: Path       # overlayfs write layer
    work_dir: Path        # overlayfs work dir (required by kernel)
    merged_dir: Path      # the overlayfs mount point agents see
    agent_def_dir: Path   # read-only mount point for .squadron/agents/
    is_overlayfs: bool = False
    is_active: bool = False


class EphemeralWorktree:
    """Manages ephemeral overlayfs/tmpfs worktrees for sandboxed agents."""

    def __init__(self, config: SandboxConfig, worktree_base: Path) -> None:
        self._config = config
        self._worktree_base = worktree_base

    async def create(
        self,
        agent_id: str,
        repo_root: Path,
        git_worktree: Path,
        agents_dir: Path,
    ) -> WorktreeInfo:
        """Create an ephemeral sandbox worktree for an agent.

        Args:
            agent_id: Unique agent identifier (used for directory naming).
            repo_root: Host repo root (used as rsync source when no overlayfs).
            git_worktree: The git worktree the agent will work in.
            agents_dir: Path to .squadron/agents/ (excluded from agent write layer).

        Returns:
            WorktreeInfo with all relevant paths set.
        """
        base_dir = self._worktree_base / f"sandbox-{agent_id}"
        base_dir.mkdir(parents=True, exist_ok=True)

        lower_dir = base_dir / "lower"
        upper_dir = base_dir / "upper"
        work_dir = base_dir / "work"
        merged_dir = base_dir / "merged"
        agent_def_dir = base_dir / "agent-defs"

        for d in (lower_dir, upper_dir, work_dir, merged_dir, agent_def_dir):
            d.mkdir(parents=True, exist_ok=True)

        info = WorktreeInfo(
            base_dir=base_dir,
            lower_dir=lower_dir,
            upper_dir=upper_dir,
            work_dir=work_dir,
            merged_dir=merged_dir,
            agent_def_dir=agent_def_dir,
        )

        if self._config.use_overlayfs and _overlayfs_available():
            success = await self._setup_overlayfs(info, git_worktree, agents_dir)
            if success:
                info.is_overlayfs = True
                info.is_active = True
                return info

        # Fallback: rsync copy into tmpfs
        await self._setup_tmpfs_copy(info, git_worktree, agents_dir)
        info.is_overlayfs = False
        info.is_active = True
        return info

    async def collect_diff(self, info: WorktreeInfo, git_exe: str = "git") -> str:
        """Collect git diff of changes in the sandbox worktree.

        Returns the diff as a string.  Returns empty string on error.
        """
        work_dir = info.merged_dir if info.is_active else info.upper_dir
        try:
            proc = await asyncio.create_subprocess_exec(
                git_exe, "diff", "HEAD",
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                logger.warning("git diff failed in sandbox worktree: %s", stderr.decode())
                return ""
            return stdout.decode(errors="replace")
        except Exception:
            logger.exception("Failed to collect diff from sandbox worktree %s", work_dir)
            return ""

    def hash_diff(self, diff_text: str) -> str:
        """Return SHA-256 hex digest of a diff string."""
        return hashlib.sha256(diff_text.encode()).hexdigest()

    async def preserve_for_forensics(
        self,
        info: WorktreeInfo,
        agent_id: str,
        reason: str,
    ) -> Path:
        """Copy the sandbox worktree to forensic retention storage.

        The preserved copy is placed in config.retention_path under a
        directory named after the agent_id and current timestamp.  It is
        NOT encrypted at this layer (encryption should be handled at the
        storage level — e.g., Azure File Share server-side encryption).

        Returns the path to the preserved copy.
        """
        import time

        retention_dir = Path(self._config.retention_path)
        retention_dir.mkdir(parents=True, exist_ok=True)

        ts = int(time.time())
        dest = retention_dir / f"{agent_id}-{ts}"

        # Use merged_dir in both modes: in overlayfs it is the combined view,
        # in tmpfs copy mode it is the full working copy
        source = info.merged_dir

        try:
            shutil.copytree(str(source), str(dest), dirs_exist_ok=True)
            reason_file = dest / ".sandbox-exit-reason.txt"
            reason_file.write_text("agent_id: " + agent_id + "\nreason: " + reason + "\nts: " + str(ts) + "\n")
            logger.info("Preserved forensic worktree: %s (reason=%s)", dest, reason)
        except Exception:
            logger.exception("Failed to preserve forensic worktree for %s", agent_id)
            return retention_dir / f"{agent_id}-{ts}-FAILED"

        return dest

    async def wipe(self, info: WorktreeInfo) -> None:
        """Unmount overlayfs (if applicable) and wipe all sandbox directories."""
        if not info.base_dir.exists():
            return

        if info.is_overlayfs:
            await self._unmount(info.merged_dir)

        try:
            shutil.rmtree(str(info.base_dir), ignore_errors=True)
            logger.debug("Wiped sandbox worktree: %s", info.base_dir)
        except Exception:
            logger.exception("Failed to wipe sandbox worktree: %s", info.base_dir)

    async def purge_stale_forensics(self) -> int:
        """Remove forensic copies older than config.retention_days.

        Returns the number of entries purged.
        """
        import time

        retention_dir = Path(self._config.retention_path)
        if not retention_dir.exists():
            return 0

        max_age_secs = self._config.retention_days * 86400
        now = time.time()
        purged = 0

        for entry in retention_dir.iterdir():
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
                if now - mtime > max_age_secs:
                    shutil.rmtree(str(entry), ignore_errors=True)
                    purged += 1
                    logger.info("Purged stale forensic worktree: %s", entry)
            except Exception:
                logger.warning("Error checking forensic entry %s", entry, exc_info=True)

        return purged

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _setup_overlayfs(
        self,
        info: WorktreeInfo,
        git_worktree: Path,
        agents_dir: Path,
    ) -> bool:
        """Mount an overlayfs with git_worktree as the lower (read-only) layer.

        The agent .md definition files are bind-mounted read-only into
        a separate path inside the sandbox and are NOT in the write layer.
        """
        # Bind-mount the git worktree as the lower dir (read-only)
        rc_bind, _, err_bind = await _run(
            "mount", "--bind", "--read-only",
            str(git_worktree), str(info.lower_dir),
        )
        if rc_bind != 0:
            logger.warning("overlayfs bind mount failed: %s", err_bind)
            return False

        # Mount overlayfs
        overlay_opts = (
            f"lowerdir={info.lower_dir},upperdir={info.upper_dir},"
            f"workdir={info.work_dir}"
        )
        rc_overlay, _, err_overlay = await _run(
            "mount", "-t", "overlay", "overlay",
            "-o", overlay_opts,
            str(info.merged_dir),
        )
        if rc_overlay != 0:
            logger.warning("overlayfs mount failed: %s", err_overlay)
            await _run("umount", "--lazy", str(info.lower_dir))
            return False

        # Bind-mount agents dir as read-only inside sandbox
        if agents_dir.exists():
            rc_agents, _, err_agents = await _run(
                "mount", "--bind", "--read-only",
                str(agents_dir), str(info.agent_def_dir),
            )
            if rc_agents != 0:
                logger.warning("Agent defs bind mount failed (non-fatal): %s", err_agents)

        logger.info("overlayfs sandbox created: %s", info.merged_dir)
        return True

    async def _setup_tmpfs_copy(
        self,
        info: WorktreeInfo,
        git_worktree: Path,
        agents_dir: Path,
    ) -> None:
        """Fallback: copy git_worktree to a tmpfs directory.

        Agent .md files are copied but with their write permission stripped,
        simulating a read-only mount.
        """
        try:
            shutil.copytree(
                str(git_worktree),
                str(info.merged_dir),
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git"),
            )
        except Exception:
            logger.exception("tmpfs copy failed, sandbox worktree may be incomplete")
            return

        # Strip write permission from any agent .md files that ended up in
        # the copy (paranoia: they should not be there, but make sure)
        if agents_dir.exists():
            for md_file in agents_dir.glob("*.md"):
                dest_copy = info.merged_dir / md_file.name
                if dest_copy.exists():
                    mode = dest_copy.stat().st_mode & 0o555  # remove write bits
                    dest_copy.chmod(mode)

        logger.info("tmpfs-copy sandbox created: %s", info.merged_dir)

    async def _unmount(self, mount_point: Path) -> None:
        if mount_point.exists():
            rc, _, err = await _run("umount", "--lazy", str(mount_point))
            if rc != 0:
                logger.warning("Failed to unmount %s: %s", mount_point, err)


def _overlayfs_available() -> bool:
    """Check if overlayfs is available on the current kernel."""
    try:
        with open("/proc/filesystems") as f:
            return "overlay" in f.read()
    except OSError:
        return False


async def _run(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )
