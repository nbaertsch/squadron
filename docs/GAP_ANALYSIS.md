# Documentation Gap Analysis

**Date:** February 2026  
**Scope:** Full repository documentation audit for Squadron project  
**Purpose:** Identifies missing docs, outdated content, and structural issues discovered during the documentation overhaul (issue #135).

---

## 1. Existing Documentation Inventory

| File | Status | Notes |
|------|--------|-------|
| `README.md` | ⚠️ Outdated | Wrong env var names, non-existent CLI commands |
| `docs/getting-started.md` | ⚠️ Outdated | Non-existent commands (`squadron status`, `validate-config`, etc.) |
| `docs/architecture.md` | ✅ Accurate | Good technical overview |
| `docs/troubleshooting.md` | ✅ Accurate | Minor cleanup needed |
| `docs/observability.md` | ✅ Accurate | Good dashboard reference |
| `docs/agent-collaboration.md` | ✅ Accurate | Good collaboration guide |
| `docs/reference/agent-configuration.md` | ⚠️ Incomplete | Missing `display_name`, `emoji`, `infer`, `skills` frontmatter fields |
| `docs/reference/tools.md` | ✅ Accurate | Good tool reference |
| `deploy/README.md` | ⚠️ Outdated | Wrong env var names in one section |
| `deploy/github-app-setup.md` | ✅ Accurate | Good step-by-step guide |
| `deploy/workflows/README.md` | ✅ Accurate | Good workflow reference |
| `deploy/azure-container-apps/README.md` | ✅ Accurate | Good ACA deployment guide |
| `examples/README.md` | ⚠️ Outdated | References non-existent CLI commands |
| `infra/DEPLOYMENT.md` | ⚠️ Internal | Internal deployment notes, not user docs |
| `infra/agent-pr-communication-patch.md` | ⚠️ Internal | Internal patch notes |
| `infra/pr-communication-guidelines.md` | ⚠️ Internal | Internal guidelines |
| `timeout_analysis.md` | ⚠️ Internal | Internal analysis document at root |
| `docs/ACTION-PLAN.md` | ⚠️ Internal | Internal planning doc |
| `docs/DESIGN-REVIEW-PLAN.md` | ⚠️ Internal | Internal planning doc |
| `docs/IMPLEMENTATION-PLAN.md` | ⚠️ Internal | Internal planning doc |
| `docs/project-plan/` | ⚠️ Internal | Internal design research docs |
| `docs/testing-strategy.md` | ✅ Accurate | Good testing reference |

---

## 2. Critical Accuracy Issues

### 2a. Wrong Environment Variable Names
**Affected files:** `README.md`, `docs/getting-started.md`, `deploy/README.md`

The README and getting-started guide reference environment variables that don't match the actual `.env.example` and `__main__.py`:

| Documented (wrong) | Actual |
|--------------------|----|
| `GITHUB_APP_ID` | `SQ_APP_ID_DEV` |
| `GITHUB_APP_PRIVATE_KEY` | `SQ_APP_PRIVATE_KEY_FILE` / `SQ_APP_PRIVATE_KEY` |
| `GITHUB_WEBHOOK_SECRET` | `GITHUB_WEBHOOK_SECRET` ✅ (consistent in deploy code) |
| `OPENAI_API_KEY` | `OPENAI_API_KEY` ✅ |

**Note:** The `__main__.py` deploy command uses `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`, `GITHUB_INSTALLATION_ID`, `GITHUB_WEBHOOK_SECRET` — these are the production deployment variables. The `.env.example` uses `SQ_APP_ID_DEV` etc. for local development. Both sets need to be clearly documented.

### 2b. Non-Existent CLI Commands
**Affected files:** `README.md`, `docs/getting-started.md`, `examples/README.md`

Commands referenced that don't exist in `src/squadron/__main__.py`:

- `squadron setup-github-app` — does not exist
- `squadron --version` — does not exist
- `squadron status` — does not exist
- `squadron logs` — does not exist
- `squadron monitor` — does not exist
- `squadron validate-config` — does not exist
- `squadron validate-agents` — does not exist
- `squadron test-webhook` — does not exist
- `squadron test-agent` — does not exist
- `squadron test-workflow` — does not exist

**Actual commands:**
- `squadron serve` — starts the webhook server
- `squadron deploy` — deploys to Azure Container Apps

### 2c. Package Not on PyPI
**Affected files:** `README.md`, `docs/getting-started.md`

Both files reference `pip install squadron`, but the package is not published on PyPI. Installation is source-only.

### 2d. Missing CONTRIBUTING.md
**Affected files:** `README.md` links to `CONTRIBUTING.md` which doesn't exist.

---

## 3. Missing Documentation

| Missing Item | Priority | Notes |
|--------------|----------|-------|
| `CONTRIBUTING.md` | High | Referenced from README, needed for contributors |
| `docs/index.md` | High | No navigation hub for the docs/ directory |
| `docs/configuration.md` | Medium | No dedicated config.yaml reference |
| Agent docs for `merge-conflict` | Medium | Agent exists in `.squadron/agents/` but not in user docs |
| Agent docs for `code-search` | Low | Subagent, limited user exposure |
| Agent docs for `test-writer` | Low | Subagent, limited user exposure |

---

## 4. Structural Issues

### 4a. Internal Docs Mixed with User Docs
The following files are internal planning/design documents that should not be in the main user-facing documentation path:

- `docs/ACTION-PLAN.md` — internal action plan
- `docs/DESIGN-REVIEW-PLAN.md` — internal design review
- `docs/IMPLEMENTATION-PLAN.md` — internal implementation plan
- `docs/project-plan/` — research and design docs (10+ files)
- `timeout_analysis.md` — root-level internal analysis
- `infra/agent-pr-communication-patch.md` — internal patch notes
- `infra/pr-communication-guidelines.md` — internal guidelines

**Recommendation:** Move to `docs/internal/` or leave in place but don't surface them in the main navigation.

### 4b. Observability Doc Location
`docs/observability.md` is a detailed dashboard API reference. This could be surfaced more prominently in the `README.md` documentation links section.

### 4c. No Top-Level Docs Index
The `docs/` directory has no index file. Users browsing the directory have no clear navigation path.

---

## 5. Inconsistencies

### 5a. Label Trigger Documentation
The PM agent config clearly documents that `infrastructure` label does **not** auto-spawn agents, but multiple user-facing docs imply all type labels trigger agents automatically.

### 5b. Agent Roster
The full agent roster (including `code-search`, `merge-conflict`, `test-writer`) is only documented in agent config files. User-facing documentation only shows the primary agents.

### 5c. `agent-configuration.md` Missing Fields
The reference doc for agent configuration doesn't include these frontmatter fields that exist in actual agent configs:
- `display_name` — human-readable name
- `emoji` — agent signature emoji
- `infer` — whether SDK should infer context
- `skills` — skill directory assignments

---

## 6. Changes Made in This Overhaul

### Updated Files
- `README.md` — Fixed CLI commands, env vars, removed non-existent features, added accurate quickstart
- `docs/getting-started.md` — Fixed all command references, corrected env var documentation
- `docs/reference/agent-configuration.md` — Added missing frontmatter fields, updated tool lists
- `deploy/README.md` — Corrected environment variable documentation
- `examples/README.md` — Removed non-existent CLI commands

### New Files
- `docs/GAP_ANALYSIS.md` — This document
- `docs/index.md` — Navigation hub for the docs/ directory
- `docs/configuration.md` — Dedicated configuration reference
- `docs/agents.md` — Complete agent roles reference
- `CONTRIBUTING.md` — Contributor guide

### Not Changed (Accurate)
- `docs/architecture.md` — Accurate, no changes needed
- `docs/observability.md` — Accurate, no changes needed
- `docs/troubleshooting.md` — Accurate, minor cleanup only
- `deploy/github-app-setup.md` — Accurate
- `deploy/azure-container-apps/README.md` — Accurate
