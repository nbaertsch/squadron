"""Shared fixtures for E2E sandbox proxy tests.

Provides:
- Ephemeral CA (real ECDSA P-256 key generation)
- InferenceProxy on localhost (real TLS, no network namespaces needed)
- Mock upstream HTTPS server (captures requests for assertion)
- Credential helpers
"""

from __future__ import annotations

import asyncio
import os
import ssl
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio

from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.config import SandboxConfig


# ── Ephemeral CA ──────────────────────────────────────────────────────────────


@pytest.fixture
def ca_dir(tmp_path: Path) -> Path:
    """Create a temporary CA directory."""
    d = tmp_path / "ca"
    d.mkdir()
    return d


@pytest.fixture
def ca(ca_dir: Path) -> SandboxCA:
    """Initialised ephemeral CA with real ECDSA keys."""
    ca = SandboxCA(str(ca_dir), validity_days=1)
    ca.ensure_ca()
    return ca


# ── Mock upstream HTTPS server ────────────────────────────────────────────────


@dataclass
class CapturedRequest:
    """One request captured by the mock upstream."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class MockUpstream:
    """A real HTTPS server that captures requests and returns canned responses.

    Used as the "real provider API" in E2E tests.  The proxy forwards
    requests here instead of to the actual internet.
    """

    host: str = "127.0.0.1"
    port: int = 0  # 0 = OS picks a free port
    captured: list[CapturedRequest] = field(default_factory=list)
    response_status: int = 200
    response_body: bytes = b'{"ok": true}'
    response_headers: dict[str, str] = field(
        default_factory=lambda: {"content-type": "application/json"}
    )
    _server: asyncio.AbstractServer | None = field(default=None, repr=False)

    async def start(self, ssl_ctx: ssl.SSLContext) -> None:
        """Start the mock HTTPS server."""
        self._server = await asyncio.start_server(
            self._handle,
            host=self.host,
            port=self.port,
            ssl=ssl_ctx,
        )
        # Resolve the actual port assigned by the OS.
        socks = self._server.sockets
        if socks:
            self.port = socks[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Read HTTP/1.1 request.
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return
            parts = request_line.decode("latin-1").strip().split(" ", 2)
            method = parts[0] if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"

            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                decoded = line.decode("latin-1").strip()
                if ":" in decoded:
                    k, _, v = decoded.partition(":")
                    headers[k.strip().lower()] = v.strip()

            body = b""
            cl = headers.get("content-length")
            if cl:
                try:
                    body = await reader.readexactly(int(cl))
                except (asyncio.IncompleteReadError, ValueError):
                    pass

            self.captured.append(CapturedRequest(method, path, headers, body))

            # Send canned response.
            resp_body = self.response_body
            writer.write(f"HTTP/1.1 {self.response_status} OK\r\n".encode("latin-1"))
            for k, v in self.response_headers.items():
                writer.write(f"{k}: {v}\r\n".encode("latin-1"))
            writer.write(f"content-length: {len(resp_body)}\r\n".encode("latin-1"))
            writer.write(b"\r\n")
            writer.write(resp_body)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


@pytest_asyncio.fixture
async def mock_upstream(ca: SandboxCA):
    """A real HTTPS server that captures incoming requests.

    Uses a self-signed leaf cert from the ephemeral CA for TLS.
    """
    # The upstream uses a cert signed by the *real* upstream CA (not our
    # ephemeral one).  For E2E tests where the proxy connects to this
    # upstream, we disable verification on the proxy's httpx client.
    # In production, the proxy connects to real provider endpoints with
    # system-trusted certs.
    cert_pem, key_pem = ca.sign_leaf("mock-upstream")
    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cert_file.write(cert_pem)
    cert_file.close()
    key_file.write(key_pem)
    key_file.close()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file.name, key_file.name)

    upstream = MockUpstream()
    await upstream.start(ctx)

    yield upstream

    await upstream.stop()
    os.unlink(cert_file.name)
    os.unlink(key_file.name)


# ── Sandbox config for E2E ────────────────────────────────────────────────────


@pytest.fixture
def e2e_sandbox_config(tmp_path: Path) -> SandboxConfig:
    """SandboxConfig pointing to localhost (no real bridge needed)."""
    return SandboxConfig(
        enabled=True,
        retention_path=str(tmp_path / "forensics"),
        socket_dir=str(tmp_path / "sockets"),
        use_overlayfs=False,
        ca_dir=str(tmp_path / "ca"),
        # Listen on localhost instead of bridge IP for E2E tests.
        bridge_ip="127.0.0.1",
        proxy_port=0,  # Will be overridden per-test to use a free port.
    )
