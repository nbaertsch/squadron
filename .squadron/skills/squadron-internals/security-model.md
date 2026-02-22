# Security Model

## Overview

Squadron's security model is centered on environment isolation, secret stripping, and
sandboxed execution. The primary implementation is in `src/squadron/sandbox/`.

## Env Isolation Pattern (Issue #117)

Each agent runs in an isolated environment. The sandbox intercepts tool calls to
prevent secrets from leaking into agent context or external calls.

**Key principle:** Agents never see raw environment variables containing secrets.
The framework strips sensitive keys before injecting any environment context.

## Sandbox Architecture (`src/squadron/sandbox/`)

The sandbox provides:
1. **Namespace isolation** (Linux kernel): separate mount, PID, network, IPC, UTS namespaces
2. **Worktree isolation** via overlayfs (copy-on-write filesystem)
3. **Resource limits** via cgroups v2 (memory, CPU, disk)
4. **Tool proxy** via Unix socket — all tool calls go through a proxy that can inspect/block

**Config in `config.yaml`:**
```yaml
sandbox:
  enabled: false        # Set true in production (requires Linux >= 3.8)
  namespace_mount: true
  namespace_pid: true
  namespace_net: true
  seccomp_enabled: true
  use_overlayfs: true
  memory_limit_mb: 2048
  cpu_quota_percent: 200
```

## Trust Boundaries

| Zone | Trusted? | Notes |
|------|----------|-------|
| GitHub webhook payloads | Partially | HMAC-verified signature required |
| Agent LLM output | Untrusted | All tool calls proxied and inspected |
| Skill directories | Trusted | Read-only knowledge, no code execution |
| Worktree filesystem | Sandboxed | Overlay FS, changes contained to worktree |
| Host environment | Trusted | Framework process has full access |

## Sensitive Path Protection

The sandbox blocks direct modification of sensitive paths by agents:
```yaml
sensitive_paths:
  - ".github/**"
  - "Makefile"
  - "*.sh"
  - "pyproject.toml"
  - "Dockerfile"
  - "infra/**"
  - "deploy/**"
```

## Output Inspection

When `output_inspection_enabled: true`, the sandbox scans agent tool outputs
for potential secret patterns before returning results to the agent.

When `diff_inspection_enabled: true`, git diffs produced by agents are scanned
before being surfaced as context.

## Bot Identity Filtering

Events originating from the bot's own GitHub App account (`squadron-dev[bot]`)
are filtered by `EventRouter` to prevent infinite loops where the bot reacts
to its own comments.

Config: `project.bot_username: squadron-dev[bot]`
