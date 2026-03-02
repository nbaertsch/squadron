# Contributing to Squadron

Thank you for your interest in contributing to Squadron! This guide explains the development workflow, coding standards, and how to get your changes merged.

## Prerequisites

- **Python 3.11+** (3.12 or 3.13 recommended)
- **[uv](https://github.com/astral-sh/uv)** — fast Python package manager
- **git**
- A GitHub account with access to the repository

## Development Setup

### 1. Clone and Install

```bash
git clone https://github.com/your-org/squadron.git
cd squadron

# Install with development dependencies
uv pip install -e ".[dev]"
```

### 2. Set Up Pre-commit Hooks

```bash
pre-commit install --hook-type pre-commit --hook-type pre-push
```

This installs two hooks:
- **pre-commit**: `ruff` lint and format check (blocks commits with issues)
- **pre-push**: full unit test suite (blocks pushes if tests fail)

### 3. Configure Environment (Optional)

For running e2e tests, copy and fill in `.env.example`:

```bash
cp .env.example .env
# Edit .env with your GitHub App credentials
```

---

## Development Workflow

### Making Changes

1. **Create a branch** from `main`:
   ```bash
   git checkout main
   git pull
   git checkout -b feat/my-feature
   ```

2. **Write code** following the [coding standards](#coding-standards) below.

3. **Write tests** — all new functionality must have tests. See [testing standards](#testing-standards).

4. **Run lint and tests locally**:
   ```bash
   # Lint + format
   uv run ruff check . --fix && uv run ruff format .
   
   # Unit tests
   uv run pytest tests/ --ignore=tests/e2e -x -q
   ```

5. **Commit** with a descriptive message:
   ```bash
   git commit -m "feat: add circuit breaker for tool call limits"
   ```

6. **Push and open a PR**:
   ```bash
   git push -u origin feat/my-feature
   # Open PR on GitHub targeting main
   ```

---

## Coding Standards

### Python Style

Squadron uses [`ruff`](https://docs.astral.sh/ruff/) for linting and formatting. Configuration is in `pyproject.toml`.

Key rules:
- **Line length**: 100 characters
- **Target version**: Python 3.11+
- **Imports**: sorted, no star imports

Run the formatter before committing:
```bash
uv run ruff check . --fix
uv run ruff format .
```

### Code Quality

- **Type hints**: Use type hints for all function signatures
- **Pydantic models**: Use Pydantic v2 for all data models — see `.squadron/skills/squadron-dev-guide/pydantic-conventions.md`
- **Async**: All I/O must be async — see `.squadron/skills/squadron-dev-guide/async-patterns.md`
- **Error handling**: Handle errors explicitly. No bare `except:` clauses.
- **Docstrings**: Public functions and classes should have docstrings
- **Constants**: No hardcoded magic values — use named constants or config

### Common Pitfalls

See `.squadron/skills/squadron-dev-guide/common-pitfalls.md` for a list of known patterns to avoid.

---

## Testing Standards

### Unit Tests

All unit tests go in `tests/` (mirroring the `src/squadron/` structure). Tests must be:

- **Fast**: Unit tests should not hit real APIs or spawn real processes
- **Isolated**: Use mocks for external dependencies (GitHub API, LLM API)
- **Deterministic**: No flaky tests

```bash
# Run unit tests
uv run pytest tests/ --ignore=tests/e2e -x -q

# Run a specific test file
uv run pytest tests/test_event_router.py -v
```

### E2E Tests

End-to-end tests in `tests/e2e/` require real GitHub credentials and are only run in CI on push to `main` (when `E2E_ENABLED=true`). They test actual GitHub API interactions but do not require Copilot SDK auth.

```bash
# Run e2e tests (requires .env with GitHub App credentials)
uv run pytest tests/e2e/ -x -q
```

### Test Patterns

See `.squadron/skills/squadron-dev-guide/testing-patterns.md` for:
- How to mock GitHub API calls
- How to test async event handlers
- How to test agent tool calls

---

## Documentation

When adding or changing features:

- **Update relevant docs** in `docs/` or `deploy/`
- **Update agent configs** in `.squadron/agents/*.md` if agent behavior changes
- **Update skill files** in `.squadron/skills/` if internal API changes
- **Add code examples** for any new public interfaces

Documentation is written in Markdown. Follow the style of existing docs.

---

## PR Guidelines

### PR Title

Use conventional commits format:
- `feat: description` — new feature
- `fix: description` — bug fix
- `docs: description` — documentation changes
- `refactor: description` — code refactoring
- `test: description` — test additions/changes
- `chore: description` — build/config changes

### PR Description

Every PR should include:
- **What** changed and **why**
- A reference to the issue: `Fixes #N` or `Closes #N`
- Test plan (what did you test?)
- Any migration notes if behavior changes

### Review Process

1. CI must pass (lint + unit tests)
2. At least one approval required
3. Address all reviewer feedback
4. Squash or rebase before merge (no merge commits)

---

## Project Structure

```
squadron/
├── src/squadron/           # Main source code
│   ├── __main__.py         # CLI entrypoint (serve, deploy commands)
│   ├── server.py           # FastAPI app factory
│   ├── webhook.py          # GitHub webhook handler
│   ├── event_router.py     # Routes events to handlers
│   ├── agent_manager.py    # Agent lifecycle management
│   ├── config.py           # Configuration loading and models
│   ├── models.py           # Domain models
│   ├── registry.py         # SQLite agent state registry
│   ├── github_client.py    # GitHub API client
│   ├── copilot.py          # GitHub Copilot SDK wrapper
│   ├── activity.py         # Activity logging
│   ├── dashboard.py        # Dashboard API endpoints
│   ├── recovery.py         # Agent failure recovery
│   ├── reconciliation.py   # Periodic reconciliation loop
│   ├── resource_monitor.py # Resource usage monitoring
│   ├── sandbox/            # Agent sandbox isolation
│   ├── tools/              # Squadron tool implementations
│   └── workflow/           # Workflow engine
├── tests/                  # Test suite
│   └── e2e/                # End-to-end tests
├── .squadron/              # Squadron's own configuration
│   ├── config.yaml         # Project config
│   ├── agents/             # Agent definitions
│   └── skills/             # Knowledge files
├── deploy/                 # Deployment templates and guides
├── docs/                   # User documentation
└── examples/               # Example configurations
```

---

## Getting Help

- **GitHub Issues**: For bugs and feature requests
- **GitHub Discussions**: For questions and ideas
- **Issue comments**: For questions about specific issues

For architecture questions, see [docs/architecture.md](docs/architecture.md).  
For internal codebase knowledge, see `.squadron/skills/squadron-internals/`.
