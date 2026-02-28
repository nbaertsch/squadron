"""Tests for SDK tool filtering via available_tools (allowlist).

The frontmatter `tools:` list in each agent's .md file is the single source
of truth for which tools that agent may use.  The full list (both custom
Squadron tool names AND SDK built-in names) is passed as `available_tools`
to the SDK, which forwards it as `availableTools` in session.create.

Custom tools are also registered via `tools=` (their definitions) so the
CLI knows how to dispatch them.

This is an allowlist approach: only tools named in frontmatter are visible
to the model.
"""

from __future__ import annotations

from squadron.tools.squadron_tools import ALL_TOOL_NAMES_SET


# ── Helper ────────────────────────────────────────────────────────────────────


def _split_tools(
    frontmatter_tools: list[str],
) -> tuple[list[str], list[str] | None]:
    """Replicate the tool-splitting logic from agent_manager._run_agent.

    Returns (custom_tool_names, sdk_available_tools).

    The logic:
      1. custom_tool_names = names in ALL_TOOL_NAMES_SET → passed as tools=
      2. sdk_available_tools = full frontmatter list → passed as available_tools=
    """
    custom_tool_names = [t for t in frontmatter_tools if t in ALL_TOOL_NAMES_SET]
    sdk_available_tools = list(frontmatter_tools) if frontmatter_tools else None
    return custom_tool_names, sdk_available_tools


# ── Test Data ─────────────────────────────────────────────────────────────────

BUG_FIX_FRONTMATTER_TOOLS = [
    # SDK built-in tools (all 5)
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

PR_REVIEW_FRONTMATTER_TOOLS = [
    # SDK built-in tools (only 2 — no bash, git, write_file)
    "read_file",
    "grep",
    # Custom Squadron tools (15)
    "list_pr_files",
    "get_pr_details",
    "get_pr_feedback",
    "get_ci_status",
    "list_pr_reviews",
    "get_review_details",
    "get_pr_review_status",
    "list_requested_reviewers",
    "add_pr_line_comment",
    "reply_to_review_comment",
    "comment_on_pr",
    "comment_on_issue",
    "submit_pr_review",
    "check_for_events",
    "report_complete",
]

# Known SDK built-in tool names (for test assertions only)
SDK_BUILTIN_NAMES = {"read_file", "write_file", "grep", "bash", "git"}

# Custom Squadron tool names that must appear in custom_tool_names
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


# ── Tool Name Classification Tests ──────────────────────────────────────────


class TestToolNameClassification:
    """Core invariants: custom tools are in ALL_TOOL_NAMES_SET, SDK builtins are not."""

    def test_all_custom_tools_are_in_all_tool_names_set(self):
        """All expected custom tool names must be in ALL_TOOL_NAMES_SET."""
        for name in CUSTOM_TOOL_NAMES:
            assert name in ALL_TOOL_NAMES_SET, (
                f"Tool '{name}' should be in ALL_TOOL_NAMES_SET so it gets routed "
                f"to custom_tool_names."
            )

    def test_sdk_builtin_tools_not_in_all_tool_names_set(self):
        """SDK built-in tools must NOT be in ALL_TOOL_NAMES_SET."""
        for name in SDK_BUILTIN_NAMES:
            assert name not in ALL_TOOL_NAMES_SET, (
                f"SDK built-in tool '{name}' must NOT be in ALL_TOOL_NAMES_SET. "
                f"If it is, it would be treated as a custom tool."
            )

    def test_custom_tools_go_to_custom_tool_names(self):
        """Custom Squadron tools must appear in custom_tool_names."""
        custom_tool_names, _ = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)
        for custom_name in CUSTOM_TOOL_NAMES:
            assert custom_name in custom_tool_names, (
                f"Custom Squadron tool '{custom_name}' should be in custom_tool_names "
                f"(passed via tools=)."
            )


# ── Available Tools (Allowlist) Tests ────────────────────────────────────────


class TestAvailableToolsAllowlist:
    """Tests for the available_tools allowlist approach.

    The frontmatter `tools:` list is passed in its entirety as
    available_tools to the SDK.  This is the single source of truth.
    """

    def test_available_tools_equals_frontmatter(self):
        """available_tools must be the full frontmatter list."""
        _, available = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)
        assert available == BUG_FIX_FRONTMATTER_TOOLS, (
            "available_tools must be the full frontmatter tools list."
        )

    def test_pr_review_available_tools_equals_frontmatter(self):
        """pr-review available_tools must be its full frontmatter list."""
        _, available = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        assert available == PR_REVIEW_FRONTMATTER_TOOLS

    def test_available_tools_includes_custom_names(self):
        """available_tools must include custom tool names (not filter them out)."""
        _, available = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        assert available is not None
        available_set = set(available)
        for name in ["submit_pr_review", "report_complete", "list_pr_files"]:
            assert name in available_set, (
                f"Custom tool '{name}' must be in available_tools so the CLI "
                f"includes it in the model's tool list."
            )

    def test_available_tools_includes_sdk_builtins(self):
        """available_tools must include SDK builtin names from frontmatter."""
        _, available = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        assert available is not None
        available_set = set(available)
        assert "read_file" in available_set
        assert "grep" in available_set

    def test_available_tools_excludes_unlisted_builtins(self):
        """SDK builtins NOT in frontmatter must NOT be in available_tools."""
        _, available = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        assert available is not None
        available_set = set(available)
        # pr-review only lists read_file and grep — no bash/git/write_file
        assert "bash" not in available_set
        assert "git" not in available_set
        assert "write_file" not in available_set

    def test_none_tools_means_no_filtering(self):
        """When agent_def.tools is None, available_tools is None (all visible)."""
        agent_tools: list[str] | None = None
        if agent_tools is not None:
            sdk_available = list(agent_tools) if agent_tools else None
        else:
            sdk_available = None
        assert sdk_available is None

    def test_empty_tools_gives_none(self):
        """Empty frontmatter tools list → available_tools is None."""
        _, available = _split_tools([])
        assert available is None, "Empty frontmatter should produce None (no filtering)."


# ── Review Agent Stall Regression Tests ──────────────────────────────────────


class TestReviewAgentStallRegression:
    """Regression tests for the review agent stall bug.

    Root cause: the old available_tools=["read_file","grep"] (SDK builtins only)
    acted as a global whitelist that hid all 15 custom Squadron tools from the
    model.  The model could only use grep and read_file, could not call
    submit_pr_review or report_complete, and stalled as a zombie.

    Fix: Pass the FULL frontmatter list (including custom tool names) as
    available_tools.  The CLI sees custom tools both in the tool definitions
    (registered via tools=) and in availableTools, so they remain visible.
    """

    def test_pr_review_custom_tools_in_available_tools(self):
        """pr-review's custom tools must be in available_tools.

        This is the PRIMARY regression test.  The old code filtered custom
        names out of available_tools — the new code includes them.
        """
        custom_tools, available = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)

        # All 15 custom tools must be in custom_tool_names (registered via tools=)
        expected_custom = {
            "list_pr_files",
            "get_pr_details",
            "get_pr_feedback",
            "get_ci_status",
            "list_pr_reviews",
            "get_review_details",
            "get_pr_review_status",
            "list_requested_reviewers",
            "add_pr_line_comment",
            "reply_to_review_comment",
            "comment_on_pr",
            "comment_on_issue",
            "submit_pr_review",
            "check_for_events",
            "report_complete",
        }
        assert set(custom_tools) == expected_custom, (
            f"All 15 custom tools must be in custom_tool_names. "
            f"Missing: {expected_custom - set(custom_tools)}"
        )

        # Critical: available_tools must also include custom tool names
        assert available is not None
        available_set = set(available)
        for name in expected_custom:
            assert name in available_set, (
                f"Custom tool '{name}' must be in available_tools so the model "
                f"can see it. (Regression: review agent stall)"
            )

    def test_pr_review_gets_grep_and_read_file(self):
        """pr-review must have grep and read_file in available_tools."""
        _, available = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        assert available is not None
        available_set = set(available)
        assert "grep" in available_set, "grep must be available to pr-review"
        assert "read_file" in available_set, "read_file must be available to pr-review"

    def test_pr_review_no_bash_git_write(self):
        """pr-review must NOT have bash, git, or write_file (security constraint)."""
        _, available = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        assert available is not None
        available_set = set(available)
        assert "bash" not in available_set, "bash should not be available for pr-review"
        assert "git" not in available_set, "git should not be available for pr-review"
        assert "write_file" not in available_set, "write_file should not be available for pr-review"

    def test_available_tools_used_in_agent_manager(self):
        """agent_manager must pass available_tools=sdk_available_tools."""
        import re

        with open("src/squadron/agent_manager.py") as f:
            source = f.read()

        pattern = re.compile(r"available_tools\s*=\s*sdk_available_tools")
        matches = pattern.findall(source)
        assert len(matches) >= 1, (
            "agent_manager.py must pass available_tools=sdk_available_tools to "
            "build_session_config and/or build_resume_config."
        )

    def test_excluded_tools_not_used_in_agent_manager(self):
        """agent_manager must NOT pass excluded_tools (no deny-list pattern)."""
        import re

        with open("src/squadron/agent_manager.py") as f:
            source = f.read()

        # Must NOT have excluded_tools=sdk_excluded_tools
        buggy_pattern = re.compile(r"excluded_tools\s*=\s*sdk_excluded_tools")
        matches = buggy_pattern.findall(source)
        assert not matches, (
            "agent_manager.py still passes excluded_tools=sdk_excluded_tools. "
            "This is the deny-list approach — we must use available_tools instead."
        )


# ── Unit Tests (tool-split logic in isolation) ───────────────────────────────


class TestToolSplitUnit:
    """Unit test the tool-split logic in isolation."""

    def test_empty_frontmatter_gives_none_available(self):
        """Empty frontmatter tools list → available_tools is None."""
        _, available = _split_tools([])
        assert available is None

    def test_unknown_tool_name_included_in_available(self):
        """An unknown tool name (neither custom nor known SDK builtin) should
        still appear in available_tools — we pass through the full list."""
        tools = ["read_file", "some_future_sdk_tool", "report_complete"]
        custom, available = _split_tools(tools)

        # "some_future_sdk_tool" is not in ALL_TOOL_NAMES_SET, so NOT custom
        assert "some_future_sdk_tool" not in custom
        # But it IS in available_tools (full frontmatter list)
        assert available is not None
        assert "some_future_sdk_tool" in available

    def test_mixed_tools_split_correctly(self):
        """Mixed tools list splits correctly into custom and available."""
        agent_tools = ["bash", "read_file", "check_for_events", "report_complete", "grep"]
        custom, available = _split_tools(agent_tools)

        assert set(custom) == {"check_for_events", "report_complete"}
        # available_tools is the full list
        assert available is not None
        assert set(available) == {
            "bash",
            "read_file",
            "check_for_events",
            "report_complete",
            "grep",
        }

    def test_only_custom_tools(self):
        """When only custom tools listed, available_tools still includes them."""
        tools = ["read_issue", "comment_on_pr", "check_for_events"]
        custom, available = _split_tools(tools)

        assert set(custom) == {"read_issue", "comment_on_pr", "check_for_events"}
        assert available is not None
        assert set(available) == {"read_issue", "comment_on_pr", "check_for_events"}

    def test_only_sdk_builtins(self):
        """When only SDK builtins listed, custom_tool_names is empty."""
        tools = ["read_file", "bash", "grep"]
        custom, available = _split_tools(tools)

        assert custom == []
        assert available is not None
        assert set(available) == {"read_file", "bash", "grep"}

    def test_all_tools_listed(self):
        """When all tools are listed, everything appears correctly."""
        custom, available = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)

        # All custom tools present
        for name in CUSTOM_TOOL_NAMES:
            assert name in custom
        # available_tools = full list
        assert available is not None
        assert set(available) == set(BUG_FIX_FRONTMATTER_TOOLS)

    def test_single_custom_tool(self):
        """Single custom tool in frontmatter."""
        tools = ["report_complete"]
        custom, available = _split_tools(tools)
        assert custom == ["report_complete"]
        assert available == ["report_complete"]

    def test_single_sdk_builtin(self):
        """Single SDK builtin in frontmatter."""
        tools = ["bash"]
        custom, available = _split_tools(tools)
        assert custom == []
        assert available == ["bash"]
