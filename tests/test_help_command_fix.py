"""Test for issue #21: Using the @squadron-dev help command still invokes the PM agent."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from squadron.event_router import EventRouter
from squadron.models import GitHubEvent, SquadronEvent, SquadronEventType
from squadron.config import CommandDefinition


class TestHelpCommandFix:
    """Test that @squadron-dev help command doesn't invoke PM agent."""

    @pytest.fixture
    def github_event_help_comment(self):
        """GitHub webhook payload for @squadron-dev help comment."""
        return GitHubEvent(
            delivery_id="help-command-test",
            event_type="issue_comment",
            action="created",
            payload={
                "action": "created",
                "issue": {
                    "number": 42,
                    "title": "Test issue",
                    "state": "open",
                },
                "comment": {
                    "id": 123456,
                    "body": "@squadron-dev help",
                    "user": {
                        "login": "testuser",
                        "type": "User"
                    }
                },
                "sender": {
                    "login": "testuser",
                    "type": "User"
                }
            }
        )

    @pytest.fixture
    def mock_config(self):
        """Mock squadron config with command configuration."""
        config = AsyncMock()
        config.commands = {
            "help": CommandDefinition(
                enabled=True,
                invoke_agent=False,
                response="Available commands: help, status"
            )
        }
        return config

    @pytest.fixture  
    def mock_registry(self):
        """Mock agent registry."""
        registry = AsyncMock()
        registry.has_seen_event = AsyncMock(return_value=False)
        registry.mark_event_seen = AsyncMock()
        return registry

    async def test_help_command_doesnt_invoke_pm_agent(self, mock_config, mock_registry, github_event_help_comment):
        """Test that @squadron-dev help doesn't route to PM agent."""
        # Setup event router
        event_queue = asyncio.Queue()
        router = EventRouter(
            event_queue=event_queue, 
            registry=mock_registry, 
            config=mock_config
        )
        
        # Process the help command comment
        await router._route_event(github_event_help_comment)
        
        # Verify PM queue is empty (PM agent not invoked)
        assert router.pm_queue.empty(), "PM queue should be empty for help commands - PM agent should not be invoked"

    async def test_regular_comment_still_invokes_pm_agent(self, mock_config, mock_registry):
        """Test that regular comments still invoke PM agent."""
        # Setup event with regular comment  
        regular_comment = GitHubEvent(
            delivery_id="regular-comment-test",
            event_type="issue_comment", 
            action="created",
            payload={
                "action": "created",
                "issue": {"number": 42, "title": "Test issue", "state": "open"},
                "comment": {
                    "id": 123457,
                    "body": "This is a regular comment",
                    "user": {"login": "testuser", "type": "User"}
                },
                "sender": {"login": "testuser", "type": "User"}
            }
        )
        
        # Setup event router
        event_queue = asyncio.Queue()
        router = EventRouter(
            event_queue=event_queue,
            registry=mock_registry,
            config=mock_config
        )
        
        # Process the regular comment
        await router._route_event(regular_comment)
        
        # Verify PM queue received the event
        assert not router.pm_queue.empty(), "PM queue should contain regular comment events"
        
    async def test_unknown_command_still_invokes_pm_agent(self, mock_config, mock_registry):
        """Test that unknown @squadron-dev commands still invoke PM agent."""
        # Setup event with unknown command  
        unknown_command = GitHubEvent(
            delivery_id="unknown-command-test",
            event_type="issue_comment", 
            action="created",
            payload={
                "action": "created",
                "issue": {"number": 42, "title": "Test issue", "state": "open"},
                "comment": {
                    "id": 123458,
                    "body": "@squadron-dev unknown_command",
                    "user": {"login": "testuser", "type": "User"}
                },
                "sender": {"login": "testuser", "type": "User"}
            }
        )
        
        # Setup event router
        event_queue = asyncio.Queue()
        router = EventRouter(
            event_queue=event_queue,
            registry=mock_registry,
            config=mock_config
        )
        
        # Process the unknown command
        await router._route_event(unknown_command)
        
        # Verify PM queue received the event
        assert not router.pm_queue.empty(), "PM queue should contain unknown command events"

    def test_command_detection(self):
        """Test command detection logic."""
        config = AsyncMock()
        registry = AsyncMock()
        router = EventRouter(asyncio.Queue(), registry, config)
        
        # Test help command detection
        is_command, command_name = router._is_command_comment("@squadron-dev help")
        assert is_command is True
        assert command_name == "help"
        
        # Test case insensitive
        is_command, command_name = router._is_command_comment("@squadron-dev HELP")
        assert is_command is True
        assert command_name == "help"
        
        # Test status command
        is_command, command_name = router._is_command_comment("@squadron-dev status")
        assert is_command is True  
        assert command_name == "status"
        
        # Test not a command
        is_command, command_name = router._is_command_comment("This is just a regular comment")
        assert is_command is False
        assert command_name is None
        
        # Test non-command mention
        is_command, command_name = router._is_command_comment("@squadron-dev please help me")
        assert is_command is True
        assert command_name == "please"  # Should detect first word after mention
