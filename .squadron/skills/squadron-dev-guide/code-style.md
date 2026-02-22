# Code Style

## Tooling

Squadron uses **Ruff** for both linting and formatting.

**Config in `pyproject.toml`:**
```toml
[tool.ruff]
line-length = 100
target-version = "py311"
```

## Pre-commit

`.pre-commit-config.yaml` runs Ruff on every commit:
- `ruff check` — lint
- `ruff format` — format

**Before opening a PR, always run:**
```bash
ruff check src/ tests/
ruff format --check src/ tests/
```

**To auto-fix:**
```bash
ruff check --fix src/ tests/
ruff format src/ tests/
```

## Python Version

Target: **Python 3.11+**. Use:
- `str | None` instead of `Optional[str]`
- `dict[str, Any]` instead of `Dict[str, Any]`
- `list[str]` instead of `List[str]`
- `from __future__ import annotations` is present in most files for forward refs

## Import Ordering

Ruff enforces isort-compatible ordering:
1. Standard library
2. Third-party (pydantic, yaml, fastapi, etc.)
3. Local (`from squadron.models import ...`)

## Naming Conventions

- Classes: `PascalCase`
- Functions/methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE` (e.g. `ALL_TOOL_NAMES_SET`)
- Private methods: `_leading_underscore`
- Type aliases: `PascalCase` or `snake_case` (both used)

## Comments

- Write self-documenting code — avoid obvious comments
- Use comments to explain **why**, not what
- Issue references in comments: `# Fix for issue #42` or `# (issue #42)`
- Module docstrings at top of every file describe purpose and key exports

## Line Length

100 characters. Ruff enforces this. Long strings and log messages can use implicit
string concatenation or parentheses for continuation.
