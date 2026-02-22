# Adding New Squadron Tools

This guide walks through the steps to add a new custom Squadron tool.

## Overview

Squadron tools are custom functions that agents call via the LLM function-call mechanism.
They differ from SDK built-in tools (bash, read_file, grep) which are provided by the
Copilot SDK directly.

## Step 1: Define the Parameter Model

In `src/squadron/tools/squadron_tools.py`, add a Pydantic model for the tool's parameters.
Follow the existing patterns:

```python
class MyToolParams(BaseModel):
    """Parameters for my_tool."""
    required_field: str
    optional_field: int = 0
    optional_list: list[str] = Field(default_factory=list)
```

Place it near the other `*Params` models (around line 100-350 of `squadron_tools.py`).

## Step 2: Register the Tool Name

Add your tool name to `ALL_TOOL_NAMES` in `squadron_tools.py`:

```python
ALL_TOOL_NAMES = [
    # ... existing tools ...
    "my_tool",        # Add here in the appropriate section
]
```

This is critical — tools in `ALL_TOOL_NAMES_SET` are routed correctly vs SDK built-ins.

## Step 3: Implement the Tool Method

Add the method to the `SquadronTools` class in `squadron_tools.py`:

```python
async def my_tool(self, agent_id: str, params: MyToolParams) -> str:
    """Brief description of what this tool does.

    Returns a string that the agent sees as the tool's output.
    """
    # Log activity for audit trail
    await self._log_activity(
        agent_id=agent_id,
        action="my_tool",
        details={"field": params.required_field},
    )

    # Implement the logic
    try:
        result = await self.github.some_api_call(params.required_field)
        return f"Success: {result}"
    except Exception as e:
        logger.error("my_tool failed for agent %s: %s", agent_id, e)
        return f"Error: {e}"
```

**Convention:** Always return a string. The agent sees this as the tool's output.
For errors, return a string starting with "Error:" rather than raising exceptions.

## Step 4: Register in `get_tools()`

The `SquadronTools.get_tools()` method returns the tool list for a given agent.
Find the `get_tools()` method and ensure your new tool name is mapped to the
implementation method. The mapping uses the tool name to call `getattr(self, name)`.

Look for the tool dispatch mechanism — it likely uses `getattr` or a `_TOOL_MAP` dict.

## Step 5: Assign to Agents

Add the tool name to the `tools:` frontmatter in relevant agent `.md` files:

```yaml
# .squadron/agents/my-agent.md
---
name: my-agent
tools:
  - read_file
  - bash
  - my_tool      # ← Add here
  - report_complete
---
```

## Step 6: Write Tests

Add tests in `tests/test_squadron_tools.py` (or create a new test file):

```python
async def test_my_tool():
    """Test that my_tool works correctly."""
    # Set up mocks
    mock_github = AsyncMock(spec=GitHubClient)
    mock_github.some_api_call = AsyncMock(return_value="result")
    
    tools = SquadronTools(github=mock_github, ...)
    
    params = MyToolParams(required_field="test")
    result = await tools.my_tool("test-agent-id", params)
    
    assert "Success" in result
    mock_github.some_api_call.assert_called_once_with("test")
```

## Step 7: Verify

```bash
# Run ruff
ruff check src/squadron/tools/squadron_tools.py
ruff format src/squadron/tools/squadron_tools.py

# Run tests
python -m pytest tests/test_squadron_tools.py -v
```

## Important Notes

- **Tool names must be snake_case** (not camelCase)
- **Always add to `ALL_TOOL_NAMES`** — this is required for proper routing
- **Parameter models must be Pydantic BaseModel** — SDK validates params before calling
- **Return strings** — the SDK expects string output from tool calls
- **Use `self._log_activity`** for audit trails
- **Handle exceptions gracefully** — return error strings, don't let exceptions propagate
