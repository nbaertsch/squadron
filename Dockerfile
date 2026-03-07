# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project metadata first (cache layer)
COPY pyproject.toml README.md ./
COPY src/ src/

# Install dependencies (no dev deps in production)
RUN uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python -e .


# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
# git: needed for worktree operations
# ripgrep: fast code search used by agents (rg) - closes #98
# util-linux: unshare for namespace isolation (sandbox)
# iproute2: ip/veth for network namespace bridge (sandbox)
# iptables: traffic redirection to MitM proxy (sandbox)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        util-linux \
        iproute2 \
        iptables \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Fix Copilot CLI binary permissions
RUN chmod +x /app/.venv/lib/python*/site-packages/copilot/bin/copilot || true

# Pre-warm the Copilot CLI pkg asset extraction (fixes issue #164).
#
# The Copilot CLI is a pkg-bundled Node.js executable. On first run it
# extracts its bundled assets (ripgrep binary, pty.node native module,
# tree-sitter WASM, etc.) into $HOME/.copilot/pkg/linux-x64/<version>/.
#
# Without this step, each container start triggers a concurrent extraction
# race: multiple agent CLI subprocesses (PM + feat-dev + PR-review) all
# start simultaneously and each tries to extract to the same cache dir.
# The race can leave files mid-write, causing:
#   - bash tool: "Failed to start bash process" (pty.node not fully written)
#   - grep tool:  "spawn .../rg ENOENT"         (rg binary not fully written)
#
# Running the CLI once here bakes the fully-extracted pkg cache into the
# image layer, eliminating the extraction race entirely.
#
# --no-auto-update: prevents network calls for update checks during build.
# The CLI will exit non-zero (no auth token), but extraction completes first.
RUN /app/.venv/lib/python*/site-packages/copilot/bin/copilot --no-auto-update --help 2>/dev/null || \
    /app/.venv/lib/python*/site-packages/copilot/bin/copilot --no-auto-update 2>/dev/null || \
    true

# USE_BUILTIN_RIPGREP=false: instructs the Copilot CLI to use the system
# ripgrep binary (installed above via apt) rather than its bundled copy.
# This provides a belt-and-suspenders fallback: even if the bundled rg is
# somehow unavailable, the system rg at /usr/bin/rg will be used.
ENV USE_BUILTIN_RIPGREP=false

# Default port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Default: serve from /app (user images COPY .squadron/ into /app/)
# Can be overridden with --repo-root for volume-mount usage
ENTRYPOINT ["squadron", "serve"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
