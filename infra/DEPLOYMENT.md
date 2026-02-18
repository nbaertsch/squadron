# PR Communication Infrastructure Deployment Guide

This guide explains how to deploy the PR-based communication infrastructure for Squadron agents.

## Overview

This infrastructure fixes issue #40 by ensuring:
1. PR review communication happens on the PR, not the issue
2. The same agent that submitted the PR responds to review feedback  
3. Automatic cleanup occurs when PRs are merged (branch deletion, issue closure)

## Components

### 1. Scripts and Validation Tools
✅ **Already deployed**: 
- `infra/scripts/pr-communication-policy.py` - Policy validation script
- `infra/pr-communication-guidelines.md` - Comprehensive documentation
- `infra/agent-pr-communication-patch.md` - Agent configuration updates

### 2. GitHub Actions Workflows  
⚠️ **Requires manual deployment** (needs workflow permissions):

Copy the following files from `infra/workflows-to-deploy/` to `.github/workflows/`:

#### pr-cleanup.yml
**Purpose**: Automatic cleanup when PRs are closed/merged
**Features**:
- Deletes merged branches (except protected ones)
- Comments on linked issue with merge status
- Closes linked issue if PR was successfully merged

#### pr-review-flow.yml  
**Purpose**: Enforces PR-based communication patterns
**Features**:
- Redirects agents from issue comments to PR comments
- Tags responsible agent when review feedback is received
- Adds welcome message to new PRs explaining the process

#### validate-pr-policy.yml
**Purpose**: Validates PR communication policy compliance in CI
**Features**:
- Checks agent configurations
- Validates required workflows exist
- Ensures Squadron config handles PR events

### 3. Agent Configuration Updates
📝 **Documentation provided** in `infra/agent-pr-communication-patch.md`

## Deployment Steps

### Step 1: Deploy Core Infrastructure
```bash
# Validation tools are already deployed
python3 infra/scripts/pr-communication-policy.py --validate
```

### Step 2: Deploy GitHub Actions Workflows  
**Manual action required** (needs repository admin or workflow permissions):

```bash
# Copy workflow files to active directory
cp infra/workflows-to-deploy/*.yml .github/workflows/

# Commit and push
git add .github/workflows/
git commit -m "deploy: add PR communication automation workflows"
git push
```

### Step 3: Update Agent Configurations (Optional)
The current agent configurations already follow most best practices, but you can enhance them:

1. Review `infra/agent-pr-communication-patch.md` 
2. Apply communication policy sections to agent definitions
3. Update wake protocols to emphasize PR communication

### Step 4: Validate Deployment
```bash
# Run validation to ensure everything is properly configured
python3 infra/scripts/pr-communication-policy.py --validate

# Test a sample PR flow to verify automation
```

## Expected Behavior After Deployment

### For New PRs:
1. **PR opened** → automatic welcome comment explaining process
2. **Review submitted** → responsible agent automatically tagged
3. **PR merged** → branch deleted, issue updated and closed
4. **PR closed without merge** → issue updated but remains open

### For Agent Communication:
1. **Planning phase** → agents use `comment_on_issue`
2. **PR phase** → agents switch to `comment_on_pr`  
3. **Review response** → original agent uses `comment_on_pr` to respond
4. **Completion** → automatic cleanup via GitHub Actions

### For Compliance:
1. **CI validation** → validates configuration on every change
2. **Policy enforcement** → guides agents to correct communication channels
3. **Automatic cleanup** → prevents orphaned branches and stale issues

## Troubleshooting

### Workflows not triggering
- Check that workflows were deployed to `.github/workflows/`
- Verify repository permissions allow GitHub Actions
- Check workflow syntax with GitHub's workflow validator

### Agents still commenting on issues instead of PRs
- Review agent wake triggers in `.squadron/config.yaml`
- Check agent tool priorities in agent definitions
- Verify PR events are properly routed to agents

### Cleanup not working
- Ensure PR body contains "Fixes #123" or similar syntax
- Check GitHub Actions logs for permission issues
- Verify linked issue numbers are correctly extracted

## Testing

### Manual Testing Checklist:
- [ ] Open a test PR with "Fixes #X" in body
- [ ] Verify welcome comment appears on PR
- [ ] Request changes in review
- [ ] Verify agent is tagged and responds on PR
- [ ] Merge PR and verify cleanup (branch deletion, issue closure)

### Automated Testing:
```bash
# Validate configuration
python3 infra/scripts/pr-communication-policy.py --validate

# Run CI validation (if workflows deployed)
# This will run automatically on pushes to .squadron/ and .github/workflows/
```

## Benefits Delivered

✅ **Better Visibility**: All PR stakeholders see full conversation context  
✅ **Cleaner Issues**: Issue threads aren't polluted with review discussions  
✅ **Agent Accountability**: Clear ownership of PR responses  
✅ **Automatic Cleanup**: No manual maintenance of branches/issues  
✅ **Improved Workflow**: Natural flow from issue → PR → review → merge → cleanup

## Rollback Plan

If needed, the deployment can be rolled back by:
1. Removing the GitHub Actions workflows
2. Reverting agent configuration changes
3. The validation scripts can remain as they're non-intrusive

## Next Steps

After deployment, monitor:
- Agent behavior in PR communication
- Cleanup automation effectiveness  
- Any edge cases or issues that arise

For questions or issues, refer to `infra/pr-communication-guidelines.md` or escalate to maintainers.
