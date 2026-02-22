# Common Pitfalls

> **AD-019 MIGRATION NOTE:** The unified pipeline system (see `docs/design/unified-pipeline-system.md`) replaces ALL legacy orchestration: `triggers`, `review_policy`, `workflows`, and the Workflow Engine v2. When implementing AD-019, **delete all legacy code entirely** — no backward-compatibility shims, no auto-conversion, no deprecation period. Clean replacement is the required pattern for all refactors in this project.

## 1. Dual Routing Systems

> **LEGACY — will be removed by AD-019.** After the pipeline refactor, `agent_roles.<role>.triggers` no longer exists. All orchestration is via `pipelines:` config. The EventRouter dispatches to the PipelineEngine, not AgentManager trigger handlers.

Squadron has **two routing systems** that work together but are easily confused:

### System 1: EventRouter (`src/squadron/event_router.py`)
Converts raw `GitHubEvent` → `SquadronEvent` and dispatches to registered handlers.
`AgentManager.handle_*` methods are the handlers.

### System 2: Trigger Config (`.squadron/config.yaml`)
`agent_roles.<role>.triggers` defines which events spawn/wake/complete/sleep agents.
The `AgentManager` reads trigger configs to decide which agents to act on.

**Pitfall:** When adding a new event type, you must:
1. Add it to `SquadronEventType` in `models.py`
2. Handle conversion in `EventRouter._to_squadron_event()`
3. Add a handler in `AgentManager` 
4. Update `config.yaml` triggers for relevant agents

## 2. Hardcoded Agent List in `parse_command()`

`parse_command()` in `src/squadron/models.py` has a hardcoded list of agent names
for parsing `@squadron-dev <agent>:` mentions. When adding new agent roles,
you may need to update this function.

## 3. Config vs Definition Confusion

**`AgentRoleConfig`** (from `config.yaml` → `agent_roles`):
- Controls orchestration: triggers, lifecycle, subagents, singleton
- Lives in `SquadronConfig.agent_roles`
- Access: `agent_manager.config.agent_roles.get(role)`

**`AgentDefinition`** (from `.md` file frontmatter):
- Controls agent behavior: prompt, tools, MCP servers, skills
- Lives in `agent_manager.agent_definitions`
- Access: `agent_manager.agent_definitions.get(role)`

**Never mix them up.** They have different fields and different purposes.

## 4. Tool Name Splitting

Agent frontmatter `tools:` lists can contain two types:
- **Custom Squadron tools** (names in `ALL_TOOL_NAMES_SET`): passed as `tools=` to SDK
- **SDK built-in tools** (bash, read_file, grep, etc.): passed as `available_tools=` to SDK

If you pass SDK tool names to `tools=`, the SDK ignores them. If you pass Squadron
tool names to `available_tools=`, the SDK rejects the entire allowlist.

The splitting happens in `_run_agent()` in `agent_manager.py`:
```python
custom_tool_names = [t for t in agent_def.tools if t in ALL_TOOL_NAMES_SET]
sdk_available_tools = [t for t in agent_def.tools if t not in ALL_TOOL_NAMES_SET] or None
```

## 5. Ephemeral vs Stateful Agent IDs

Ephemeral agents (`lifecycle: ephemeral`) get timestamp-suffixed IDs:
`pm-issue-42-1708612800`

Stateful agents get simple IDs: `feat-dev-issue-42`

**Pitfall:** Queries by agent_id will fail for ephemeral agents if you use the
simple format. Use `get_agents_for_issue()` instead.

## 6. Worktree Path Can Be None

`record.worktree_path` is `None` for ephemeral agents (they use repo root).
Always guard:
```python
working_dir = Path(record.worktree_path) if record.worktree_path else self.repo_root
```

## 7. Registry UNIQUE Constraint

The registry has a UNIQUE constraint on `agent_id`. If a terminal agent with the
same ID exists, re-spawning will fail. `create_agent()` handles this by deleting
stale terminal records before creating new ones. Don't bypass this logic.

## 8. Skill Directories Must Exist at Runtime

`_resolve_skill_directories()` only warns (not errors) when skill directories are
missing. Agents start without skills if the directory doesn't exist. Always verify
that `.squadron/skills/<name>/` exists before referencing a skill in frontmatter.

## 9. The `infer: true` Flag

`infer: true` in agent frontmatter tells the Copilot SDK to infer missing context
automatically. Almost always true. Setting `infer: false` means the agent only
has explicitly provided context.

## 10. Session ID Format

Session IDs must be stable across sleep→wake cycles for session resumption to work.
The format is `"squadron-{role}-issue-{number}"`. Don't change this format.
