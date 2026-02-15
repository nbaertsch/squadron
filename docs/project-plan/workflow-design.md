# Workflow Design

## Overview

Workflows define the **decision trees and approval flows** that govern how issues are processed, how agents are assigned, and what conditions must be met before code is merged. Workflows are **separate from agent definitions** — agents define *what* an actor can do; workflows define *when and how* actors are orchestrated.

Workflows live in `.squadron/workflows/`.

---

## Key Concepts

### Workflows vs. Agents

| Concept | Where it lives | What it defines |
|---|---|---|
| **Agent** | `.squadron/agents/{role}.md` | System prompt, tools, sub-agents, constraints for a single role |
| **Workflow** | `.squadron/workflows/{name}.yaml` | Decision trees, approval flows, branch rules, event triggers |
| **Config** | `.squadron/config.yaml` | Global settings: label taxonomy, branch naming conventions, human groups |

### Workflow Triggers

Every workflow starts with an **event**. Events originate from GitHub:
- Issue created
- Issue assigned
- Issue closed
- Issue comment (with @-ping)
- PR opened
- PR review submitted
- Status check completed

### Workflow Scope

Workflows are primarily **PM-centric** — the PM agent is the entity that evaluates workflow decision trees. Other agents participate in workflows (as assignees, reviewers, etc.) but don't independently evaluate workflow logic.

---

## Workflow Examples

### Workflow 1: New Issue Triage

**Trigger:** `issues.opened`  
**Actor:** PM Agent

```yaml
name: new-issue-triage
trigger: issues.opened
description: PM triages a new issue and assigns it to the appropriate agent or human group.

decision_tree:
  - analyze: issue.title + issue.body
  - classify:
      feature_request:
        labels: [feature, needs-triage]
        assign_to: agent:feat-dev
      bug_report:
        labels: [bug, needs-triage]
        assign_to: agent:bug-fix
      security_vulnerability:
        labels: [security, critical]
        assign_to: human_group:security-team
      documentation:
        labels: [docs]
        assign_to: agent:docs-writer   # future role
      unclear:
        labels: [needs-clarification]
        actions:
          - comment: "Could not determine issue type. Requesting human clarification."
          - assign_to: human_group:maintainers
```

**Flow:**
1. New issue is created (by human or agent).
2. PM agent activates on the `issues.opened` event.
3. PM reads the issue title and body.
4. PM classifies the issue using its LLM reasoning + the decision tree hints.
5. PM applies labels and assigns to the determined target.
6. Assignment triggers agent instantiation (if assigned to an agent role).

---

### Workflow 2: Agent Escalation (Agent @-pings PM)

**Trigger:** `issue_comment.created` where comment contains `@squadron-pm`  
**Actor:** PM Agent

```yaml
name: agent-escalation
trigger: issue_comment.created
condition: comment.body contains "@squadron-pm" AND comment.author is agent
description: An agent is stuck and requests PM intervention.

decision_tree:
  - analyze: comment.body (the agent's request)
  - classify:
      merge_conflict:
        actions:
          - create_issue:
              title: "Merge conflict resolution needed for issue #{original_issue}"
              labels: [needs-human, merge-conflict]
              body: "{agent's description of the conflict}"
              references: [original_issue]
          - assign_to: human_group:maintainers
          - comment_on_original: "Escalated merge conflict to human team. Waiting for resolution."
      
      blocker_bug:
        actions:
          - create_issue:
              title: "{extracted bug summary}"
              labels: [bug, blocker]
              body: "{agent's bug description}"
              references: [original_issue]
          - comment_on_original: "Created blocking bug report #{new_issue}. Waiting for resolution."
          # PM will pick up the new issue via new-issue-triage workflow
      
      needs_clarification:
        actions:
          - add_label: needs-clarification
          - assign_to: human_group:maintainers
          - comment: "Agent requests clarification from a human. Details: {agent's question}"
      
      unknown:
        actions:
          - add_label: needs-human
          - assign_to: human_group:maintainers
          - comment: "Agent encountered an unknown issue. Human review requested."
```

**Flow:**
1. Dev agent working on issue #38 encounters a problem it can't solve.
2. Dev agent comments on issue #38: `@squadron-pm I'm unable to resolve merge conflicts in src/auth.py. The upstream changes conflict with my feature branch. Requesting human assistance.`
3. PM agent activates on the comment event.
4. PM reads the comment, classifies it as `merge_conflict`.
5. PM creates a new issue #45: "Merge conflict resolution needed for issue #38", labeled `needs-human, merge-conflict`, referencing issue #38.
6. PM assigns issue #45 to the `maintainers` human group.
7. PM comments on issue #38: "Escalated merge conflict to human team (see #45). Waiting for resolution."
8. Dev agent serializes its context and enters SLEEPING state.
9. A human picks up issue #45, comments to teammates, resolves the conflict.
10. Human closes issue #45.
11. PM detects issue #45 closure, sees it references issue #38.
12. PM comments on issue #38: "Blocker #45 resolved. @squadron-dev-1 you may continue."
13. Dev agent rehydrates from checkpoint, assesses current branch state, continues work.

---

### Workflow 3: PR Approval Flow (Branch-Specific)

**Trigger:** `pull_request.opened`  
**Actor:** Framework (orchestrates review assignments)

```yaml
name: pr-approval-main
trigger: pull_request.opened
condition: pull_request.base == "main"
description: Approval flow for PRs targeting the main branch.

required_approvals:
  - role: agent:security-review
    required: true
    auto_assign: true
  - role: agent:pr-review
    required: true
    auto_assign: true
  - role: human_group:maintainers
    required: true
    auto_assign: false   # humans self-assign from the group
    
merge_policy:
  auto_merge: false       # human must click merge
  require_ci_pass: true
  require_all_approvals: true
  delete_branch_on_merge: true
```

```yaml
name: pr-approval-feature
trigger: pull_request.opened
condition: pull_request.base matches "feat/*" OR "fix/*"
description: Approval flow for PRs targeting feature/fix branches.

required_approvals:
  - role: agent:pr-review
    required: true
    auto_assign: true

merge_policy:
  auto_merge: true        # merge automatically once approved + CI passes
  require_ci_pass: true
  require_all_approvals: true
  delete_branch_on_merge: true
```

---

## Human-Agent Collaboration on Issues

When a workflow routes to a human (or a human takes over), the issue comment thread becomes the collaboration channel:

```
Issue #45: Merge conflict resolution needed

  [squadron:pm] Created this issue from escalation by @squadron-dev-1 on #38.
                Conflict in src/auth.py between feat/issue-38 and main.
                Assigned to @maintainers.

  [human:alice] I'll take this one. @bob heads up, this touches the auth module.

  [human:bob]   Thanks for the ping. The conflict is because I refactored the
                session handler yesterday. The agent's changes should use the
                new SessionManager interface instead.

  [human:alice] Fixed. Resolved the conflict by updating the agent's code to use
                SessionManager. @squadron-dev-1 the merge is done, your branch
                is updated.

  [human:alice] Closing this issue.

  → PM detects closure → notifies Dev-1 on #38 → Dev-1 rehydrates
```

No special tooling needed — standard GitHub issue comments with @-mentions.

---

## Workflow Configuration Schema (Draft)

> **Note:** This is an early draft. The canonical config reference is now [Config Schema](research/config-schema.md), which defines the complete `.squadron/config.yaml` structure including settings shown below (label taxonomy, branch naming, human groups, agent roles) plus circuit breakers, runtime config, and escalation settings.

```yaml
# .squadron/workflows/default.yaml

# Label taxonomy used for issue classification
label_taxonomy:
  types: [feature, bug, security, docs, infrastructure]
  priorities: [critical, high, medium, low]
  states: [needs-triage, in-progress, blocked, needs-human, needs-clarification]

# Branch naming conventions
branch_naming:
  feature: "feat/issue-{issue_number}"
  bugfix: "fix/issue-{issue_number}"
  security: "security/issue-{issue_number}"
  hotfix: "hotfix/issue-{issue_number}"

# Human groups (GitHub teams or user lists)
human_groups:
  maintainers: ["@alice", "@bob"]
  security-team: ["@charlie", "@dave"]

# Agent role → assignment mapping
agent_roles:
  feat-dev:
    agent_definition: agents/feat-dev.md
    assignable_labels: [feature]
  bug-fix:
    agent_definition: agents/bug-fix.md
    assignable_labels: [bug]
  security-review:
    agent_definition: agents/security-review.md
    assignable_labels: [security]
  pr-review:
    agent_definition: agents/pr-review.md
```

---

## Open Design Questions

- ~~What is the exact event-routing mechanism from GitHub event → PM activation → workflow evaluation?~~ **Resolved:** GitHub App receives webhooks → Squadron server → Event Router → PM agent. See [AD-012](architecture-decisions.md#AD-012) and [OR-002](open-research.md#OR-002).
- How are workflows validated? Can we lint them for correctness (e.g., no unreachable branches, no undefined agent roles)? **Deferred to V2** (Roadmap V2 #10).
- Should workflows support conditional logic beyond simple classification (e.g., "if issue has more than 3 blockers, escalate to human")? **Deferred to V2** — V1 uses LLM reasoning for complex conditions rather than declarative rules.
- ~~How do we version workflows? If a workflow changes mid-flight (an issue was triaged under the old workflow), does the in-progress work follow the old or new workflow?~~ **Deferred to V2:** Live reload for V1, version pinning considered for V2. See [OR-010](open-research.md#OR-010).
