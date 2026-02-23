# Squadron Agent Roles

This document describes every agent type in the Squadron system — their purpose, triggers, responsibilities, tool sets, and lifecycle.

## Overview

Squadron includes two categories of agents:

| Category | Agents | Purpose |
|----------|--------|---------|
| **Coordination** | `pm` | Issue triage, routing, coordination |
| **Development** | `feat-dev`, `bug-fix`, `docs-dev`, `infra-dev` | Implements changes in code or docs |
| **Review** | `pr-review`, `security-review`, `test-coverage` | Evaluates pull requests |
| **Support** | `code-search`, `test-writer`, `merge-conflict` | Subagents used by other agents |

---

## Label → Agent Spawning

The PM agent applies type labels to issues. When a type label is applied, the framework **automatically spawns** the matching development agent:

| Label | Agent Spawned | Auto-spawn? |
|-------|---------------|-------------|
| `feature` | `feat-dev` | ✅ Yes |
| `bug` | `bug-fix` | ✅ Yes |
| `security` | `security-review` | ✅ Yes |
| `documentation` | `docs-dev` | ✅ Yes |
| `infrastructure` | `infra-dev` | ❌ No — requires `@squadron-dev infra-dev` mention |

> **Note:** The `infrastructure` label does not trigger automatic agent spawning. Coordinate infrastructure work by mentioning `@squadron-dev infra-dev` in an issue comment.

---

## Coordination Agents

### PM — Project Manager

**File:** `.squadron/agents/pm.md`  
**Emoji:** 🎯  
**Lifecycle:** Ephemeral (runs once per event)

**Purpose:** Central coordinator of the multi-agent system. Triages incoming issues, applies labels to trigger agent spawning, checks for duplicate work, and escalates when needed.

**Triggers:**
- New issue opened
- Issue updated or labeled
- `@squadron-dev pm` mention in a comment

**Responsibilities:**
- Classify issues (`feature`, `bug`, `security`, `documentation`, `infrastructure`)
- Set priority labels (`critical`, `high`, `medium`, `low`)
- Check for duplicate issues using `check_registry` and `get_recent_history`
- Detect dependency relationships between issues
- Escalate unclear issues with `needs-clarification` label
- Escalate issues requiring human judgment with `needs-human` label
- Post exactly one triage comment per event

**Does NOT:**
- Write code
- Review PRs
- Assign issues to specific agents (labels trigger spawning automatically)

**Tools:**
```
create_issue, read_issue, update_issue, close_issue, assign_issue, label_issue,
list_issues, list_issue_comments, list_pull_requests,
check_registry, get_recent_history, list_agent_roles,
comment_on_issue
```

**Example triage comment:**
```
🎯 Project Manager

Triage complete

- Type: feature
- Priority: medium
- Assignment: feat-dev agent (auto-spawned via label)
- Dependencies: None detected
- Rationale: Straightforward feature request with clear requirements.
```

---

## Development Agents

### feat-dev — Feature Developer

**File:** `.squadron/agents/feat-dev.md`  
**Emoji:** 👨‍💻  
**Lifecycle:** Persistent (can sleep/wake)

**Purpose:** Implements new features by writing code, tests, and opening pull requests.

**Triggers:** Issues labeled `feature`

**Responsibilities:**
- Read and understand the issue requirements
- Explore the codebase to understand existing patterns
- Implement the feature with appropriate tests
- Open a pull request with a clear description
- Respond to review feedback and address comments
- Clean up the branch after the PR merges

**Workflow:**
1. Read issue → understand requirements
2. Explore codebase → identify where changes go
3. Plan implementation → outline files and tests
4. Create branch → `feat/issue-{N}`
5. Implement → write code and tests
6. Open PR → reference `Fixes #{N}`
7. Respond to review → address feedback, push updates
8. Complete → `report_complete` after merge

**Tools:**
```
read_file, write_file, grep,
bash, git, git_push,
read_issue, list_issue_comments,
open_pr, get_pr_details, get_pr_feedback, list_pr_files,
list_pr_reviews, get_review_details, get_pr_review_status,
reply_to_review_comment, comment_on_pr, comment_on_issue,
check_for_events, report_blocked, report_complete, create_blocker_issue
```

---

### bug-fix — Bug Fix Agent

**File:** `.squadron/agents/bug-fix.md`  
**Emoji:** 🔧  
**Lifecycle:** Persistent (can sleep/wake)

**Purpose:** Diagnoses and fixes bugs by analyzing the problem, writing regression tests first, implementing the fix, and opening a pull request.

**Triggers:** Issues labeled `bug`

**Responsibilities:**
- Reproduce the bug (or confirm it via code analysis)
- Identify the root cause
- Write a failing regression test *before* fixing
- Implement the minimum fix to address the root cause
- Verify all tests pass after the fix
- Open a PR with root cause analysis in the description

**Workflow:**
1. Read issue → understand expected vs. actual behavior
2. Explore codebase → trace the execution path
3. Reproduce → create a test that fails with current code
4. Diagnose → identify the root cause
5. Create branch → `fix/issue-{N}`
6. Write regression test → must fail before fix, pass after
7. Implement fix → minimum necessary change
8. Verify → all tests pass
9. Open PR → title `fix(#{N}): description`
10. Complete → after merge

**Tools:** Same as `feat-dev`

---

### docs-dev — Documentation Developer

**File:** `.squadron/agents/docs-dev.md`  
**Emoji:** 📝  
**Lifecycle:** Persistent (can sleep/wake)

**Purpose:** Writes and updates documentation — READMEs, guides, API docs, inline comments, and architecture decision records.

**Triggers:** Issues labeled `documentation`

**Responsibilities:**
- Review existing documentation for gaps and inaccuracies
- Write clear, accurate documentation following project style
- Cross-reference documentation with actual code
- Open PRs with documentation changes
- Respond to review feedback

**Workflow:**
1. Read issue → understand what docs need updating
2. Explore existing docs and codebase
3. Plan changes → outline files to create or modify
4. Create branch → `docs/issue-{N}`
5. Write documentation → clear, accurate, with examples
6. Verify → links work, code examples are correct
7. Open PR
8. Complete → after merge

**Tools:**
```
read_file, write_file, grep,
bash, git, git_push,
read_issue, list_issue_comments,
open_pr, get_pr_details, get_pr_feedback, list_pr_files,
list_pr_reviews, get_review_details, get_pr_review_status,
reply_to_review_comment, comment_on_pr, comment_on_issue,
check_for_events, report_blocked, report_complete, create_blocker_issue
```

---

### infra-dev — Infrastructure Developer

**File:** `.squadron/agents/infra-dev.md`  
**Emoji:** 🏗️  
**Lifecycle:** Persistent (can sleep/wake)

**Purpose:** Works on CI/CD pipelines, deployment configurations, Dockerfiles, Bicep/IaC templates, and other infrastructure concerns.

**Triggers:** `@squadron-dev infra-dev` mention (NOT auto-spawned by `infrastructure` label)

**Responsibilities:**
- Modify CI/CD workflows (GitHub Actions)
- Update Dockerfiles and container configurations
- Maintain Bicep/Terraform IaC templates
- Update deployment configuration
- Monitor CI status to verify infrastructure changes work

**Workflow:**
1. Read issue → understand infrastructure changes needed
2. Explore infra files → understand current setup
3. Plan changes → consider deployment impact
4. Create branch → `infra/issue-{N}`
5. Implement → modify infra files with care
6. Verify → check CI status after push
7. Open PR
8. Complete → after merge

**Tools:**
```
read_file, write_file, grep,
bash, git, git_push,
read_issue, list_issue_comments,
open_pr, get_pr_details, get_pr_feedback, list_pr_files,
list_pr_reviews, get_review_details, get_pr_review_status,
get_ci_status,
reply_to_review_comment, comment_on_pr, comment_on_issue,
check_for_events, report_blocked, report_complete, create_blocker_issue
```

---

## Review Agents

### pr-review — Pull Request Reviewer

**File:** `.squadron/agents/pr-review.md`  
**Emoji:** 👀  
**Lifecycle:** Persistent (can sleep/wake)

**Purpose:** Reviews code changes for correctness, quality, test coverage, and adherence to project standards. Provides structured review feedback.

**Triggers:** PR opened by a development agent

**Responsibilities:**
- Read the PR description and understand the intent
- Review all changed files for correctness, quality, test coverage
- Post inline comments on specific lines
- Submit a structured review with blocking issues, suggestions, and nits
- Re-review when changes are pushed

**Review criteria:**
- **Correctness:** Does the code do what it claims?
- **Tests:** Are changes adequately tested? Do tests cover edge cases?
- **Code quality:** Clean, readable, follows project conventions?
- **Error handling:** Are errors handled explicitly?
- **Security:** Any obvious security issues?
- **Performance:** Any obvious performance problems?
- **Completeness:** Does the PR fully address the linked issue?

**Tools:**
```
read_file, grep,
list_pr_files, get_pr_details, get_pr_feedback, get_ci_status,
list_pr_reviews, get_review_details, get_pr_review_status, list_requested_reviewers,
add_pr_line_comment, reply_to_review_comment, comment_on_pr, comment_on_issue,
submit_pr_review,
check_for_events, report_complete
```

---

### security-review — Security Reviewer

**File:** `.squadron/agents/security-review.md`  
**Emoji:** 🔒  
**Lifecycle:** Persistent (can sleep/wake)

**Purpose:** Reviews code changes for security vulnerabilities, unsafe patterns, and potential attack vectors. Checks for OWASP Top 10, secrets exposure, dependency risks, and insecure configurations.

**Triggers:**
- PRs labeled `security` or touching security-sensitive files
- `@squadron-dev security-review` mention
- Issues labeled `security` (for security analysis tasks)

**Responsibilities:**
- Analyze for OWASP Top 10 vulnerabilities
- Check for hardcoded secrets or credentials
- Review authentication and authorization logic
- Assess dependency risks
- Provide remediation recommendations
- Delegate implementation to appropriate fix agents

**Does NOT:**
- Implement fixes (analysis and recommendation only)
- Write code

**Tools:**
```
read_file, grep,
list_pr_files, get_pr_details, get_pr_feedback, get_ci_status,
list_pr_reviews, get_review_details, get_pr_review_status, list_requested_reviewers,
add_pr_line_comment, reply_to_review_comment, comment_on_pr, comment_on_issue,
submit_pr_review,
read_issue, list_issue_comments,
check_for_events, report_complete
```

---

### test-coverage — Test Coverage Reviewer

**File:** `.squadron/agents/test-coverage.md`  
**Emoji:** 🧪  
**Lifecycle:** Persistent (can sleep/wake)

**Purpose:** Reviews code changes specifically for test coverage adequacy. Verifies that new code has corresponding tests and that tests cover edge cases.

**Triggers:** PR opened (typically alongside `pr-review`)

**Responsibilities:**
- Map changed source files to their test files
- Verify new functions/methods have test cases
- Check for coverage gaps (missing edge cases, error paths, branches)
- Evaluate test quality (not just presence, but correctness)
- Flag tests that don't actually verify behavior

**Tools:**
```
read_file, grep,
list_pr_files, get_pr_details, get_pr_feedback, get_ci_status,
list_pr_reviews, get_pr_review_status,
add_pr_line_comment, comment_on_pr, comment_on_issue,
submit_pr_review,
check_for_events, report_complete
```

---

## Support Agents (Subagents)

These agents are typically invoked by other agents rather than directly by the framework.

### code-search — Code Search Agent

**File:** `.squadron/agents/code-search.md`  
**Emoji:** 🔍  
**Lifecycle:** Ephemeral

**Purpose:** Searches the codebase to find relevant files, patterns, and implementations. Used as a subagent by `feat-dev` and `bug-fix` to locate code before making changes.

**Tools:** `read_file`, `grep`, `bash`

---

### test-writer — Test Writer Agent

**File:** `.squadron/agents/test-writer.md`  
**Emoji:** ✅  
**Lifecycle:** Ephemeral

**Purpose:** Writes tests for new or existing code. Used as a subagent by `feat-dev` to ensure adequate test coverage for implementations.

**Tools:** `read_file`, `write_file`, `bash`, `grep`

---

### merge-conflict — Merge Conflict Resolver

**File:** `.squadron/agents/merge-conflict.md`  
**Emoji:** 🔀  
**Lifecycle:** Ephemeral

**Purpose:** Resolves git merge conflicts by analyzing conflicting changes, understanding the intent of both sides, and producing a clean merge that preserves all intended functionality.

**Tools:**
```
read_file, write_file, grep,
bash, git, git_push,
get_pr_details, get_pr_feedback, list_pr_files, get_ci_status,
comment_on_issue,
check_for_events, report_blocked, report_complete
```

---

## Agent Collaboration via @ Mentions

Agents can collaborate using the `@squadron-dev {role}` mention system in issue and PR comments. This is how agents request help or delegate tasks across domains.

**Format:** `@squadron-dev {agent-role}`

**Examples:**
```
@squadron-dev security-review Please review the OAuth implementation in src/auth/oauth.py

@squadron-dev docs-dev Please update the API documentation for these new endpoints

@squadron-dev infra-dev This feature needs new environment variables in the deployment config
```

See [Agent Collaboration Guide](../docs/agent-collaboration.md) for detailed patterns.
