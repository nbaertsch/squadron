# Module Map

> **AD-019 MIGRATION NOTE:** The unified pipeline system (see `docs/design/unified-pipeline-system.md`) will delete `src/squadron/workflow/` entirely and replace it with `src/squadron/pipeline/`. The new `pipeline/` module contains: `engine.py` (PipelineEngine), `registry.py` (UnifiedRegistry — merges AgentRegistry + WorkflowRegistryV2), `gates.py` (pluggable gate evaluators), and `stages.py` (stage type executors). When implementing AD-019, delete the entire `workflow/` directory — no backward-compatibility shims.

Every module in `src/squadron/` with its purpose and key exports.

## Core Modules

### `src/squadron/__init__.py`
Package init. Exports: nothing significant.

### `src/squadron/__main__.py`
CLI entry point. Runs the server via `uvicorn`.

### `src/squadron/server.py`
FastAPI application factory. Creates the app, registers routes (`/webhook`, `/dashboard`, etc.),
initializes all components (EventRouter, AgentManager, etc.) on startup.

### `src/squadron/webhook.py`
GitHub webhook handler. Validates HMAC signatures, parses `GitHubEvent`, emits to EventRouter.
Key: `POST /webhook` route.

### `src/squadron/config.py`
Configuration loading and Pydantic models.
Key exports: `SquadronConfig`, `AgentDefinition`, `AgentRoleConfig`, `SkillsConfig`,
`SkillDefinition`, `load_config()`, `load_agent_definitions()`, `parse_agent_definition()`.

### `src/squadron/models.py`
Domain models shared across the codebase.
Key exports: `AgentStatus`, `AgentRecord`, `GitHubEvent`, `SquadronEvent`, `SquadronEventType`,
`MailMessage`, `ParsedCommand`, `parse_command()`.

### `src/squadron/event_router.py`
Routes `GitHubEvent` objects to handlers. Handles command parsing and dispatch.
Key exports: `EventRouter`.

### `src/squadron/agent_manager.py`
Central orchestration engine. Manages agent lifecycle, worktrees, session configs.
Key exports: `AgentManager`.
Key methods: `create_agent()`, `wake_agent()`, `complete_agent()`, `sleep_agent()`,
`_run_agent()`, `_resolve_skill_directories()`, `_build_session_config()`.

### `src/squadron/copilot.py`
Wraps the GitHub Copilot SDK. One `CopilotAgent` = one CLI subprocess.
Key exports: `CopilotAgent`, `build_session_config()`, `build_resume_config()`.

### `src/squadron/registry.py`
SQLite-backed persistence for `AgentRecord` objects.
Key exports: `AgentRegistry`.
Key methods: `create_agent()`, `get_agent()`, `update_agent()`, `get_agents_for_issue()`.

### `src/squadron/github_client.py`
GitHub REST API client. Issues, PRs, labels, comments.
Key exports: `GitHubClient`.

### `src/squadron/activity.py`
Activity logging for audit trails. Writes agent actions to SQLite.
Key exports: `ActivityLogger`.

### `src/squadron/reconciliation.py`
Periodic reconciliation — checks for stale agents, wakes sleeping agents whose
blockers are resolved, cleans up orphaned records.
Key exports: `ReconciliationLoop`.

### `src/squadron/recovery.py`
Server restart recovery — restores agents that were active before the server crashed.

### `src/squadron/workflow/`

> **LEGACY — will be deleted by AD-019.** This entire directory is replaced by `src/squadron/pipeline/`. The `WorkflowEngine`, `WorkflowRun`, and `WorkflowRegistryV2` are all superseded by the new PipelineEngine and UnifiedRegistry.

Deterministic multi-agent workflow engine.
Key exports: `WorkflowEngine`, `WorkflowRun`.

### `src/squadron/sandbox/`
Sandboxed execution environment. Linux namespace isolation, overlayfs, seccomp.
Key exports: `SandboxManager`, `SandboxConfig`.

### `src/squadron/tools/`
Squadron-specific tool implementations.

#### `src/squadron/tools/squadron_tools.py`
All custom Squadron tools (lifecycle, issue management, PR ops, etc.).
Key exports: `SquadronTools`, `ALL_TOOL_NAMES`, `ALL_TOOL_NAMES_SET`.

### `src/squadron/dashboard.py`
Web dashboard for monitoring agents. Serves static frontend, provides SSE stream.

### `src/squadron/dashboard_security.py`
Dashboard authentication and security middleware.

### `src/squadron/resource_monitor.py`
Monitors process count and memory usage, triggers cleanup if thresholds exceeded.
