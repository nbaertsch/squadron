# Squadron Documentation

Welcome to the Squadron documentation. Squadron is a GitHub-native multi-LLM-agent autonomous development framework.

## Getting Started

| Document | Description |
|----------|-------------|
| [Getting Started Guide](getting-started.md) | Step-by-step setup: installation, GitHub App, first workflow |
| [GitHub App Setup](../deploy/github-app-setup.md) | Create and configure your GitHub App |
| [Examples](../examples/README.md) | Example configurations to copy and customize |

## Core Concepts

| Document | Description |
|----------|-------------|
| [Architecture](architecture.md) | System overview, components, data flow, design decisions |
| [Agent Roles](agents.md) | All agent types, triggers, responsibilities, and tool sets |
| [Configuration Reference](configuration.md) | `config.yaml` schema, all options with defaults |

## Reference

| Document | Description |
|----------|-------------|
| [Agent Configuration Reference](reference/agent-configuration.md) | Agent frontmatter fields, lifecycle types, circuit breakers |
| [Tools Reference](reference/tools.md) | All available tools with descriptions and usage |
| [Observability & Dashboard](observability.md) | Activity logging, SSE streaming, dashboard API |

## Operations

| Document | Description |
|----------|-------------|
| [Deployment Guide](../deploy/README.md) | Deploy Squadron to Azure or other platforms |
| [Azure Container Apps](../deploy/azure-container-apps/README.md) | Azure-specific deployment guide |
| [Troubleshooting](troubleshooting.md) | Common problems and solutions |

## Contributing

| Document | Description |
|----------|-------------|
| [Contributing Guide](../CONTRIBUTING.md) | How to contribute, development workflow, coding standards |
| [Testing Strategy](testing-strategy.md) | Test architecture and how to write tests |

## Meta

| Document | Description |
|----------|-------------|
| [Gap Analysis](GAP_ANALYSIS.md) | Documentation audit and overhaul notes (issue #135) |

---

## How Squadron Works (30-second summary)

1. **Issue opened** → GitHub webhook fires → **PM agent** triages, applies a label
2. **Label applied** → framework **spawns a dev agent** matching the label (e.g. `feature` → `feat-dev`)
3. **Dev agent** creates a branch, writes code, opens a PR
4. **PR opened** → **review agents** analyze the diff and post feedback
5. **PR approved** → merge (automatic or manual)

All configuration lives in `.squadron/` in your repository. The Squadron service is deployed separately and points at your repo.
