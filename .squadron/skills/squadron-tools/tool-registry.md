# Tool Registry

All custom Squadron tools are defined in `src/squadron/tools/squadron_tools.py`.
These are registered as SDK tools and called by agents via the LLM function-call mechanism.

## Framework / Agent Lifecycle Tools

| Tool | Purpose | Key Params |
|------|---------|------------|
| `check_for_events` | Poll for new events (PR feedback, messages, blocker resolutions) | none |
| `report_blocked` | Mark agent as sleeping (blocked on another issue) | `blocker_issue: int`, `reason: str` |
| `report_complete` | Mark agent as completed | `summary: str` |
| `create_blocker_issue` | Create a new GitHub issue for a blocker | `title: str`, `body: str`, `labels?: list[str]` |
| `escalate_to_human` | Escalate to human intervention | `reason: str`, `context?: str` |

## PR Tools

| Tool | Purpose | Key Params |
|------|---------|------------|
| `open_pr` | Open a pull request | `title: str`, `body: str`, `head: str`, `base: str` |
| `git_push` | Push current branch | `force?: bool` |
| `submit_pr_review` | Submit a PR review | `pr_number: int`, `review_type: str`, `body: str` |
| `get_pr_details` | Get PR metadata | `pr_number: int` |
| `get_pr_feedback` | Get review comments on a PR | `pr_number: int` |
| `list_pr_files` | List files changed in a PR | `pr_number: int` |
| `list_pr_reviews` | List reviews on a PR | `pr_number: int` |
| `get_review_details` | Get details of a specific review | `pr_number: int`, `review_id: int` |
| `get_pr_review_status` | Get comprehensive review status | `pr_number: int` |
| `list_requested_reviewers` | List requested reviewers | `pr_number: int` |
| `add_pr_line_comment` | Add inline comment to a PR | `pr_number: int`, `path: str`, `line: int`, `body: str` |
| `reply_to_review_comment` | Reply to a review comment thread | `pr_number: int`, `comment_id: int`, `body: str` |
| `merge_pr` | Merge a pull request | `pr_number: int`, `method?: str` |
| `delete_branch` | Delete a git branch | `branch: str` |

## Issue Management Tools

| Tool | Purpose | Key Params |
|------|---------|------------|
| `create_issue` | Create a GitHub issue | `title: str`, `body: str`, `labels?: list[str]` |
| `read_issue` | Get full issue details | `issue_number: int` |
| `update_issue` | Update issue title/body/labels | `issue_number: int`, `title?: str`, `body?: str` |
| `close_issue` | Close an issue | `issue_number: int` |
| `assign_issue` | Assign issue to a user | `issue_number: int`, `assignee: str` |
| `label_issue` | Add/remove labels | `issue_number: int`, `labels: list[str]` |
| `list_issues` | List open issues | `state?: str`, `labels?: list[str]` |
| `list_issue_comments` | Get issue comments | `issue_number: int`, `limit?: int` |
| `comment_on_issue` | Post a comment on an issue | `issue_number: int`, `body: str` |
| `comment_on_pr` | Post a comment on a PR | `pr_number: int`, `body: str` |

## Repository Context Tools

| Tool | Purpose | Key Params |
|------|---------|------------|
| `get_repo_info` | Get repository metadata | none |
| `get_ci_status` | Get CI check status for a PR/SHA | `pr_number?: int`, `sha?: str` |
| `list_pull_requests` | List PRs | `state?: str` |

## Introspection Tools

| Tool | Purpose | Key Params |
|------|---------|------------|
| `check_registry` | Check agent registry state | `issue_number?: int` |
| `get_recent_history` | Get recent agent activity | `limit?: int` |
| `list_agent_roles` | List available agent roles | none |

## Tool Assignment by Agent Role

The `tools:` frontmatter in `.squadron/agents/<role>.md` controls which tools each agent gets.

Typical assignments:
- **pm**: `create_issue`, `read_issue`, `update_issue`, `assign_issue`, `label_issue`, `list_issues`, `comment_on_issue`, `check_for_events`, `report_complete`, `escalate_to_human`, `list_agent_roles`, `check_registry`
- **feat-dev**: All tools above plus `open_pr`, `git_push`, `get_pr_details`, `get_pr_feedback`, `report_blocked`, `create_blocker_issue`
- **bug-fix**: Same as feat-dev
- **pr-review**: `get_pr_details`, `get_pr_feedback`, `list_pr_files`, `list_pr_reviews`, `submit_pr_review`, `get_pr_review_status`, `add_pr_line_comment`, `reply_to_review_comment`, `comment_on_pr`, `check_for_events`, `report_complete`
- **security-review**: Same as pr-review plus read-only file tools
- **test-coverage**: Same as pr-review
