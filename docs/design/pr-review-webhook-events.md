# PR Review Webhook Events

## Overview

Squadron now supports comprehensive GitHub webhook events for pull request reviews, including inline code comments and detailed review activity. This addresses the issue where agents couldn't see important PR review activities like change requests and inline comments.

## Supported Events

### 1. Pull Request Review Comment Events

#### `pull_request_review_comment.created`
- **Trigger**: When a reviewer adds an inline comment to a specific line of code
- **Use Cases**: 
  - Wake development agents when reviewers request specific changes
  - Notify PM agents of detailed review activity
  - Track inline feedback for code quality metrics

#### `pull_request_review_comment.edited`
- **Trigger**: When a reviewer modifies an existing inline comment
- **Use Cases**:
  - Track evolving review feedback
  - Update agent context when review comments are clarified

#### `pull_request_review_comment.deleted`
- **Trigger**: When a reviewer deletes an inline comment
- **Use Cases**:
  - Clean up obsolete feedback from agent context
  - Track resolved review concerns

### 2. Enhanced Review Support

The existing `pull_request_review.submitted` event now works better with the new inline comment events to provide complete review visibility.

## Event Data Structure

### PR Review Comment Events
```json
{
  "action": "created|edited|deleted",
  "comment": {
    "id": 123456,
    "body": "This needs refactoring @squadron-dev feat-dev: please update",
    "path": "src/example.py",
    "line": 42,
    "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/5",
    "user": {"login": "reviewer"}
  },
  "pull_request": {
    "number": 5,
    "title": "Feature implementation"
  },
  "sender": {"login": "reviewer"}
}
```

## Configuration Examples

### Wake Developer on Inline Comments
```yaml
agent_roles:
  feat-dev:
    agent_definition: agents/feat-dev.md
    triggers:
      - event: "pull_request_review_comment.created"
        action: wake
      - event: "pull_request_review.submitted" 
        condition: {review_state: "changes_requested"}
        action: wake
```

### PM Monitoring All Review Activity
```yaml
agent_roles:
  pm:
    agent_definition: agents/pm.md
    triggers:
      - event: "pull_request_review_comment.created"
      - event: "pull_request_review_comment.edited"
      - event: "pull_request_review.submitted"
```

### PR Review Agent Enhancement
```yaml
agent_roles:
  pr-review:
    agent_definition: agents/pr-review.md
    triggers:
      - event: "pull_request.opened"
      - event: "pull_request_review_comment.created"
        action: wake  # See new inline feedback
      - event: "pull_request.synchronize"
        action: wake  # New commits
```

## Command Support

PR review comments support `@squadron-dev` command syntax:

```
@squadron-dev feat-dev: Please address this security concern in line 42
@squadron-dev security-review: This looks suspicious, please verify
```

## Integration Benefits

### For PM Agents
- ✅ **Complete visibility** into all PR review activity
- ✅ **Real-time updates** on change requests and inline comments
- ✅ **Better coordination** between reviewers and developers

### For Development Agents  
- ✅ **Immediate notification** of inline feedback
- ✅ **Targeted changes** based on specific line comments
- ✅ **Faster iteration** on review feedback

### For Review Agents
- ✅ **Enhanced context** from all review activity
- ✅ **Thread awareness** for ongoing discussions
- ✅ **Better review coordination** with multiple reviewers

## Implementation Details

### Event Router Updates
- Added `PR_REVIEW_COMMENT_CREATED`, `PR_REVIEW_COMMENT_EDITED`, `PR_REVIEW_COMMENT_DELETED` to `SquadronEventType`
- Enhanced `_to_squadron_event()` to handle PR review comment events
- Added proper PR number extraction from comment URLs
- Command parsing support for inline comments

### Agent Manager Integration
- Added PR review comment event handling
- Logging for review comment activity
- Future-ready for workflow engine integration

### Testing
- Comprehensive webhook tests for all new events
- Event router tests for proper event mapping
- Payload parsing and command extraction tests

## Migration Notes

### Existing Configurations
All existing webhook configurations continue to work unchanged. The new events are additive.

### Backward Compatibility  
- ✅ Existing `pull_request_review.submitted` events unchanged
- ✅ Existing agent triggers continue working
- ✅ No breaking changes to webhook payloads

### Recommended Updates
Consider updating agent configurations to leverage new events:

1. **Wake development agents** on inline comments for faster feedback loops
2. **Enhanced PM monitoring** of all review activity
3. **Review agent improvements** with better context awareness

## Related Issues

- **Fixes #66**: Add GitHub webhook support for PR review events
- **Enables #67**: Enhanced GitHub tools for reading review content
- **Prepares for #68**: Agent tools for making inline PR comments

## Security Considerations

- All new webhook events use the same HMAC signature verification
- Installation ID and repository scope validation applies to new events
- Rate limiting includes new event types
- No additional attack surface introduced

## Performance Impact

- Minimal overhead for event processing
- Same deduplication and queuing as existing events  
- New events follow existing async processing patterns
- No impact on webhook response times (still <10s GitHub requirement)
