"""Squadron Server — FastAPI application that ties all components together.

Startup sequence (from runtime-architecture.md):
1. Load .squadron/ config
2. Initialize SQLite database
3. Recover stale agents (ACTIVE → SLEEPING)
4. Start FastAPI (uvicorn)
5. Start Event Router consumer loop
6. Start Reconciliation Loop
7. Begin accepting webhooks

Shutdown:
1. Stop accepting webhooks
2. Drain event queue
3. Stop all agents (save sessions)
4. Close database
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

import aiosqlite

from squadron.activity import ActivityLogger
from squadron.agent_manager import AgentManager
from squadron.config import (
    SquadronConfig,
    load_agent_definitions,
    load_config,
)
from squadron.dashboard import configure as configure_dashboard
from squadron.dashboard import router as dashboard_router
from squadron.event_router import EventRouter
from squadron.github_client import GitHubClient
from squadron.log_buffer import LogBuffer, RingBufferHandler
from squadron.models import AgentStatus, GitHubEvent, SquadronEvent, SquadronEventType
from squadron.reconciliation import ReconciliationLoop
from squadron.registry import AgentRegistry
from squadron.resource_monitor import ResourceMonitor
from squadron.webhook import configure as configure_webhook
from squadron.webhook import router as webhook_router
from squadron.pipeline import GateCheckRegistry, PipelineEngine, PipelineRegistry

logger = logging.getLogger(__name__)


class SquadronServer:
    """Encapsulates all server components and lifecycle."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = repo_root or Path.cwd()
        # Use SQUADRON_CONFIG_DIR if set (Azure Files mount), else default to repo
        config_dir = os.environ.get("SQUADRON_CONFIG_DIR", "").strip()
        self.squadron_dir = Path(config_dir) if config_dir else self.repo_root / ".squadron"

        # Components (initialized in start())
        self.config: SquadronConfig | None = None
        self.registry: AgentRegistry | None = None
        self.github: GitHubClient | None = None
        self.event_queue: asyncio.Queue[GitHubEvent] | None = None
        self.router: EventRouter | None = None
        self.agent_manager: AgentManager | None = None
        self.reconciliation: ReconciliationLoop | None = None
        self.resource_monitor: ResourceMonitor | None = None
        self._config_version: str | None = None  # Commit SHA of current config
        self.pipeline_engine: PipelineEngine | None = None
        self.pipeline_db: aiosqlite.Connection | None = None
        self.pipeline_registry: PipelineRegistry | None = None
        self.activity_logger: ActivityLogger | None = None
        self.log_buffer: LogBuffer = LogBuffer(maxlen=20_000)

    async def start(self) -> None:
        """Initialize all components and start background loops."""
        logger.info("Squadron server starting (repo=%s)", self.repo_root)

        # 0. Clone repo if SQUADRON_REPO_URL is set (container environment)
        repo_url = os.environ.get("SQUADRON_REPO_URL", "").strip()
        if repo_url:
            await self._clone_repo(repo_url)

        # 1. Load config
        self.config = load_config(self.squadron_dir)
        agent_definitions = load_agent_definitions(self.squadron_dir)
        logger.info(
            "Loaded %d agent definitions: %s",
            len(agent_definitions),
            list(agent_definitions.keys()),
        )
        if self.config.pipelines:
            logger.info(
                "Loaded %d pipeline definitions: %s",
                len(self.config.pipelines),
                list(self.config.pipelines.keys()),
            )

        # 2. Initialize database (container-local disk, NOT a network mount)
        data_dir = Path(
            os.environ.get("SQUADRON_DATA_DIR") or str(self.repo_root / ".squadron-data")
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(data_dir / "registry.db")
        logger.info("Registry DB path: %s", db_path)

        self.registry = AgentRegistry(db_path)
        await self.registry.initialize()

        # 2b. Initialize activity logger (same data dir as registry)
        activity_db_path = str(data_dir / "activity.db")
        self.activity_logger = ActivityLogger(activity_db_path)
        await self.activity_logger.initialize()
        logger.info("Activity logger initialized: %s", activity_db_path)

        # 2c. Attach ring-buffer log handler to root logger for remote log access
        self.log_buffer.attach_loop(asyncio.get_running_loop())
        ring_handler = RingBufferHandler(self.log_buffer)
        logging.getLogger().addHandler(ring_handler)
        logger.info("Ring-buffer log handler attached (capacity=%d)", self.log_buffer.maxlen)

        # 3. Recover stale agents + reconstruct from GitHub
        await self._recover_agents()

        # 4. Initialize GitHub client
        self.github = GitHubClient(
            app_id=os.environ.get("GITHUB_APP_ID"),
            private_key=os.environ.get("GITHUB_PRIVATE_KEY"),
            webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"),
            installation_id=os.environ.get("GITHUB_INSTALLATION_ID"),
        )
        await self.github.start()

        # 4b. Ensure label taxonomy exists on the repo
        await self._ensure_labels()

        # 4c. Reconstruct agent state from GitHub (Phase 2 recovery)
        await self._reconstruct_from_github()

        self.event_queue = asyncio.Queue(maxsize=1000)
        self.router = EventRouter(
            event_queue=self.event_queue,
            registry=self.registry,
            config=self.config,
        )

        # 6. Create agent manager
        self.agent_manager = AgentManager(
            config=self.config,
            registry=self.registry,
            github=self.github,
            router=self.router,
            agent_definitions=agent_definitions,
            repo_root=self.repo_root,
            activity_logger=self.activity_logger,
        )

        # 6a. Warn if sandbox is disabled and agents have bash access (#117)
        sandbox_cfg = self.config.get_sandbox_config()
        if not sandbox_cfg.enabled:
            bash_agents = [
                defn.name or defn.role
                for defn in agent_definitions.values()
                if defn.tools is None or "bash" in defn.tools
            ]
            if bash_agents:
                logger.warning(
                    "SECURITY: sandbox is disabled but %d agent(s) have bash "
                    "tool access (%s). Application secrets are stripped from "
                    "agent subprocess environments, but filesystem-level "
                    "isolation is NOT active. Set sandbox.enabled=true in "
                    "config.yaml for full protection.",
                    len(bash_agents),
                    ", ".join(bash_agents),
                )

        # 7. Create reconciliation loop
        self.reconciliation = ReconciliationLoop(
            config=self.config,
            registry=self.registry,
            github=self.github,
            owner=self.config.project.owner,
            repo=self.config.project.repo,
            on_wake_agent=self.agent_manager.wake_agent,
            on_complete_agent=self.agent_manager.complete_agent,
        )

        # 8. Wire webhook endpoint (single-tenant security validation)
        repo_full_name = None
        if self.config.project.owner and self.config.project.repo:
            repo_full_name = f"{self.config.project.owner}/{self.config.project.repo}"

        configure_webhook(
            self.event_queue,
            self.github,
            expected_installation_id=os.environ.get("GITHUB_INSTALLATION_ID"),
            expected_repo_full_name=repo_full_name,
        )

        # 8a. Configure dashboard endpoints (activity logging + SSE + log buffer + pipelines)
        # NOTE: Moved after pipeline engine setup so dashboard has pipeline visibility.

        # 8b. Create pipeline engine
        pipeline_db_path = str(data_dir / "pipeline.db")
        self.pipeline_db = await aiosqlite.connect(pipeline_db_path)
        self.pipeline_db.row_factory = aiosqlite.Row
        self.pipeline_registry = PipelineRegistry(self.pipeline_db)
        await self.pipeline_registry.initialize()

        gate_registry = GateCheckRegistry()

        # Load custom gate check plugins from config (AD-019 Phase 5)
        custom_gates = self.config.pipeline_settings.get("custom_gates", [])
        if custom_gates:
            gate_registry.load_custom_gates(custom_gates)

        self.pipeline_engine = PipelineEngine(
            registry=self.pipeline_registry,
            gate_registry=gate_registry,
            github_client=self.github,
            owner=self.config.project.owner,
            repo=self.config.project.repo,
        )

        # Register pipeline definitions from config (convert dicts to PipelineDefinition)
        for name, defn in self.config.get_pipeline_definitions().items():
            self.pipeline_engine.add_pipeline(name, defn)

        # Validate all pipelines at startup
        errors = self.pipeline_engine.validate_all_pipelines()
        if errors:
            for err in errors:
                logger.error("Pipeline validation error: %s", err)
            raise RuntimeError(f"Pipeline validation failed with {len(errors)} error(s)")

        # Wire callbacks: agent manager ↔ pipeline engine
        self.pipeline_engine.set_spawn_callback(
            self.agent_manager.spawn_pipeline_agent,
        )
        self.pipeline_engine.set_action_callback(
            self.agent_manager.pipeline_action_callback,
        )
        self.pipeline_engine.set_notify_callback(
            self.agent_manager.pipeline_notify_callback,
        )
        self.agent_manager.set_pipeline_engine(self.pipeline_engine)

        # Recover active pipelines from before restart
        recovered = await self.pipeline_engine.recover_active_pipelines()
        if recovered:
            logger.info("Recovered %d active pipeline(s) from previous run", recovered)

        # 8a. Configure dashboard endpoints (with all dependencies now available)
        configure_dashboard(
            self.activity_logger,
            self.registry,
            self.log_buffer,
            pipeline_engine=self.pipeline_engine,
            pipeline_registry=self.pipeline_registry,
        )

        # 9. Start background loops
        await self.router.start()
        await self.agent_manager.start()
        await self.reconciliation.start()

        # 9b. Register config hot-reload on push to default branch (D-5)
        self.router.on(SquadronEventType.PUSH, self._handle_config_reload)

        # 10. Start resource monitor
        worktree_dir = (
            Path(self.config.runtime.worktree_dir) if self.config.runtime.worktree_dir else None
        )
        self.resource_monitor = ResourceMonitor(
            self.repo_root, interval=60, worktree_dir=worktree_dir
        )
        await self.resource_monitor.start()

        logger.info("Squadron server started successfully")

    async def stop(self) -> None:
        """Graceful shutdown — stop all components."""
        logger.info("Squadron server shutting down")

        if self.resource_monitor:
            await self.resource_monitor.stop()
        if self.reconciliation:
            await self.reconciliation.stop()
        if self.agent_manager:
            await self.agent_manager.stop()
        if self.router:
            await self.router.stop()
        if self.github:
            await self.github.close()
        if self.registry:
            await self.registry.close()
        if self.activity_logger:
            await self.activity_logger.close()
        if self.pipeline_db:
            await self.pipeline_db.close()

        logger.info("Squadron server stopped")

    async def _clone_repo(self, repo_url: str) -> None:
        """Clone the repository at startup so we have .squadron/ config and a git repo for worktrees.

        Uses GitHub App credentials to generate an installation token for auth.
        Clones into /tmp/squadron-repo (ephemeral — dies with the container).
        """
        clone_dir = Path("/tmp/squadron-repo")

        if clone_dir.exists() and (clone_dir / ".git").exists():
            logger.info("Repo already cloned at %s — pulling latest", clone_dir)
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(clone_dir), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0:
                self.repo_root = clone_dir
                self.squadron_dir = clone_dir / ".squadron"
                logger.info("Repo updated at %s", clone_dir)
                return
            logger.warning("git pull failed (%d): %s — re-cloning", proc.returncode, proc.stderr)
            import shutil

            shutil.rmtree(clone_dir, ignore_errors=True)

        # Generate installation token for authenticated clone
        app_id = os.environ.get("GITHUB_APP_ID")
        private_key = os.environ.get("GITHUB_PRIVATE_KEY")
        installation_id = os.environ.get("GITHUB_INSTALLATION_ID")

        if not all([app_id, private_key, installation_id]):
            logger.error("Cannot clone repo — missing GitHub App credentials")
            return

        # Create a temporary GitHubClient just for token generation
        temp_client = GitHubClient(
            app_id=app_id,
            private_key=private_key,
            installation_id=installation_id,
        )
        await temp_client.start()
        try:
            token = await temp_client._ensure_token()
        finally:
            await temp_client.close()

        # Build authenticated URL: https://x-access-token:TOKEN@github.com/owner/repo.git
        auth_url = repo_url.replace("https://", f"https://x-access-token:{token}@")

        # Determine default branch from config or use 'main'
        default_branch = os.environ.get("SQUADRON_DEFAULT_BRANCH", "main")

        logger.info("Cloning %s (branch: %s) into %s", repo_url, default_branch, clone_dir)
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "clone", "--branch", default_branch, auth_url, str(clone_dir)],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if proc.returncode != 0:
            logger.error("git clone failed (%d): %s", proc.returncode, proc.stderr)
            raise RuntimeError(f"Failed to clone repo: {proc.stderr}")

        # Strip the token from the remote URL so it doesn't leak
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(clone_dir), "remote", "set-url", "origin", repo_url],
            capture_output=True,
            text=True,
        )

        self.repo_root = clone_dir
        self.squadron_dir = clone_dir / ".squadron"
        logger.info("Repo cloned successfully at %s", clone_dir)

    async def _recover_agents(self) -> None:
        """On startup, handle agents left over from a previous run.

        Phase 1 (immediate): Mark stale ACTIVE/CREATED agents as FAILED.
        Phase 2 (after GitHub client init): Reconstruct from GitHub.

        Phase 2 runs later via ``_reconstruct_from_github()`` once the
        GitHub client is available.  Phase 1 runs here synchronously
        since it only needs the local registry.
        """
        if not self.registry:
            return

        # Phase 1 only (no GitHub client yet) — just mark stale as FAILED
        stale_statuses = [AgentStatus.ACTIVE, AgentStatus.CREATED]
        for status in stale_statuses:
            agents = await self.registry.get_agents_by_status(status)
            if agents:
                logger.warning(
                    "Found %d stale %s agents from previous run — marking FAILED",
                    len(agents),
                    status.value,
                )
                for agent in agents:
                    agent.status = AgentStatus.FAILED
                    agent.active_since = None
                    await self.registry.update_agent(agent)

    async def _reconstruct_from_github(self) -> None:
        """Phase 2 recovery: reconstruct agent records from GitHub state.

        Called after the GitHub client is initialized so we can query
        the Issues and PRs APIs.
        """
        if not self.registry or not self.github or not self.config:
            return

        from squadron.recovery import recover_on_startup

        try:
            summary = await recover_on_startup(self.config, self.registry, self.github)
            logger.info("GitHub reconstruction: %s", summary)
        except Exception:
            logger.exception("GitHub state reconstruction failed — continuing without")

    async def _ensure_labels(self) -> None:
        """Create label taxonomy on the GitHub repo if labels don't exist.

        Reads types, priorities, and states from config.labels and calls
        ensure_labels_exist (idempotent — 422 on duplicates is ignored).
        """
        if not self.config or not self.github:
            return

        owner = self.config.project.owner
        repo = self.config.project.repo
        if not owner or not repo:
            return

        all_labels = (
            self.config.labels.types + self.config.labels.priorities + self.config.labels.states
        )
        if not all_labels:
            return

        try:
            await self.github.ensure_labels_exist(owner, repo, all_labels)
            logger.info("Ensured %d labels exist on %s/%s", len(all_labels), owner, repo)
        except Exception:
            logger.warning(
                "Failed to ensure labels on %s/%s — continuing without",
                owner,
                repo,
                exc_info=True,
            )

    async def _handle_config_reload(self, event: SquadronEvent) -> None:
        """Handle push event — reload config if .squadron/ files changed on default branch.

        D-5: Config hot-reload. On push to default branch:
        1. Check if any modified files are under .squadron/
        2. git pull to get latest
        3. Re-parse config.yaml and agent definitions
        4. If valid: swap config atomically (new spawns use new config)
        5. If invalid: keep old config, log error
        """
        payload = event.data.get("payload", {})

        # Only reload on pushes to the default branch
        default_branch = self.config.project.default_branch if self.config else "main"
        ref = payload.get("ref", "")
        if ref != f"refs/heads/{default_branch}":
            return

        # Check if any commits touched .squadron/ files
        commits = payload.get("commits", [])
        squadron_changed = False
        for commit in commits:
            changed_files = (
                commit.get("added", []) + commit.get("modified", []) + commit.get("removed", [])
            )
            if any(f.startswith(".squadron/") for f in changed_files):
                squadron_changed = True
                break

        if not squadron_changed:
            return

        logger.info("Config change detected on %s — reloading", default_branch)

        # Pull latest changes
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(self.repo_root), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                logger.error("git pull failed during config reload: %s", proc.stderr)
                return
        except Exception:
            logger.exception("Failed to git pull during config reload")
            return

        # Re-read config
        try:
            new_config = load_config(self.squadron_dir)
            new_agent_defs = load_agent_definitions(self.squadron_dir)
        except Exception:
            logger.exception(
                "Config reload failed — keeping old config. Fix the config and push again."
            )
            return

        # Swap atomically
        old_version = self._config_version
        head_sha = payload.get("after", "unknown")
        self._config_version = head_sha

        self.config = new_config

        # Update agent manager config + definitions for new spawns
        self.agent_manager.config = new_config
        self.agent_manager.agent_definitions = new_agent_defs

        # Re-register pipeline event handlers with new config
        self.agent_manager._register_pipeline_handlers()

        # Re-register lifecycle handlers that may have been cleared
        self.router.on(SquadronEventType.ISSUE_CLOSED, self.agent_manager._handle_issue_closed)
        self.router.on(SquadronEventType.ISSUE_ASSIGNED, self.agent_manager._handle_issue_assigned)
        self.router.on(SquadronEventType.PUSH, self._handle_config_reload)

        # Update reconciliation config
        self.reconciliation.config = new_config

        # Update pipeline engine definitions
        if self.pipeline_engine:
            # Clear old definitions and re-register from new config
            self.pipeline_engine._pipelines.clear()
            for name, defn in new_config.get_pipeline_definitions().items():
                self.pipeline_engine.add_pipeline(name, defn)

            errors = self.pipeline_engine.validate_all_pipelines()
            if errors:
                for err in errors:
                    logger.error("Pipeline validation error after reload: %s", err)

        logger.info(
            "Config reloaded successfully (version: %s → %s, %d agent defs, %d pipelines)",
            old_version or "initial",
            head_sha[:8],
            len(new_agent_defs),
            len(new_config.pipelines),
        )


# ── FastAPI App ──────────────────────────────────────────────────────────────

_server = SquadronServer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan — startup and shutdown."""
    await _server.start()
    yield
    await _server.stop()


def create_app(repo_root: Path | None = None) -> FastAPI:
    """Create the FastAPI application."""
    global _server
    _server = SquadronServer(repo_root)

    app = FastAPI(
        title="Squadron",
        version="0.1.0",
        description="GitHub-Native multi-LLM-agent autonomous development framework",
        lifespan=lifespan,
    )

    # Mount routes
    app.include_router(webhook_router)
    app.include_router(dashboard_router)

    @app.get("/health")
    async def health():
        """Health check endpoint with operational metrics."""
        agent_counts = {}
        total_agents = 0
        if _server.registry:
            for status in AgentStatus:
                agents = await _server.registry.get_agents_by_status(status)
                count = len(agents)
                if count:
                    agent_counts[status.value] = count
                total_agents += count

        resources = None
        if _server.resource_monitor:
            snap = _server.resource_monitor.latest
            resources = {
                "memory_percent": snap.memory_percent,
                "disk_percent": snap.disk_percent,
                "disk_free_mb": snap.disk_free_mb,
                "active_agent_count": snap.active_agent_count,
                "process_count": snap.process_count,
            }

        # Queue and event metrics
        queue_depth = _server.event_queue.qsize() if _server.event_queue else 0
        last_event_ts = _server.router.last_event_time if _server.router else None
        last_spawn_ts = _server.agent_manager.last_spawn_time if _server.agent_manager else None

        return {
            "status": "ok",
            "project": _server.config.project.name if _server.config else None,
            "agents": agent_counts,
            "total_agents": total_agents,
            "queue_depth": queue_depth,
            "last_event_time": last_event_ts,
            "last_spawn_time": last_spawn_ts,
            "resources": resources,
        }

    @app.get("/agents")
    async def list_agents():
        """List all tracked agents."""
        if not _server.registry:
            return {"agents": []}
        agents = await _server.registry.get_all_active_agents()
        return {"agents": [a.model_dump(mode="json") for a in agents]}

    return app
