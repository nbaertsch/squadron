# Squadron PR Workflow Integration

This directory contains GitHub Actions workflows that should be manually deployed to `.github/workflows/` to enable PR-based review and cleanup automation.

## Workflows

### pr-cleanup.yml
**Purpose**: Automates cleanup when PRs are merged
- Automatically closes issues referenced by merged PRs (using "Fixes #N" patterns)
- Deletes merged feature branches
- Posts completion notifications

**Triggers**: When PRs are closed with `merged: true`

**Required Permissions**:
- `contents: write` (for deleting branches)
- `issues: write` (for closing issues)
- `pull-requests: write` (for commenting on PRs)

### pr-review-routing.yml
**Purpose**: Routes PR review events to appropriate agents
- Notifies agents when their PRs receive review feedback
- Posts review status updates directly on PRs
- Ensures review responses happen on PR rather than linked issues

**Triggers**: 
- `pull_request_review.submitted`
- `pull_request.review_requested`

**Required Permissions**:
- `contents: read`
- `issues: write` (for posting PR comments)
- `pull-requests: write`

## Deployment

To deploy these workflows:

1. Copy files from `deploy/workflows/` to `.github/workflows/`
2. Commit and push to main branch
3. Workflows will be active on the next PR event

```bash
cp deploy/workflows/*.yml .github/workflows/
git add .github/workflows/
git commit -m "Add PR review and cleanup workflows"
git push
```

## Integration with Squadron

These workflows complement the Squadron agent improvements:
- Agents now have `comment_on_pr` tool for PR-specific responses
- Agent prompts updated to use PRs for review feedback instead of issues
- Automatic cleanup ensures no orphaned branches or stale issues
