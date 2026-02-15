# Roadmap

## V1 — Core Loop (MVP)

The minimum viable system: a human creates an issue → PM triages → dev agent implements → PR review → merge.

### V1 Must-Have Features

| # | Feature | Research | Status |
|---|---|---|---|
| 1 | **Event ingestion** — receive GitHub events and route to PM | ✅ AD-012 | Not started |
| 2 | **PM agent: issue triage** — classify, label, assign | ✅ AD-003 | Not started |
| 3 | **Agent instantiation** — create LLM client from agent definition on assignment | ✅ AD-003, AD-011 | Not started |
| 4 | **Dev agent: basic feature dev** — create branch, implement, write tests, open PR | ✅ AD-003 | Not started |
| 5 | **Dev agent: basic bug fix** — create branch, fix, write regression test, open PR | ✅ AD-003 | Not started |
| 6 | **Branch management** — branch-per-issue naming, creation, cleanup | — | Not started |
| 7 | **Agent sleep/wake** — serialize context on block, rehydrate on unblock | ✅ AD-003, AD-011 | Not started |
| 8 | **Blocker tracking** — PM detects blocker closure, notifies waiting agents | ✅ AD-013 | Not started |
| 9 | **PR approval flow (basic)** — configurable required approvals per branch | ✅ AD-015 | Not started |
| 10 | **PR review agent** — review code, approve/request changes | ✅ AD-003 | Not started |
| 11 | **Human escalation** — agent stuck → PM creates needs-human issue → notify humans | — | Not started |
| 12 | **Agent communication via issues** — @-ping based messaging, comment threads | — | Not started |
| 13 | **Circuit breakers** — max retries, max cost, max time per agent task | ✅ AD-018 | Not started |
| 14 | **Agent runtime** — single-process monolith with per-agent CopilotClient (containers V2+) | ✅ AD-017 | Not started |
| 15 | **`.squadron/` config format** — agent definitions, workflow configs, global config | ✅ [Config Schema](research/config-schema.md) | Not started |
| 16 | **Merge conflict resolution** — agent attempt → human escalation path | — | Not started |
| 17 | **Concurrency safety** — handle race conditions on issue state | ✅ AD-014 | Not started |
| 18 | **Webhook reliability** — retry handling, polling fallback, idempotent processing | ✅ AD-013 | Not started |

### V1 Stretch Goals

| # | Feature | Notes |
|---|---|---|
| S1 | Security review agent | Important but could ship without it if approval flows support human-only security review |
| S2 | ~~Dependency cycle detection~~ | ✅ Designed in AD-013 (BFS cycle detection in agent registry). Promoted to V1 core via EC-001. |
| S3 | Agent cost tracking / budgets | Important for production use but can be approximated with simple counters initially |

---

## V2 — Enhanced Coordination & Visibility

### V2 Features

| # | Feature | Notes |
|---|---|---|
| 1 | **GitHub Projects (Kanban) management** | PM agent maintains Kanban board — moves cards between columns as issues progress |
| 2 | **Enhanced merge conflict resolution agent** | Dedicated agent role specialized in merge resolution (evaluate if needed based on V1 experience) |
| 3 | **Semantic conflict detection** | Pre-merge analysis to detect incompatible changes across branches (OR-008) |
| 4 | **Architecture review agent** | Reviews structural/design decisions in PRs |
| 5 | **Test coverage agent** | Dedicated agent for improving test suites |
| 6 | **Documentation agent** | Auto-generates/updates docs from code changes |
| 7 | **Dependency update agent** | Monitors and updates project dependencies |
| 8 | **Multi-repo support** | Squadron manages agents across multiple linked repositories |
| 9 | **Agent performance analytics** | Track agent success rates, average time to resolution, cost per issue |
| 10 | **Workflow linting/validation** | Validate workflow configs for correctness (no unreachable branches, no undefined roles) |
| 11 | **Workflow versioning** | Pin workflow version at issue creation time (OR-010) |
| 12 | **Parallel issue processing** | PM handles multiple issues concurrently with proper ordering |
| 13 | **Advanced escalation routing** | Smarter routing of human escalations based on expertise, availability, load |

---

## Development Phases (Proposed)

### Phase 0: Research & Prototyping ✅
- ~~Resolve OR-001 (framework selection)~~ → Copilot SDK selected (AD-003)
- ~~Resolve OR-002 (event architecture)~~ → GitHub App + webhook routing designed (AD-012)
- ~~Resolve OR-003 (agent registry)~~ → SQLite registry with BFS cycle detection (AD-013)
- ~~Resolve OR-004 (concurrency)~~ → Six-layer concurrency strategy (AD-014)
- ~~Resolve OR-005 (approval flows)~~ → Complete YAML schema designed (AD-015)
- ~~Resolve OR-006 (role enforcement)~~ → Dual-layer enforcement (AD-016)
- ~~Resolve OR-009 (session persistence)~~ → Copilot SDK native persistence (AD-011)
- OR-007/008/010 deferred to V2
- ~~Define `.squadron/` config schema~~ → Canonical schema designed ([Config Schema](research/config-schema.md))
- ~~Design agent runtime model~~ → Single-process monolith with per-agent CopilotClient (AD-017)

### Phase 1: Single-Agent Loop
- PM agent can triage a single issue
- Dev agent can implement a simple feature (branch → code → test → PR)
- PR review agent can review and approve
- Merge to main with basic branch protection
- Human can create issue and see it through to merge

### Phase 2: Multi-Agent Coordination
- Blocker tracking and agent sleep/wake
- Human escalation path
- Merge conflict resolution flow
- Circuit breakers and cost controls
- Multiple agents working concurrently on different issues

### Phase 3: Production Hardening
- Webhook reliability (retry, polling fallback)
- Race condition handling
- API rate limit management
- Security sandboxing
- Monitoring and alerting

### Phase 4: V2 Features
- Kanban management
- Additional agent roles
- Analytics and reporting
