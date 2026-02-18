# Agent PR Communication Policy Updates

This file contains updates that should be applied to agent definitions to ensure proper PR-based communication flow.

## Communication Guidelines Addition

Add this section to all agents that handle PRs (feat-dev, bug-fix, docs-dev, infra-dev, etc.):

```markdown
## Communication Policy

**IMPORTANT**: Follow PR-based communication flow:

- **For PR-related topics**: Use `comment_on_pr` to ensure all stakeholders see the conversation
- **For issue planning**: Use `comment_on_issue` only for initial planning and clarification
- **After opening PR**: Switch to `comment_on_pr` for all status updates, questions, and responses

### When to use each tool:

✅ **Use `comment_on_pr`** for:
- Responding to PR review feedback
- Asking clarifying questions during review
- Providing status updates on PR progress
- Explaining implementation decisions

✅ **Use `comment_on_issue`** for:
- Initial issue analysis and planning (before PR)
- Escalating blockers that prevent PR creation
- Clarifying requirements with humans

### Agent Responsibility:
- The agent that opened the PR is responsible for responding to review feedback
- All communication about the PR should happen on the PR itself
- Use the original issue only for planning and escalation
```

## Wake Protocol Updates

Update the wake protocol sections to emphasize PR communication:

```markdown
## Wake Protocol

When you are resumed from a sleeping state:

1. **Check for pending events** — call `check_for_events` to see what triggered your wake
2. **If woken for PR feedback** — use `get_pr_feedback` to see review comments
3. **Respond on the PR** — use `comment_on_pr` to respond to reviewers (NOT comment_on_issue)
4. **Address feedback** — make code changes, push updates, or clarify questions
5. **Continue your work** from where you left off

**IMPORTANT**: When responding to PR reviews, always use `comment_on_pr` to keep the conversation in context.
```

## Example Usage Patterns

Add these examples to agent definitions:

```markdown
## Example Communication Flow

### Initial Work (Issue Phase):
```
# Planning and clarification
comment_on_issue(issue_number=42, body="Analyzing requirements for user authentication feature...")

# Open PR when ready
open_pr(title="feat: implement user authentication", body="Fixes #42\n\nImplements...")
```

### PR Review Phase:
```
# Respond to review feedback (NOT on issue!)
comment_on_pr(pr_number=123, body="Thanks for the review! I've addressed the concerns about...")

# If clarification needed
comment_on_pr(pr_number=123, body="Could you clarify what you mean by 'better error handling'?")
```

### Completion:
```
# PR merged automatically via review policy
# Cleanup happens automatically via GitHub Actions
report_complete(summary="Feature implemented and merged successfully")
```
```

## Tool Priority Updates

Ensure tool lists prioritize PR communication:

```yaml
tools:
  # ... other tools ...
  # Communication (PR-first priority)
  - comment_on_pr      # PRIMARY for PR-related topics  
  - comment_on_issue   # For planning/escalation only
  # ... other tools ...
```

## Agent Role Specific Updates

### Feature/Bug/Docs Development Agents
- Emphasize switching from issue comments to PR comments after opening PR
- Clarify responsibility for responding to review feedback

### Review Agents (pr-review, security-review)
- Always use submit_pr_review for formal reviews
- Use comment_on_pr for questions and follow-up

### PM Agent
- Use comment_on_issue for initial triage and planning
- Avoid commenting on PRs (leave that to development agents)

## Implementation

Apply these changes to:
- `.squadron/agents/feat-dev.md`
- `.squadron/agents/bug-fix.md` 
- `.squadron/agents/docs-dev.md`
- `.squadron/agents/infra-dev.md`
- `.squadron/agents/pr-review.md`
- `.squadron/agents/security-review.md`
