# Squadron — Project Plan

**Squadron** is a GitHub-Native multi-LLM-agent autonomous development framework that enables multiple AI agents of various roles to collaborate alongside humans on software projects, using GitHub's own primitives (Issues, Projects, PRs, Actions, branch protections, webhooks) as the orchestration and state layer.

## Core Thesis

GitHub itself is the orchestration layer — not a separate control plane. GitHub's native primitives serve as the state machine driving agent behavior:

- **Issues** are work units and communication channels
- **Pull Requests** are the code review and merge gate
- **Actions / Webhooks** are the event bus (nervous system)
- **Branch protections** enforce guardrails
- **Projects (Kanban)** provide visibility (V2)
- Every action produces a **human-readable audit trail** for free

Humans and agents interact through the **same interfaces** — no special dashboards.

## Project Plan Documents

| Document | Description |
|---|---|
| [Architecture Decisions](architecture-decisions.md) | Decisions made and rationale |
| [Agent Design](agent-design.md) | Agent identity, lifecycle, roles, definitions |
| [Workflow Design](workflow-design.md) | Approval flows, event routing, PM decision trees |
| [Open Research & TODOs](open-research.md) | Unresolved questions and research tasks |
| [Edge Cases](edge-cases.md) | Known edge cases and mitigation strategies |
| [Roadmap](roadmap.md) | V1 vs V2 feature scoping |
| [Research: Context Serialization](research/context-serialization.md) | Original SDK comparison (superseded by OR-001) |
| [Research: Copilot SDK Session Persistence](research/copilot-sdk-session-persistence.md) | Copilot SDK persistence mechanics, deployment patterns |
| [Research: Event Architecture](research/event-architecture.md) | GitHub App vs Actions vs Webhooks analysis (OR-002) |
| [Research: Event Routing](research/event-routing.md) | Agent Registry, dependency graph, event dispatch (OR-003) |
| [Research: Concurrency Strategy](research/concurrency-strategy.md) | Layered race condition handling (OR-004) |
| [Research: Approval Flow Schema](research/approval-flow-schema.md) | YAML schema for configurable approval flows (OR-005) |
| [Research: Role Enforcement](research/role-enforcement.md) | Dual-layer role enforcement via GitHub + framework (OR-006) |
| [Research: Runtime Architecture](research/runtime-architecture.md) | Server process model, agent lifecycle, tech stack (AD-017) |
| [Research: Circuit Breaker Design](research/circuit-breakers.md) | Limit enforcement, escalation flow, configuration (AD-018) |
| [Research: Config Schema](research/config-schema.md) | Canonical `.squadron/` directory structure and config reference |

## Repo Structure (Planned)

```
squadron/
├── docs/
│   └── project-plan/          # ← You are here
│       ├── research/           # Research findings
│       └── ...
├── .squadron/                  # Per-project config (consumed by the framework)
│   ├── agents/                 # Agent definitions (system prompts, tools, sub-agents)
│   │   ├── pm.md
│   │   ├── feat-dev.md
│   │   ├── bug-fix.md
│   │   ├── pr-review.md
│   │   └── security-review.md
│   ├── workflows/              # Approval flows, PM decision trees, branch rules
│   │   ├── default.yaml
│   │   └── ...
│   └── config.yaml             # Global config: label taxonomy, branch naming, permissions
└── src/                        # Framework source code (TBD)
```
