"""Tests for Squadron core data models."""

import pytest

from squadron.models import (
    AgentRecord,
    AgentRole,
    AgentStatus,
    GitHubEvent,
    SquadronEvent,
    SquadronEventType,
)


class TestGitHubEvent:
    def test_full_type_with_action(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="issues",
            action="opened",
            payload={},
        )
        assert event.full_type == "issues.opened"

    def test_full_type_without_action(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="push",
            payload={},
        )
        assert event.full_type == "push"

    def test_sender_extraction(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="issues",
            action="opened",
            payload={"sender": {"login": "alice", "type": "User"}},
        )
        assert event.sender == "alice"
        assert event.is_bot is False

    def test_bot_detection(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="issues",
            action="opened",
            payload={"sender": {"login": "squadron[bot]", "type": "Bot"}},
        )
        assert event.sender == "squadron[bot]"
        assert event.is_bot is True

    def test_repo_full_name(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="issues",
            payload={"repository": {"full_name": "owner/repo"}},
        )
        assert event.repo_full_name == "owner/repo"

    def test_issue_extraction(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="issues",
            action="opened",
            payload={"issue": {"number": 42, "title": "Fix bug"}},
        )
        assert event.issue is not None
        assert event.issue["number"] == 42

    def test_pull_request_extraction(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="pull_request",
            action="opened",
            payload={"pull_request": {"number": 7, "title": "Add feature"}},
        )
        assert event.pull_request is not None
        assert event.pull_request["number"] == 7

    def test_comment_extraction(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="issue_comment",
            action="created",
            payload={"comment": {"body": "Hello"}},
        )
        assert event.comment is not None
        assert event.comment["body"] == "Hello"

    def test_missing_optional_fields(self):
        event = GitHubEvent(
            delivery_id="abc-123",
            event_type="push",
            payload={},
        )
        assert event.sender is None
        assert event.repo_full_name is None
        assert event.issue is None
        assert event.pull_request is None
        assert event.comment is None


class TestAgentRecord:
    def test_defaults(self):
        record = AgentRecord(
            agent_id="feat-dev-issue-1",
            role=AgentRole.FEAT_DEV,
            issue_number=1,
        )
        assert record.status == AgentStatus.CREATED
        assert record.blocked_by == []
        assert record.iteration_count == 0
        assert record.tool_call_count == 0
        assert record.turn_count == 0
        assert record.branch is None
        assert record.pr_number is None
        assert record.active_since is None
        assert record.sleeping_since is None

    def test_all_statuses(self):
        for status in AgentStatus:
            record = AgentRecord(
                agent_id="test",
                role=AgentRole.PM,
                status=status,
            )
            assert record.status == status

    def test_all_roles(self):
        expected = {"pm", "feat-dev", "bug-fix", "pr-review", "security-review"}
        assert {r.value for r in AgentRole} == expected


class TestSquadronEvent:
    def test_event_types_cover_all_github_events(self):
        """Ensure we have internal types for key GitHub events."""
        github_types = {
            SquadronEventType.ISSUE_OPENED,
            SquadronEventType.ISSUE_CLOSED,
            SquadronEventType.ISSUE_ASSIGNED,
            SquadronEventType.ISSUE_LABELED,
            SquadronEventType.ISSUE_COMMENT,
            SquadronEventType.PR_OPENED,
            SquadronEventType.PR_CLOSED,
            SquadronEventType.PR_REVIEW_SUBMITTED,
            SquadronEventType.PR_SYNCHRONIZED,
            SquadronEventType.PUSH,
        }
        internal_types = {
            SquadronEventType.AGENT_BLOCKED,
            SquadronEventType.AGENT_COMPLETED,
            SquadronEventType.AGENT_ESCALATED,
            SquadronEventType.BLOCKER_RESOLVED,
            SquadronEventType.WAKE_AGENT,
        }
        all_types = set(SquadronEventType)
        assert github_types | internal_types == all_types

    def test_default_data(self):
        event = SquadronEvent(event_type=SquadronEventType.ISSUE_OPENED)
        assert event.data == {}
        assert event.agent_id is None
        assert event.issue_number is None
        assert event.pr_number is None
        assert event.source_delivery_id is None
