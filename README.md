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

Each agent runs in its own Copilot SDK session with its own git worktree, tools, and context. The PM agent delegates work using the SDK's native **subagent** and **custom agent** primitives. Everything is configurable via YAML and Markdown.

### Key Features

- **Multi-agent orchestration** — PM, feature dev, bug fix, PR review, security review agents
- **Copilot SDK native** — uses `CustomAgentConfig`, subagents, introspection tools
- **GitHub-native** — GitHub App webhooks, issue/PR CRUD, label taxonomy, approval flows
- **Branch isolation** — each agent gets its own git worktree
- **Dependency tracking** — agents can block on other issues with BFS cycle detection
- **Circuit breakers** — per-role limits on tool calls, turns, and active duration
- **Sleep/wake lifecycle** — agents sleep when blocked, wake when blockers resolve
- **Reconciliation loop** — detects stuck agents and auto-escalates
- **Tool-based architecture** — 20+ specialized tools with per-agent selection
- **BYOK support** — bring your own API key (OpenAI, Anthropic, etc.) or use Copilot auth

## Architecture

```
GitHub Webhooks ──▶ FastAPI Server ──▶ Event Router ──▶ PM Queue
                                                           │
                                                    PM Agent (Copilot SDK)
                                                    ├── Introspection Tools
                                                    ├── label_issue()
                                                    ├── assign_issue()
                                                    └── create agent for issue
                                                           │
                                              ┌────────────┴──────────────┐
                                         Dev Agent                   Review Agent
                                     (git worktree)              (reads PR diff)
                                     ├── git_push()              ├── submit_pr_review()
                                     ├── open_pr()               ├── get_pr_feedback()
                                     └── 20+ specialized tools   └── merge_pr()
```

### Core Architecture Principles

1. **GitHub as the State Machine**: Issues, PRs, labels, and webhooks drive all agent behavior
2. **Tool-Based Agents**: Agents select from 20+ specialized tools via Markdown frontmatter
3. **Introspection Over Injection**: Agents use tools to understand state rather than receiving injected context
4. **Persistent Sessions**: Development agents maintain context across sleep/wake cycles
5. **Human-Compatible Interface**: All agent actions are human-readable and reversible

## Quick Start

### 1. Prerequisites

- Python 3.11+
- GitHub repository with admin access
- LLM API key (OpenAI, Anthropic) or GitHub Copilot access

### 2. Installation

```bash
# Install Squadron
pip install squadron

# Or install from source
git clone https://github.com/your-org/squadron.git
cd squadron
pip install -e .
```

### 3. GitHub App Setup

Create a GitHub App for your repository:

```bash
# Follow the interactive setup guide
squadron setup-github-app
```

Or see [detailed GitHub App setup guide](deploy/github-app-setup.md).

### 4. Configure Your Project

Copy the example configuration:

```bash
# Copy examples to your repository
cp -r examples/.squadron /path/to/your/repo/.squadron

# Edit configuration
vim /path/to/your/repo/.squadron/config.yaml
```

Minimal configuration:

```yaml
# .squadron/config.yaml
project:
  name: "my-project"
  owner: "my-github-org"
  repo: "my-repo"
  default_branch: main

human_groups:
  maintainers: ["your-github-username"]
```

### 5. Deploy Squadron

```bash
# Run locally for testing
squadron serve --repo-root /path/to/your/repo

# Or deploy to Azure Container Apps
# See deploy/azure-container-apps/README.md
```

### 6. Test the System

1. **Open an issue** in your repository labeled with `feature`, `bug`, or `security`
2. **Watch the PM agent** triage and assign the issue
3. **See development agents** create branches and implement solutions
4. **Review the PR** created by the development agent

## Agent System

Squadron includes several pre-configured agent types:

### PM Agent (Project Manager)
- **Triggers**: New issues, issue updates, @squadron-dev mentions
- **Responsibilities**: Triaging, labeling, assigning work to specialized agents
- **Tools**: Issue management, registry introspection, escalation
- **Lifecycle**: Ephemeral (runs once per event)

### Development Agents
- **feat-dev**: Implements new features
- **bug-fix**: Fixes bugs and regressions  
- **docs-dev**: Updates documentation
- **infra-dev**: Infrastructure and deployment changes
- **test-coverage**: Adds tests and improves coverage

### Review Agents
- **pr-review**: General code review
- **security-review**: Security-focused review

### Agent Configuration

Agents are configured using Markdown files with YAML frontmatter:

```yaml
---
name: feat-dev
description: Feature development agent
tools:
  - read_issue
  - open_pr
  - git_push
  - check_for_events
  - report_complete
lifecycle: persistent
---

# Feature Development Agent

You implement new features by writing code, tests, and documentation...
```

See [Agent Configuration Reference](docs/reference/agent-configuration.md) for detailed configuration options.

## Tools System

Squadron provides 20+ specialized tools organized into categories:

- **Framework Tools**: Agent lifecycle management (report_complete, check_for_events)
- **Issue Management**: Create, read, update, label, assign issues
- **Pull Request Tools**: Create PRs, get feedback, merge, review
- **Repository Context**: CI status, repo info, branch management
- **Introspection**: Agent registry, recent history, role information
- **Communication**: Comments, notifications, escalations

See [Tools Reference](docs/reference/tools.md) for complete tool documentation.

## Configuration

### Project Configuration

```yaml
# .squadron/config.yaml
project:
  name: "my-project"
  owner: "github-org"
  repo: "repo-name"
  default_branch: main

human_groups:
  maintainers: ["alice", "bob"]
  reviewers: ["charlie", "diana"]

labels:
  types: [feature, bug, security, docs, infra]
  priorities: [critical, high, medium, low]
  states: [needs-triage, in-progress, blocked, needs-human]

# Optional: Custom agent triggers
agents:
  custom-agent:
    triggers:
      - issue_opened
      - issue_labeled: ["custom-label"]
```

### Environment Variables

```bash
# Required for GitHub integration
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----..."
GITHUB_WEBHOOK_SECRET=your-webhook-secret

# Required for LLM access
OPENAI_API_KEY=sk-...
# OR
ANTHROPIC_API_KEY=sk-ant-...
# OR for GitHub Copilot
GITHUB_TOKEN=ghp_...

# Optional: Database and runtime
DATABASE_URL=sqlite:///squadron.db
LOG_LEVEL=INFO
```

## Deployment

Squadron can be deployed in several ways:

### Local Development
```bash
squadron serve --repo-root /path/to/repo
```

### Azure Container Apps
```bash
# See deploy/azure-container-apps/README.md
az deployment group create --template-file deploy/azure-container-apps/main.bicep
```

### Docker
```bash
docker build -t squadron .
docker run -d \
  -e GITHUB_APP_ID=$GITHUB_APP_ID \
  -e GITHUB_APP_PRIVATE_KEY="$GITHUB_APP_PRIVATE_KEY" \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -p 8000:8000 \
  squadron
```

See [Deployment Guide](deploy/README.md) for detailed deployment instructions.

## Examples

### Basic Feature Development
```yaml
# .squadron/agents/feat-dev.md
---
name: feat-dev
tools: [read_issue, open_pr, git_push, check_for_events, report_complete]
---

# Feature Development Agent
You implement new features by writing clean, tested code...
```

### Custom Agent for API Documentation
```yaml
# .squadron/agents/api-docs.md  
---
name: api-docs
tools: [read_issue, open_pr, git_push, get_repo_info]
---

# API Documentation Agent
You generate and maintain API documentation...
```

More examples in the [examples/](examples/) directory.

## Monitoring and Observability

Squadron provides built-in monitoring:

```bash
# View agent status
squadron status

# Check recent activity
squadron logs --agent-id feat-dev-123

# Monitor resource usage
squadron monitor
```

## Contributing

1. **Fork and clone** the repository
2. **Install development dependencies**: `pip install -e ".[dev]"`
3. **Set up pre-commit hooks**: `pre-commit install`
4. **Run tests**: `pytest tests/`
5. **Submit a pull request**

See [Contributing Guide](CONTRIBUTING.md) for detailed guidelines.

## Development

After installing (see above), set up the git hooks:

```bash
pip install -e ".[dev]"
pre-commit install --hook-type pre-commit --hook-type pre-push
```

This installs two hooks that run automatically:

- **pre-commit** — `ruff` lint + format check (blocks commits with issues)
- **pre-push** — full unit test suite (blocks pushes if tests fail)

You can also run them manually:

```bash
# Lint + format
ruff check . --fix && ruff format .

# Unit tests
pytest tests/ --ignore=tests/e2e -x -q

# E2E tests (requires credentials)  
pytest tests/e2e/ -x -q
```

## Documentation

- [Getting Started Guide](docs/getting-started.md) - Step-by-step setup
- [Agent Configuration Reference](docs/reference/agent-configuration.md) - Complete agent config guide  
- [Tools Reference](docs/reference/tools.md) - All available tools
- [Deployment Guide](deploy/README.md) - Production deployment
- [Architecture Deep Dive](docs/architecture.md) - Technical details
- [Troubleshooting](docs/troubleshooting.md) - Common issues and solutions

## License

MIT

## Support

- **GitHub Issues**: [Report bugs and request features](https://github.com/your-org/squadron/issues)
- **Discussions**: [Community discussion and questions](https://github.com/your-org/squadron/discussions)
- **Documentation**: [docs/](docs/) directory
