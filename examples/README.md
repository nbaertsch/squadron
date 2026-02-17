# Example Squadron Configuration

This directory contains a complete, minimal, working Squadron configuration.

## Quick Start

1. Copy this `.squadron/` directory into your repository root
2. Edit `config.yaml` — set `project.name`, `project.owner`, `project.repo`, and `human_groups.maintainers`
3. Deploy your Squadron container (see `deploy/README.md`)
4. Create a GitHub issue — Squadron agents will start working

## Files

```
.squadron/
├── config.yaml              # Project configuration (2 required fields)
└── agents/
    ├── pm.md                # PM agent — triages issues, applies labels
    ├── feat-dev.md          # Feature dev agent — implements features
    ├── bug-fix.md           # Bug fix agent — fixes bugs
    ├── pr-review.md         # PR review agent — reviews pull requests
    └── security-review.md   # Security review agent — security-focused review
```

## How It Works

1. A GitHub issue is created
2. The PM agent triages it and applies a label (e.g., `feature`)
3. The label triggers a dev agent (e.g., `feat-dev`)
4. The dev agent creates a branch, implements, and opens a PR
5. Review agents review the PR
6. You merge when ready

## Customization

- **Add a new role**: Add a `.md` file in `agents/`, add the role to `agent_roles` in `config.yaml`
- **Change triggers**: Edit the `triggers` section for any role
- **Adjust limits**: Uncomment and edit the `circuit_breakers` section
- **Use a different model**: Uncomment and edit the `runtime` section
