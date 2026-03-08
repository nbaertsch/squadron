# Epic Issues

An **Epic Issue** is Squadron's canonical planning artifact for long-horizon work. It is a structured GitHub Issue that serves as the single source of truth for a large initiative — containing the goal, Definition of Done, Work Breakdown Structure (WBS), wave plan, and current state.

## When to Use an Epic Issue

Use an Epic Issue when:

- The work spans **multiple sub-tasks** that will be handled by different agents or in different waves
- The initiative requires **planning before execution** (WBS, dependency ordering, wave sequencing)
- You need a **single source of truth** that both humans and agents can reference for status and context

Do **not** use an Epic Issue for:

- Single-task bugs or features that one agent can complete in one pass
- Questions, discussions, or proposals (use regular issues with appropriate labels)

## Creating an Epic Issue

Use the **Epic Issue** template when creating a new issue on GitHub. The template is located at `.github/ISSUE_TEMPLATE/epic.md` and pre-applies the `epic` and `planning` labels.

## Schema

Every Epic Issue must contain these sections:

### Goal

One paragraph describing what this epic achieves and why it matters. This is the north star for all agents working on sub-tasks.

### Definition of Done (DoD)

A checklist of criteria that must **all** be met for the epic to transition to `DONE`. Each criterion should be objectively verifiable.

### Work Breakdown Structure (WBS)

A table tracking every sub-task:

| Column | Description | Valid Values |
|--------|-------------|-------------|
| **#** | Sequential row number | Integer |
| **Sub-Task** | Brief description of the work | Free text |
| **Wave** | Which wave this task belongs to | Integer (1, 2, 3, ...) |
| **Effort** | Estimated effort | `XS`, `S`, `M`, `L`, `XL` |
| **Agent Role** | Which agent role will execute this | Any registered role (e.g., `feat-dev`, `docs-dev`) |
| **Issue** | Link to the sub-task issue | `#NNN` or blank if not yet created |
| **Status** | Current status of this sub-task | `Backlog`, `In Progress`, `Done`, `Blocked`, `Cancelled` |
| **Dependencies** | Which WBS rows must complete first | Comma-separated row numbers (e.g., `#1, #2`) or `—` |

#### Effort Estimates

| Value | Meaning |
|-------|---------|
| **XS** | Trivial — a few lines, config change, or label update |
| **S** | Small — single file, well-scoped change |
| **M** | Medium — multiple files, moderate complexity |
| **L** | Large — significant feature, multiple components |
| **XL** | Extra large — cross-cutting change, architectural impact |

### Wave Plan

Waves are ordered phases of execution. Each wave lists its scope and exit criteria. A wave is **blocked** until all prior waves are complete.

- **Wave 1** tasks have no cross-wave dependencies and can start immediately.
- **Wave 2+** tasks depend on prior wave completion.

Within a wave, tasks may run in parallel unless the Dependencies column indicates otherwise.

### GitHub Project Board

A link to the GitHub Projects V2 board tracking this epic, or `TBD` if the board has not yet been created.

### State

The current lifecycle state of the epic (see State Machine below).

### Notes & Decisions

A running log of key decisions, blockers encountered, and context that future agents may need. Entries should be date-stamped.

## State Machine

An Epic Issue transitions through these states:

```
PLANNING ──→ IN_PROGRESS ──→ DONE
                  │
                  ↓
               BLOCKED ──→ IN_PROGRESS
                  │
                  ↓
              CANCELLED

PLANNING ──→ CANCELLED
```

### State Definitions

| State | Description | Entry Condition | Exit Condition |
|-------|-------------|----------------|----------------|
| **PLANNING** | PM agent is drafting or refining the WBS. No dev work has started. | Epic issue created (default state) | WBS is complete and at least one sub-task issue exists |
| **IN_PROGRESS** | One or more waves are actively being executed by dev agents. | WBS finalized, first sub-task assigned | All DoD criteria met, or a blocker is identified |
| **BLOCKED** | A blocker prevents progress on the current wave. | Blocker identified during execution | Blocker resolved |
| **DONE** | All Definition of Done criteria are met and all WBS items are complete. | All DoD checkboxes checked | Terminal state |
| **CANCELLED** | Epic abandoned. Reason must be logged in Notes & Decisions. | Human decision to cancel | Terminal state |

### Valid Transitions

| From | To | Trigger |
|------|-----|---------|
| PLANNING | IN_PROGRESS | WBS finalized, first wave started |
| PLANNING | CANCELLED | Human cancels before execution begins |
| IN_PROGRESS | BLOCKED | Blocker identified |
| IN_PROGRESS | DONE | All DoD criteria met |
| IN_PROGRESS | CANCELLED | Human cancels during execution |
| BLOCKED | IN_PROGRESS | Blocker resolved |
| BLOCKED | CANCELLED | Human cancels while blocked |

### Who Triggers Transitions

- **PLANNING → IN_PROGRESS**: `pm-project` agent (future) or human
- **IN_PROGRESS → BLOCKED**: Any agent that detects a blocker, or human
- **BLOCKED → IN_PROGRESS**: Agent or human that resolves the blocker
- **→ DONE**: `pm-project` agent (future) after verifying all DoD criteria, or human
- **→ CANCELLED**: Human only (cancellation requires human decision)

## Labels

| Label | Color | Purpose |
|-------|-------|---------|
| `epic` | Purple (`#6f42c1`) | Identifies the issue as an Epic Issue. Applied automatically by the template. |
| `planning` | Light blue (`#bfd4f2`) | Indicates the epic is in PLANNING state. Applied automatically by the template; removed when execution begins. |

## Relationship to Other Artifacts

- **GitHub Projects V2 Board**: Each epic should have a linked Projects board for Kanban-style tracking of its sub-tasks. The board link goes in the "GitHub Project Board" section.
- **Sub-task Issues**: Each row in the WBS should have a corresponding GitHub Issue (linked in the Issue column). Sub-tasks are regular issues — they do not use the Epic template.
- **Parent Epic (#151)**: Epics can reference a parent epic in their body for hierarchical organization.

## Agent Behavior

- The **PM agent** should recognize the `epic` label during triage and skip normal classification — epic issues are already classified.
- The **`pm-project` agent** (Issue #155, future) will be responsible for creating, updating, and managing Epic Issues throughout their lifecycle.
- Dev agents working on sub-tasks should reference the parent Epic Issue for context about the broader initiative.
