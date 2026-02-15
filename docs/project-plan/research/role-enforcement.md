# Research: Role Enforcement via GitHub (OR-006)

Can GitHub's branch protection mechanisms natively enforce agent role constraints, or must all enforcement be framework-level?

---

## Answer: Dual-Layer Enforcement

Role enforcement uses **two complementary layers**:

| Layer | Mechanism | What It Enforces | Bypass Risk |
|---|---|---|---|
| **GitHub-native** | Branch protection + required status checks | Merge gates — no PR merges without required checks passing | None (enforced by GitHub) |
| **Framework-level** | Role → allowed-actions mapping in Event Router | Action permissions — which agents can perform which operations | Could be bypassed if framework has bugs |

Neither layer alone is sufficient. Combined, they provide defense-in-depth.

---

## GitHub-Native Enforcement

### Required Status Checks as Role Gates

**This is the primary mechanism.** The Squadron App posts role-specific status checks per PR, and branch protection requires them to pass before merge.

#### How It Works

**1. Agent performs review → Squadron posts status check:**

```
POST /repos/{owner}/{repo}/statuses/{sha}
{
  "state": "success",
  "context": "squadron/security-review",
  "description": "Security review approved — no vulnerabilities found",
  "target_url": "https://squadron.dev/reviews/{review_id}"
}
```

**2. Branch protection requires the check:**

```
PUT /repos/{owner}/{repo}/branches/main/protection
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "squadron/security-review",
      "squadron/pr-review",
      "ci/tests"
    ]
  }
}
```

**3. Result:** GitHub blocks merge until ALL required status checks report `success`. No code path in Squadron (or anywhere else) can bypass this — it's enforced by GitHub itself.

#### Status Check Naming Convention

```
squadron/{agent-role}          → Per-role review status
squadron/approval-flow         → Overall approval flow check
ci/tests                       → CI pipeline (not Squadron-controlled)
```

Examples:
- `squadron/security-review` — Security agent approved
- `squadron/pr-review` — PR review agent approved  
- `squadron/approval-flow` — All required approvals are in (meta-check)

#### Key Insight: Status Checks Per-App

When a status check is set by a GitHub App, GitHub tracks which App set it. The `required_status_checks.checks[]` field can specify `app_id` to require the check comes from a specific app:

```json
{
  "required_status_checks": {
    "checks": [
      { "context": "squadron/security-review", "app_id": 12345 }
    ]
  }
}
```

This means **only the Squadron App can satisfy its own required checks** — a human can't manually set `squadron/security-review: success` to bypass the flow.

### Required Pull Request Reviews

GitHub can require N approving reviews before merge:

```json
{
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_last_push_approval": true
  }
}
```

**Limitation for Squadron:** GitHub counts reviews by user/app identity, not by "role." Since all Squadron agents operate under `squadron[bot]` (single App identity), GitHub sees one reviewer regardless of how many agent roles reviewed the PR.

**Workaround:** Use status checks (above) as the primary role gate, and required reviews for human approvals:

| Role Type | Enforcement Mechanism |
|---|---|
| Agent roles | Required status checks (`squadron/{role}`) |
| Human approvals | Required pull request reviews (`required_approving_review_count`) |

### CODEOWNERS Integration

**Question:** Can CODEOWNERS require reviews from `squadron[bot]`?

**Answer: Partially.** CODEOWNERS specifies review requirements by GitHub user/team. You can add the App's bot account:

```
# CODEOWNERS
*.py    @squadron[bot]
```

**However, this has limitations:**
- GitHub treats `@squadron[bot]` as a single reviewer — it can't distinguish between roles
- CODEOWNERS + `require_code_owner_reviews` forces a review from the App but not from a *specific agent role*
- Status checks are strictly more powerful for role-based enforcement

**Recommendation:** Do NOT use CODEOWNERS for agent role enforcement. Use it only in the traditional way — for human code owners. Use status checks for agent roles.

### Rulesets (Modern Alternative to Branch Protection)

GitHub Rulesets are the newer, more flexible alternative to branch protection rules:

**Advantages over branch protection:**
- Multiple rulesets can apply simultaneously (layered — most restrictive wins)
- Rulesets have statuses (active, disabled, evaluate) — can test rules without blocking
- Can target branches via `fnmatch` patterns (e.g., `releases/**/*`)
- Organization-level rulesets for GitHub Team/Enterprise plans
- Better audit trail — anyone with read access can view active rulesets

**Recommendation for Squadron:** Support both branch protection (broader compatibility) and rulesets (better UX for GitHub Team/Enterprise). V1 uses branch protection API. V2 can add rulesets support.

---

## Framework-Level Enforcement

### Why Framework Enforcement Is Still Needed

GitHub's branch protection only gates **merge**. It does NOT control:

1. **Which agent posts which status check** — Framework must ensure only the security-review agent posts `squadron/security-review`
2. **Which agent can push to which branch** — All agents use the same App token
3. **Which agent can comment on which issue** — Framework-enforced routing
4. **Which agent can create issues** — Framework policy
5. **Action rate limiting per role** — Not a GitHub concept

### Role → Action Permission Matrix

Enforced by the Event Router before dispatching actions:

```python
ROLE_PERMISSIONS = {
    "pm": {
        "allowed": [
            "issues.create", "issues.label", "issues.assign", "issues.comment",
            "issues.close", "issues.reopen",
        ],
        "denied": [
            "contents.push", "pulls.merge", "pulls.create",
            "statuses.create",
        ],
    },
    "feat-dev": {
        "allowed": [
            "contents.push",          # to own branch only
            "pulls.create",           # from own branch only
            "issues.comment",         # on own issue only
        ],
        "denied": [
            "pulls.merge",            # never — only framework merges
            "issues.label",           # PM only
            "issues.assign",          # PM only
            "statuses.create",        # review agents only
        ],
    },
    "security-review": {
        "allowed": [
            "pulls.review",           # approve/reject
            "statuses.create",        # post squadron/security-review status
            "issues.comment",         # on PRs assigned for review
        ],
        "denied": [
            "contents.push",
            "pulls.merge",
            "issues.create",
        ],
    },
    "pr-review": {
        "allowed": [
            "pulls.review",
            "statuses.create",        # post squadron/pr-review status
            "issues.comment",
        ],
        "denied": [
            "contents.push",
            "pulls.merge",
            "issues.create",
        ],
    },
}
```

### Enforcement Point

Enforcement happens in the GitHub API wrapper that agents use — the framework wraps the raw GitHub API client with permission checks:

```python
class SquadronGitHubClient:
    """Wraps the GitHub API with role-based permission checks."""
    
    def __init__(self, agent_role: str, agent_id: str, installation_token: str):
        self.role = agent_role
        self.agent_id = agent_id
        self.client = GitHubClient(token=installation_token)
        self.permissions = ROLE_PERMISSIONS[agent_role]
    
    async def create_status(self, repo, sha, context, state, description):
        self._check_permission("statuses.create")
        # Also verify the agent is using its own status check context
        expected_context = f"squadron/{self.role}"
        if context != expected_context:
            raise PermissionError(
                f"Agent {self.agent_id} ({self.role}) cannot post status check "
                f"'{context}' — only '{expected_context}' is allowed"
            )
        return await self.client.create_status(repo, sha, context, state, description)
    
    async def push_to_branch(self, repo, branch, ...):
        self._check_permission("contents.push")
        # Verify agent is pushing to its own branch
        agent_record = await registry.get_agent(self.agent_id)
        if branch != agent_record.branch:
            raise PermissionError(
                f"Agent {self.agent_id} can only push to {agent_record.branch}, "
                f"not {branch}"
            )
        return await self.client.push(repo, branch, ...)
    
    def _check_permission(self, action: str):
        if action in self.permissions["denied"]:
            raise PermissionError(
                f"Agent role '{self.role}' is not allowed to perform '{action}'"
            )
```

---

## Decision: Dual-Layer Role Enforcement (AD-014)

### Summary

| Question | Answer |
|---|---|
| Can status checks serve as role-based gates? | **Yes** — primary mechanism for merge enforcement |
| Can CODEOWNERS enforce agent roles? | **No** — single bot identity, can't distinguish roles |
| Can we differentiate roles via commit metadata? | **Partially** — commit messages include `[squadron:{role}]` tags for audit, but not for enforcement |
| Where does enforcement happen? | **Two layers:** GitHub (merge gates) + Framework (action permissions) |

### Architecture

```
Agent Action Request
        │
        ▼
┌─────────────────────┐
│  Framework Layer     │  ← Role → Action permission check
│  (SquadronGitHub-   │  ← Branch ownership verification
│   Client wrapper)    │  ← Status check context validation
└─────────┬───────────┘
          │ (if allowed)
          ▼
┌─────────────────────┐
│  GitHub API          │  ← Token scoped to installed repos
└─────────┬───────────┘
          │ (action applied)
          ▼
┌─────────────────────┐
│  GitHub Branch       │  ← Required status checks must pass
│  Protection          │  ← Required reviews must approve
│                      │  ← Merge restrictions enforced
└─────────────────────┘
```

### Implications

1. **Security model is defense-in-depth** — even if a framework bug allows an unauthorized status check post, GitHub still requires ALL required checks to pass
2. **Agent roles are pure framework concepts** — GitHub sees only `squadron[bot]`
3. **Branch protection is the hard backstop** — it cannot be bypassed by any code path
4. **Status checks are the bridge** — they translate framework-level role approvals into GitHub-enforceable gates

---

## Relationship to Other Decisions

- **AD-002** (single bot identity) — confirmed: role enforcement is framework-level because GitHub sees one identity
- **AD-006** (configurable approvals) — status checks + branch protection provide the enforcement layer
- **AD-009** (fully configurable) — approval flow schema (OR-005) defines WHAT; this document defines HOW it's enforced
- **AD-012** (GitHub App) — App identity locks status checks to the specific App
- **AD-013** (Agent Registry) — registry tracks which agent owns which branch/PR for permission verification
