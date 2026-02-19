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
# ripgrep: fast code search used by agents (rg)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

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
