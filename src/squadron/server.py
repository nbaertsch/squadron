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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from squadron.agent_manager import AgentManager
from squadron.config import SquadronConfig, load_agent_definitions, load_config
from squadron.event_router import EventRouter
from squadron.github_client import GitHubClient
from squadron.models import AgentStatus, GitHubEvent
from squadron.reconciliation import ReconciliationLoop
from squadron.registry import AgentRegistry
from squadron.resource_monitor import ResourceMonitor
from squadron.webhook import configure as configure_webhook
from squadron.webhook import router as webhook_router

logger = logging.getLogger(__name__)


class SquadronServer:
    """Encapsulates all server components and lifecycle."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = repo_root or Path.cwd()
        self.squadron_dir = self.repo_root / ".squadron"

        # Components (initialized in start())
        self.config: SquadronConfig | None = None
        self.registry: AgentRegistry | None = None
        self.github: GitHubClient | None = None
        self.event_queue: asyncio.Queue[GitHubEvent] | None = None
        self.router: EventRouter | None = None
        self.agent_manager: AgentManager | None = None
        self.reconciliation: ReconciliationLoop | None = None
        self.resource_monitor: ResourceMonitor | None = None

    async def start(self) -> None:
        """Initialize all components and start background loops."""
        logger.info("Squadron server starting (repo=%s)", self.repo_root)

        # 1. Load config
        self.config = load_config(self.squadron_dir)
        agent_definitions = load_agent_definitions(self.squadron_dir)
        logger.info(
            "Loaded %d agent definitions: %s",
            len(agent_definitions),
            list(agent_definitions.keys()),
        )

        # 2. Initialize database
        data_dir = self.repo_root / ".squadron-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(data_dir / "registry.db")

        self.registry = AgentRegistry(db_path)
        await self.registry.initialize()

        # 3. Recover stale agents
        await self._recover_stale_agents()

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
        self.event_queue = asyncio.Queue(maxsize=1000)
        self.router = EventRouter(
            event_queue=self.event_queue,
            registry=self.registry,
            config=self.config,
            bot_username=self.config.project.bot_username,
        )

        # 6. Create agent manager
        self.agent_manager = AgentManager(
            config=self.config,
            registry=self.registry,
            github=self.github,
            router=self.router,
            agent_definitions=agent_definitions,
            repo_root=self.repo_root,
        )

        # 7. Create reconciliation loop
        self.reconciliation = ReconciliationLoop(
            config=self.config,
            registry=self.registry,
            github=self.github,
            owner=self.config.project.owner,
            repo=self.config.project.repo,
            on_wake_agent=self.agent_manager.wake_agent,
        )

        # 8. Wire webhook endpoint
        configure_webhook(self.event_queue, self.github)

        # 9. Start background loops
        await self.router.start()
        await self.agent_manager.start()
        await self.reconciliation.start()

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

        logger.info("Squadron server stopped")

    async def _recover_stale_agents(self) -> None:
        """On startup, mark any ACTIVE agents as SLEEPING.

        If the server crashed while agents were active, their SDK sessions
        are persisted to disk but the Python tasks are lost. Marking them
        SLEEPING lets the reconciliation loop re-evaluate them.
        """
        if not self.registry:
            return

        active = await self.registry.get_agents_by_status(AgentStatus.ACTIVE)
        if active:
            logger.warning(
                "Found %d stale ACTIVE agents from previous run — marking SLEEPING", len(active)
            )
            for agent in active:
                agent.status = AgentStatus.SLEEPING
                await self.registry.update_agent(agent)

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
            logger.warning("Failed to ensure labels on %s/%s — continuing without", owner, repo)


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

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        agent_counts = {}
        if _server.registry:
            for status in AgentStatus:
                agents = await _server.registry.get_agents_by_status(status)
                if agents:
                    agent_counts[status.value] = len(agents)

        resources = None
        if _server.resource_monitor:
            snap = _server.resource_monitor.latest
            resources = {
                "memory_percent": snap.memory_percent,
                "disk_percent": snap.disk_percent,
                "disk_free_mb": snap.disk_free_mb,
                "active_agent_count": snap.active_agent_count,
            }

        return {
            "status": "ok",
            "project": _server.config.project.name if _server.config else None,
            "agents": agent_counts,
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
