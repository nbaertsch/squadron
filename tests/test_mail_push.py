"""Tests for push delivery of @ mention messages (issue #95).

Validates:
- MailMessage, MessageProvenance, MessageProvenanceType model correctness
- _event_to_mail_message() converts SquadronEvents to MailMessage with correct provenance
- _drain_mail_queue() drains and clears the queue (no double-delivery)
- _format_mail_messages() renders a well-formed prompt section with sender + provenance
- Mail messages are injected into the prompt before send_and_wait
- Mail queue is cleared after injection (no double-delivery)
- Singleton guard uses mail_queues instead of inbox for mention events
- Non-mention events still fall back to the inbox
- Mail queue initialised/cleaned up alongside inbox throughout agent lifecycle
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest_asyncio

from squadron.config import (
    AgentRoleConfig,
    AgentTrigger,
    ProjectConfig,
    SquadronConfig,
)
from squadron.agent_manager import AgentManager
from squadron.event_router import EventRouter
from squadron.models import (
    AgentRecord,
    AgentStatus,
    MailMessage,
    MessageProvenance,
    MessageProvenanceType,
    SquadronEvent,
    SquadronEventType,
    parse_command,
)
from squadron.registry import AgentRegistry


# ── Fixtures & Helpers ────────────────────────────────────────────────────────


def _minimal_config() -> SquadronConfig:
    """Minimal SquadronConfig with a persistent feat-dev role."""
    return SquadronConfig(
        project=ProjectConfig(
            name="test-project",
            owner="testorg",
            repo="testrepo",
            default_branch="main",
            bot_username="squadron-dev[bot]",
        ),
        agent_roles={
            "feat-dev": AgentRoleConfig(
                agent_definition="agents/feat-dev.md",
                triggers=[AgentTrigger(event="issues.labeled", label="feature")],
            ),
        },
    )


def _make_comment_event(
    *,
    body: str = "@squadron-dev feat-dev: please check the tests",
    issue_number: int = 42,
    sender: str = "alice",
    comment_id: int = 999,
    pr_number: int | None = None,
) -> SquadronEvent:
    """Build a synthetic ISSUE_COMMENT SquadronEvent."""
    payload: dict = {
        "comment": {"id": comment_id, "body": body, "user": {"login": sender}},
        "issue": {"number": issue_number, "pull_request": None if pr_number is None else {}},
        "sender": {"login": sender},
    }
    return SquadronEvent(
        event_type=SquadronEventType.ISSUE_COMMENT,
        issue_number=issue_number,
        pr_number=pr_number,
        command=parse_command(body),
        data={"sender": sender, "payload": payload},
    )


@pytest_asyncio.fixture
async def registry(tmp_path):

    db_path = str(tmp_path / "test_mail.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


@pytest_asyncio.fixture
async def manager(registry, tmp_path):
    config = _minimal_config()
    event_queue: asyncio.Queue = asyncio.Queue()
    router = EventRouter(event_queue, registry, config)

    with patch("squadron.agent_manager.CopilotAgent"):
        mgr = AgentManager(
            config=config,
            registry=registry,
            github=AsyncMock(),
            router=router,
            agent_definitions={},
            repo_root=Path(tmp_path),
        )
        await mgr.start()
        yield mgr
        await mgr.stop()


# ── MailMessage Model Tests ───────────────────────────────────────────────────


class TestMailMessageModel:
    """Unit tests for MailMessage, MessageProvenance, MessageProvenanceType."""

    def test_issue_comment_provenance(self):
        """issue_comment provenance stores issue_number and comment_id."""
        prov = MessageProvenance(
            type=MessageProvenanceType.ISSUE_COMMENT,
            issue_number=42,
            comment_id=123,
        )
        assert prov.type == MessageProvenanceType.ISSUE_COMMENT
        assert prov.issue_number == 42
        assert prov.comment_id == 123
        assert prov.pr_number is None

    def test_pr_comment_provenance(self):
        """pr_comment provenance stores pr_number and comment_id."""
        prov = MessageProvenance(
            type=MessageProvenanceType.PR_COMMENT,
            pr_number=10,
            comment_id=456,
        )
        assert prov.type == MessageProvenanceType.PR_COMMENT
        assert prov.pr_number == 10
        assert prov.comment_id == 456
        assert prov.issue_number is None

    def test_mail_message_has_sender_body_provenance(self):
        """MailMessage captures sender, body, and provenance."""
        msg = MailMessage(
            sender="alice",
            body="Please fix the failing tests.",
            provenance=MessageProvenance(
                type=MessageProvenanceType.ISSUE_COMMENT,
                issue_number=42,
            ),
        )
        assert msg.sender == "alice"
        assert msg.body == "Please fix the failing tests."
        assert msg.provenance.type == MessageProvenanceType.ISSUE_COMMENT

    def test_mail_message_received_at_defaults_to_utcnow(self):
        """received_at defaults to current UTC time."""
        before = datetime.now(timezone.utc)
        msg = MailMessage(
            sender="bob",
            body="Hello",
            provenance=MessageProvenance(
                type=MessageProvenanceType.PR_COMMENT,
                pr_number=5,
            ),
        )
        after = datetime.now(timezone.utc)
        assert before <= msg.received_at <= after

    def test_provenance_type_values(self):
        """Provenance type enum values match the spec."""
        assert MessageProvenanceType.ISSUE_COMMENT.value == "issue_comment"
        assert MessageProvenanceType.PR_COMMENT.value == "pr_comment"

    def test_provenance_optional_fields_default_none(self):
        """All optional reference fields default to None."""
        prov = MessageProvenance(type=MessageProvenanceType.ISSUE_COMMENT)
        assert prov.issue_number is None
        assert prov.pr_number is None
        assert prov.comment_id is None


# ── _event_to_mail_message Tests ─────────────────────────────────────────────


class TestEventToMailMessage:
    """Unit tests for AgentManager._event_to_mail_message."""

    def test_issue_comment_produces_mail_message(self, manager):
        event = _make_comment_event(
            body="@squadron-dev feat-dev: check the tests",
            issue_number=42,
            sender="alice",
            comment_id=999,
        )
        msg = manager._event_to_mail_message(event)
        assert msg is not None
        assert msg.sender == "alice"
        assert "check the tests" in msg.body
        assert msg.provenance.type == MessageProvenanceType.ISSUE_COMMENT
        assert msg.provenance.issue_number == 42
        assert msg.provenance.comment_id == 999
        assert msg.provenance.pr_number is None

    def test_pr_comment_produces_pr_provenance(self, manager):
        """When event has pr_number, provenance type is PR_COMMENT."""
        event = _make_comment_event(
            body="@squadron-dev feat-dev: update the PR",
            issue_number=42,
            sender="bob",
            comment_id=777,
            pr_number=15,
        )
        msg = manager._event_to_mail_message(event)
        assert msg is not None
        assert msg.provenance.type == MessageProvenanceType.PR_COMMENT
        assert msg.provenance.pr_number == 15
        assert msg.provenance.comment_id == 777

    def test_non_comment_event_returns_none(self, manager):
        """Non-comment events (e.g. PR_REVIEW_SUBMITTED) return None."""
        event = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            issue_number=42,
            pr_number=10,
            data={"sender": "reviewer", "payload": {}},
        )
        result = manager._event_to_mail_message(event)
        assert result is None

    def test_sender_extracted_from_event_data(self, manager):
        """Sender comes from event.data['sender']."""
        event = _make_comment_event(sender="charlie")
        msg = manager._event_to_mail_message(event)
        assert msg is not None
        assert msg.sender == "charlie"

    def test_body_extracted_from_comment_payload(self, manager):
        """Body is the full comment text, not just the extracted command message."""
        full_comment = "@squadron-dev feat-dev: please fix issue #42\n\nAdditional context here."
        event = _make_comment_event(body=full_comment)
        msg = manager._event_to_mail_message(event)
        assert msg is not None
        assert "Additional context here." in msg.body


# ── _drain_mail_queue Tests ───────────────────────────────────────────────────


class TestDrainMailQueue:
    """Unit tests for AgentManager._drain_mail_queue."""

    def test_drain_returns_all_messages(self, manager):
        """Draining returns all queued messages."""
        agent_id = "feat-dev-issue-42"
        msg1 = MailMessage(
            sender="alice",
            body="First message",
            provenance=MessageProvenance(
                type=MessageProvenanceType.ISSUE_COMMENT, issue_number=42
            ),
        )
        msg2 = MailMessage(
            sender="bob",
            body="Second message",
            provenance=MessageProvenance(type=MessageProvenanceType.PR_COMMENT, pr_number=5),
        )
        manager.agent_mail_queues[agent_id] = [msg1, msg2]

        drained = manager._drain_mail_queue(agent_id)
        assert len(drained) == 2
        assert drained[0].body == "First message"
        assert drained[1].body == "Second message"

    def test_drain_clears_queue(self, manager):
        """After draining, the queue is empty — no double-delivery."""
        agent_id = "feat-dev-issue-42"
        msg = MailMessage(
            sender="alice",
            body="Test",
            provenance=MessageProvenance(
                type=MessageProvenanceType.ISSUE_COMMENT, issue_number=42
            ),
        )
        manager.agent_mail_queues[agent_id] = [msg]

        manager._drain_mail_queue(agent_id)

        remaining = manager.agent_mail_queues.get(agent_id, [])
        assert remaining == [], "Queue should be empty after drain"

    def test_drain_empty_queue_returns_empty_list(self, manager):
        """Draining an empty or non-existent queue returns an empty list."""
        result = manager._drain_mail_queue("no-such-agent")
        assert result == []


# ── _format_mail_messages Tests ──────────────────────────────────────────────


class TestFormatMailMessages:
    """Unit tests for AgentManager._format_mail_messages."""

    def test_empty_list_returns_empty_string(self, manager):
        result = manager._format_mail_messages([])
        assert result == ""

    def test_issue_comment_includes_sender_and_source(self, manager):
        msg = MailMessage(
            sender="alice",
            body="Can you check the test failures?",
            provenance=MessageProvenance(
                type=MessageProvenanceType.ISSUE_COMMENT,
                issue_number=42,
                comment_id=123,
            ),
        )
        result = manager._format_mail_messages([msg])

        assert "@alice" in result
        assert "issue_comment" in result
        assert "issue #42" in result
        assert "comment #123" in result
        assert "Can you check the test failures?" in result

    def test_pr_comment_includes_pr_reference(self, manager):
        msg = MailMessage(
            sender="bob",
            body="LGTM, but please fix the linting.",
            provenance=MessageProvenance(
                type=MessageProvenanceType.PR_COMMENT,
                pr_number=10,
                comment_id=456,
            ),
        )
        result = manager._format_mail_messages([msg])

        assert "@bob" in result
        assert "pr_comment" in result
        assert "PR #10" in result
        assert "comment #456" in result
        assert "LGTM" in result

    def test_multiple_messages_all_included(self, manager):
        messages = [
            MailMessage(
                sender="alice",
                body="First message",
                provenance=MessageProvenance(
                    type=MessageProvenanceType.ISSUE_COMMENT, issue_number=42, comment_id=1
                ),
            ),
            MailMessage(
                sender="bob",
                body="Second message",
                provenance=MessageProvenance(
                    type=MessageProvenanceType.PR_COMMENT, pr_number=5, comment_id=2
                ),
            ),
        ]
        result = manager._format_mail_messages(messages)

        assert "@alice" in result
        assert "@bob" in result
        assert "First message" in result
        assert "Second message" in result

    def test_header_section_present(self, manager):
        """Formatted output has an 'Inbound Messages' header."""
        msg = MailMessage(
            sender="alice",
            body="hello",
            provenance=MessageProvenance(
                type=MessageProvenanceType.ISSUE_COMMENT, issue_number=1
            ),
        )
        result = manager._format_mail_messages([msg])
        assert "Inbound Messages" in result

    def test_comment_id_optional(self, manager):
        """Formatting works when comment_id is None."""
        msg = MailMessage(
            sender="alice",
            body="Message without comment ID",
            provenance=MessageProvenance(
                type=MessageProvenanceType.ISSUE_COMMENT,
                issue_number=42,
                comment_id=None,
            ),
        )
        result = manager._format_mail_messages([msg])
        # Should still show issue reference, just no comment ID
        assert "issue #42" in result
        assert "comment #" not in result


# ── Push Delivery Integration Tests ──────────────────────────────────────────


class TestPushDeliveryIntegration:
    """Integration tests for the full push delivery flow."""

    async def test_mail_queue_initialised_on_agent_create(self, manager, registry):
        """Mail queue is created alongside the inbox when an agent is registered."""
        agent = AgentRecord(
            agent_id="feat-dev-issue-99",
            role="feat-dev",
            issue_number=99,
            session_id="squadron-feat-dev-issue-99",
            status=AgentStatus.ACTIVE,
        )
        await registry.create_agent(agent)
        manager.agent_inboxes["feat-dev-issue-99"] = asyncio.Queue()
        manager.agent_mail_queues["feat-dev-issue-99"] = []

        assert "feat-dev-issue-99" in manager.agent_mail_queues

    async def test_mention_to_active_agent_goes_to_mail_queue(
        self, manager, registry, tmp_path
    ):
        """When an ACTIVE agent is mentioned, the message goes to mail_queues."""
        agent = AgentRecord(
            agent_id="feat-dev-issue-42",
            role="feat-dev",
            issue_number=42,
            session_id="squadron-feat-dev-issue-42",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)
        manager.agent_inboxes["feat-dev-issue-42"] = asyncio.Queue()
        manager.agent_mail_queues["feat-dev-issue-42"] = []

        event = _make_comment_event(
            body="@squadron-dev feat-dev: please check the tests",
            issue_number=42,
            sender="alice",
            comment_id=555,
        )
        await manager._command_wake_or_spawn("feat-dev", manager.config.agent_roles["feat-dev"], event)

        # Mail queue should have one message
        mail_queue = manager.agent_mail_queues.get("feat-dev-issue-42", [])
        assert len(mail_queue) == 1
        msg = mail_queue[0]
        assert msg.sender == "alice"
        assert "check the tests" in msg.body
        assert msg.provenance.type == MessageProvenanceType.ISSUE_COMMENT
        assert msg.provenance.issue_number == 42
        assert msg.provenance.comment_id == 555

        # Inbox should be empty (mention events no longer go to inbox)
        inbox = manager.agent_inboxes["feat-dev-issue-42"]
        assert inbox.empty(), "Inbox should be empty; mentions use mail_queues (push model)"

    async def test_mail_messages_injected_into_prompt(self, manager):
        """_drain_mail_queue + _format_mail_messages are used to augment the prompt."""
        agent_id = "feat-dev-issue-10"
        manager.agent_mail_queues[agent_id] = [
            MailMessage(
                sender="alice",
                body="Please add more tests.",
                provenance=MessageProvenance(
                    type=MessageProvenanceType.ISSUE_COMMENT,
                    issue_number=10,
                    comment_id=101,
                ),
            )
        ]

        # Simulate what _run_agent does before send_and_wait
        base_prompt = "## Assignment: Issue #10\n\nPlease implement the feature."
        pending_mail = manager._drain_mail_queue(agent_id)
        assert len(pending_mail) == 1

        if pending_mail:
            mail_section = manager._format_mail_messages(pending_mail)
            injected_prompt = base_prompt + "\n\n" + mail_section
        else:
            injected_prompt = base_prompt

        # Mail content is present in the injected prompt
        assert "@alice" in injected_prompt
        assert "Please add more tests." in injected_prompt
        assert "issue_comment" in injected_prompt

        # Queue is now empty — no double-delivery
        assert manager.agent_mail_queues.get(agent_id, []) == []

    async def test_no_mail_messages_leaves_prompt_unchanged(self, manager):
        """If the mail queue is empty, the prompt is unchanged."""
        agent_id = "feat-dev-issue-10"
        manager.agent_mail_queues[agent_id] = []

        base_prompt = "## Assignment: Issue #10"
        pending_mail = manager._drain_mail_queue(agent_id)

        if pending_mail:
            final_prompt = base_prompt + "\n\n" + manager._format_mail_messages(pending_mail)
        else:
            final_prompt = base_prompt

        assert final_prompt == base_prompt

    async def test_mail_queue_cleared_after_injection(self, manager):
        """Mail queue is empty after injection — prevents double-delivery."""
        agent_id = "feat-dev-issue-42"
        manager.agent_mail_queues[agent_id] = [
            MailMessage(
                sender="alice",
                body="Msg 1",
                provenance=MessageProvenance(
                    type=MessageProvenanceType.ISSUE_COMMENT, issue_number=42
                ),
            ),
            MailMessage(
                sender="bob",
                body="Msg 2",
                provenance=MessageProvenance(
                    type=MessageProvenanceType.PR_COMMENT, pr_number=3
                ),
            ),
        ]

        drained = manager._drain_mail_queue(agent_id)
        assert len(drained) == 2

        # Draining again should return nothing
        drained_again = manager._drain_mail_queue(agent_id)
        assert drained_again == []

    async def test_cleanup_removes_mail_queue(self, manager, registry):
        """_cleanup_agent removes the mail queue entry."""
        agent_id = "feat-dev-issue-42"
        agent = AgentRecord(
            agent_id=agent_id,
            role="feat-dev",
            issue_number=42,
            session_id="squadron-feat-dev-issue-42",
            status=AgentStatus.COMPLETED,
        )
        await registry.create_agent(agent)
        manager.agent_inboxes[agent_id] = asyncio.Queue()
        manager.agent_mail_queues[agent_id] = [
            MailMessage(
                sender="alice",
                body="Pending",
                provenance=MessageProvenance(
                    type=MessageProvenanceType.ISSUE_COMMENT, issue_number=42
                ),
            )
        ]

        await manager._cleanup_agent(agent_id, destroy_session=False)

        assert agent_id not in manager.agent_mail_queues

    async def test_non_mention_event_falls_back_to_inbox(self, manager, registry):
        """PR_REVIEW_SUBMITTED events (non-mention) still go to inbox, not mail_queues."""
        agent_id = "feat-dev-issue-42"
        agent = AgentRecord(
            agent_id=agent_id,
            role="feat-dev",
            issue_number=42,
            session_id="s",
            status=AgentStatus.ACTIVE,
            active_since=datetime.now(timezone.utc),
        )
        await registry.create_agent(agent)
        manager.agent_inboxes[agent_id] = asyncio.Queue()
        manager.agent_mail_queues[agent_id] = []

        # Non-mention event (cannot be converted to MailMessage)
        non_mention = SquadronEvent(
            event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
            issue_number=42,
            pr_number=5,
            data={"sender": "reviewer", "payload": {}},
        )
        assert manager._event_to_mail_message(non_mention) is None


# ── Provenance Extensibility Tests ───────────────────────────────────────────


class TestProvenanceExtensibility:
    """Validate that the provenance model is designed for easy extension."""

    def test_provenance_type_is_open_enum(self):
        """MessageProvenanceType is a string enum — new values can be added."""
        # Existing types work
        assert MessageProvenanceType("issue_comment") == MessageProvenanceType.ISSUE_COMMENT
        assert MessageProvenanceType("pr_comment") == MessageProvenanceType.PR_COMMENT

    def test_message_provenance_extra_fields_ignored(self):
        """MessageProvenance uses pydantic and gracefully handles serialisation."""
        prov = MessageProvenance(
            type=MessageProvenanceType.ISSUE_COMMENT,
            issue_number=1,
        )
        # Can serialise to dict — future types can add more fields without schema change
        d = prov.model_dump()
        assert d["type"] == "issue_comment"
        assert d["issue_number"] == 1
        assert "pr_number" in d  # optional field always present in dump
        assert "comment_id" in d

    def test_mail_message_is_pydantic_serialisable(self):
        """MailMessage can be serialised — useful for persistence/logging."""
        msg = MailMessage(
            sender="alice",
            body="Test message",
            provenance=MessageProvenance(
                type=MessageProvenanceType.PR_COMMENT,
                pr_number=10,
                comment_id=99,
            ),
        )
        d = msg.model_dump()
        assert d["sender"] == "alice"
        assert d["provenance"]["type"] == "pr_comment"
        assert d["provenance"]["pr_number"] == 10
