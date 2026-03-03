# Squadron Config Review — Issue #148

**Status:** Implementation complete.

## Summary of Findings (from review phase)

- 2 high-severity issues (security + silent runtime failure)
- 7 medium-severity issues (missing pipelines, gaps in config coverage)
- 5 low-severity issues (readability, optimization opportunities)

## Implementation Status

All findings from the greenfield recommendation have been implemented in `.squadron/config.yaml`
and the relevant agent definition files.

### Changes Made

**`config.yaml`**
- ✅ [SECURITY] Added `.squadron/**` and `.squadron-data/**` to `sandbox.sensitive_paths`
- ✅ [BUG] Registered `code-search` and `test-writer` in `agent_roles` (was: silent runtime failure)
- ✅ [GAP] Added `infra-dev-lifecycle` pipeline for `infrastructure` label
- ✅ [GAP] Added `merge-conflict-resolution` pipeline
- ✅ [GAP] Added `pull_request.synchronize` wake events to all lifecycle pipelines
- ✅ [GAP] Added `issue_comment.created` wake event to `docs-dev-lifecycle`
- ✅ [SECURITY] Expanded `pr-lifecycle` security review to all `src/**` changes
- ✅ [FEATURE] `pr-lifecycle` now uses parallel stage for test-coverage + security-review
- ✅ [FEATURE] Human approval gate added to `pr-lifecycle` for security/infra PRs
- ✅ [FEATURE] Added `config-change-review` pipeline (self-improvement with guardrails)
- ✅ [OPS] Increased `sandbox.retention_days` from 1 to 7
- ✅ [OPS] Made `max_concurrent_agents: 15` explicit
- ✅ [OPS] Made network bridge config explicit (`bridge_name`, `bridge_subnet`, etc.)
- ✅ [UX] Added `triage` and `review` commands
- ✅ [UX] Added `security-team` human group
- ✅ [BUG] Removed `chore` from `branch_naming` (was: silently ignored by Pydantic)
- ✅ [OPS] Added `merge-conflict` circuit breaker override
- ℹ️  `reasoning_effort` NOT set — omitted per project owner instruction (model support unconfirmed)

**Agent definition files**
- ✅ `docs-dev.md`: added `squadron-internals`, `squadron-dev-guide` skills
- ✅ `merge-conflict.md`: added `squadron-internals`, `squadron-dev-guide` skills
- ✅ `code-search.md`: added `squadron-internals` skill
- ✅ `pm.md`: added `squadron-dev-guide` skill
- ✅ `infra-dev.md`: added `squadron-tools` skill
