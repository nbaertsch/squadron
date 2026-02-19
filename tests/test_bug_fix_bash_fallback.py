"""Regression test for issue #81.

Bug: bug-fix agent escalates when bash environment is unavailable instead of
working around it.

The agent's instructions should explicitly guide it to continue working
without bash — using read tools, writing file changes, and submitting PRs —
rather than escalating with report_blocked due to a tooling issue.
"""


def _load_bug_fix_config():
    with open(".squadron/agents/bug-fix.md", "r") as f:
        return f.read()


def test_bug_fix_agent_treats_bash_as_optional():
    """Regression test for #81: bash should be optional, not a hard prerequisite."""
    config = _load_bug_fix_config()

    # The instructions should explicitly state bash is optional or a fallback exists
    bash_optional_indicators = [
        "optional",
        "if bash is unavailable",
        "without bash",
        "bash is not available",
        "bash unavailable",
        "fallback",
    ]
    has_bash_optional = any(indicator in config.lower() for indicator in bash_optional_indicators)

    assert has_bash_optional, (
        "Bug-fix agent instructions should treat bash as optional and provide a "
        "fallback path when it is unavailable. "
        f"None of {bash_optional_indicators} found in instructions."
    )


def test_bug_fix_agent_does_not_escalate_for_tooling_issues():
    """Regression test for #81: report_blocked should not be used for missing tools."""
    config = _load_bug_fix_config()

    # The instructions should clarify that report_blocked is for human judgment,
    # not for missing tooling
    human_judgment_indicators = [
        "human judgment",
        "human decision",
        "not for tooling",
        "not for missing tool",
        "not because of missing tools",
        "tool unavailability",
        "tooling issue",
    ]
    has_human_judgment_guidance = any(
        indicator in config.lower() for indicator in human_judgment_indicators
    )

    assert has_human_judgment_guidance, (
        "Bug-fix agent instructions should clarify that report_blocked is for "
        "human judgment calls, not for tooling/infrastructure unavailability. "
        f"None of {human_judgment_indicators} found in instructions."
    )


def test_bug_fix_agent_can_submit_pr_without_bash():
    """Regression test for #81: agent should be able to submit PRs without bash."""
    config = _load_bug_fix_config()

    # The workflow should mention that code editing + PR submission is possible without bash
    pr_without_bash_indicators = [
        "without running",
        "can still",
        "primary output",
        "code editing",
        "file changes",
        "direct edit",
        "read tools",
        "grep",
    ]
    has_pr_without_bash = any(indicator in config.lower() for indicator in pr_without_bash_indicators)

    assert has_pr_without_bash, (
        "Bug-fix agent instructions should indicate that code analysis, editing, "
        "and PR submission are possible without bash execution. "
        f"None of {pr_without_bash_indicators} found in instructions."
    )


def test_bug_fix_agent_has_bash_fallback_in_verify_step():
    """Regression test for #81: the 'Verify' step must have a bash-free fallback."""
    config = _load_bug_fix_config()

    # The verify/test step should acknowledge when tests can't be run locally
    verify_fallback_indicators = [
        "if bash is unavailable",
        "cannot run tests",
        "skip running",
        "without running tests",
        "bash is not available",
        "bash unavailable",
        "unable to run",
        "note in the pr",
        "flag in the pr",
        "ci will",
        "ci/cd",
    ]
    has_verify_fallback = any(indicator in config.lower() for indicator in verify_fallback_indicators)

    assert has_verify_fallback, (
        "Bug-fix agent instructions should provide a fallback in the verify/test "
        "step for when bash is unavailable (e.g., rely on CI, note it in the PR). "
        f"None of {verify_fallback_indicators} found in instructions."
    )
