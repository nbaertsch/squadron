"""SandboxManager -- orchestrates the full sandboxed agent session lifecycle.

Responsibilities:
1. Start/stop the single AuthBroker instance.
2. For each agent spawn:
   a. Generate a cryptographic session token.
   b. Register it with the AuthBroker.
   c. Create a ToolProxy (per-agent Unix socket).
   d. Create an ephemeral sandbox worktree (overlayfs/tmpfs).
   e. Set up Linux namespaces for the agent subprocess.
   f. Log the session start to the audit log.
3. On normal exit:
   a. Collect and inspect the agent diff.
   b. Log the diff hash to the audit log.
   c. Push via the auth broker (if diff passes inspection).
   d. Wipe the sandbox worktree.
   e. Tear down the tool proxy.
   f. Unregister the session token.
4. On abnormal exit:
   a. Preserve the worktree in forensic retention storage.
   b. Log the abnormal event.
   c. Tear down proxy and unregister token.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from squadron.sandbox.audit import SandboxAuditLogger
from squadron.sandbox.broker import AuthBroker
from squadron.sandbox.inspector import DiffInspector, InspectionResult, OutputInspector
from squadron.sandbox.namespace import SandboxNamespace
from squadron.sandbox.proxy import ToolProxy
from squadron.sandbox.worktree import EphemeralWorktree, WorktreeInfo

if TYPE_CHECKING:
    from squadron.sandbox.config import SandboxConfig
    from squadron.github_client import GitHubClient

logger = logging.getLogger(__name__)


@dataclass
class SandboxSession:
    """All sandbox state for one active agent session."""

    agent_id: str
    issue_number: int
    session_token: bytes
    proxy: ToolProxy
    worktree: WorktreeInfo | None
    namespace: SandboxNamespace


class SandboxManager:
    """Manages sandbox sessions for all active agents.

    A single instance is shared by the AgentManager.  The AuthBroker
    is a long-running async service (started once, stopped with the
    process).  Everything else is per-agent-session.
    """

    def __init__(
        self,
        config: SandboxConfig,
        github: GitHubClient,
        repo_root: Path,
        owner: str,
        repo: str,
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._owner = owner
        self._repo = repo
        self._enabled = config.enabled

        # Shared services (single instances)
        self._broker = AuthBroker(github) if self._enabled else None
        self._audit = SandboxAuditLogger(
            Path(config.retention_path).parent / "audit"
            if self._enabled
            else Path("/tmp/squadron-audit")
        )
        self._diff_inspector = DiffInspector(config)
        self._output_inspector = OutputInspector(config)
        self._worktree_mgr = EphemeralWorktree(
            config,
            repo_root / ".squadron-data" / "sandboxes",
        )

        # Active sessions keyed by agent_id
        self._sessions: dict[str, SandboxSession] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start shared services (auth broker, audit log)."""
        if not self._enabled:
            logger.info("SandboxManager: sandbox disabled, using legacy execution")
            return

        await self._audit.start()
        if self._broker:
            await self._broker.start()
        logger.info("SandboxManager started")

    async def stop(self) -> None:
        """Stop shared services and tear down all active sessions."""
        if not self._enabled:
            return

        # Tear down any sessions still open
        for agent_id in list(self._sessions):
            try:
                await self.teardown_session(agent_id, abnormal=True, reason="manager stopped")
            except Exception:
                logger.exception("Error tearing down session %s on shutdown", agent_id)

        if self._broker:
            await self._broker.stop()
        logger.info("SandboxManager stopped")

    # ── Session Lifecycle ─────────────────────────────────────────────────────

    async def create_session(
        self,
        agent_id: str,
        issue_number: int,
        allowed_tools: list[str],
        git_worktree: Path,
        agents_dir: Path,
    ) -> SandboxSession:
        """Set up a full sandbox session for an agent.

        Args:
            agent_id: Unique agent ID.
            issue_number: The GitHub issue number this agent is assigned to.
            allowed_tools: Tool names from the agent frontmatter allowlist.
            git_worktree: The git worktree path for this agent.
            agents_dir: Path to .squadron/agents/ (excluded from sandbox write layer).

        Returns:
            SandboxSession with all active components.
        """
        if not self._enabled:
            # Return a no-op session for non-sandbox mode
            session = SandboxSession(
                agent_id=agent_id,
                issue_number=issue_number,
                session_token=b"",
                proxy=None,  # type: ignore[arg-type]
                worktree=None,
                namespace=SandboxNamespace(self._config),
            )
            self._sessions[agent_id] = session
            return session

        # 1. Generate cryptographic session token
        token = secrets.token_bytes(self._config.session_token_bytes)

        # 2. Register session with auth broker
        if self._broker:
            self._broker.register_session(agent_id, token)

        # 3. Create tool proxy
        proxy = ToolProxy(
            agent_id=agent_id,
            issue_number=issue_number,
            session_token=token,
            allowed_tools=allowed_tools,
            broker=self._broker,
            audit=self._audit,
            output_inspector=self._output_inspector,
            config=self._config,
            owner=self._owner,
            repo=self._repo,
        )
        await proxy.start()

        # 4. Create ephemeral sandbox worktree
        worktree_info = await self._worktree_mgr.create(
            agent_id=agent_id,
            repo_root=self._repo_root,
            git_worktree=git_worktree,
            agents_dir=agents_dir,
        )

        # 5. Namespace isolation (applied at subprocess spawn time)
        namespace = SandboxNamespace(self._config)

        # 6. Audit: log session start
        await self._audit.log_session_event(
            agent_id=agent_id,
            session_token=token,
            event="start",
            details={
                "issue_number": issue_number,
                "allowed_tools": allowed_tools,
                "worktree": str(worktree_info.merged_dir) if worktree_info else None,
                "socket": str(proxy.socket_path),
            },
        )

        session = SandboxSession(
            agent_id=agent_id,
            issue_number=issue_number,
            session_token=token,
            proxy=proxy,
            worktree=worktree_info,
            namespace=namespace,
        )
        self._sessions[agent_id] = session
        logger.info("Sandbox session created for %s", agent_id)
        return session

    async def inspect_diff_before_push(self, agent_id: str) -> InspectionResult:
        """Collect and inspect the agent diff before pushing.

        Should be called by AgentManager before executing any git push.

        Returns InspectionResult.  If passed=False, the push should be
        blocked and the agent flagged for review.
        """
        session = self._sessions.get(agent_id)
        if not session or not self._enabled:
            return InspectionResult(passed=True, reason="sandbox not active")

        if not session.worktree:
            return InspectionResult(passed=True, reason="no sandbox worktree")

        diff_text = await self._worktree_mgr.collect_diff(session.worktree)
        if not diff_text:
            return InspectionResult(passed=True, reason="empty diff")

        result = self._diff_inspector.inspect_diff(diff_text)

        # Always log the diff hash
        diff_hash = self._worktree_mgr.hash_diff(diff_text)
        await self._audit.log_worktree_hash(
            agent_id=agent_id,
            session_token=session.session_token,
            diff_hash=diff_hash,
        )

        return result

    async def teardown_session(
        self,
        agent_id: str,
        abnormal: bool = False,
        reason: str = "normal exit",
    ) -> None:
        """Tear down a sandbox session.

        On abnormal exit: preserve worktree for forensics before wiping.
        On normal exit: wipe immediately.
        Also purges stale forensic entries respecting retention_days.
        """
        session = self._sessions.pop(agent_id, None)
        if not session:
            return

        if not self._enabled:
            return

        # Log session end
        await self._audit.log_session_event(
            agent_id=agent_id,
            session_token=session.session_token,
            event="end" if not abnormal else "abnormal_exit",
            details={"reason": reason, "abnormal": abnormal},
        )

        # Preserve forensics on abnormal exit
        if abnormal and session.worktree:
            await self._worktree_mgr.preserve_for_forensics(
                info=session.worktree,
                agent_id=agent_id,
                reason=reason,
            )

        # Wipe sandbox worktree
        if session.worktree:
            await self._worktree_mgr.wipe(session.worktree)

        # Stop tool proxy
        if session.proxy:
            await session.proxy.stop()

        # Unregister session token from broker
        if self._broker and session.session_token:
            self._broker.unregister_session(session.session_token)

        # Background: purge stale forensic copies
        if self._enabled:
            asyncio.create_task(self._purge_stale_forensics())

        logger.info("Sandbox session torn down: %s (abnormal=%s)", agent_id, abnormal)

    async def _purge_stale_forensics(self) -> None:
        try:
            purged = await self._worktree_mgr.purge_stale_forensics()
            if purged:
                logger.info("Purged %d stale forensic worktree(s)", purged)
        except Exception:
            logger.exception("Error purging stale forensics")

    def get_session(self, agent_id: str) -> SandboxSession | None:
        return self._sessions.get(agent_id)

    def get_working_directory(self, agent_id: str, fallback: Path) -> Path:
        """Return the sandbox working directory for an agent, or fallback."""
        session = self._sessions.get(agent_id)
        if session and session.worktree and session.worktree.is_active:
            return session.worktree.merged_dir
        return fallback

    def wrap_agent_command(self, agent_id: str, cmd: list[str]) -> list[str]:
        """Wrap an agent command with namespace isolation if enabled."""
        session = self._sessions.get(agent_id)
        if session:
            return session.namespace.wrap_command(cmd)
        return cmd

    def get_socket_path(self, agent_id: str) -> Path | None:
        """Return the Unix socket path for an agent proxy (for env injection)."""
        session = self._sessions.get(agent_id)
        if session and session.proxy:
            return session.proxy.socket_path
        return None

    def get_session_token_hex(self, agent_id: str) -> str | None:
        """Return hex-encoded session token for injection into agent environment."""
        session = self._sessions.get(agent_id)
        if session and session.session_token:
            return session.session_token.hex()
        return None
