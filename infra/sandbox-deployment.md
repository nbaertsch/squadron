# Sandbox Deployment Guide (Issue #85 / #97)

This document describes the infrastructure changes required to enable the sandboxed agent execution model and host-side authentication broker.

## Overview

The sandbox module (`src/squadron/sandbox/`) implements:
- Linux namespace isolation (`unshare -p -n -m -i -u -f`)
- seccomp-bpf syscall allowlist (via ctypes, no libseccomp dependency)
- Ephemeral overlayfs/tmpfs worktrees
- Forensic retention on Azure File Share
- Cryptographic session tokens + per-agent Unix socket auth broker

Sandbox is **disabled by default** (`sandbox.enabled: false` in `.squadron/config.yaml`).

---

## Infrastructure components

### 1. Docker image (Dockerfile)

Added packages to the runtime stage:

| Package | Purpose |
|---------|---------|
| `util-linux` | Provides `unshare` for PID/net/mount/IPC/UTS namespace isolation |
| `fuse-overlayfs` | Rootless overlayfs for ephemeral sandbox worktrees |
| `libseccomp2` | seccomp-bpf runtime support |
| `libfuse3-3` | FUSE 3 runtime required by fuse-overlayfs |

These packages are always installed in the image but have **zero effect** unless `sandbox.enabled: true`.

### 2. Azure Bicep (infra/main.bicep)

New resources:
- **`forensicsShare`** — Azure File Share `squadron-forensics` (10 GB quota) for retaining abnormal-exit worktree snapshots
- **`envForensicsStorage`** — Container App Environment storage binding for the forensics share

New parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sandboxEnabled` | `bool` | `false` | Activates sandbox-specific resources/capabilities |
| `sandboxRetentionPath` | `string` | `/mnt/squadron-data/forensics` | Container mount path for forensic evidence |

When `sandboxEnabled = true`:
- `CAP_SYS_ADMIN` added to the container security context (required by `unshare`)
- `SQUADRON_SANDBOX_ENABLED=true` injected as environment variable
- `SQUADRON_SANDBOX_RETENTION_PATH` env var set
- Forensics volume mounted at `sandboxRetentionPath`

### 3. `.squadron/config.yaml`

Added `sandbox:` configuration block:

```yaml
sandbox:
  enabled: false
  max_tool_calls_per_session: 500
  timing_min_delay_ms: 50
  retention_path: /mnt/squadron-data/forensics
  forensic_retention_days: 1
```

### 4. CI workflow (`.github/workflows/ci.yml`)

Added **"Check sandbox system dependencies"** step to the `unit-tests` job.  
The step is informational (non-blocking) — it reports which sandbox tools are available in the runner environment.

---

## Activation procedure

### Step 1: Deploy updated infrastructure

```bash
az deployment group create \
  --resource-group <rg> \
  --template-file infra/main.bicep \
  --parameters \
    appName=<name> \
    ghcrImage=ghcr.io/nbaertsch/squadron:latest \
    githubAppId=<id> \
    githubInstallationId=<id> \
    githubPrivateKey='<pem>' \
    githubWebhookSecret='<secret>' \
    sandboxEnabled=true
```

> **Note**: `sandboxEnabled=true` adds `CAP_SYS_ADMIN` to the container.  
> Azure Container Apps supports this capability but it must be explicitly requested at deployment time.

### Step 2: Enable in config

Edit `.squadron/config.yaml`:

```yaml
sandbox:
  enabled: true   # ← change this
```

Sync to Azure File Share (GitHub Actions deploys this automatically on push to the default branch).

### Step 3: Verify

Check container logs for sandbox initialization:

```bash
az containerapp logs show \
  --name <app-name> \
  --resource-group <rg> \
  --follow \
  | grep -i sandbox
```

Expected log lines on healthy startup:
```
INFO squadron.sandbox.broker   AuthBroker initialized
INFO squadron.sandbox.manager  SandboxManager ready (namespace=linux, overlayfs=fuse)
```

---

## Rollback

To disable sandbox without redeploying:

1. Set `sandbox.enabled: false` in `.squadron/config.yaml`
2. Push to trigger GitHub Actions config sync

To fully remove sandbox capabilities from the container (revert to default capability set):

```bash
az deployment group create \
  ... \
  --parameters sandboxEnabled=false
```

---

## Capability requirements

| Feature | Linux capability | Required |
|---------|-----------------|---------|
| Namespace isolation (`unshare`) | `CAP_SYS_ADMIN` | Yes (when sandbox enabled) |
| seccomp-bpf filter | `PR_SET_NO_NEW_PRIVS` (unprivileged) | No |
| fuse-overlayfs | FUSE device access | Provided by Azure CApp environment |
| Audit log + token broker | None | No |

> **Security note**: `CAP_SYS_ADMIN` is broad. It is gated behind `sandboxEnabled=false` and should only be activated once the sandbox code has passed security review. The feature was designed with this operational gate in mind.

---

## Forensic evidence retention

Abnormal-exit sandbox sessions are preserved to `/mnt/squadron-data/forensics` (Azure File Share `squadron-forensics`).

Structure:
```
/mnt/squadron-data/forensics/
  <session-id>/
    worktree/          ← snapshot of the agent's working directory
    audit.ndjson       ← hash-chained tool call log
    metadata.json      ← session metadata (agent, issue, exit reason)
```

Retention is controlled by `sandbox.forensic_retention_days` (default: 1 day).  
The `SandboxManager` prunes evidence older than this threshold at session cleanup time.

Share quota: **10 GB** (accommodates ~10 retained worktree snapshots at ~1 GB each).
