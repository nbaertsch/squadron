# Research: Approval Flow Schema (OR-005)

Concrete YAML schema for configurable approval flows in `.squadron/workflows/`.

---

## Design Goals

1. **Declarative** — approval requirements expressed as data, not code
2. **Composable** — branch-specific flows override defaults via layering
3. **GitHub-mappable** — every config option maps to a GitHub API capability
4. **Human-readable** — a developer should understand the rules by reading the YAML

---

## Schema Definition

### Complete Example

```yaml
# .squadron/workflows/approval-flows.yaml

# Default approval flow — applies to all PRs unless overridden
default:
  required_reviews:
    - role: agent:pr-review
      required: true
      auto_assign: true
      
  merge_policy:
    auto_merge: false
    require_ci_pass: true
    require_all_reviews: true
    delete_branch: true
    merge_method: squash          # squash | merge | rebase

  escalation:
    on_ci_failure:
      notify: [human_group:maintainers]
      action: comment             # comment | label | assign
      max_retries: 2              # agent retries before escalating
    on_review_rejection:
      notify: [agent:pm]
      action: comment
    on_timeout:
      timeout_hours: 48
      notify: [human_group:maintainers]
      action: label
      label: needs-attention

# Branch-specific overrides — merged with default (more restrictive wins)
branch_rules:
  - match: main
    required_reviews:
      - role: agent:security-review
        required: true
        auto_assign: true
      - role: agent:pr-review
        required: true
        auto_assign: true
      - role: human_group:maintainers
        required: true
        auto_assign: false
        min_approvals: 1
        
    merge_policy:
      auto_merge: false           # human must click merge
      require_ci_pass: true
      require_all_reviews: true
      delete_branch: true
      merge_method: squash
      
    # Status checks that must pass (maps to GitHub required_status_checks)
    required_status_checks:
      - context: squadron/security-review
        description: "Security review agent must approve"
      - context: squadron/pr-review
        description: "PR review agent must approve"
      - context: ci/tests
        description: "CI test suite must pass"

    # Files/paths that require additional approval
    protected_paths:
      - pattern: ".squadron/**"
        additional_review: human_group:maintainers
        reason: "Changes to Squadron config require human approval"
      - pattern: "*.lock"
        additional_review: agent:security-review
        reason: "Dependency changes require security review"
      - pattern: ".github/workflows/**"
        additional_review: human_group:maintainers
        reason: "CI/CD changes require human approval"

  - match: "feat/*"
    required_reviews:
      - role: agent:pr-review
        required: true
        auto_assign: true

    merge_policy:
      auto_merge: true            # auto-merge after approval + CI
      require_ci_pass: true
      require_all_reviews: true
      delete_branch: true
      merge_method: squash

  - match: "fix/*"
    required_reviews:
      - role: agent:pr-review
        required: true
        auto_assign: true

    merge_policy:
      auto_merge: true
      require_ci_pass: true
      require_all_reviews: true
      delete_branch: true
      merge_method: squash

  - match: "hotfix/*"
    required_reviews:
      - role: human_group:maintainers
        required: true
        auto_assign: false
        min_approvals: 1

    merge_policy:
      auto_merge: false
      require_ci_pass: true
      require_all_reviews: true
      delete_branch: true
      merge_method: merge         # preserve commit history for hotfixes
```

---

## Schema Reference

### `required_reviews[]`

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | string | yes | Reviewer identity. Prefixed with `agent:` for agent roles or `human_group:` for human teams. |
| `required` | bool | yes | Whether this review is mandatory for merge. |
| `auto_assign` | bool | no | Whether Squadron automatically requests this review when PR opens. Default: `false`. |
| `min_approvals` | int | no | Number of approvals needed from this group. Default: `1`. Only meaningful for `human_group:` roles. |

**Role format:**
- `agent:security-review` — maps to an agent definition in `.squadron/agents/security-review.md`
- `agent:pr-review` — maps to `.squadron/agents/pr-review.md`
- `human_group:maintainers` — maps to a team defined in `.squadron/config.yaml`

### `merge_policy`

| Field | Type | Required | Description |
|---|---|---|---|
| `auto_merge` | bool | yes | If `true`, Squadron merges the PR automatically once all conditions are met. If `false`, a human must click merge. |
| `require_ci_pass` | bool | no | Require CI status checks to pass. Default: `true`. |
| `require_all_reviews` | bool | no | All `required: true` reviews must approve. Default: `true`. |
| `delete_branch` | bool | no | Delete branch after merge. Default: `true`. |
| `merge_method` | enum | no | `squash`, `merge`, or `rebase`. Default: `squash`. |

### `required_status_checks[]`

| Field | Type | Required | Description |
|---|---|---|---|
| `context` | string | yes | The status check context name (e.g., `squadron/security-review`, `ci/tests`). |
| `description` | string | no | Human-readable description of what this check verifies. |

**Mapping to GitHub:** These map directly to `required_status_checks.contexts` in the branch protection API. The Squadron App posts these status checks via `POST /repos/{owner}/{repo}/statuses/{sha}`.

### `protected_paths[]`

| Field | Type | Required | Description |
|---|---|---|---|
| `pattern` | string | yes | Glob pattern matching file paths. |
| `additional_review` | string | yes | Additional reviewer required for PRs touching these files. |
| `reason` | string | no | Why this path needs extra protection. |

**Implementation:** When a PR is opened, Squadron inspects the changed files. If any match a `protected_paths` pattern, the additional reviewer is requested.

### `escalation`

| Field | Type | Required | Description |
|---|---|---|---|
| `on_ci_failure` | object | no | What to do when CI fails. |
| `on_review_rejection` | object | no | What to do when a review is rejected. |
| `on_timeout` | object | no | What to do when a PR sits too long without action. |

Each escalation rule supports:

| Field | Type | Description |
|---|---|---|
| `notify` | string[] | Who to notify — agent or human group references. |
| `action` | enum | `comment` (post PR comment), `label` (add label), `assign` (assign to reviewer). |
| `label` | string | Label to apply (for `action: label`). |
| `max_retries` | int | For CI failures — how many times agent retries before escalating. |
| `timeout_hours` | int | For timeouts — hours before escalation triggers. |

---

## GitHub API Mapping

### How Squadron Enforces Approval Flows

Squadron's approval flow maps to GitHub mechanisms at two levels:

#### Level 1: GitHub Branch Protection (Baseline Guardrails)

Squadron configures branch protection rules via the API on install/config change:

```
PUT /repos/{owner}/{repo}/branches/{branch}/protection
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["squadron/security-review", "squadron/pr-review", "ci/tests"]
  },
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "enforce_admins": true,
  "restrictions": {
    "apps": ["squadron"]
  }
}
```

This ensures that **even if Squadron's framework-level enforcement fails**, GitHub itself blocks the merge.

#### Level 2: Framework-Level Orchestration (Richer Logic)

Squadron's framework handles the richer logic that branch protection can't express:
- **Path-based additional reviews** — GitHub CODEOWNERS can do this, but it maps to users/teams, not agent roles
- **Auto-merge orchestration** — after all checks pass, call `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge`
- **Escalation timers** — framework tracks PR creation time and fires escalation at timeout
- **CI retry logic** — re-trigger CI on failure before escalating

### Status Check Lifecycle

When an agent reviews a PR, Squadron posts status checks:

```
POST /repos/{owner}/{repo}/statuses/{sha}
{
  "state": "pending",
  "context": "squadron/security-review",
  "description": "Security review in progress..."
}
```

After the agent completes its review:

```
POST /repos/{owner}/{repo}/statuses/{sha}
{
  "state": "success",           // or "failure"
  "context": "squadron/security-review",
  "description": "Security review approved"
}
```

Branch protection requires these status checks to pass before merge.

---

## Flow Resolution Algorithm

When a PR is opened/updated, Squadron determines the applicable approval flow:

```python
def resolve_approval_flow(pr: PullRequest, config: ApprovalConfig) -> ApprovalFlow:
    # Start with default
    flow = deepcopy(config.default)
    
    # Find matching branch rule (first match wins)
    for rule in config.branch_rules:
        if fnmatch(pr.base_branch, rule.match):
            # Merge: branch rule overrides default
            flow = merge_flows(flow, rule)
            break
    
    # Check protected paths
    changed_files = github.get_pr_files(pr.number)
    for path_rule in flow.protected_paths:
        if any(fnmatch(f.filename, path_rule.pattern) for f in changed_files):
            flow.required_reviews.append(ReviewRequirement(
                role=path_rule.additional_review,
                required=True,
                auto_assign=True,
                reason=path_rule.reason,
            ))
    
    return flow
```

---

## Privileged Actions Requiring Approval

Actions that the framework considers "privileged" — requiring explicit approval configuration:

| Action | Default | Configurable? |
|---|---|---|
| Merge to `main` | Human must click merge | Yes — `auto_merge: true` for brave teams |
| Merge to feature branch | Auto-merge after CI + review | Yes |
| Delete branch | After merge | Yes — `delete_branch` |
| Close issue as resolved | Agent can close | No — always allowed |
| Create new issue (blocker) | Agent can create | No — always allowed |
| Modify `.squadron/**` files | Requires human review | Yes — `protected_paths` |
| Modify CI config | Requires human review | Yes — `protected_paths` |
| Modify dependency files | Requires security review | Yes — `protected_paths` |
| Direct push to protected branch | Never allowed | No — enforced by GitHub branch protection |

---

## Relationship to Other Decisions

- **AD-006** (configurable per-branch approvals) — this schema IS the implementation of AD-006
- **AD-009** (fully configurable approval flows) — this schema IS the implementation of AD-009
- **OR-006** (role enforcement via GitHub) — status checks + branch protection provide the enforcement mechanism
- **AD-012** (GitHub App) — the App posts status checks and calls merge API
- **AD-013** (Agent Registry) — the registry tracks which agents need to review each PR

---

## Open Questions (Minor)

1. **Workflow inheritance:** Should branch rules support an `inherits:` field for DRY configuration across similar branches?
2. **Dynamic rules:** Can approval requirements be modified per-issue (e.g., PM decides a particular feature needs extra review)?
3. **CODEOWNERS interaction:** Should Squadron generate a `CODEOWNERS` file from the approval config, or treat them as independent?
