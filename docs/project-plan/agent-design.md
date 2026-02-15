# Agent Design

## Overview

Agents are autonomous LLM-powered actors that participate in the software development lifecycle through GitHub's native interfaces. Each agent has a defined role, a set of tools, and operates within the constraints of its workflow configuration.

---

## Agent Roles (Initial Set)

### PM Agent (Singleton)

The PM agent is the central coordinator. It is **event-driven** — it activates on:
- New issue creation
- Issue state changes (closed, labeled, assigned)
- Agent @-pings in issue comments

**Responsibilities:**
- Triage new issues (determine type: feature, bug, security, etc.)
- Tag issues with appropriate labels (from configurable label taxonomy)
- Assign issues to the appropriate agent (or human group) based on workflow decision trees
- Track blockers and dependencies between issues (cross-references)
- Detect blocker resolution and notify waiting agents
- Escalate to humans when workflows require it
- Manage project board state (V2)

**Key design constraint:** The PM agent processes issues sequentially (AD-008). It does NOT run multiple triage operations concurrently.

**Lifecycle:** The PM agent uses **fresh sessions per event batch** (AD-017). It does not maintain persistent conversation history — its "memory" is GitHub itself (issue state, labels, cross-references, agent registry). Context is injected at the start of each session.

---

### Dev Agent (Feature) — `feat-dev`

Created per-issue when the PM assigns a feature issue.

**Responsibilities:**
- Read and understand the issue requirements
- Create a feature branch (`feat/issue-{N}`)
- Implement the feature
- Write unit tests
- Run tests locally (in container)
- If blocked (e.g., by a bug discovered during development):
  - Create a new blocking issue with cross-reference
  - Update the original issue citing the blocker
  - Serialize context and wait for blocker resolution
- When unblocked: rehydrate, assess branch state, complete remaining work
- Open a Pull Request when implementation is complete
- Respond to PR review feedback

---

### Dev Agent (Bug Fix) — `bug-fix`

Created per-issue when the PM assigns a bug report issue.

**Responsibilities:**
- Read and understand the bug report
- Create a fix branch (`fix/issue-{N}`)
- Reproduce the bug (if possible)
- Implement the fix
- Write a regression test
- Run tests
- Open a Pull Request
- Respond to PR review feedback

---

### Security Review Agent — `security-review`

Invoked as part of an approval flow (not necessarily per-issue).

**Responsibilities:**
- Review code changes in a PR for security vulnerabilities
- Check for common security anti-patterns (injection, auth bypass, secrets in code, etc.)
- Post findings as PR review comments
- Approve or request changes on the PR

---

### PR Review Agent — `pr-review`

Invoked as part of an approval flow.

**Responsibilities:**
- Review code changes for correctness, style, and best practices
- Check test coverage adequacy
- Post review comments
- Approve or request changes
- Merge the PR (if configured to do so and all required approvals are present)

---

### Future Roles (Not Yet Designed)

- **Architecture Review Agent** — reviews structural/design decisions
- **Test Coverage Agent** — dedicated to writing/improving test suites
- **Documentation Agent** — generates/updates docs from code changes
- **Dependency Update Agent** — monitors and updates dependencies
- **DevOps/Deployment Agent** — manages deployment pipelines

---

## Agent Definition Format

Agent definitions live in `.squadron/agents/` as Markdown files. Each file defines the agent's system prompt, tools, sub-agents, constraints, and (for stateful agents) a wake protocol. See the actual agent definitions in `.squadron/agents/` for complete examples. The template below shows the general structure:

```markdown
# Agent: feat-dev

## System Prompt

You are a feature development agent working on the {project_name} project.
Your task is to implement the feature described in the assigned issue.
...

## Tools

- `gh` — GitHub CLI for issue/PR management
- `git` — Version control operations
- `pytest` / `cargo test` / `npm test` — Test runner (project-dependent)
- `grep` / `ripgrep` — Code search
- `read_file` / `write_file` — File operations within the repo

## Sub-Agents

- `code-search`: A lightweight sub-agent for searching the codebase for relevant patterns
- `test-writer`: A sub-agent specialized in generating test cases

## Constraints

- Max iterations before escalation: 5 (AD-018)
- Max tool calls: 200 (AD-018)
- Max active duration: 2 hours (AD-018)
- Must run tests before opening a PR
- Must not merge without required approvals

## Composability (Workflow-Level)

This agent can be invoked by workflows and can invoke other agents
via the PM agent (by creating issues or @-pinging the PM). Direct
agent-to-agent invocation is NOT supported — all inter-agent
communication goes through GitHub's issue/comment system.
```

### Key Distinction: Sub-Agents vs. Composability

| Concept | Scope | Mechanism |
|---|---|---|
| **Sub-agents** | Internal to an agent definition | Defined in the agent's `.md` file. Invoked directly within the agent's LLM session. Share the parent's context. |
| **Composability** | Across agents via workflows | One agent triggers another by creating an issue or @-pinging the PM. The PM's workflow routes the request. Each agent has its own independent context/lifecycle. |

---

## Agent Instantiation

```
Event (issue assigned) 
  → Framework detects assignment
  → Looks up agent role from issue labels / workflow config
  → Loads agent definition from .squadron/agents/{role}.md
  → Creates new CopilotClient instance with:
      - System prompt from agent definition
      - Custom tools via @define_tool (check_for_events, report_blocked, etc.)
      - Issue context (title, body, comments, linked issues)
      - Repo context (relevant files, branch state)
  → Agent begins execution in its git worktree (isolated working directory)
  → On completion or block: session persists automatically, agent reports via issue comments
```

---

## Agent Communication Model

Agents do **not** communicate directly with each other. All inter-agent communication is mediated through GitHub:

1. **Agent → PM**: @-ping in an issue comment, or create a new issue.
2. **PM → Agent**: Assign an issue (triggers agent creation), or comment on the agent's assigned issue.
3. **Agent → Human**: Via the PM's escalation workflow — PM creates a `needs-human` issue and notifies the configured human group.
4. **Human → Agent**: Comment on the agent's assigned issue using @-pings.

This ensures:
- Full audit trail of all communications
- No hidden side-channels between agents  
- Humans can observe and intervene at any point
- The PM agent maintains global awareness of project state

---

## Agent State Machine

```
                    ┌──────────┐
                    │  CREATED  │
                    └─────┬────┘
                          │ (issue assigned)
                          ▼
                    ┌──────────┐
              ┌────►│  ACTIVE   │◄─────┐
              │     └─────┬────┘      │
              │           │            │
              │     ┌─────┴────┐      │
              │     │ Working  │      │
              │     └─────┬────┘      │
              │           │            │
              │     ┌─────▼────┐      │
              │     │ Blocked? │      │
              │     └──┬───┬───┘      │
              │   No   │   │ Yes      │
              │        │   ▼          │
              │        │ ┌────────┐   │
              │        │ │SLEEPING│   │
              │        │ └───┬────┘   │
              │        │     │        │
              │        │     │ (blocker resolved)
              │        │     └────────┘
              │        │
              │        ▼
              │  ┌───────────┐
              │  │ Open PR   │
              │  └─────┬─────┘
              │        │
              │  ┌─────▼─────┐
              │  │ PR Review  │
              │  └─────┬─────┘
              │        │
              │   ┌────┴────┐
              │   │Changes? │
              │   └──┬───┬──┘
              │  No  │   │ Yes
              │      │   └──────┘ (address feedback, loop)
              │      ▼
              │ ┌──────────┐
              │ │ COMPLETED│
              │ └──────────┘
              │
              │  (error / max retries)
              │      ▼
              │ ┌──────────┐
              └─┤ESCALATED │
                └──────────┘
```

States:
- **CREATED**: Agent instance initialized, loading context.
- **ACTIVE**: Agent is executing its task.
- **SLEEPING**: Agent is blocked on a dependency. Context serialized to storage.
- **COMPLETED**: Issue closed, PR merged (or determined no code change needed).
- **ESCALATED**: Agent exceeded retry limits or encountered an unrecoverable error. Human intervention requested.
