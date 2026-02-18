# PR Communication Guidelines for Squadron Agents

## Overview

This document establishes guidelines for Squadron agents to ensure that pull request review and response flows happen on the PR itself, not on the linked issue.

## Key Principles

1. **PR-First Communication**: All review discussions, feedback responses, and status updates related to a PR should happen on the PR, not the linked issue.

2. **Agent Responsibility**: The same agent that submitted the PR should be the one responding to review feedback and addressing change requests.

3. **Automatic Cleanup**: When a PR is merged or closed, automatic cleanup should occur:
   - Delete the merged branch (unless protected)
   - Update the linked issue with merge status
   - Close the linked issue if PR was successfully merged

## Agent Tool Usage Guidelines

### For PR-Related Communication

**✅ USE:** `comment_on_pr` for:
- Responding to review feedback
- Asking clarifying questions about PR reviews
- Providing status updates on PR progress
- Explaining implementation decisions during review

**❌ AVOID:** `comment_on_issue` for PR-related topics

### For Issue-Related Communication

**✅ USE:** `comment_on_issue` for:
- Initial issue analysis and planning
- Clarifying requirements
- Escalating blockers
- Reporting completion when no PR is involved

## Agent Configuration Changes

### Required Tool Updates

Agents that work with PRs should include these tools:

```yaml
tools:
  # PR Communication (PRIMARY for PR-related topics)
  - comment_on_pr
  - submit_pr_review
  - get_pr_feedback
  - get_pr_details
  
  # Issue Communication (for initial planning/escalation only)
  - comment_on_issue
  
  # Lifecycle management
  - check_for_events
  - report_complete
```

### Event Handling Updates

Agents should be configured to wake on PR events:

```yaml
triggers:
  # Initial issue work
  - event: issues.labeled
    label: [appropriate-label]
  
  # PR workflow 
  - event: pull_request.opened
    action: sleep  # Let reviewers take over
    
  - event: pull_request_review.submitted
    action: wake
    condition:
      review_state: changes_requested  # Wake to address feedback
      
  - event: pull_request.closed
    action: complete
    condition:
      merged: true  # Complete when successfully merged
```

## Implementation Guidelines

### For Development Agents (feat-dev, bug-fix, etc.)

1. **Initial Work**: Use `comment_on_issue` for planning and clarification
2. **PR Submission**: After opening PR, switch to `comment_on_pr` for all communication
3. **Review Response**: When woken by review feedback, use `comment_on_pr` to respond
4. **Completion**: Report complete when PR is merged

### For Review Agents (pr-review, security-review, etc.)

1. **Reviews**: Always use `submit_pr_review` for formal reviews
2. **Questions**: Use `comment_on_pr` for clarification requests
3. **Follow-up**: Use `comment_on_pr` for continued discussion

### Example Agent Behavior Flow

```
1. Issue opened → agent uses comment_on_issue for planning
2. Agent opens PR → agent uses comment_on_pr for any further communication
3. Review submitted → reviewer uses submit_pr_review + comment_on_pr
4. Review requests changes → original agent wakes, uses comment_on_pr to respond
5. PR merged → automatic cleanup (branch deletion, issue closure)
```

## Infrastructure Components

### GitHub Actions Workflows

1. **pr-cleanup.yml**: Handles automatic cleanup when PRs close/merge
2. **pr-review-flow.yml**: Enforces PR communication patterns and redirects

### Validation Tools

1. **pr-communication-policy.py**: Validates agent configurations follow guidelines
2. **Squadron config validation**: Ensures proper event routing

## Migration Strategy

### Phase 1: Infrastructure Setup
- ✅ Deploy GitHub Actions workflows
- ✅ Create validation scripts
- ✅ Update documentation

### Phase 2: Agent Updates
- Update agent definitions to prefer comment_on_pr for PR contexts
- Add proper PR event handling
- Update tool recommendations

### Phase 3: Enforcement
- Enable validation in CI
- Monitor compliance
- Address any edge cases

## Benefits

1. **Better Visibility**: All PR stakeholders see the full conversation context
2. **Cleaner Issues**: Issue threads aren't polluted with PR review discussions  
3. **Agent Accountability**: Clear ownership of PR responses
4. **Automatic Cleanup**: Reduces manual maintenance overhead
5. **Improved Workflow**: Natural flow from issue → planning → PR → review → merge → cleanup

## Monitoring and Compliance

The system includes automated monitoring to ensure:
- Agents use appropriate communication channels
- Review responses come from the correct agent
- Cleanup actions complete successfully
- No orphaned branches or issues remain

## Troubleshooting

### Common Issues

**Problem**: Agent comments on issue instead of PR during review
**Solution**: Check agent configuration - ensure triggers properly wake agent on PR events

**Problem**: Wrong agent responds to PR feedback  
**Solution**: Verify branch naming matches agent role patterns

**Problem**: Cleanup doesn't trigger
**Solution**: Ensure PR body contains proper "Fixes #123" syntax

For additional support, see Squadron documentation or escalate to maintainers.
