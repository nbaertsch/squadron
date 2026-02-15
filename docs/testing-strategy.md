# Squadron Testing Strategy

## The Problem

The initial test suite mocked both GitHub API and Copilot SDK completely.
Tests verified internal logic (state machines, registry CRUD, routing) but had
**zero confidence** that real HTTP calls, SDK usage, or webhook parsing would
work against the actual services.

## Testing Tiers

### Tier 1: Unit Tests (always run, no network)

Standard pytest tests with `AsyncMock` for external dependencies. These cover
business logic like state transitions, approval flows, circuit breakers, and
registry operations.

**Files:**
- `test_models.py` — Pydantic model validation, state enums
- `test_registry.py` — SQLite CRUD, blocker management, cycle detection
- `test_config.py` — Config loading, agent definition parsing
- `test_event_router.py` — Event classification, handler dispatch
- `test_copilot.py` — Session config builders (uses **real SDK types**)
- `test_approval_flow.py` — Multi-step approval state machine
- `test_lifecycle.py` — Agent lifecycle transitions, circuit breakers
- `test_pm_and_escalation.py` — PM tools, escalation to human
- `test_reconciliation.py` — Sleeping agent checks, stale agent escalation

### Tier 2: Contract Tests (always run, no network, respx)

Use [respx](https://lundberg.github.io/respx/) to intercept `httpx` requests
at the transport level. These verify that our `GitHubClient` sends the correct:

- **HTTP method** (GET/POST/PATCH/PUT)
- **URL path** (e.g., `/repos/{owner}/{repo}/issues/{number}`)
- **JSON body** (correct field names and values)
- **Auth headers** (`Authorization: Bearer {token}`)
- **Rate limit tracking** from response headers
- **Error handling** (404, 422, 500)
- **Webhook signature verification** (HMAC-SHA256)

These run without hitting GitHub's servers but validate that API integration
code is correctly formed.

**Files:**
- `test_github_client.py` — 21 contract tests covering all GitHubClient methods

### Tier 3: Payload Integration Tests (always run, no network)

Use **real GitHub webhook payloads** (captured from actual GitHub deliveries)
to test the full webhook→EventRouter→handler chain. Unlike unit tests which
use synthetic payloads, these verify that our code handles the actual JSON
structure GitHub sends.

**Files:**
- `tests/fixtures/github_payloads.json` — 9 realistic webhook payloads
- `test_webhook_integration.py` — 12 tests verifying full payload chain

### Tier 4: Server Integration Tests (always run, no network)

Boot the full `SquadronServer` with real SQLite, real FastAPI, real config
loading. Only external services (GitHub API, Copilot CLI) are mocked. These
verify that all components wire together correctly through the actual startup
sequence.

Also validates that our config builders produce dicts compatible with the
**real Copilot SDK types** (not mocks — actual `from copilot.types import`).

**Files:**
- `test_server_integration.py` — Server boot, HTTP endpoints, SDK type checks

### Tier 5: Live Integration Tests (manual, requires credentials)

For development, run against real GitHub repos + Copilot SDK:

```bash
# Set credentials
export GITHUB_APP_ID=...
export GITHUB_PRIVATE_KEY="$(cat private-key.pem)"
export GITHUB_WEBHOOK_SECRET=...
export GITHUB_INSTALLATION_ID=...

# Run with integration flag
SQUADRON_INTEGRATION=1 pytest tests/ -m integration
```

These are **not** in CI. They require a real GitHub App installation and
Copilot CLI binary.

## What's Mocked vs Real

| Component | Unit Tests | Contract Tests | Server Tests |
|-----------|-----------|---------------|-------------|
| SQLite registry | **real** | n/a | **real** |
| Config loading | **real** | n/a | **real** |
| Agent definitions | **real** | n/a | **real** |
| FastAPI endpoints | n/a | n/a | **real** |
| Event routing | **real** | n/a | **real** |
| GitHub HTTP calls | mocked | **respx** (validates shape) | mocked |
| Copilot SDK types | **real imports** | n/a | **real imports** |
| Copilot CLI binary | mocked | n/a | mocked |
| Webhook signatures | tested directly | **respx** | mocked |
| Git worktree ops | mocked | n/a | mocked |

## Running Tests

```bash
# All tests (Tiers 1-4)
pytest

# Contract tests only
pytest tests/test_github_client.py -v

# Server integration only
pytest tests/test_server_integration.py -v

# With verbose output
pytest -v --tb=short
```

## Test Count Summary

| File | Tests | Tier |
|------|-------|------|
| test_models.py | 14 | Unit |
| test_registry.py | 15 | Unit |
| test_config.py | 15 | Unit |
| test_event_router.py | 11 | Unit |
| test_webhook.py | 7 | Unit |
| test_copilot.py | 14 | Unit |
| test_approval_flow.py | 18 | Unit |
| test_lifecycle.py | 20 | Unit |
| test_pm_and_escalation.py | 20 | Unit |
| test_reconciliation.py | 14 | Unit |
| test_github_client.py | 21 | Contract |
| test_webhook_integration.py | 12 | Payload |
| test_server_integration.py | 10 | Server |
| **Total** | **192** | |
