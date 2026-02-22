# Testing Patterns

## Setup

**Pytest config** (`pyproject.toml`):
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`asyncio_mode = "auto"` means **all async test functions are automatically detected** —
no `@pytest.mark.asyncio` decorator needed.

## No Shared Conftest

Squadron tests do **not** use a shared `conftest.py`. Each test file is self-contained.
Fixtures are defined within the test file itself.

```python
# Within test_my_feature.py
@pytest.fixture
def squadron_dir(tmp_path: Path) -> Path:
    sq = tmp_path / ".squadron"
    sq.mkdir()
    # ... setup ...
    return sq
```

## Async Tests

```python
async def test_something():
    result = await my_async_function()
    assert result == expected
```

No decorator needed — `asyncio_mode = "auto"` handles it.

## Mocking CopilotAgent

`CopilotAgent` is mocked using `AsyncMock` and `MagicMock`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.fixture
def mock_copilot():
    copilot = MagicMock(spec=CopilotAgent)
    copilot.start = AsyncMock()
    copilot.stop = AsyncMock()
    copilot.create_session = AsyncMock(return_value=mock_session)
    copilot.resume_session = AsyncMock(return_value=mock_session)
    return copilot
```

## Patching

Use `unittest.mock.patch` as context manager or decorator:

```python
# As context manager
with patch("squadron.agent_manager.CopilotAgent") as mock_cls:
    mock_cls.return_value = mock_copilot
    # ... test code ...

# As decorator
@patch("squadron.copilot.CopilotClient")
async def test_create(mock_client_cls):
    mock_client = AsyncMock()
    mock_client_cls.return_value = mock_client
    # ... test code ...
```

## Fixture Patterns

### tmp_path (built-in pytest fixture)
Always use `tmp_path` for temporary files:
```python
def test_config_loading(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(yaml.dump({...}))
```

### Squadron dir fixture (common pattern)
```python
@pytest.fixture
def squadron_dir(tmp_path: Path) -> Path:
    sq = tmp_path / ".squadron"
    sq.mkdir()
    (sq / "config.yaml").write_text(yaml.dump(minimal_config))
    agents = sq / "agents"
    agents.mkdir()
    (agents / "pm.md").write_text("---\nname: pm\n---\nYou are PM.\n")
    return sq
```

## Test File Structure

```python
"""Tests for <module>."""

from pathlib import Path
import pytest
from squadron.config import load_config

# Fixtures
@pytest.fixture
def ...

# Test classes group related tests
class TestMyFeature:
    def test_happy_path(self, ...):
        ...
    
    def test_edge_case(self, ...):
        ...
    
    async def test_async_operation(self, ...):
        ...

# Or plain test functions for simple cases
def test_simple():
    assert 1 + 1 == 2
```

## Assertion Style

```python
# Direct assertions (no assert helpers needed)
assert result.name == "expected"
assert len(items) == 3
assert "key" in mapping
assert result is not None

# pytest.raises for exceptions
with pytest.raises(ValueError, match="expected error message"):
    parse_agent_definition("", invalid_content)
```
