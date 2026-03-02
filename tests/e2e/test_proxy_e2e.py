"""E2E tests for sandbox MitM proxy — live API only.

These tests hit REAL external APIs through the REAL proxy with REAL
credential injection.  No mocks, no test doubles, no fake upstreams.

This test connects directly to the proxy (no network namespace needed).
It validates the proxy's TLS termination, credential injection, and
upstream forwarding in isolation from the network namespace infrastructure.

Requires ``COPILOT_GITHUB_TOKEN`` in the environment (set locally or
via ``secrets.SQ_COPILOT_TOKEN`` in GitHub Actions).

Run::

    sudo pytest tests/e2e/test_proxy_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import ssl
from pathlib import Path

import pytest

from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.config import SandboxConfig
from squadron.sandbox.inference_proxy import InferenceProxy


# ═══════════════════════════════════════════════════════════════════════════════
# Live Copilot API (requires COPILOT_GITHUB_TOKEN)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.live
class TestLiveCopilotAPI:
    """Test real API call through the MitM proxy to Copilot.

    Requires ``COPILOT_GITHUB_TOKEN`` in the environment.
    Skips automatically if the token is missing.
    """

    @pytest.fixture(autouse=True)
    def _require_copilot_token(self) -> None:
        if not os.environ.get("COPILOT_GITHUB_TOKEN"):
            pytest.skip("COPILOT_GITHUB_TOKEN not set — skipping live test")

    async def test_live_copilot_through_proxy(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Real Copilot API call through MitM proxy with credential injection.

        This test does NOT use a mock upstream — it hits the real Copilot API.
        """
        copilot_token = os.environ["COPILOT_GITHUB_TOKEN"]

        # Start proxy with real credentials.
        proxy_config = SandboxConfig(
            enabled=True,
            bridge_ip="127.0.0.1",
            proxy_port=0,
            ca_dir=str(ca.cert_path.parent),
        )
        proxy = InferenceProxy(proxy_config, ca, {"copilot_token": copilot_token})
        await proxy.start()

        assert proxy._server is not None
        port = proxy._server.sockets[0].getsockname()[1]

        try:
            # Use a direct TLS connection to the proxy (like the agent would).
            # No monkey-patching — the proxy forwards to real api.githubcopilot.com.
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                port,
                ssl=client_ctx,
                server_hostname="api.githubcopilot.com",
            )

            # Send a chat completion request with required Copilot headers.
            body = b'{"messages":[{"role":"user","content":"Say hello in one word"}],"model":"gpt-4o","max_tokens":10}'
            raw = (
                f"POST /chat/completions HTTP/1.1\r\n"
                f"host: api.githubcopilot.com\r\n"
                f"content-type: application/json\r\n"
                f"copilot-integration-id: vscode-chat\r\n"
                f"editor-version: vscode/1.96.0\r\n"
                f"openai-intent: conversation-panel\r\n"
                f"content-length: {len(body)}\r\n"
                f"\r\n"
            ).encode() + body

            writer.write(raw)
            await writer.drain()

            # Read response — allow generous timeout for real API.
            response_data = b""
            try:
                while True:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
                    if not chunk:
                        break
                    response_data += chunk
            except asyncio.TimeoutError:
                # Timeout is expected after the full response has been read
                # (server keeps connection alive but we stop reading).
                assert response_data, (
                    "Timed out before receiving any response data — proxy may not be forwarding"
                )

            writer.close()
            await writer.wait_closed()

            # Verify we got a valid HTTP response.
            response_str = response_data.decode("latin-1", errors="replace")
            assert "HTTP/1.1" in response_str, f"No HTTP response: {response_str[:200]}"

            status_line = response_str.split("\r\n")[0]
            status_code = int(status_line.split(" ")[1])

            # 200 is the only acceptable success — 401/403 would mean
            # credential injection is broken (proxy failed to inject token
            # or injected the wrong one).
            assert status_code == 200, (
                f"Expected 200 but got {status_code} — credential injection "
                f"may be broken: {response_str[:500]}"
            )

        finally:
            await proxy.stop()
