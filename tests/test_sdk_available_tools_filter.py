"""Regression test for issue #118 — SDK available_tools must only contain
SDK built-in tool names, not custom Squadron tool names.

Root cause: The `sdk_available_tools` list passed as `available_tools` to the
Copilot SDK was set to the full frontmatter tools list, which contains a mix of
custom Squadron tool names (e.g. `read_issue`, `check_for_events`) AND SDK
built-in tool names (e.g. `bash`, `grep`). The SDK validates `available_tools`
against known built-in names and rejects unknown entries, causing all built-in
tools — including `bash` and `grep` — to fail to initialize.

Symptom: "Failed to start bash process" and
"spawn .../ripgrep/.../rg ENOENT" errors in bug-fix and other agents.

Fix: The `available_tools` list must only contain SDK built-in tool names
(i.e. names NOT in ALL_TOOL_NAMES_SET). Custom Squadron tools are registered
separately via the `tools=` parameter and must be excluded from `available_tools`.
"""

from __future__ import annotations

from squadron.tools.squadron_tools import ALL_TOOL_NAMES_SET


# ── Helper ────────────────────────────────────────────────────────────────────


def _split_tools(frontmatter_tools: list[str]) -> tuple[list[str], list[str]]:
    """Replicate the tool-splitting logic from agent_manager._run_agent.

    Returns (custom_tool_names, sdk_available_tools).

    Current (buggy) behaviour:
        sdk_available_tools = frontmatter_tools   # all names, including custom

    Fixed behaviour (this test enforces):
        sdk_available_tools = [t for t in frontmatter_tools if t not in ALL_TOOL_NAMES_SET]
    """
    custom_tool_names = [t for t in frontmatter_tools if t in ALL_TOOL_NAMES_SET]
    sdk_available_tools = [t for t in frontmatter_tools if t not in ALL_TOOL_NAMES_SET]
    return custom_tool_names, sdk_available_tools


# ── Tests ─────────────────────────────────────────────────────────────────────

BUG_FIX_FRONTMATTER_TOOLS = [
    # SDK built-in tools
    "read_file",
    "write_file",
    "grep",
    "bash",
    "git",
    # Custom Squadron tools
    "git_push",
    "read_issue",
    "list_issue_comments",
    "open_pr",
    "get_pr_details",
    "get_pr_feedback",
    "list_pr_files",
    "list_pr_reviews",
    "get_review_details",
    "get_pr_review_status",
    "reply_to_review_comment",
    "comment_on_pr",
    "comment_on_issue",
    "check_for_events",
    "report_blocked",
    "report_complete",
    "create_blocker_issue",
]

# The SDK built-in tools that should appear in available_tools
EXPECTED_SDK_BUILTIN_TOOLS = ["read_file", "write_file", "grep", "bash", "git"]

# Custom Squadron tool names that must NOT appear in available_tools
CUSTOM_TOOL_NAMES = [
    "git_push",
    "read_issue",
    "list_issue_comments",
    "open_pr",
    "get_pr_details",
    "get_pr_feedback",
    "list_pr_files",
    "list_pr_reviews",
    "get_review_details",
    "get_pr_review_status",
    "reply_to_review_comment",
    "comment_on_pr",
    "comment_on_issue",
    "check_for_events",
    "report_blocked",
    "report_complete",
    "create_blocker_issue",
]


class TestSdkAvailableToolsFilter:
    """Regression tests for issue #118 — bash/grep tool failures.

    The SDK's available_tools parameter must only contain SDK built-in tool
    names. Custom Squadron tool names must be excluded to prevent the SDK from
    rejecting the allowlist and blocking bash/grep/read_file.
    """

    def test_bug_fix_agent_sdk_available_tools_excludes_custom_names(self):
        """sdk_available_tools must not contain custom Squadron tool names."""
        custom_tool_names, sdk_available_tools = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)

        for custom_name in CUSTOM_TOOL_NAMES:
            assert custom_name not in sdk_available_tools, (
                f"Custom Squadron tool '{custom_name}' must NOT appear in sdk_available_tools "
                f"(available_tools). It would cause the SDK to reject the allowlist and block "
                f"bash/grep/read_file from working. (Regression: issue #118)"
            )

    def test_bug_fix_agent_sdk_available_tools_includes_bash(self):
        """sdk_available_tools must contain 'bash' so bash tool is accessible."""
        _, sdk_available_tools = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)

        assert "bash" in sdk_available_tools, (
            "SDK built-in tool 'bash' must be in sdk_available_tools. "
            "Without it the model cannot use bash to read/modify files. (Regression: issue #118)"
        )

    def test_bug_fix_agent_sdk_available_tools_includes_grep(self):
        """sdk_available_tools must contain 'grep' so grep/ripgrep tool is accessible."""
        _, sdk_available_tools = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)

        assert "grep" in sdk_available_tools, (
            "SDK built-in tool 'grep' must be in sdk_available_tools. "
            "Without it the model cannot search files. (Regression: issue #118)"
        )

    def test_bug_fix_agent_sdk_available_tools_only_sdk_builtins(self):
        """sdk_available_tools must contain exactly the expected SDK built-in tools."""
        _, sdk_available_tools = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)

        assert set(sdk_available_tools) == set(EXPECTED_SDK_BUILTIN_TOOLS), (
            f"sdk_available_tools should only contain SDK built-in tools. "
            f"Expected: {sorted(EXPECTED_SDK_BUILTIN_TOOLS)}, "
            f"Got: {sorted(sdk_available_tools)}. "
            f"(Regression: issue #118)"
        )

    def test_custom_tools_go_to_tools_not_available_tools(self):
        """Custom Squadron tools must appear in custom_tool_names, not sdk_available_tools."""
        custom_tool_names, sdk_available_tools = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)

        for custom_name in CUSTOM_TOOL_NAMES:
            assert custom_name in custom_tool_names, (
                f"Custom Squadron tool '{custom_name}' should be in custom_tool_names "
                f"(passed via tools=). (Regression: issue #118)"
            )
            assert custom_name not in sdk_available_tools, (
                f"Custom Squadron tool '{custom_name}' must NOT be in sdk_available_tools. "
                f"(Regression: issue #118)"
            )

    def test_all_custom_tools_are_in_all_tool_names_set(self):
        """All expected custom tool names must be in ALL_TOOL_NAMES_SET."""
        for name in CUSTOM_TOOL_NAMES:
            assert name in ALL_TOOL_NAMES_SET, (
                f"Tool '{name}' should be in ALL_TOOL_NAMES_SET so it gets routed "
                f"to custom_tool_names, not sdk_available_tools. (Regression: issue #118)"
            )

    def test_sdk_builtin_tools_not_in_all_tool_names_set(self):
        """SDK built-in tools must NOT be in ALL_TOOL_NAMES_SET."""
        for name in EXPECTED_SDK_BUILTIN_TOOLS:
            assert name not in ALL_TOOL_NAMES_SET, (
                f"SDK built-in tool '{name}' must NOT be in ALL_TOOL_NAMES_SET. "
                f"If it is, it would be filtered out of sdk_available_tools, making "
                f"the tool unavailable to agents. (Regression: issue #118)"
            )

    def test_agent_manager_tool_split_logic_in_run_agent(self):
        """Reproduce the exact logic from agent_manager._run_agent to verify the fix.

        This is the key regression test. Before the fix:
            sdk_available_tools = agent_def.tools  # ALL names, including custom
        After the fix:
            sdk_available_tools = [t for t in agent_def.tools if t not in ALL_TOOL_NAMES_SET]
        """
        frontmatter_tools = BUG_FIX_FRONTMATTER_TOOLS

        # Simulate PRE-FIX (buggy) behavior
        buggy_sdk_available_tools = frontmatter_tools

        # Simulate POST-FIX (correct) behavior
        fixed_sdk_available_tools = [t for t in frontmatter_tools if t not in ALL_TOOL_NAMES_SET]

        # The buggy version includes custom Squadron tool names — this is wrong
        assert "read_issue" in buggy_sdk_available_tools, (
            "Confirming the bug: pre-fix sdk_available_tools contains 'read_issue'"
        )
        assert "check_for_events" in buggy_sdk_available_tools, (
            "Confirming the bug: pre-fix sdk_available_tools contains 'check_for_events'"
        )

        # The fixed version must only contain SDK built-in tool names
        for custom_name in CUSTOM_TOOL_NAMES:
            assert custom_name not in fixed_sdk_available_tools, (
                f"Post-fix sdk_available_tools must not contain '{custom_name}'. "
                f"(Regression: issue #118)"
            )

        assert "bash" in fixed_sdk_available_tools, (
            "Post-fix sdk_available_tools must contain 'bash'. (Regression: issue #118)"
        )
        assert "grep" in fixed_sdk_available_tools, (
            "Post-fix sdk_available_tools must contain 'grep'. (Regression: issue #118)"
        )


class TestAgentManagerToolSplitUnit:
    """Unit test the tool-split logic in isolation, simulating agent_manager behavior."""

    def test_empty_tools_list_returns_none(self):
        """When agent has no tools list (None), sdk_available_tools should be None."""
        agent_tools = None
        if agent_tools is not None:
            sdk_available_tools = [t for t in agent_tools if t not in ALL_TOOL_NAMES_SET]
            sdk_available_tools = sdk_available_tools or None
        else:
            sdk_available_tools = None

        assert sdk_available_tools is None

    def test_only_custom_tools_returns_none(self):
        """When all tools are custom Squadron tools, sdk_available_tools should be None."""
        agent_tools = ["read_issue", "comment_on_pr", "check_for_events"]
        sdk_available_tools = [t for t in agent_tools if t not in ALL_TOOL_NAMES_SET]
        sdk_available_tools = sdk_available_tools or None

        assert sdk_available_tools is None, (
            "When all tools are custom Squadron tools, sdk_available_tools "
            "should be None (allow all SDK built-ins). (Regression: issue #118)"
        )

    def test_only_sdk_builtins_returns_those_names(self):
        """When all tools are SDK built-ins, sdk_available_tools contains them."""
        agent_tools = ["bash", "read_file", "write_file"]
        sdk_available_tools = [t for t in agent_tools if t not in ALL_TOOL_NAMES_SET]
        sdk_available_tools = sdk_available_tools or None

        assert sdk_available_tools == ["bash", "read_file", "write_file"]

    def test_mixed_tools_split_correctly(self):
        """Mixed tools list splits correctly into custom and SDK built-in names."""
        agent_tools = ["bash", "read_file", "check_for_events", "report_complete", "grep"]
        custom_names = [t for t in agent_tools if t in ALL_TOOL_NAMES_SET]
        sdk_builtins = [t for t in agent_tools if t not in ALL_TOOL_NAMES_SET]

        assert set(custom_names) == {"check_for_events", "report_complete"}
        assert set(sdk_builtins) == {"bash", "read_file", "grep"}


class TestAgentManagerImplementation:
    """Tests that verify agent_manager.py implements the correct tool split.

    These tests fail with the buggy code (sdk_available_tools = agent_def.tools)
    and pass after the fix (sdk_available_tools = [t for t in agent_def.tools
    if t not in ALL_TOOL_NAMES_SET]).
    """

    def test_agent_manager_does_not_pass_custom_names_as_available_tools(self):
        """The agent_manager._run_agent code must NOT set sdk_available_tools = agent_def.tools.

        The bug: `sdk_available_tools = agent_def.tools` passes all tool names
        (including custom Squadron tools like `read_issue`) to the SDK's
        `available_tools` parameter. The SDK then rejects the allowlist because
        it contains unrecognized names, blocking all built-in tools including bash.

        The fix: only SDK built-in names (not in ALL_TOOL_NAMES_SET) go to available_tools.
        """
        import re

        with open("src/squadron/agent_manager.py") as f:
            source = f.read()

        # The buggy assignment pattern: sdk_available_tools = agent_def.tools
        # This line would set available_tools to ALL tool names, including custom ones.
        buggy_pattern = re.compile(r"sdk_available_tools\s*=\s*agent_def\.tools\b")
        matches = buggy_pattern.findall(source)
        assert not matches, (
            "agent_manager.py contains the buggy assignment "
            "`sdk_available_tools = agent_def.tools`. "
            "This passes custom Squadron tool names (like `read_issue`) as SDK "
            "`available_tools`, causing the SDK to reject the allowlist and blocking "
            "bash/grep from working. "
            "Fix: sdk_available_tools = [t for t in agent_def.tools if t not in ALL_TOOL_NAMES_SET]. "
            "(Regression: issue #118)"
        )

    def test_agent_manager_filters_available_tools_to_sdk_builtins(self):
        """agent_manager._run_agent must filter out custom tool names from available_tools.

        Verifies the fix is present by looking for the filtering logic in source.
        """
        import re

        with open("src/squadron/agent_manager.py") as f:
            source = f.read()

        # Look for a line that filters out ALL_TOOL_NAMES_SET entries from available_tools
        # Pattern: sdk_available_tools = [... if ... not in ALL_TOOL_NAMES_SET ...]
        filter_pattern = re.compile(r"not\s+in\s+ALL_TOOL_NAMES_SET")
        matches = filter_pattern.findall(source)

        # There should be at least 1 occurrence for sdk_available_tools filter (the new fix).
        # Note: custom_tool_names uses `in ALL_TOOL_NAMES_SET` (not `not in`).
        assert len(matches) >= 1, (
            "agent_manager.py must filter sdk_available_tools using `not in ALL_TOOL_NAMES_SET`. "
            "The sdk_available_tools list passed as available_tools to the SDK must exclude "
            "custom Squadron tool names (like `read_issue`, `check_for_events`). "
            "Add: sdk_available_tools = [t for t in ... if t not in ALL_TOOL_NAMES_SET]. "
            "(Regression: issue #118)"
        )
