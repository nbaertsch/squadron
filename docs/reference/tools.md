# Squadron Tools Reference

Squadron agents have access to a comprehensive set of tools for interacting with GitHub, managing workflows, and coordinating with other agents. Tools are selected per-agent using the `tools:` list in the agent's `.md` frontmatter.

## Tool Categories

### Framework (Agent Lifecycle)
Core tools for agent coordination and workflow management.

- **`check_for_events`** - Check for pending events (PR reviews, blocker resolutions, messages)
- **`report_blocked`** - Report that the agent is blocked on another issue
- **`report_complete`** - Report that the assigned task is complete
- **`create_blocker_issue`** - Create a new GitHub issue that blocks current work
- **`escalate_to_human`** - Escalate to human maintainers for assistance
- **`submit_pr_review`** - Submit a code review on a pull request
- **`open_pr`** - Open a new pull request
- **`git_push`** - Push commits to remote repository

### Issue Management
Tools for creating, updating, and managing GitHub issues.

- **`create_issue`** - Create a new GitHub issue
- **`assign_issue`** - Assign an issue to users or teams
- **`label_issue`** - Apply or remove labels from an issue
- **`read_issue`** - Read detailed information about an issue
- **`close_issue`** - Close an issue with optional comment
- **`update_issue`** - Update issue title, body, or other metadata

### Pull Request Context
Tools for working with pull requests and code reviews.

- **`list_pr_files`** - List files changed in a pull request with diff stats
- **`get_pr_details`** - Get detailed PR information including mergeable state
- **`get_pr_feedback`** - Get review comments, status, and changed files for a PR
- **`merge_pr`** - Merge a pull request (subject to branch protection rules)

### Repository Context
Tools for accessing repository information and status.

- **`get_ci_status`** - Get CI/CD status for a commit or PR
- **`get_repo_info`** - Get repository metadata and statistics
- **`delete_branch`** - Delete a remote branch

### Introspection
Tools for understanding the current system state and agent activity.

- **`check_registry`** - Query the agent registry for active/dormant agents
- **`get_recent_history`** - Get recently completed, failed, or escalated agents
- **`list_agent_roles`** - List all configured agent roles with triggers and lifecycle

### Listing
Tools for querying GitHub resources.

- **`list_issues`** - List repository issues with optional filters (state, labels)
- **`list_pull_requests`** - List repository pull requests with optional state filter
- **`list_issue_comments`** - Get comments on a GitHub issue

### Communication
Tools for posting updates and communicating.

- **`comment_on_issue`** - Post a comment on a GitHub issue
- **`comment_on_pr`** - Post a comment on a GitHub pull request

## Tool Selection

Tools are selected per-agent in the YAML frontmatter of agent definition files:

```yaml
---
name: pm
description: Project manager that triages issues
tools:
  - read_issue
  - label_issue
  - assign_issue
  - create_issue
  - comment_on_issue
  - comment_on_pr
  - check_registry
  - list_agent_roles
---
```

### Common Tool Combinations

**PM Agent (Project Manager):**
```yaml
tools:
  - read_issue
  - label_issue
  - assign_issue
  - create_issue
  - comment_on_issue
  - comment_on_pr
  - check_registry
  - escalate_to_human
  - get_recent_history
  - list_agent_roles
  - list_issues
  - list_issue_comments
```

**Development Agent (Feature/Bug Fix):**
```yaml
tools:
  - read_issue
  - comment_on_issue
  - comment_on_pr
  - open_pr
  - git_push
  - check_for_events
  - report_blocked
  - report_complete
  - get_pr_feedback
  - list_issue_comments
```

**Review Agent (PR Review/Security):**
```yaml
tools:
  - get_pr_details
  - get_pr_feedback
  - list_pr_files
  - submit_pr_review
  - comment_on_issue
  - comment_on_pr
  - check_for_events
  - report_complete
```

## Best Practices

1. **Minimal Tool Sets**: Only include tools the agent actually needs to reduce complexity
2. **Role-Appropriate Access**: Dev agents shouldn't have issue management tools, PM agents shouldn't have git tools
3. **Common Patterns**: Most agents need `check_for_events` and `report_complete` for lifecycle management
4. **Communication**: All agents typically need `comment_on_issue` for status updates. Use `comment_on_pr` when responding to PR review feedback.

## Tool Implementation

All tools are implemented in `src/squadron/tools/squadron_tools.py` using the Copilot SDK's `@define_tool` decorator. Each tool includes:
- Parameter validation using Pydantic models
- Async implementation for non-blocking operation
- Proper error handling and logging
- GitHub API integration through the squadron GitHub client


### PR Review Comments
Tools for making targeted, human-like code review comments on pull requests.

- **`add_pr_line_comment`** - Add an inline comment to a specific line in a PR file
- **`add_pr_file_comment`** - Add a general comment about a file (not tied to specific line)  
- **`suggest_code_change`** - Suggest a specific code change with GitHub's suggestion syntax
- **`add_pr_diff_comment`** - Add a comment to a specific position in a PR diff
- **`start_pr_review`** - Begin a review session for batching comments before submission
- **`submit_review_with_comments`** - Submit a review with multiple inline comments at once
- **`update_pr_review_comment`** - Update an existing PR review comment
- **`delete_pr_review_comment`** - Delete a PR review comment
- **`resolve_review_thread`** - Mark a review comment thread as resolved
- **`reply_to_review_comment`** - Reply to an existing review comment

#### Usage Examples

**Security review agent commenting on specific security concerns:**
```yaml
# In .squadron/agents/security-review.md
tools:
  - add_pr_line_comment
  - suggest_code_change
  - submit_review_with_comments
```

**PR review agent making comprehensive code reviews:**
```python
# Add inline comment to specific line
add_pr_line_comment(
    pr_number=123,
    file_path="src/auth.py", 
    line_number=42,
    comment="Consider using constant-time comparison to prevent timing attacks"
)

# Suggest specific code improvement
suggest_code_change(
    pr_number=123,
    file_path="src/utils.py",
    line_start=10,
    line_end=12, 
    suggestion="if value is None:\n    return default_value\nreturn validate(value)"
)

# Submit comprehensive review with multiple comments
submit_review_with_comments(
    pr_number=123,
    action="REQUEST_CHANGES",
    summary="Found security and performance issues that need addressing",
    comments=[
        {"path": "src/auth.py", "line": 42, "body": "Security issue here"},
        {"path": "src/utils.py", "line": 15, "body": "Performance concern"}
    ]
)
```

**Bug fix agent providing targeted feedback:**
```python
# Comment on test coverage
add_pr_line_comment(
    pr_number=123,
    file_path="tests/test_fix.py",
    line_number=25,
    comment="This test should also cover the edge case where input is None"
)

# Reply to existing discussion
reply_to_review_comment(
    comment_id=12345,
    reply_body="Good point! I'll add error handling for that scenario."
)
```

#### Review Actions

When using `submit_review_with_comments` or `submit_pr_review`, you can specify:

- **`APPROVE`** - Approve the changes for merging
- **`REQUEST_CHANGES`** - Request changes before merging (blocks merge)
- **`COMMENT`** - Provide feedback without blocking merge

#### Best Practices

1. **Use inline comments for specific issues** - `add_pr_line_comment` for targeted feedback on specific lines
2. **Batch related comments** - Use `submit_review_with_comments` when making multiple related comments
3. **Provide actionable suggestions** - Use `suggest_code_change` with concrete code improvements
4. **Follow up on discussions** - Use `reply_to_review_comment` to continue conversations
5. **Resolve when addressed** - Use `resolve_review_thread` when issues are fixed

#### GitHub Suggestion Syntax

The `suggest_code_change` tool automatically formats suggestions using GitHub's suggestion syntax:

```markdown
```suggestion
// Your improved code here
const result = safeOperation(input);
```
```

Recipients can apply suggestions with one click in the GitHub interface.
