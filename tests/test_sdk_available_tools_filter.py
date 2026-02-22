"""Regression tests for SDK tool filtering.

Issue #118: SDK available_tools must only contain SDK built-in tool names,
not custom Squadron tool names.  The SDK validates available_tools against
known builtins and rejects unknown entries, blocking all built-in tools.

Issue #118 regression (review-agent stall): Using available_tools as a
whitelist hides custom Squadron tools from the model entirely.  The fix
switches to excluded_tools (deny-list) so custom tools registered via
tools= remain visible to the model while unwanted SDK builtins are blocked.

Fix: Compute sdk_excluded_tools = SDK_BUILTIN_TOOLS - frontmatter_sdk_builtins,
pass as excluded_tools instead of available_tools.
"""

from __future__ import annotations

from squadron.tools.squadron_tools import ALL_TOOL_NAMES_SET, SDK_BUILTIN_TOOLS


# ── Helper ────────────────────────────────────────────────────────────────────


def _split_tools(
    frontmatter_tools: list[str],
) -> tuple[list[str], list[str] | None]:
    """Replicate the tool-splitting logic from agent_manager._run_agent.

    Returns (custom_tool_names, sdk_excluded_tools).

    The logic:
      1. custom_tool_names = names in ALL_TOOL_NAMES_SET → passed as tools=
      2. frontmatter_sdk_builtins = names NOT in ALL_TOOL_NAMES_SET
      3. sdk_excluded_tools = SDK_BUILTIN_TOOLS - frontmatter_sdk_builtins
         (i.e. block SDK builtins the agent shouldn't have)
    """
    custom_tool_names = [t for t in frontmatter_tools if t in ALL_TOOL_NAMES_SET]
    frontmatter_sdk_builtins = {t for t in frontmatter_tools if t not in ALL_TOOL_NAMES_SET}
    sdk_excluded_tools = sorted(SDK_BUILTIN_TOOLS - frontmatter_sdk_builtins) or None
    return custom_tool_names, sdk_excluded_tools


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


# ── Issue #118 Core Tests (tool name classification) ─────────────────────────


class TestToolNameClassification:
    """Core invariants: custom tools are in ALL_TOOL_NAMES_SET, SDK builtins are not."""

    def test_all_custom_tools_are_in_all_tool_names_set(self):
        """All expected custom tool names must be in ALL_TOOL_NAMES_SET."""
        for name in CUSTOM_TOOL_NAMES:
            assert name in ALL_TOOL_NAMES_SET, (
                f"Tool '{name}' should be in ALL_TOOL_NAMES_SET so it gets routed "
                f"to custom_tool_names. (Regression: issue #118)"
            )

    def test_sdk_builtin_tools_not_in_all_tool_names_set(self):
        """SDK built-in tools must NOT be in ALL_TOOL_NAMES_SET."""
        for name in EXPECTED_SDK_BUILTIN_TOOLS:
            assert name not in ALL_TOOL_NAMES_SET, (
                f"SDK built-in tool '{name}' must NOT be in ALL_TOOL_NAMES_SET. "
                f"If it is, it would be treated as a custom tool. (Regression: issue #118)"
            )

    def test_sdk_builtin_tools_constant_matches_expected(self):
        """SDK_BUILTIN_TOOLS constant must match expected set."""
        assert SDK_BUILTIN_TOOLS == frozenset(EXPECTED_SDK_BUILTIN_TOOLS)

    def test_custom_tools_go_to_custom_tool_names(self):
        """Custom Squadron tools must appear in custom_tool_names."""
        custom_tool_names, _ = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)
        for custom_name in CUSTOM_TOOL_NAMES:
            assert custom_name in custom_tool_names, (
                f"Custom Squadron tool '{custom_name}' should be in custom_tool_names "
                f"(passed via tools=). (Regression: issue #118)"
            )


# ── Excluded Tools Tests (deny-list approach) ────────────────────────────────


class TestExcludedToolsLogic:
    """Tests for the excluded_tools deny-list approach.

    When an agent's frontmatter lists a subset of SDK builtins, the
    complement (SDK builtins NOT in frontmatter) should be passed as
    excluded_tools.  This blocks unwanted SDK builtins without hiding
    custom tools from the model.
    """

    def test_all_sdk_builtins_listed_means_no_exclusions(self):
        """When all 5 SDK builtins are in frontmatter, excluded_tools is None."""
        _, sdk_excluded = _split_tools(BUG_FIX_FRONTMATTER_TOOLS)
        assert sdk_excluded is None, (
            "When all SDK builtins are in frontmatter, excluded_tools should be "
            "None (no exclusions needed)."
        )

    def test_partial_sdk_builtins_excludes_complement(self):
        """When only read_file + grep are listed, bash/git/write_file are excluded."""
        _, sdk_excluded = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        assert sdk_excluded is not None
        assert set(sdk_excluded) == {"bash", "git", "write_file"}, (
            f"pr-review lists only read_file + grep, so bash/git/write_file "
            f"should be excluded. Got: {sdk_excluded}"
        )

    def test_no_sdk_builtins_excludes_all(self):
        """When only custom tools listed, all SDK builtins are excluded."""
        only_custom = ["read_issue", "comment_on_pr", "check_for_events"]
        _, sdk_excluded = _split_tools(only_custom)
        assert sdk_excluded is not None
        assert set(sdk_excluded) == SDK_BUILTIN_TOOLS, (
            "When no SDK builtins in frontmatter, all should be excluded."
        )

    def test_single_sdk_builtin_excludes_rest(self):
        """When only 'bash' is listed, the other 4 SDK builtins are excluded."""
        tools = ["bash", "report_complete"]
        _, sdk_excluded = _split_tools(tools)
        assert sdk_excluded is not None
        assert set(sdk_excluded) == {"read_file", "write_file", "grep", "git"}

    def test_excluded_tools_are_sorted(self):
        """Excluded tools list should be deterministically sorted."""
        _, sdk_excluded = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        assert sdk_excluded is not None
        assert sdk_excluded == sorted(sdk_excluded), (
            "excluded_tools should be sorted for deterministic config."
        )

    def test_none_tools_means_no_exclusions(self):
        """When agent_def.tools is None, no exclusions (all visible)."""
        # Simulates the None branch in agent_manager
        agent_tools: list[str] | None = None
        if agent_tools is not None:
            frontmatter_sdk = {t for t in agent_tools if t not in ALL_TOOL_NAMES_SET}
            sdk_excluded = sorted(SDK_BUILTIN_TOOLS - frontmatter_sdk) or None
        else:
            sdk_excluded = None
        assert sdk_excluded is None


# ── Review Agent Stall Regression Tests ──────────────────────────────────────


class TestReviewAgentStallRegression:
    """Regression tests for the review agent stall bug.

    Root cause: available_tools=["read_file","grep"] acted as a global
    whitelist that hid all 15 custom Squadron tools from the model.
    The model could only use grep and read_file, could not call
    submit_pr_review or report_complete, and stalled as a zombie.

    Fix: Use excluded_tools instead of available_tools so custom tools
    registered via tools= remain visible.
    """

    def test_pr_review_custom_tools_not_hidden(self):
        """pr-review agent's custom tools must not be blocked by SDK tool filtering.

        This is the PRIMARY regression test.  The old available_tools=["read_file","grep"]
        approach hid all 15 custom tools.  The new excluded_tools approach must NOT
        interfere with custom tools.
        """
        custom_tools, sdk_excluded = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)

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

        # Critical: excluded_tools must NOT contain any custom tool names
        if sdk_excluded:
            for tool in sdk_excluded:
                assert tool not in ALL_TOOL_NAMES_SET, (
                    f"excluded_tools contains custom tool '{tool}' — this would "
                    f"break custom tools. excluded_tools must only contain SDK builtins."
                )

    def test_pr_review_gets_grep_and_read_file(self):
        """pr-review must have grep and read_file available (not excluded)."""
        _, sdk_excluded = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        excluded_set = set(sdk_excluded) if sdk_excluded else set()

        assert "grep" not in excluded_set, "grep must be available to pr-review"
        assert "read_file" not in excluded_set, "read_file must be available to pr-review"

    def test_pr_review_no_bash_git_write(self):
        """pr-review must NOT have bash, git, or write_file (security constraint)."""
        _, sdk_excluded = _split_tools(PR_REVIEW_FRONTMATTER_TOOLS)
        excluded_set = set(sdk_excluded) if sdk_excluded else set()

        assert "bash" in excluded_set, "bash should be excluded for pr-review"
        assert "git" in excluded_set, "git should be excluded for pr-review"
        assert "write_file" in excluded_set, "write_file should be excluded for pr-review"

    def test_available_tools_not_used_in_agent_manager(self):
        """agent_manager must NOT pass available_tools — it must use excluded_tools.

        available_tools acts as a global whitelist that hides custom tools.
        """
        import re

        with open("src/squadron/agent_manager.py") as f:
            source = f.read()

        # Must NOT have available_tools=sdk_available_tools or similar
        buggy_pattern = re.compile(r"available_tools\s*=\s*sdk_available_tools")
        matches = buggy_pattern.findall(source)
        assert not matches, (
            "agent_manager.py still passes available_tools=sdk_available_tools. "
            "This hides custom tools from the model. Use excluded_tools instead. "
            "(Regression: review agent stall)"
        )

    def test_excluded_tools_used_in_agent_manager(self):
        """agent_manager must pass excluded_tools=sdk_excluded_tools."""
        import re

        with open("src/squadron/agent_manager.py") as f:
            source = f.read()

        pattern = re.compile(r"excluded_tools\s*=\s*sdk_excluded_tools")
        matches = pattern.findall(source)
        assert len(matches) >= 1, (
            "agent_manager.py must pass excluded_tools=sdk_excluded_tools to "
            "build_session_config and/or build_resume_config. "
            "(Regression: review agent stall)"
        )

    def test_sdk_builtin_tools_constant_imported(self):
        """agent_manager must import SDK_BUILTIN_TOOLS for deny-list computation."""
        with open("src/squadron/agent_manager.py") as f:
            source = f.read()

        assert "SDK_BUILTIN_TOOLS" in source, (
            "agent_manager.py must import SDK_BUILTIN_TOOLS from squadron_tools "
            "to compute the excluded_tools deny-list."
        )


# ── Unit Tests (tool-split logic in isolation) ───────────────────────────────


class TestToolSplitUnit:
    """Unit test the tool-split logic in isolation."""

    def test_empty_frontmatter_excludes_all_builtins(self):
        """Empty frontmatter tools list → all SDK builtins excluded."""
        _, sdk_excluded = _split_tools([])
        assert sdk_excluded is not None
        assert set(sdk_excluded) == SDK_BUILTIN_TOOLS

    def test_unknown_tool_name_not_treated_as_custom(self):
        """An unknown tool name (neither custom nor known SDK builtin) is treated
        as an SDK builtin, so it goes to frontmatter_sdk_builtins and is NOT
        excluded."""
        tools = ["read_file", "some_future_sdk_tool", "report_complete"]
        custom, sdk_excluded = _split_tools(tools)

        # "some_future_sdk_tool" is not in ALL_TOOL_NAMES_SET, so it's treated
        # as an SDK builtin. It should not appear in custom_tool_names.
        assert "some_future_sdk_tool" not in custom
        # It also should not appear in sdk_excluded (it's in frontmatter)
        excluded_set = set(sdk_excluded) if sdk_excluded else set()
        assert "some_future_sdk_tool" not in excluded_set

    def test_mixed_tools_split_correctly(self):
        """Mixed tools list splits correctly into custom and excluded."""
        agent_tools = ["bash", "read_file", "check_for_events", "report_complete", "grep"]
        custom, sdk_excluded = _split_tools(agent_tools)

        assert set(custom) == {"check_for_events", "report_complete"}
        # bash, read_file, grep are in frontmatter → not excluded
        # write_file, git are NOT in frontmatter → excluded
        assert sdk_excluded is not None
        assert set(sdk_excluded) == {"write_file", "git"}
