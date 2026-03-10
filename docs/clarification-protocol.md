# Clarification-First Protocol

The Clarification-First (Clarity-Before-Commitment) Protocol ensures that the PM agent drives clarity with the issue author **before** committing dev agents to work on large or ambiguous issues.

## Problem

Without this protocol, the PM immediately spawns a dev agent on any classified issue — even if the requirements are vague. This leads to:

- Dev agents building the wrong thing
- Wasted compute on underspecified work
- Issues that bounce between agents without converging

## How It Works

### 1. Ambiguity Detection

During triage (Decision Framework step 2), the PM evaluates whether the issue is clear enough for a dev agent to execute. An issue requires clarification if **any** of the following are true:

| Signal | Example |
|--------|---------|
| Vague or underspecified body | "Build a user dashboard" with no details |
| No acceptance criteria or Definition of Done | Issue says what but not when it's done |
| Scope implies multiple sub-tasks | "Overhaul the authentication system" |
| Title contains scope-expanding words | "system", "platform", "overhaul", "redesign", "epic", "rework" |
| Cannot determine what "done" looks like | Unclear deliverables |

### Issues That Skip Clarification

The protocol does **not** apply to:

- Small, clearly-scoped issues with obvious acceptance criteria
- Bug reports with clear reproduction steps
- Issues with an explicit Definition of Done or acceptance criteria
- Issues created from the Epic Issue template (already structured)

### 2. Clarification Phase

When ambiguity is detected, the PM:

1. Labels the issue `needs-clarification` and `planning`
2. Posts a structured comment with five specific questions:
   - **Goal** — What is the specific end state?
   - **Scope** — What is in/out of scope?
   - **Definition of Done** — How will we know it's complete?
   - **Constraints** — Technology, timeline, integration requirements?
   - **Priority** — What's the most critical outcome if scope must be cut?
3. Sleeps (calls `report_blocked`) to wait for the author's response

### 3. Commitment Gate

When the author responds (PM is woken by `issue_comment.created` event):

1. **Assess sufficiency** — Can the PM now determine a clear scope, type, and Definition of Done?
2. **If sufficient** — Remove `needs-clarification`, proceed to classification and labeling (which triggers dev agent spawning)
3. **If still insufficient** — Post a second round of targeted follow-up questions addressing the specific remaining gaps
4. **After 2 inconclusive rounds** — Escalate by applying `needs-human` label. The PM does not loop indefinitely.

### Flow Diagram

```
New Issue
    │
    ▼
Assess Clarity ──── Clear ──────────────→ Classify & Label (normal triage)
    │
    │ Ambiguous
    ▼
Label: needs-clarification, planning
Post structured questions (Round 1)
Sleep
    │
    ▼
Author Responds
    │
    ▼
Assess Sufficiency ── Sufficient ──────→ Remove needs-clarification
    │                                     Classify & Label
    │ Still unclear
    ▼
Post follow-up questions (Round 2)
Sleep
    │
    ▼
Author Responds
    │
    ▼
Assess Sufficiency ── Sufficient ──────→ Remove needs-clarification
    │                                     Classify & Label
    │ Still unclear
    ▼
Label: needs-human
Escalate to maintainers
```

## Configuration

The PM agent wakes on these events for issues it has triaged:

- `issue_comment.created` — author responds to clarification questions
- `issues.unlabeled` — `needs-clarification` label removed (e.g., by a human)
- `issues.closed` — issue closed while awaiting clarification
- `pull_request.closed` — related PR activity

These are configured in `.squadron/config.yaml` under the `issue-triage` and `issue-reopen-triage` pipelines.

## Labels Used

| Label | Purpose |
|-------|---------|
| `needs-clarification` | Issue is awaiting author response to clarification questions |
| `planning` | Issue is in the planning/scoping phase (not yet ready for dev work) |
| `needs-human` | Escalated after 2 inconclusive clarification rounds |

## Relationship to Other Features

- **Epic Issues (#153)**: After clarification, large issues may be promoted to Epics using the Epic Issue template and `epic` label.
- **`pm-project` Agent (#155)**: The future pm-project agent will manage Epic lifecycle after the PM completes clarification and creates the Epic.
- **Existing Triage**: Small, clear issues are unaffected — they flow through the normal Decision Framework without triggering clarification.
