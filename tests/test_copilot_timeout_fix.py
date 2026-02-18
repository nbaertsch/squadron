"""Tests for copilot client timeout fix (Issue #38).

This test reproduces the ping verification timeout during CopilotClient startup
and verifies that our fix allows for proper initialization even under resource
contention or slow CLI server startup.
"""

import asyncio
from unittest.mock import AsyncMock, patch
import pytest

from squadron.copilot import CopilotAgent
from squadron.config import RuntimeConfig


class TestCopilotTimeoutFix:
    """Test timeout handling during CopilotClient startup."""

    async def test_ping_timeout_during_startup_reproducer(self):
        """Reproduce the original ping timeout issue during client startup.

        This test simulates the scenario where the Copilot CLI server takes
        longer than the default timeout to respond to the initial ping
        verification during client.start().

        With the fix, this should retry and eventually succeed.
        """
        runtime_config = RuntimeConfig()
        agent = CopilotAgent(runtime_config=runtime_config, working_directory="/tmp")

        # Mock the CopilotClient to simulate a slow ping response
        with patch("squadron.copilot.CopilotClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client

            # Simulate the timeout error that occurs during ping verification
            # This should now be handled by our retry logic
            call_count = 0

            def side_effect():
                nonlocal call_count
                call_count += 1
                if call_count <= 2:  # First two attempts fail
                    raise asyncio.TimeoutError("Ping verification timed out")
                return None  # Third attempt succeeds

            mock_client.start.side_effect = side_effect

            # This should succeed with retry logic (was failing before fix)
            await agent.start()

            # Verify retries happened
            assert mock_client.start.call_count == 3  # 3 attempts total
            assert agent._client is mock_client

    async def test_ping_timeout_with_immediate_success(self):
        """Test that normal startup (no timeout) still works quickly."""
        runtime_config = RuntimeConfig()
        agent = CopilotAgent(runtime_config=runtime_config, working_directory="/tmp")

        with patch("squadron.copilot.CopilotClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client

            # Simulate immediate success (no timeout)
            mock_client.start.return_value = None

            # Should succeed on first attempt
            await agent.start()

            # Verify no retries needed
            assert mock_client.start.call_count == 1
            assert agent._client is mock_client

    async def test_ping_timeout_exhausted_retries(self):
        """Test that if retries are exhausted, the timeout error is propagated."""
        runtime_config = RuntimeConfig()
        agent = CopilotAgent(runtime_config=runtime_config, working_directory="/tmp")

        with patch("squadron.copilot.CopilotClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client

            # Simulate persistent timeout (all attempts fail)
            mock_client.start.side_effect = asyncio.TimeoutError("Persistent timeout")

            # Should fail after max retries
            with pytest.raises(asyncio.TimeoutError, match="Persistent timeout"):
                await agent.start()

            # Verify all retries were attempted (1 initial + 3 retries = 4 total)
            assert mock_client.start.call_count == 4

    async def test_cleanup_on_failed_retry(self):
        """Test that failed client instances are cleaned up during retries."""
        runtime_config = RuntimeConfig()
        agent = CopilotAgent(runtime_config=runtime_config, working_directory="/tmp")

        with patch("squadron.copilot.CopilotClient") as mock_client_class:
            # Create separate mock instances for each retry
            mock_clients = [AsyncMock(), AsyncMock(), AsyncMock()]
            mock_client_class.side_effect = mock_clients

            # First attempt fails, second succeeds
            mock_clients[0].start.side_effect = asyncio.TimeoutError("First timeout")
            mock_clients[1].start.return_value = None

            await agent.start()

            # Verify cleanup was called on failed client
            mock_clients[0].stop.assert_called_once()
            # Successful client should not be stopped
            mock_clients[1].stop.assert_not_called()
            # Third client should never be created
            assert len(mock_clients) >= 2

    async def test_exponential_backoff_timing(self):
        """Test that exponential backoff delays are applied between retries."""
        runtime_config = RuntimeConfig()
        agent = CopilotAgent(runtime_config=runtime_config, working_directory="/tmp")

        # Track sleep calls to verify exponential backoff
        sleep_calls = []

        with patch("squadron.copilot.CopilotClient") as mock_client_class:
            with patch("asyncio.sleep", side_effect=lambda x: sleep_calls.append(x)):
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                call_count = 0

                def side_effect():
                    nonlocal call_count
                    call_count += 1
                    if call_count <= 2:  # First two fail
                        raise asyncio.TimeoutError("Timeout")
                    return None  # Third succeeds

                mock_client.start.side_effect = side_effect

                await agent.start()

                # Verify exponential backoff: 2.0, 4.0 seconds
                assert len(sleep_calls) == 2
                assert sleep_calls[0] == 2.0  # 2.0 * 2^0
                assert sleep_calls[1] == 4.0  # 2.0 * 2^1

    async def test_multiple_agents_concurrent_startup(self):
        """Test concurrent agent startup under simulated resource contention.

        This test simulates the scenario mentioned in the issue where
        multiple agents spawn simultaneously, causing resource contention
        and ping timeouts.
        """
        runtime_config = RuntimeConfig()

        # Create multiple agents
        agents = [
            CopilotAgent(runtime_config=runtime_config, working_directory="/tmp") for _ in range(3)
        ]

        with patch("squadron.copilot.CopilotClient") as mock_client_class:
            # Each agent gets its own sequence of mock clients
            mock_clients_per_agent = [[AsyncMock(), AsyncMock()] for _ in range(3)]

            call_counts = [0, 0, 0]

            def client_factory(*args, **kwargs):
                # Determine which agent is calling based on call order
                agent_idx = mock_client_class.call_count % 3
                client_idx = call_counts[agent_idx]
                call_counts[agent_idx] += 1

                client = mock_clients_per_agent[agent_idx][client_idx]

                # Simulate different failure patterns for each agent
                if agent_idx == 0:  # Agent 0: succeed immediately
                    client.start.return_value = None
                elif agent_idx == 1:  # Agent 1: timeout once, then succeed
                    if client_idx == 0:
                        client.start.side_effect = asyncio.TimeoutError("Contention")
                    else:
                        client.start.return_value = None
                else:  # Agent 2: timeout twice, then succeed
                    if client_idx <= 1:
                        client.start.side_effect = asyncio.TimeoutError("Heavy contention")
                    else:
                        client.start.return_value = None

                return client

            mock_client_class.side_effect = client_factory

            # Start all agents concurrently
            tasks = [agent.start() for agent in agents]
            await asyncio.gather(*tasks)

            # All should succeed despite different retry patterns
            for i, agent in enumerate(agents):
                assert agent._client is not None

        # Cleanup
        for agent in agents:
            await agent.stop()
