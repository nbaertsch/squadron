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

# Install system dependencies:
#   git            — worktree operations
#   util-linux     — provides `unshare` for Linux namespace isolation (sandbox)
#   fuse-overlayfs — rootless overlayfs for ephemeral sandbox worktrees
#   libseccomp2    — seccomp-bpf runtime support (sandbox syscall filter)
#   libfuse3-3     — FUSE 3 runtime required by fuse-overlayfs
#
# Sandbox namespace isolation (unshare -p -n -m -i -u -f) requires the
# container to run with CAP_SYS_ADMIN.  When sandbox.enabled is false
# (the default) none of these extra capabilities are exercised.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        util-linux \
        fuse-overlayfs \
        libseccomp2 \
        libfuse3-3 \
    && rm -rf /var/lib/apt/lists/*

# Ensure the FUSE device is usable by the container user.
# This no-ops gracefully when the device is absent (non-sandbox deployments).
RUN chmod 666 /dev/fuse 2>/dev/null || true

# Copy venv from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Fix Copilot CLI binary permissions
RUN chmod +x /app/.venv/lib/python*/site-packages/copilot/bin/copilot || true

# Default port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Default: serve from /app (user images COPY .squadron/ into /app/)
# Can be overridden with --repo-root for volume-mount usage
ENTRYPOINT ["squadron", "serve"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
