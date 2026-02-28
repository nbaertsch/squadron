<p align="center">
  <img src="assets/logo.svg" alt="Squadron" width="128" height="128"/>
</p>

<h1 align="center">Squadron</h1>

<p align="center">
  <strong>GitHub-native multi-LLM-agent autonomous development framework</strong>
</p>

<p align="center">
  A <a href="https://github.com/github/copilot-sdk">GitHub Copilot SDK</a> extension that turns your GitHub repo into a self-organizing software team.
  <br/>
  <em>Issue opened → PM triages → Dev agent writes code → Review agent reviews → PR merged.</em>
</p>

---

## What is Squadron?

Squadron is an autonomous development system built on the [GitHub Copilot SDK](https://github.com/github/copilot-sdk). It listens for GitHub webhooks, triages issues with a **PM agent**, spawns specialized **dev and review agents** to do the work, and coordinates the entire lifecycle — from issue to merged PR — without human intervention.

Each agent runs in its own Copilot SDK session with its own git worktree, tools, and context. Everything is configurable via YAML and Markdown agent definitions stored in your repository.

### Key Features

- **Multi-agent orchestration** — PM, feature dev, bug fix, docs, infra, PR review, security review agents
- **GitHub-native** — GitHub App webhooks, issue/PR CRUD, label taxonomy, approval flows
- **Label-driven spawning** — applying `feature`, `bug`, `security`, or `documentation` labels automatically spawns the matching agent
- **Branch isolation** — each agent gets its own git worktree
- **Dependency tracking** — agents can block on other issues
- **Circuit breakers** — per-role limits on tool calls, turns, and active duration
- **Sleep/wake lifecycle** — persistent agents sleep when blocked, wake when blockers resolve
- **Reconciliation loop** — detects stuck agents and auto-escalates
- **Tool-based architecture** — 20+ specialized tools with per-agent selection
- **Observability dashboard** — real-time agent activity via SSE streaming

## Quick Start

### Prerequisites

- Python 3.11+
- GitHub repository with admin access
- GitHub Copilot access (or an LLM API key: OpenAI or Anthropic)

### 1. Install from Source

```bash
git clone https://github.com/your-org/squadron.git
cd squadron
pip install -e .
```

### 2. Copy Example Configuration

Copy the example configuration into your target repository:

```bash
# In your target repository
cp -r /path/to/squadron/examples/.squadron .
```

Edit `.squadron/config.yaml` with your project details:

```yaml
project:
  name: "my-project"
  owner: "my-github-org"
  repo: "my-repo"
  default_branch: main

human_groups:
  maintainers: ["your-github-username"]
```

> **Security note:** The `human_groups.maintainers` list controls who can trigger Squadron
> system events (agent spawning, PM triage, command routing). Only users listed here can
> activate Squadron workflows. The Squadron bot identity (`squadron-dev[bot]`) is always
> permitted regardless of this list. An empty or missing `maintainers` group blocks all
> human-triggered event processing. See the
> [Configuration Reference](docs/configuration.md#human_groups--human-contact-groups)
> for full details.

### 3. Create a GitHub App

Follow the [GitHub App setup guide](deploy/github-app-setup.md) to create a GitHub App for your repository and obtain:
- App ID
- Private key (`.pem` file)
- Installation ID
- Webhook secret

### 4. Run Locally

```bash
# Set required environment variables
export GITHUB_APP_ID=123456
export GITHUB_PRIVATE_KEY="$(cat your-app.private-key.pem)"
export GITHUB_INSTALLATION_ID=78901234
export GITHUB_WEBHOOK_SECRET=your-webhook-secret
export COPILOT_GITHUB_TOKEN=github_pat_...

# Start the server
squadron serve --repo-root /path/to/your/repo
```

For local webhook testing, use [ngrok](https://ngrok.com/):
```bash
ngrok http 8000
# Update your GitHub App webhook URL to the ngrok URL + /webhook
```

### 5. Deploy to Production

```bash
# Deploy to Azure Container Apps
squadron deploy --repo-root /path/to/your/repo
```

See the [Deployment Guide](deploy/README.md) for detailed deployment instructions.

### 6. Test the System

1. **Open an issue** in your repository labeled with `feature`, `bug`, or `documentation`
2. **Watch the PM agent** triage and assign the issue (should appear within seconds)
3. **See development agents** create branches and implement solutions
4. **Review the PR** created by the development agent

## How It Works

```
GitHub Webhooks ──▶ FastAPI Server ──▶ Event Router ──▶ Agent Manager
                                                              │
                                                       PM Agent (ephemeral)
                                                       ├── Reads issue
                                                       ├── Applies label
                                                       └── Posts triage comment
                                                              │
                                              ┌───────────────┴──────────────┐
                                         Dev Agent                    Review Agent
                                      (persistent)                  (persistent)
                                      ├── Creates branch             ├── Reads PR diff
                                      ├── Writes code                ├── Posts inline comments
                                      ├── Opens PR                   └── Submits review
                                      └── Handles feedback
```

**Label → Agent spawning:**

| Label | Agent | Auto-spawn |
|-------|-------|-----------|
| `feature` | `feat-dev` | ✅ |
| `bug` | `bug-fix` | ✅ |
| `security` | `security-review` | ✅ |
| `documentation` | `docs-dev` | ✅ |
| `infrastructure` | `infra-dev` | Via `@squadron-dev infra-dev` mention |

## Agent System

Squadron includes these pre-configured agent types:

| Agent | Role | Lifecycle |
|-------|------|-----------|
| `pm` | Project Manager — triages, labels, coordinates | Ephemeral |
| `feat-dev` | Feature Developer — implements new features | Persistent |
| `bug-fix` | Bug Fix Agent — diagnoses and fixes bugs | Persistent |
| `docs-dev` | Documentation Developer — writes and updates docs | Persistent |
| `infra-dev` | Infrastructure Developer — CI/CD, deployment | Persistent |
| `pr-review` | PR Reviewer — code quality review | Persistent |
| `security-review` | Security Reviewer — vulnerability analysis | Persistent |
| `test-coverage` | Test Coverage Reviewer — test adequacy review | Persistent |

See [Agent Roles Reference](docs/agents.md) for complete agent documentation.

## Configuration

Agents are defined as Markdown files with YAML frontmatter in `.squadron/agents/`:

```yaml
---
name: feat-dev
display_name: Feature Developer
emoji: "👨‍💻"
description: Implements new features
tools:
  - read_file
  - write_file
  - bash
  - git_push
  - open_pr
  - check_for_events
  - report_complete
lifecycle: persistent
---

# Feature Development Agent

You implement new features by writing code, tests, and opening pull requests...
```

See [Agent Configuration Reference](docs/reference/agent-configuration.md) and [Configuration Reference](docs/configuration.md) for all options.

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/getting-started.md) | Step-by-step setup guide |
| [Documentation Index](docs/index.md) | Full docs navigation |
| [Architecture](docs/architecture.md) | System design and components |
| [Agent Roles](docs/agents.md) | All agent types and triggers |
| [Configuration](docs/configuration.md) | `config.yaml` reference |
| [Tools Reference](docs/reference/tools.md) | All available tools |
| [Agent Config Reference](docs/reference/agent-configuration.md) | Agent frontmatter reference |
| [Observability](docs/observability.md) | Dashboard and monitoring |
| [Deployment Guide](deploy/README.md) | Production deployment |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and solutions |
| [Contributing](CONTRIBUTING.md) | Development guide |

## Development

After cloning, install development dependencies and set up hooks:

```bash
pip install -e ".[dev]"
pre-commit install --hook-type pre-commit --hook-type pre-push
```

Manually run checks:

```bash
# Lint + format
ruff check . --fix && ruff format .

# Unit tests
pytest tests/ --ignore=tests/e2e -x -q

# E2E tests (requires credentials in .env)
pytest tests/e2e/ -x -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full development guidelines.

## License

MIT

## Support

- **GitHub Issues**: [Report bugs and request features](https://github.com/your-org/squadron/issues)
- **Discussions**: [Community discussion and questions](https://github.com/your-org/squadron/discussions)
- **Documentation**: [docs/](docs/index.md) directory
