"""Integration tests for sandbox MitM proxy (Issue #146).

These tests use real crypto, real TLS, and real HTTP but do NOT hit any
external APIs.  They validate the internal proxy chain:
    ephemeral CA → leaf cert → TLS handshake → credential injection → mock upstream.

The mock upstream (``MockUpstream``) is a local HTTPS server that captures
requests for assertion — this makes these integration tests, not E2E.

Previously in tests/e2e/test_proxy_e2e.py — moved here because external
APIs are not involved.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.config import SandboxConfig
from squadron.sandbox.inference_proxy import InferenceProxy


# ── Mock upstream HTTPS server (test double) ─────────────────────────────────


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

    Used as the "real provider API" in integration tests.  The proxy forwards
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
        socks = self._server.sockets
        if socks:
            self.port = socks[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
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


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def ca_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ca"
    d.mkdir()
    return d


@pytest.fixture
def ca(ca_dir: Path) -> SandboxCA:
    ca = SandboxCA(str(ca_dir), validity_days=1)
    ca.ensure_ca()
    return ca


@pytest.fixture
async def mock_upstream(ca: SandboxCA):
    """A real HTTPS server that captures incoming requests."""
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


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Proxy TLS Handshake — SNI callback, per-hostname certs
# ═══════════════════════════════════════════════════════════════════════════════


class TestProxyTLSHandshake:
    """Validate the proxy's TLS termination with dynamic SNI-based certs."""

    async def _start_proxy(
        self,
        ca: SandboxCA,
        credentials: dict[str, str] | None = None,
    ) -> tuple[InferenceProxy, int]:
        """Start an InferenceProxy on a free port, return (proxy, port)."""
        config = SandboxConfig(
            enabled=True,
            bridge_ip="127.0.0.1",
            proxy_port=0,
            ca_dir=str(ca.cert_path.parent),
        )
        proxy = InferenceProxy(config, ca, credentials or {})
        await proxy.start()

        assert proxy._server is not None
        socks = proxy._server.sockets
        assert socks, "Proxy has no listening sockets"
        port = socks[0].getsockname()[1]
        return proxy, port

    async def test_proxy_starts_and_accepts_tls(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Proxy accepts a TLS connection with SNI and serves correct cert."""
        proxy, port = await self._start_proxy(ca)
        try:
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                port,
                ssl=client_ctx,
                server_hostname="api.anthropic.com",
            )

            ssl_obj = writer.transport.get_extra_info("ssl_object")
            peer_cert = ssl_obj.getpeercert()
            subject = dict(x[0] for x in peer_cert["subject"])
            assert subject["commonName"] == "api.anthropic.com"

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    async def test_proxy_sni_different_hosts(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Proxy generates unique leaf certs per SNI hostname."""
        proxy, port = await self._start_proxy(ca)
        try:
            hostnames = ["api.anthropic.com", "api.openai.com", "api.githubcopilot.com"]
            seen_cns: list[str] = []

            for hostname in hostnames:
                client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

                reader, writer = await asyncio.open_connection(
                    "127.0.0.1",
                    port,
                    ssl=client_ctx,
                    server_hostname=hostname,
                )

                ssl_obj = writer.transport.get_extra_info("ssl_object")
                peer_cert = ssl_obj.getpeercert()
                cn = dict(x[0] for x in peer_cert["subject"])["commonName"]
                seen_cns.append(cn)

                writer.close()
                await writer.wait_closed()

            assert seen_cns == hostnames
        finally:
            await proxy.stop()

    async def test_proxy_rejects_without_ca_trust(self, ca: SandboxCA) -> None:
        """Client that doesn't trust the CA gets a TLS error."""
        proxy, port = await self._start_proxy(ca)
        try:
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

            with pytest.raises(ssl.SSLCertVerificationError):
                await asyncio.open_connection(
                    "127.0.0.1",
                    port,
                    ssl=client_ctx,
                    server_hostname="api.anthropic.com",
                )
        finally:
            await proxy.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Credential Injection — per-provider auth headers (via mock upstream)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCredentialInjection:
    """Test that the proxy injects correct auth headers per provider.

    Each test does a full round-trip: client → proxy → mock_upstream.
    The mock upstream captures the request, and we assert the headers.
    """

    async def _roundtrip(
        self,
        ca: SandboxCA,
        ca_dir: Path,
        mock_upstream: MockUpstream,
        credentials: dict[str, str],
        target_host: str,
        request_path: str = "/v1/messages",
        request_body: bytes = b'{"model": "test"}',
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        config = SandboxConfig(
            enabled=True,
            bridge_ip="127.0.0.1",
            proxy_port=0,
            ca_dir=str(ca.cert_path.parent),
        )
        proxy = InferenceProxy(config, ca, credentials)
        await proxy.start()

        assert proxy._server is not None
        port = proxy._server.sockets[0].getsockname()[1]

        original_forward = proxy._forward_upstream

        async def _patched_forward(
            method: str, url: str, headers: dict[str, str], body: bytes
        ) -> httpx.Response | None:
            rewritten = f"https://127.0.0.1:{mock_upstream.port}{request_path}"
            if proxy._upstream_client:
                proxy._upstream_client._transport = httpx.AsyncHTTPTransport(
                    verify=False,
                )
            return await original_forward(method, rewritten, headers, body)

        proxy._forward_upstream = _patched_forward  # type: ignore[assignment]

        try:
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                port,
                ssl=client_ctx,
                server_hostname=target_host,
            )

            headers_dict = {
                "host": target_host,
                "content-type": "application/json",
                "content-length": str(len(request_body)),
            }
            if extra_headers:
                headers_dict.update(extra_headers)

            request_line = f"POST {request_path} HTTP/1.1\r\n"
            header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers_dict.items())
            raw_request = (request_line + header_lines + "\r\n").encode() + request_body

            writer.write(raw_request)
            await writer.drain()

            response_data = await asyncio.wait_for(reader.read(8192), timeout=10.0)
            assert response_data, "No response received from proxy"

            writer.close()
            await writer.wait_closed()

            assert len(mock_upstream.captured) > 0, "Mock upstream received no requests"
            return mock_upstream.captured[-1].headers
        finally:
            await proxy.stop()

    async def test_anthropic_credential_injection(
        self, ca: SandboxCA, ca_dir: Path, mock_upstream: MockUpstream
    ) -> None:
        """Anthropic requests get x-api-key header injected."""
        captured_headers = await self._roundtrip(
            ca,
            ca_dir,
            mock_upstream,
            credentials={"anthropic_key": "sk-ant-test-key-123"},
            target_host="api.anthropic.com",
        )
        assert captured_headers.get("x-api-key") == "sk-ant-test-key-123"
        assert "authorization" not in captured_headers

    async def test_openai_credential_injection(
        self, ca: SandboxCA, ca_dir: Path, mock_upstream: MockUpstream
    ) -> None:
        """OpenAI requests get Authorization: Bearer header injected."""
        captured_headers = await self._roundtrip(
            ca,
            ca_dir,
            mock_upstream,
            credentials={"openai_key": "sk-openai-test-key-456"},
            target_host="api.openai.com",
        )
        assert captured_headers.get("authorization") == "Bearer sk-openai-test-key-456"

    async def test_copilot_credential_injection(
        self, ca: SandboxCA, ca_dir: Path, mock_upstream: MockUpstream
    ) -> None:
        """Copilot requests get Authorization: Bearer <copilot_token>."""
        captured_headers = await self._roundtrip(
            ca,
            ca_dir,
            mock_upstream,
            credentials={"copilot_token": "ghu_copilot-test-token-789"},
            target_host="api.githubcopilot.com",
        )
        assert captured_headers.get("authorization") == "Bearer ghu_copilot-test-token-789"

    async def test_copilot_proxy_host(
        self, ca: SandboxCA, ca_dir: Path, mock_upstream: MockUpstream
    ) -> None:
        """copilot-proxy.githubusercontent.com also gets Copilot credentials."""
        captured_headers = await self._roundtrip(
            ca,
            ca_dir,
            mock_upstream,
            credentials={"copilot_token": "ghu_copilot-proxy-token"},
            target_host="copilot-proxy.githubusercontent.com",
        )
        assert captured_headers.get("authorization") == "Bearer ghu_copilot-proxy-token"

    async def test_byok_fallback_credential_injection(
        self, ca: SandboxCA, ca_dir: Path, mock_upstream: MockUpstream
    ) -> None:
        """Unknown hosts get byok_key as Bearer token (BYOK fallback)."""
        captured_headers = await self._roundtrip(
            ca,
            ca_dir,
            mock_upstream,
            credentials={"byok_key": "sk-custom-provider-key"},
            target_host="custom.llm-provider.com",
        )
        assert captured_headers.get("authorization") == "Bearer sk-custom-provider-key"

    async def test_existing_auth_headers_stripped(
        self, ca: SandboxCA, ca_dir: Path, mock_upstream: MockUpstream
    ) -> None:
        """Agent's auth headers are stripped before injection (defense in depth)."""
        captured_headers = await self._roundtrip(
            ca,
            ca_dir,
            mock_upstream,
            credentials={"anthropic_key": "sk-real-key"},
            target_host="api.anthropic.com",
            extra_headers={
                "authorization": "Bearer stolen-token",
                "x-api-key": "stolen-api-key",
            },
        )
        assert captured_headers.get("x-api-key") == "sk-real-key"
        assert captured_headers.get(
            "authorization"
        ) is None or "stolen" not in captured_headers.get("authorization", "")

    async def test_no_credentials_passthrough(
        self, ca: SandboxCA, ca_dir: Path, mock_upstream: MockUpstream
    ) -> None:
        """When no credentials are configured, no auth headers are injected."""
        captured_headers = await self._roundtrip(
            ca,
            ca_dir,
            mock_upstream,
            credentials={},
            target_host="api.anthropic.com",
        )
        assert "authorization" not in captured_headers
        assert "x-api-key" not in captured_headers


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Full Stack — CA + proxy + env scrub + mock upstream (integration)
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullStackIntegration:
    """CA + proxy + env scrub wired together, forwarding to mock upstream."""

    async def test_full_stack_anthropic(
        self,
        ca: SandboxCA,
        ca_dir: Path,
        mock_upstream: MockUpstream,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full stack: Anthropic request through proxy with env scrubbing."""
        from squadron.sandbox.env_scrub import build_sanitized_env, get_dynamic_byok_vars
        from squadron.sandbox.inference_proxy import build_credentials_from_env

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_fullstack_copilot")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fullstack")
        monkeypatch.setenv("GITHUB_APP_ID", "99999")
        monkeypatch.setenv("GITHUB_PRIVATE_KEY", "-----BEGIN RSA-----")
        monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "dashboard-key")
        monkeypatch.setenv("SAFE_VAR", "keep-this")

        config = SandboxConfig(enabled=True, ca_dir=str(ca_dir))
        extra_strip = get_dynamic_byok_vars("ANTHROPIC_API_KEY")
        sanitized = build_sanitized_env(config, ca_cert_path=ca.cert_path, extra_strip=extra_strip)

        assert "COPILOT_GITHUB_TOKEN" not in sanitized
        assert "ANTHROPIC_API_KEY" not in sanitized
        assert "GITHUB_APP_ID" not in sanitized
        assert "GITHUB_PRIVATE_KEY" not in sanitized
        assert "SQUADRON_DASHBOARD_API_KEY" not in sanitized
        assert sanitized.get("SAFE_VAR") == "keep-this"
        assert sanitized.get("SSL_CERT_FILE") == str(ca.cert_path)

        creds = build_credentials_from_env("anthropic", "ANTHROPIC_API_KEY")
        assert creds["anthropic_key"] == "sk-ant-fullstack"
        assert creds["copilot_token"] == "ghu_fullstack_copilot"

        proxy_config = SandboxConfig(
            enabled=True,
            bridge_ip="127.0.0.1",
            proxy_port=0,
            ca_dir=str(ca_dir),
        )
        proxy = InferenceProxy(proxy_config, ca, creds)
        await proxy.start()

        assert proxy._server is not None
        port = proxy._server.sockets[0].getsockname()[1]

        original_forward = proxy._forward_upstream

        async def _patched_forward(
            method: str, url: str, headers: dict[str, str], body: bytes
        ) -> httpx.Response | None:
            rewritten = f"https://127.0.0.1:{mock_upstream.port}/v1/messages"
            if proxy._upstream_client:
                proxy._upstream_client._transport = httpx.AsyncHTTPTransport(verify=False)
            return await original_forward(method, rewritten, headers, body)

        proxy._forward_upstream = _patched_forward  # type: ignore[assignment]

        try:
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                port,
                ssl=client_ctx,
                server_hostname="api.anthropic.com",
            )

            body = b'{"model": "claude-3", "messages": []}'
            raw = (
                f"POST /v1/messages HTTP/1.1\r\n"
                f"host: api.anthropic.com\r\n"
                f"content-type: application/json\r\n"
                f"content-length: {len(body)}\r\n"
                f"\r\n"
            ).encode() + body

            writer.write(raw)
            await writer.drain()

            response = await asyncio.wait_for(reader.read(8192), timeout=10.0)
            assert b"200" in response

            writer.close()
            await writer.wait_closed()

            assert len(mock_upstream.captured) >= 1
            req = mock_upstream.captured[-1]
            assert req.headers.get("x-api-key") == "sk-ant-fullstack"
            assert "authorization" not in req.headers
        finally:
            await proxy.stop()

    async def test_full_stack_copilot(
        self,
        ca: SandboxCA,
        ca_dir: Path,
        mock_upstream: MockUpstream,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full stack: Copilot request through proxy."""
        from squadron.sandbox.inference_proxy import build_credentials_from_env

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_fullstack_copilot_2")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        creds = build_credentials_from_env("copilot", "")
        assert creds["copilot_token"] == "ghu_fullstack_copilot_2"

        proxy_config = SandboxConfig(
            enabled=True,
            bridge_ip="127.0.0.1",
            proxy_port=0,
            ca_dir=str(ca_dir),
        )
        proxy = InferenceProxy(proxy_config, ca, creds)
        await proxy.start()

        assert proxy._server is not None
        port = proxy._server.sockets[0].getsockname()[1]

        original_forward = proxy._forward_upstream

        async def _patched_forward(
            method: str, url: str, headers: dict[str, str], body: bytes
        ) -> httpx.Response | None:
            rewritten = f"https://127.0.0.1:{mock_upstream.port}/chat/completions"
            if proxy._upstream_client:
                proxy._upstream_client._transport = httpx.AsyncHTTPTransport(verify=False)
            return await original_forward(method, rewritten, headers, body)

        proxy._forward_upstream = _patched_forward  # type: ignore[assignment]

        try:
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                port,
                ssl=client_ctx,
                server_hostname="api.githubcopilot.com",
            )

            body = b'{"messages": [{"role": "user", "content": "hello"}]}'
            raw = (
                f"POST /chat/completions HTTP/1.1\r\n"
                f"host: api.githubcopilot.com\r\n"
                f"content-type: application/json\r\n"
                f"content-length: {len(body)}\r\n"
                f"\r\n"
            ).encode() + body

            writer.write(raw)
            await writer.drain()

            response = await asyncio.wait_for(reader.read(8192), timeout=10.0)
            assert b"200" in response

            writer.close()
            await writer.wait_closed()

            assert len(mock_upstream.captured) >= 1
            req = mock_upstream.captured[-1]
            assert req.headers.get("authorization") == "Bearer ghu_fullstack_copilot_2"
        finally:
            await proxy.stop()
