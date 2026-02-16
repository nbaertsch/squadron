<p align="center">
  <img src="assets/logo.svg" alt="Squadron" width="128" height="128"/>
</p>

<h1 align="center">Squadron</h1>

<p align="center">
  <strong>GitHub-Native multi-LLM-agent autonomous development framework</strong>
</p>

<p align="center">
  A <a href="https://www.npmjs.com/package/github-copilot-sdk">Copilot SDK</a> extension that turns your GitHub repo into a self-organizing software team.
  <br/>
  <em>Issue opened → PM triages → Dev agent writes code → Review agent reviews → PR merged.</em>
</p>

---

## What is Squadron?

Squadron is an autonomous development system built on the [GitHub Copilot SDK](https://www.npmjs.com/package/github-copilot-sdk). It listens for GitHub webhooks, triages issues with a **PM agent**, spawns specialized **dev and review agents** to do the work, and coordinates the entire lifecycle — from issue to merged PR — without human intervention.

Each agent runs in its own Copilot SDK session with its own git worktree, tools, and context. The PM agent delegates work using the SDK's native **subagent** and **custom agent** primitives. Everything is configurable via YAML.

### Key Features

- **Multi-agent orchestration** — PM, feature dev, bug fix, PR review, security review agents
- **Copilot SDK native** — uses `CustomAgentConfig`, subagents, MCP servers, skills
- **GitHub-native** — GitHub App webhooks, issue/PR CRUD, label taxonomy, approval flows
- **Branch isolation** — each agent gets its own git worktree
- **Dependency tracking** — agents can block on other issues with BFS cycle detection
- **Circuit breakers** — per-role limits on tool calls, turns, and active duration
- **Sleep/wake lifecycle** — agents sleep when blocked, wake when blockers resolve
- **Reconciliation loop** — detects stuck agents and auto-escalates
- **BYOK support** — bring your own API key (OpenAI, Anthropic, etc.) or use Copilot auth

## Architecture

```
GitHub Webhooks ──▶ FastAPI Server ──▶ Event Router ──▶ PM Queue
                                                           │
                                                    PM Agent (Copilot SDK)
                                                    ├── label_issue()
                                                    ├── assign_issue()
                                                    └── create agent for issue
                                                           │
                                              ┌────────────┴──────────────┐
                                         Dev Agent                   Review Agent
                                     (git worktree)              (reads PR diff)
                                     ├── code changes             ├── submit_pr_review()
                                     ├── run tests                └── comment_on_issue()
                                     ├── open_pr()
                                     └── report_complete()
```

### Components

| Component | File | Purpose |
|-----------|------|---------|
| **Server** | `src/squadron/server.py` | FastAPI app, startup/shutdown lifecycle |
| **Webhook** | `src/squadron/webhook.py` | HMAC-SHA256 verified webhook endpoint |
| **Event Router** | `src/squadron/event_router.py` | Async consumer, bot self-filtering, PM queue |
| **Agent Manager** | `src/squadron/agent_manager.py` | Agent lifecycle, PM consumer, session management |
| **Copilot** | `src/squadron/copilot.py` | CopilotAgent wrapper, session config building |
| **Config** | `src/squadron/config.py` | YAML config + agent definition parsing |
| **Registry** | `src/squadron/registry.py` | SQLite agent registry with blocker tracking |
| **GitHub Client** | `src/squadron/github_client.py` | Async GitHub REST API (JWT → installation token) |
| **Reconciliation** | `src/squadron/reconciliation.py` | Background loop for stuck/orphaned agent detection |
| **Recovery** | `src/squadron/recovery.py` | GitHub-based state reconstruction on restart |
| **Models** | `src/squadron/models.py` | Pydantic models, enums, event types |
| **Tools** | `src/squadron/tools/squadron_tools.py` | Unified `@define_tool` tools with per-role selection |

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- GitHub App (for webhook + API access)
- GitHub Copilot authentication (via `copilot auth login`)

### Install

```bash
git clone https://github.com/nbaertsch/squadron.git
cd squadron
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Authenticate Copilot

```bash
# The Copilot CLI ships with github-copilot-sdk
.venv/lib/python3.13/site-packages/copilot/bin/copilot auth login
```

### Configure

Create a `.env` with your GitHub App credentials:

```bash
GITHUB_APP_ID=your_app_id
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_INSTALLATION_ID=your_installation_id
GITHUB_WEBHOOK_SECRET=your_webhook_secret
```

### Run

```bash
squadron  # or: uv run python -m squadron
```

The server starts on `http://localhost:8000`. Point your GitHub App's webhook URL at `/webhook`.

## Configuration

All configuration lives in `.squadron/`:

```
.squadron/
├── config.yaml      # Project config, labels, runtime, circuit breakers
└── agents/          # Agent definitions (YAML frontmatter + markdown)
    ├── pm.md
    ├── feat-dev.md
    ├── bug-fix.md
    ├── pr-review.md
    ├── security-review.md
    ├── code-search.md
    └── test-writer.md
```

### config.yaml

```yaml
project:
  name: "my-project"
  owner: "org-name"
  repo: "repo-name"
  default_branch: main
  bot_username: "squadron[bot]"  # for self-event filtering

labels:
  types: [feature, bug, security, docs]
  priorities: [critical, high, medium, low]
  states: [needs-triage, in-progress, blocked, needs-human]

runtime:
  default_model: "claude-sonnet-4"
  provider:
    type: "anthropic"          # or "copilot" for Copilot-native auth
    base_url: "https://api.anthropic.com"
    api_key_env: "ANTHROPIC_API_KEY"

circuit_breakers:
  defaults:
    max_tool_calls: 200
    max_turns: 50
    max_active_duration: 7200  # 2 hours

approval_flows:
  enabled: true
  default_reviewers: [pr-review]
  rules:
    - name: security-sensitive
      match_labels: [security]
      match_paths: ["src/**/auth/**"]
      reviewers: [security-review]
```

### Agent Definitions

Agent `.md` files use YAML frontmatter that maps directly to Copilot SDK's `CustomAgentConfig`:

```markdown
---
name: feat-dev
display_name: Feature Developer
description: Implements features from GitHub issues.
infer: true
tools:
  - check_for_events
  - report_blocked
  - report_complete
  - open_pr
  - comment_on_issue
subagents:
  - code-search
  - test-writer
mcp_servers:
  my-server:
    type: http
    url: https://my-mcp-server.example.com
tool_restrictions:
  allowed_commands: [pytest, ruff, git]
  denied_commands: [rm -rf]
constraints:
  max_time: 3600
  can_write_code: true
---
You are a feature development agent for **{project_name}**.

Your task: implement the feature described in issue #{issue_number}.
Branch: `{branch_name}`, base: `{base_branch}`.

## Workflow
1. Read the issue requirements
2. Write code on your branch
3. Run tests with `pytest`
4. Open a PR with `open_pr`
5. Call `report_complete` when done
```

**Frontmatter fields:**

| Field | SDK Mapping | Description |
|-------|-------------|-------------|
| `name` | `CustomAgentConfig.name` | Agent identifier |
| `display_name` | `CustomAgentConfig.display_name` | Human-readable name |
| `description` | `CustomAgentConfig.description` | What the agent does |
| `infer` | `CustomAgentConfig.infer` | Whether SDK infers when to delegate |
| `tools` | `CustomAgentConfig.tools` | Available tool names |
| `subagents` | *(Squadron extension)* | Names of child agents |
| `mcp_servers` | `CustomAgentConfig.mcp_servers` | MCP server configs |
| `tool_restrictions` | *(Squadron extension)* | Allowed/denied shell commands |
| `constraints` | *(Squadron extension)* | Time limits, permissions |

The markdown body becomes the agent's **prompt** (system message).

## Agent Lifecycle

```
CREATED → ACTIVE → SLEEPING → ACTIVE → COMPLETED
                       ↑                    │
                       └── (blocker resolved)│
                                            ↓
                                      ESCALATED / FAILED
```

| State | Description |
|-------|-------------|
| `CREATED` | Agent record exists, resources being provisioned |
| `ACTIVE` | Agent has a live Copilot SDK session, working |
| `SLEEPING` | Blocked on dependency — session preserved, task removed |
| `COMPLETED` | Work done, PR merged, resources freed |
| `ESCALATED` | Circuit breaker tripped or unhandled error |
| `FAILED` | Lost state on restart — requires human re-trigger |

## Tools

All 13 tools live in a unified `SquadronTools` registry. Each role gets only its configured subset via `tools:` in `config.yaml` (or lifecycle-based defaults if omitted).

| Tool | Default For | Description |
|------|-------------|-------------|
| `check_for_events` | persistent | Poll for pending framework events |
| `report_blocked` | persistent | Declare a dependency, enter SLEEPING |
| `report_complete` | persistent | Mark work as done, enter COMPLETED |
| `create_blocker_issue` | persistent | Create a blocking issue + register dependency |
| `escalate_to_human` | persistent | Escalate to human with notification |
| `comment_on_issue` | both | Post comments on a GitHub issue |
| `submit_pr_review` | persistent | Submit a PR review (approve/request changes) |
| `open_pr` | persistent | Open a pull request from the agent's branch |
| `create_issue` | ephemeral | Create a new GitHub issue |
| `assign_issue` | ephemeral | Assign an issue to users |
| `label_issue` | ephemeral | Apply labels to an issue |
| `read_issue` | ephemeral | Read an issue's full details |
| `check_registry` | ephemeral | Query the agent registry for active agents |

## Testing

```bash
# All tests (unit + E2E)
uv run pytest

# Unit tests only (no credentials needed — runs everywhere)
uv run pytest tests/ --ignore=tests/e2e/

# E2E GitHub API tests (requires GitHub App credentials)
uv run pytest tests/e2e/ -v -m "not live" --ignore=tests/e2e/test_lifecycle_e2e.py

# E2E lifecycle tests (requires Copilot auth + GitHub App credentials)
uv run pytest tests/e2e/test_lifecycle_e2e.py -v

# Skip live LLM tests
uv run pytest -m "not live"
```

### Test tiers

| Tier | Count | Credentials | Runs in CI? |
|------|-------|-------------|-------------|
| **Unit tests** | ~334 | None | Yes |
| **E2E GitHub API** | ~26 | GitHub App secrets | Yes (with secrets configured) |
| **E2E Copilot** | ~9 | Copilot CLI + GitHub App secrets | **No** — local only |
| **Lifecycle E2E** | ~9 | Copilot CLI + GitHub App secrets | **No** — local only |

Tests that require missing credentials **skip** automatically — they never fail
due to missing auth. The `copilot_authenticated` fixture checks Copilot CLI
auth status and skips if not authenticated.

### CI/CD

The CI pipeline (`.github/workflows/ci.yml`) runs:
- **lint** + **unit tests** on every push/PR (no secrets needed)
- **E2E GitHub API** tests on push to main (requires GitHub Actions secrets)
- **Docker build** on push to main

Lifecycle E2E tests (live LLM inference) require Copilot SDK auth via a
fine-grained PAT from a **Copilot-licensed** GitHub account.

#### Required GitHub Actions secrets for E2E

| Secret | Value |
|--------|-------|
| `SQ_APP_ID_DEV` | App ID |
| `SQ_APP_PRIVATE_KEY` | Full PEM content (not a file path) |
| `SQ_INSTALLATION_ID_DEV` | Installation ID |
| `SQ_COPILOT_TOKEN` | Fine-grained PAT from a Copilot-licensed user |

#### Required repository variables

| Variable | Value |
|----------|-------|
| `E2E_ENABLED` | `true` (enables the E2E job) |
| `E2E_TEST_OWNER` | GitHub owner (e.g. `nbaertsch`) |
| `E2E_TEST_REPO` | Test repo name (e.g. `squadron-e2e-test`) |

### Local E2E setup

```bash
# 1. Set up .env with GitHub App credentials
cp .env.example .env  # edit with your values

# 2. Authenticate Copilot CLI (interactive — opens browser)
COPILOT_BIN=$(find .venv -name copilot -type f -path '*/bin/*' | head -1)
$COPILOT_BIN auth login

# 3. Run all E2E tests
uv run pytest tests/e2e/ -v
```

## Development

```bash
# Format + lint
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/

# Run a specific test file
uv run pytest tests/test_lifecycle.py -v

# Run with logging
uv run pytest tests/e2e/ -v --log-cli-level=INFO
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_APP_ID` | Yes | GitHub App ID |
| `GITHUB_PRIVATE_KEY` | Yes | PEM-encoded private key |
| `GITHUB_INSTALLATION_ID` | Yes | Installation ID for target repo |
| `GITHUB_WEBHOOK_SECRET` | Yes | Webhook HMAC secret |
| `COPILOT_GITHUB_TOKEN` | Yes | Fine-grained PAT from a Copilot-licensed account |
| `SQUADRON_WORKTREE_DIR` | No | Override worktree base path (default: `.squadron-data/worktrees`) |
| `ANTHROPIC_API_KEY` | BYOK only | API key for Anthropic provider |
| `OPENAI_API_KEY` | BYOK only | API key for OpenAI provider |

## Deployment

Squadron runs as a **standalone service** — you deploy a container instance and point it at your repo via the GitHub App. You don't install Squadron into your repo's codebase.

**Quick start:**
1. Install the [Squadron GitHub App](https://github.com/apps/squadron-dev) on your repo
2. Copy `examples/.squadron/` into your repo root and customize
3. Copy the deployment workflow template into `.github/workflows/`
4. Set repository secrets and deploy

See **[deploy/](deploy/)** for full instructions and workflow templates:

| Target | Guide |
|--------|-------|
| **Azure Container Apps** | [deploy/azure-container-apps/](deploy/azure-container-apps/) |

The pre-built container image `ghcr.io/nbaertsch/squadron:latest` is published on every push to main — you never need to build your own.

## Development

After installing (see above), set up the git hooks:

```bash
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

This installs two hooks that run automatically:

- **pre-commit** — `ruff` lint + format check (blocks commits with issues)
- **pre-push** — full unit test suite (blocks pushes if tests fail)

You can also run them manually:

```bash
# Lint + format
uv run ruff check . --fix && uv run ruff format .

# Unit tests
uv run pytest tests/ --ignore=tests/e2e -x -q

# E2E tests (requires credentials)
uv run pytest tests/e2e/ -x -q
```

## License

MIT
