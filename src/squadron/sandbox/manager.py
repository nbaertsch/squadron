"""SandboxManager -- orchestrates the full sandboxed agent session lifecycle.

Responsibilities:
1. Start/stop the single AuthBroker instance.
2. Start/stop the NetworkBridge + InferenceProxy (Issue #146).
3. Generate and manage the ephemeral CA (Issue #146).
4. For each agent spawn:
   a. Generate a cryptographic session token.
   b. Register it with the AuthBroker.
   c. Create a ToolProxy (per-agent Unix socket).
   d. Create a veth pair + named network namespace (Issue #146).
   e. Create an ephemeral sandbox worktree (overlayfs/tmpfs).
   f. Set up Linux namespaces for the agent subprocess.
   g. Build a sanitized environment (strip all secrets).
   h. Log the session start to the audit log.
5. On normal exit:
   a. Collect and inspect the agent diff.
   b. Log the diff hash to the audit log.
   c. Push via the auth broker (if diff passes inspection).
   d. Wipe the sandbox worktree.
   e. Tear down the tool proxy + veth pair.
   f. Unregister the session token.
6. On abnormal exit:
   a. Preserve the worktree in forensic retention storage.
   b. Log the abnormal event.
   c. Tear down proxy, veth, and unregister token.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from squadron.sandbox.audit import SandboxAuditLogger
from squadron.sandbox.broker import AuthBroker
from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.env_scrub import build_sanitized_env, get_dynamic_byok_vars
from squadron.sandbox.inference_proxy import InferenceProxy, build_credentials_from_env
from squadron.sandbox.inspector import DiffInspector, InspectionResult, OutputInspector
from squadron.sandbox.namespace import SandboxNamespace
from squadron.sandbox.net_bridge import NetworkBridge, VethPair
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
    veth: VethPair | None = None
    sanitized_env: dict[str, str] = field(default_factory=dict)


class SandboxManager:
    """Manages sandbox sessions for all active agents.

    A single instance is shared by the AgentManager.  The AuthBroker,
    NetworkBridge, InferenceProxy, and SandboxCA are long-running
    services (started once, stopped with the process).  Everything else
    is per-agent-session.
    """

    def __init__(
        self,
        config: SandboxConfig,
        github: GitHubClient,
        repo_root: Path,
        owner: str,
        repo: str,
        *,
        provider_type: str = "copilot",
        provider_api_key_env: str = "",
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._owner = owner
        self._repo = repo
        self._enabled = config.enabled
        self._provider_type = provider_type
        self._provider_api_key_env = provider_api_key_env

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

        # Issue #146: Network isolation + MitM proxy
        self._ca = SandboxCA(config.ca_dir, config.ca_validity_days) if self._enabled else None
        self._bridge = NetworkBridge(config) if self._enabled else None
        self._inference_proxy: InferenceProxy | None = None

        # Active sessions keyed by agent_id
        self._sessions: dict[str, SandboxSession] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start shared services (auth broker, CA, bridge, inference proxy, audit)."""
        if not self._enabled:
            logger.info("SandboxManager: sandbox disabled, using legacy execution")
            return

        await self._audit.start()

        if self._broker:
            await self._broker.start()

        # Issue #146: Initialize CA, network bridge, and inference proxy.
        if self._ca:
            self._ca.ensure_ca()
            logger.info("SandboxManager: ephemeral CA ready at %s", self._ca.cert_path)

        if self._bridge:
            bridge_ok = await self._bridge.setup_bridge()
            if bridge_ok:
                logger.info("SandboxManager: network bridge up")
            else:
                logger.warning(
                    "SandboxManager: network bridge setup failed — "
                    "falling back to bare network namespace isolation"
                )

        # Start inference proxy (only if bridge + CA are active).
        if self._bridge and self._bridge.is_available and self._ca:
            credentials = build_credentials_from_env(
                self._provider_type, self._provider_api_key_env
            )
            self._inference_proxy = InferenceProxy(
                config=self._config,
                ca=self._ca,
                credentials=credentials,
            )
            await self._inference_proxy.start()

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

        # Stop inference proxy.
        if self._inference_proxy:
            await self._inference_proxy.stop()

        # Tear down network bridge.
        if self._bridge:
            await self._bridge.teardown_bridge()

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
            broker=self._broker,  # type: ignore[arg-type]  # guaranteed non-None when enabled
            audit=self._audit,
            output_inspector=self._output_inspector,
            config=self._config,
            owner=self._owner,
            repo=self._repo,
        )
        await proxy.start()

        # 4. Create veth pair + network namespace (Issue #146)
        veth: VethPair | None = None
        use_bridge_net = False
        if self._bridge and self._bridge.is_available:
            agent_index = self._bridge.allocate_index()
            veth = await self._bridge.create_veth(agent_id, agent_index)
            if veth:
                use_bridge_net = True

        # 5. Create ephemeral sandbox worktree
        worktree_info = await self._worktree_mgr.create(
            agent_id=agent_id,
            repo_root=self._repo_root,
            git_worktree=git_worktree,
            agents_dir=agents_dir,
        )

        # 6. Namespace isolation (applied at subprocess spawn time)
        # When bridge is active, --net is omitted (bridge provides network ns).
        namespace = SandboxNamespace(self._config, use_bridge_net=use_bridge_net)

        # 7. Build sanitized environment (Issue #146 — strip all secrets)
        extra_strip = get_dynamic_byok_vars(self._provider_api_key_env)
        sanitized_env = build_sanitized_env(
            self._config,
            ca_cert_path=self._ca.cert_path if self._ca else None,
            socket_path=proxy.socket_path,
            session_token_hex=token.hex(),
            extra_strip=extra_strip,
        )

        # 8. Audit: log session start
        await self._audit.log_session_event(
            agent_id=agent_id,
            session_token=token,
            event="start",
            details={
                "issue_number": issue_number,
                "allowed_tools": allowed_tools,
                "worktree": str(worktree_info.merged_dir) if worktree_info else None,
                "socket": str(proxy.socket_path),
                "veth": veth.host_iface if veth else None,
                "netns": veth.netns_name if veth else None,
                "env_scrubbed": True,
            },
        )

        session = SandboxSession(
            agent_id=agent_id,
            issue_number=issue_number,
            session_token=token,
            proxy=proxy,
            worktree=worktree_info,
            namespace=namespace,
            veth=veth,
            sanitized_env=sanitized_env,
        )
        self._sessions[agent_id] = session
        logger.info(
            "Sandbox session created for %s (veth=%s, netns=%s, env_scrubbed=True)",
            agent_id,
            veth.host_iface if veth else "none",
            veth.netns_name if veth else "none",
        )
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

        # Destroy veth pair + network namespace (Issue #146)
        if session.veth and self._bridge:
            await self._bridge.destroy_veth(session.veth)

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

    def get_sanitized_env(self, agent_id: str) -> dict[str, str] | None:
        """Return the sanitized env dict for an agent, or None if no session."""
        session = self._sessions.get(agent_id)
        if session and session.sanitized_env:
            return dict(session.sanitized_env)  # defensive copy
        return None

    def build_standalone_sanitized_env(self) -> dict[str, str] | None:
        """Build a sanitized env without requiring a full sandbox session.

        Used for lightweight agents (e.g. workflow review agents) that don't
        need full worktree/proxy isolation but still shouldn't inherit secrets.
        Returns None when sandbox is disabled.
        """
        if not self._enabled:
            return None
        extra_strip = get_dynamic_byok_vars(self._provider_api_key_env)
        return build_sanitized_env(
            self._config,
            ca_cert_path=self._ca.cert_path if self._ca else None,
            extra_strip=extra_strip,
        )

    def wrap_agent_command(self, agent_id: str, cmd: list[str]) -> list[str]:
        """Wrap an agent command with namespace + network isolation if enabled.

        When the network bridge is active, the command is first wrapped in
        ``ip netns exec <netns>`` (network namespace), then in ``unshare``
        (mount/pid/ipc/uts namespaces — without --net).

        NOTE: This method is currently not called for CopilotClient subprocesses
        because the Copilot SDK builds and executes the subprocess command
        internally (no wrapping hook).  Network isolation is provided by the
        veth bridge + iptables DNAT.  Process namespace isolation requires
        either a wrapper-script approach or SDK enhancement (tracked as
        follow-up work).  Env scrubbing + network mediation provide the
        primary security boundary.
        """
        session = self._sessions.get(agent_id)
        if not session:
            return cmd

        # Apply other namespace isolation (mount, pid, ipc, uts — no --net).
        wrapped = session.namespace.wrap_command(cmd)

        # Wrap in network namespace if veth is active.
        if session.veth and self._bridge:
            wrapped = self._bridge.wrap_command_in_netns(session.veth, wrapped)

        return wrapped

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
