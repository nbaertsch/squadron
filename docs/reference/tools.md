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
  - check_for_events
  - report_complete
```

## Best Practices

1. **Minimal Tool Sets**: Only include tools the agent actually needs to reduce complexity
2. **Role-Appropriate Access**: Dev agents shouldn't have issue management tools, PM agents shouldn't have git tools
3. **Common Patterns**: Most agents need `check_for_events` and `report_complete` for lifecycle management
4. **Communication**: All agents typically need `comment_on_issue` for status updates

## Tool Implementation

All tools are implemented in `src/squadron/tools/squadron_tools.py` using the Copilot SDK's `@define_tool` decorator. Each tool includes:
- Parameter validation using Pydantic models
- Async implementation for non-blocking operation
- Proper error handling and logging
- GitHub API integration through the squadron GitHub client

