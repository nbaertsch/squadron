# Squadron Tools Reference

Squadron agents have access to a set of specialized tools for interacting with GitHub and managing their workflow. Tools are selected per-agent using the `tools:` list in the agent's `.md` frontmatter.

> **Two types of tools:** Squadron provides its own custom tools (listed here). Agents can also use SDK built-in tools (`read_file`, `write_file`, `bash`, `git`, `grep`) — these are listed in the same `tools:` array and are handled transparently.

---

## Squadron Tool Categories

### Framework — Agent Lifecycle

Core tools for agent coordination and workflow management.

| Tool | Description |
|------|-------------|
| `check_for_events` | Check for pending events (PR reviews, blocker resolutions, human messages) |
| `report_blocked` | Report that the agent is blocked on another issue (agent sleeps until blocker resolves) |
| `report_complete` | Report that the assigned task is complete (agent session ends) |
| `create_blocker_issue` | Create a new GitHub issue that blocks current work |
| `escalate_to_human` | Request human intervention; adds `needs-human` label and notifies maintainers |
| `open_pr` | Open a new pull request |
| `git_push` | Push local commits to the remote branch |
| `submit_pr_review` | Submit a formal code review on a pull request (APPROVE, REQUEST_CHANGES, COMMENT) |

---

### Issue Management

Tools for creating, reading, updating, and managing GitHub issues.

| Tool | Description |
|------|-------------|
| `create_issue` | Create a new GitHub issue with title, body, and labels |
| `read_issue` | Read detailed information about an issue including body, labels, and assignees |
| `update_issue` | Update an issue's title, body, or other metadata |
| `close_issue` | Close an issue with an optional comment |
| `assign_issue` | Assign an issue to users |
| `label_issue` | Apply or remove labels from an issue |

---

### Pull Request — Reading

Tools for reading pull request data.

| Tool | Description |
|------|-------------|
| `get_pr_details` | Get detailed PR information including mergeable state, head/base branches, and description |
| `get_pr_feedback` | Get review comments, review status, and changed files for a PR |
| `list_pr_files` | List files changed in a PR with diff stats and patch previews |
| `merge_pr` | Merge a pull request (subject to branch protection rules) |
| `list_pr_reviews` | List all reviews on a PR with reviewer info and state (APPROVED, CHANGES_REQUESTED, etc.) |
| `get_review_details` | Get details of a specific review including its inline comments |
| `get_pr_review_status` | Get comprehensive review status — approvals, changes requested, pending reviewers |
| `list_requested_reviewers` | List users and teams requested to review a PR |

---

### Pull Request — Writing

Tools for posting comments and reviews on pull requests.

| Tool | Description |
|------|-------------|
| `add_pr_line_comment` | Post an inline comment on a specific line in a PR diff |
| `reply_to_review_comment` | Reply to an existing review comment thread |
| `comment_on_pr` | Post a general comment on a pull request |

---

### Repository Context

Tools for accessing repository and CI information.

| Tool | Description |
|------|-------------|
| `get_ci_status` | Get CI/CD check status for a commit or PR |
| `get_repo_info` | Get repository metadata (description, language, default branch, etc.) |
| `delete_branch` | Delete a remote branch |

---

### Listing

Tools for querying lists of GitHub resources.

| Tool | Description |
|------|-------------|
| `list_issues` | List repository issues with optional filters (state, labels, assignee) |
| `list_pull_requests` | List repository pull requests with optional state filter |
| `list_issue_comments` | Get all comments on a GitHub issue |

---

### Communication

Tools for posting updates.

| Tool | Description |
|------|-------------|
| `comment_on_issue` | Post a comment on a GitHub issue |
| `comment_on_pr` | Post a general comment on a GitHub pull request |

> **Tip:** Use `comment_on_pr` (not `comment_on_issue`) when responding to pull request review feedback. This keeps the conversation in the PR where reviewers are watching.

---

### Introspection

Tools for understanding the current Squadron system state.

| Tool | Description |
|------|-------------|
| `check_registry` | Query the agent registry for active, sleeping, and recent agents |
| `get_recent_history` | Get recently completed, failed, or escalated agents |
| `list_agent_roles` | List all configured agent roles with their triggers and lifecycle |

---

## GitHub Projects V2

Tools for creating and managing GitHub Projects V2 boards programmatically. All tools use the GitHub GraphQL API (Projects V2 is GraphQL-only).

> **Authentication:** These tools use the same `GITHUB_TOKEN` / GitHub App credentials as all other Squadron tools. The token must have the `project` OAuth scope (or the GitHub App must be granted project permissions).

### Project Lifecycle

| Tool | Description |
|------|-------------|
| `create_project` | Create a new GitHub Projects V2 board linked to the repo. Returns `project_id`, `number`, `title`, `url`. |
| `get_project` | Read a project by global node ID or display number. Returns title, description, fields, and item count. |
| `list_projects` | List all Projects V2 boards in the configured repository. |

### Custom Fields

| Tool | Description |
|------|-------------|
| `add_project_field` | Add a custom field to a project board (`TEXT`, `NUMBER`, `DATE`, or `SINGLE_SELECT` with option values). |
| `list_project_fields` | Return all fields defined on a project, including IDs, types, and single-select options. |

### Item (Issue) Management

| Tool | Description |
|------|-------------|
| `add_issue_to_project` | Add an existing GitHub Issue to a project board. Returns `item_id` needed for field updates. |
| `remove_issue_from_project` | Remove an item from a project board by its `item_id`. |
| `update_project_item_field` | Set the value of a custom field on a project item (e.g. `Status="In Progress"`, `Wave=1`). |
| `get_project_items` | List all items on a project board with their field values. Supports filtering by field name and value. |

### Field Value Format

When calling `update_project_item_field`, the `value` parameter format depends on the field type:

| Field Type | Value Format | Example |
|-----------|-------------|---------|
| `TEXT` | `{"text": "<string>"}` | `{"text": "feat-dev"}` |
| `NUMBER` | `{"number": <int/float>}` | `{"number": 1}` |
| `SINGLE_SELECT` | `{"singleSelectOptionId": "<option_node_id>"}` | `{"singleSelectOptionId": "opt_abc123"}` |
| `DATE` | `{"date": "<YYYY-MM-DD>"}` | `{"date": "2025-03-15"}` |

Use `list_project_fields` to retrieve the field node IDs and single-select option IDs needed for updates.

### Projects V2 Workflow Example

```python
# 1. Create a project board
project = create_project(title="Long-Horizon Epic #151", description="Wave tracking board")
project_id = project["project_id"]

# 2. Add custom fields
status_field = add_project_field(
    project_id=project_id,
    name="Status",
    data_type="SINGLE_SELECT",
    single_select_options=[
        {"name": "Backlog", "color": "GRAY"},
        {"name": "In Progress", "color": "BLUE"},
        {"name": "Blocked", "color": "RED"},
        {"name": "Done", "color": "GREEN"},
    ]
)
wave_field = add_project_field(project_id=project_id, name="Wave", data_type="NUMBER")
role_field  = add_project_field(project_id=project_id, name="Agent Role", data_type="TEXT")

# 3. Add an issue to the board
item = add_issue_to_project(project_id=project_id, issue_number=152)
item_id = item["item_id"]

# 4. Set field values (use option IDs from list_project_fields)
fields = list_project_fields(project_id=project_id)
in_progress_id = next(o["id"] for f in fields if f["name"] == "Status" for o in f["options"] if o["name"] == "In Progress")
update_project_item_field(project_id=project_id, item_id=item_id, field_id=wave_field["id"], value={"number": 1})
update_project_item_field(project_id=project_id, item_id=item_id, field_id=role_field["id"], value={"text": "infra-dev"})
update_project_item_field(project_id=project_id, item_id=item_id, field_id=status_field["id"], value={"singleSelectOptionId": in_progress_id})

# 5. Query the board state (e.g. find all blocked issues in Wave 2)
blocked_items = get_project_items(project_id=project_id, filter_field="Status", filter_value="Blocked")
```

---

## SDK Built-in Tools

These tools are provided by the GitHub Copilot SDK and available to any agent. List them alongside Squadron tools in the `tools:` frontmatter array.

| Tool | Description |
|------|-------------|
| `read_file` | Read the contents of a file in the repository |
| `write_file` | Write content to a file in the repository |
| `bash` | Execute shell commands (in the agent's git worktree) |
| `git` | Git operations (status, log, diff, etc.) |
| `grep` | Fast code search using ripgrep |

---

## Tool Selection Guide

List only the tools your agent actually needs. Over-granting tools increases attack surface and can lead to unintended behavior.

### PM Agent

```yaml
tools:
  - create_issue
  - read_issue
  - update_issue
  - close_issue
  - assign_issue
  - label_issue
  - list_issues
  - list_issue_comments
  - list_pull_requests
  - check_registry
  - get_recent_history
  - list_agent_roles
  - comment_on_issue
```

### Development Agent (feat-dev, bug-fix, docs-dev, infra-dev)

```yaml
tools:
  # SDK built-in tools
  - read_file
  - write_file
  - grep
  - bash
  - git
  - git_push
  # Issue context
  - read_issue
  - list_issue_comments
  # PR operations
  - open_pr
  - get_pr_details
  - get_pr_feedback
  - list_pr_files
  - list_pr_reviews
  - get_review_details
  - get_pr_review_status
  # Communication
  - reply_to_review_comment
  - comment_on_pr
  - comment_on_issue
  # Lifecycle
  - check_for_events
  - report_blocked
  - report_complete
  - create_blocker_issue
```

### Review Agent (pr-review, security-review, test-coverage)

```yaml
tools:
  # SDK built-in tools (read-only)
  - read_file
  - grep
  # PR reading
  - list_pr_files
  - get_pr_details
  - get_pr_feedback
  - get_ci_status
  - list_pr_reviews
  - get_review_details
  - get_pr_review_status
  - list_requested_reviewers
  # Review writing
  - add_pr_line_comment
  - reply_to_review_comment
  - comment_on_pr
  - submit_pr_review
  # Lifecycle
  - check_for_events
  - report_complete
```

---

## Tool Implementation

All Squadron tools are implemented in `src/squadron/tools/squadron_tools.py`. Each tool:
- Uses Pydantic models for parameter validation
- Is implemented as an async function for non-blocking operation
- Includes proper error handling and logging
- Interacts with GitHub via the internal `GitHubClient`

To see all registered tool names, see `ALL_TOOL_NAMES` in `src/squadron/tools/squadron_tools.py`.
