"""Regression tests for issue #139: PR-Reviewer agent cannot request changes on
PRs opened by squadron-dev[bot].

GitHub rejects REQUEST_CHANGES reviews where the reviewer is the same user as
the PR author. When this happens (HTTP 403), the framework must:

1. Fall back gracefully — apply a ``needs-changes`` label on the PR so external
   tooling (branch protection rules, pipelines) can still gate the merge.
2. Record ``changes_requested`` state in the internal pr_approvals database so
   the Squadron auto-merge path is also blocked.
3. Return a descriptive message to the agent explaining exactly what happened
   and confirming the label was applied.

Without the fix the 403 handler simply told the agent "use comment_on_pr
instead", leaving both the GitHub label and the internal approval table
untouched — meaning blocking issues could not prevent a merge.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest_asyncio

from squadron.registry import AgentRegistry
from squadron.tools.squadron_tools import SquadronTools, SubmitPRReviewParams
from squadron.models import AgentRecord, AgentStatus


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_bot_authored.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


def _make_github_mock():
    github = AsyncMock()
    github.add_labels = AsyncMock()
    github.comment_on_pr = AsyncMock(return_value={"id": 1})
    github.ensure_labels_exist = AsyncMock()
    return github


def _make_403_error(message: str = "Validation Failed") -> httpx.HTTPStatusError:
    """Build a fake 403 HTTPStatusError like GitHub would return."""
    request = MagicMock(spec=httpx.Request)
    response = MagicMock(spec=httpx.Response)
    response.status_code = 403
    response.text = message
    return httpx.HTTPStatusError(message, request=request, response=response)


def _make_tools(registry, github) -> SquadronTools:
    return SquadronTools(
        registry=registry,
        github=github,
        agent_inboxes={},
        owner="testowner",
        repo="testrepo",
    )


async def _register_agent(registry: AgentRegistry, pr_number: int = 42) -> AgentRecord:
    agent = AgentRecord(
        agent_id="pr-review-issue-99",
        role="pr-review",
        issue_number=99,
        pr_number=pr_number,
        status=AgentStatus.ACTIVE,
    )
    await registry.create_agent(agent)
    return agent


# ── tests ─────────────────────────────────────────────────────────────────────


class TestRequestChangesFallbackOnBotAuthoredPR:
    """When REQUEST_CHANGES is rejected with 403, the fallback must be applied."""

    async def test_needs_changes_label_applied_on_403(self, registry):
        """Regression for #139: 'needs-changes' label is applied to the PR
        when GitHub rejects REQUEST_CHANGES with 403."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)

        await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Critical bug in auth logic — must be fixed before merge.",
                event="REQUEST_CHANGES",
            ),
        )

        # Label must have been applied to the PR
        github.add_labels.assert_called_once()
        call_args = github.add_labels.call_args
        # add_labels(owner, repo, issue_number, labels)
        labels_applied = (
            call_args.args[3] if len(call_args.args) >= 4 else call_args.kwargs.get("labels", [])
        )
        assert "needs-changes" in labels_applied, (
            "The 'needs-changes' label must be applied to the PR when REQUEST_CHANGES "
            "is rejected because the reviewer is the PR author (issue #139). "
            f"Labels applied: {labels_applied}"
        )

    async def test_internal_changes_requested_recorded_on_403(self, registry):
        """Regression for #139: 'changes_requested' is recorded in the internal
        pr_approvals table even when GitHub rejects the review with 403.

        This ensures the Squadron auto-merge path is still blocked."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)

        # Set up a review requirement so check_pr_merge_ready can evaluate
        await registry.set_pr_requirements(42, [{"role": "pr-review", "count": 1}])

        await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Please fix the critical security issue before merging.",
                event="REQUEST_CHANGES",
            ),
        )

        # Internal approval record must exist
        approvals = await registry.get_pr_approvals(42)
        changes_requested = [a for a in approvals if a["state"] == "changes_requested"]
        assert len(changes_requested) >= 1, (
            "A 'changes_requested' record must be written to pr_approvals even when "
            "GitHub rejects the review (issue #139). Without it, auto-merge is not blocked."
        )

    async def test_merge_blocked_after_403_fallback(self, registry):
        """Regression for #139: check_pr_merge_ready returns False (blocked)
        after the 403 fallback records changes_requested internally."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)
        await registry.set_pr_requirements(42, [{"role": "pr-review", "count": 1}])

        await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Security vulnerability — changes required.",
                event="REQUEST_CHANGES",
            ),
        )

        is_ready, missing = await registry.check_pr_merge_ready(42)
        assert not is_ready, (
            "check_pr_merge_ready must return False (blocked) after the 403 fallback "
            "records changes_requested (issue #139)"
        )
        assert any("changes requested" in m.lower() for m in missing), (
            f"Missing reasons should mention 'changes requested', got: {missing}"
        )

    async def test_return_message_describes_fallback(self, registry):
        """The return value must clearly explain that the label fallback was used."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)

        result = await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Style issues — please address before merging.",
                event="REQUEST_CHANGES",
            ),
        )

        # Message must mention the label fallback
        assert "needs-changes" in result.lower(), (
            f"Return message must mention 'needs-changes' label. Got: {result!r}"
        )
        assert "label" in result.lower(), (
            f"Return message must mention that a label was applied. Got: {result!r}"
        )
        # Must instruct agent to notify author (not falsely claim it already happened)
        assert "you should notify" in result.lower() or "comment_on_pr" in result.lower(), (
            f"Return message must instruct agent to notify author. Got: {result!r}"
        )

    async def test_non_403_errors_not_affected(self, registry):
        """Non-403 errors (422, 500) are NOT silently caught by the 403 fallback."""
        github = _make_github_mock()
        request = MagicMock(spec=httpx.Request)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 422
        response.text = "Validation Failed"
        github.submit_pr_review = AsyncMock(
            side_effect=httpx.HTTPStatusError("422", request=request, response=response)
        )

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)

        result = await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Issues found.",
                event="REQUEST_CHANGES",
            ),
        )

        # Label must NOT be applied for non-403 errors
        github.add_labels.assert_not_called()
        # Message should describe the 422 error
        assert "422" in result or "unprocessable" in result.lower(), (
            f"422 error path should mention '422', got: {result!r}"
        )

    async def test_approve_event_403_does_not_apply_needs_changes_label(self, registry):
        """APPROVE events that fail with 403 don't incorrectly apply needs-changes label."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)

        await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Looks good.",
                event="APPROVE",
            ),
        )

        # needs-changes label must NOT be applied for failed APPROVE reviews.
        # Use assert_not_called() — the weak `if .called:` guard would silently
        # pass even if the code erroneously skips add_labels for the wrong reason.
        github.add_labels.assert_not_called()
        github.ensure_labels_exist.assert_not_called()


class TestRequestChangesFallbackPartialFailure:
    """Partial-failure scenarios: one or more fallback steps fail."""

    async def test_label_fails_db_succeeds(self, registry):
        """When add_labels raises, the return message must NOT claim that the
        'needs-changes' label will prevent auto-merge, but SHOULD mention the
        internal DB recording."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())
        github.add_labels = AsyncMock(side_effect=RuntimeError("label API down"))

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)

        result = await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Critical bug found.",
                event="REQUEST_CHANGES",
            ),
        )

        # The return message must NOT claim the label blocks merges
        assert "label will prevent auto-merge" not in result.lower(), (
            f"Return message falsely claims label blocks merges when label "
            f"application failed. Got: {result!r}"
        )
        # The DB fallback should still have succeeded
        assert "recorded changes_requested in internal db" in result.lower(), (
            f"Return message should mention DB recording succeeded. Got: {result!r}"
        )
        # Internal DB should still have the record
        approvals = await registry.get_pr_approvals(42)
        changes_requested = [a for a in approvals if a["state"] == "changes_requested"]
        assert len(changes_requested) >= 1, (
            "DB recording must succeed even when label application fails."
        )

    async def test_both_label_and_db_fail(self, registry):
        """When both add_labels AND record_pr_approval fail, the return message
        must say 'no fallback actions succeeded' and NOT claim merge is blocked."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())
        github.add_labels = AsyncMock(side_effect=RuntimeError("label API down"))

        tools = _make_tools(registry, github)
        # Do NOT register the agent — get_agent will return None, so
        # record_pr_approval is never called (the `if agent:` guard skips it).
        # This simulates "both fallback paths produce no result".

        result = await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Critical bug found.",
                event="REQUEST_CHANGES",
            ),
        )

        # Should indicate no fallback actions succeeded
        assert "no fallback actions succeeded" in result.lower(), (
            f"Return message should say 'no fallback actions succeeded' when "
            f"both label and DB fail. Got: {result!r}"
        )
        # Must NOT claim the label blocks merges
        assert "label will prevent auto-merge" not in result.lower(), (
            f"Return message must not claim label blocks merges when no fallback "
            f"actions succeeded. Got: {result!r}"
        )


class TestReturnMessageAccuracy:
    """Additional regression tests for issue #140: return message accuracy."""

    async def test_return_message_does_not_claim_phantom_notification(self, registry):
        """The return message must NOT claim the PR author will be notified
        via comment_on_pr when no such call is made in the fallback code path."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)

        result = await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Issues found.",
                event="REQUEST_CHANGES",
            ),
        )

        # Must NOT claim the author will be notified — no such call is made
        phantom_phrases = [
            "will be notified via comment_on_pr",
            "author agent will be notified",
        ]
        for phrase in phantom_phrases:
            assert phrase not in result, (
                f"Return message must NOT claim '{phrase}' — no comment_on_pr call is "
                f"made in the 403 fallback path (issue #140). "
                f"Got: {result!r}"
            )

        # Verify comment_on_pr was never called in the fallback
        github.comment_on_pr.assert_not_called()

    async def test_lowercase_event_reaches_403_fallback(self, registry):
        """Guard against LLM case variation: 'request_changes' (lowercase) must
        also trigger the 403 fallback, not the APPROVE/COMMENT 403 path."""
        github = _make_github_mock()
        github.submit_pr_review = AsyncMock(side_effect=_make_403_error())

        tools = _make_tools(registry, github)
        await _register_agent(registry, pr_number=42)

        result = await tools.submit_pr_review(
            "pr-review-issue-99",
            SubmitPRReviewParams(
                pr_number=42,
                body="Issues found.",
                event="request_changes",  # lowercase variant
            ),
        )

        # The 403 fallback should have fired (label applied)
        github.add_labels.assert_called_once()
        # And message should describe the fallback, not the generic 403 error
        assert "fallback" in result.lower(), (
            f"Lowercase 'request_changes' event should reach the 403 fallback path. Got: {result!r}"
        )
