# Config System

## Two-Layer Design

Squadron uses two separate config artifacts:

| Layer | File | Purpose |
|-------|------|---------|
| **Agent Definition** | `.squadron/agents/<role>.md` | Per-agent: prompt, tools, MCP servers, skills |
| **Project Config** | `.squadron/config.yaml` | Orchestration: triggers, workflows, review policy, runtime |

These are parsed separately and kept separate at runtime.

## Agent Definition Files (`.squadron/agents/<role>.md`)

YAML frontmatter + markdown body.

### Frontmatter Fields

```yaml
---
name: feat-dev                    # Agent name (defaults to filename without .md)
display_name: Feature Developer   # Human-readable name
emoji: "👨‍💻"                       # Agent signature emoji
description: |                    # Short description
  Implements features...
infer: true                       # Whether SDK should infer missing context (default: true)
tools:                            # Mixed list: Squadron tools + SDK built-in tools
  - read_file                     # SDK built-in → goes to available_tools
  - bash                          # SDK built-in
  - read_issue                    # Squadron tool → goes to tools= parameter
  - report_complete               # Squadron tool
mcp_servers:                      # MCP server definitions
  github:
    type: http
    url: https://api.githubcopilot.com/mcp/
skills:                           # Skill names from skills.definitions in config.yaml
  - squadron-internals
  - squadron-dev-guide
---

You are a feature developer agent...  ← This becomes the system message
```

### Parsing: `parse_agent_definition()` (`src/squadron/config.py`, line ~919)

Returns an `AgentDefinition` Pydantic model. The markdown body becomes `agent_def.prompt`.

## Project Config (`config.yaml`)

**Loaded by:** `load_config()` at `src/squadron/config.py`

### SquadronConfig Model Hierarchy

```
SquadronConfig
├── project: ProjectConfig          # name, owner, repo, default_branch, bot_username
├── labels: LabelsConfig            # types, priorities, states
├── branch_naming: BranchNamingConfig  # templates for feat/, fix/, etc.
├── human_groups: dict[str, list[str]]  # group→[usernames]
├── agent_roles: dict[str, AgentRoleConfig]
│   └── AgentRoleConfig
│       ├── agent_definition: str   # path to .md file
│       ├── singleton: bool
│       ├── lifecycle: ephemeral|persistent|stateful
│       ├── triggers: list[AgentTrigger]  # event→action mappings
│       └── subagents: list[str]
├── circuit_breakers: CircuitBreakerConfig
├── runtime: RuntimeConfig          # models, provider
├── escalation: EscalationConfig
├── review_policy: ReviewPolicyConfig
├── human_invocation: HumanInvocationConfig
├── sandbox: SandboxConfig (lazy)
├── commands: dict[str, CommandDefinition]
├── workflows: dict[str, WorkflowConfig]
└── skills: SkillsConfig            # NEW (issue #125)
    ├── base_path: str              # default: .squadron/skills
    └── definitions: dict[str, SkillDefinition]
        └── SkillDefinition
            ├── path: str           # relative to base_path
            └── description: str
```

## AgentTrigger

Defines when an agent is spawned/woken/completed/slept:

```yaml
triggers:
  - event: issues.labeled
    label: feature           # Only when this label is applied
  - event: pull_request.opened
    action: sleep            # Sleep (not spawn) on PR open
  - event: pull_request_review.submitted
    action: wake
    condition:
      review_state: changes_requested
```

Actions: `spawn` (default), `wake`, `complete`, `sleep`

## Loading Flow

1. `load_config(squadron_dir)` reads `config.yaml` → `SquadronConfig`
2. `load_agent_definitions(squadron_dir, config)` reads each `.md` file → `dict[str, AgentDefinition]`
3. `AgentManager(config, agent_definitions, ...)` stores both
4. At runtime, `agent_manager.agent_definitions[role]` gives the `AgentDefinition`
