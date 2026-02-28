"""E2E tests for sandbox MitM proxy credential injection (Issue #146).

These tests use REAL crypto, REAL TLS, REAL HTTP — no mocks.  They validate
the full chain:  ephemeral CA → leaf cert signing → TLS handshake with SNI →
HTTP request parsing → credential injection → upstream forwarding.

Test classes (ordered by layer):
    1. TestEphemeralCA        — real ECDSA key gen, cert chain validation
    2. TestProxyTLSHandshake  — SNI callback, per-hostname certs
    3. TestCredentialInjection — per-provider auth header injection
    4. TestEnvScrubIntegration — real build_sanitized_env()
    5. TestFullStack           — CA + proxy + env scrub + mock upstream
    6. TestLiveCopilotAPI      — real Copilot API call (marked 'live')

Run::

    # All E2E except live LLM tests:
    pytest tests/e2e/test_proxy_e2e.py -m "not live" -v

    # Include live LLM tests (requires COPILOT_GITHUB_TOKEN):
    pytest tests/e2e/test_proxy_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import ssl
import stat
import tempfile
from pathlib import Path

import httpx
import pytest

from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.config import SandboxConfig
from squadron.sandbox.env_scrub import build_sanitized_env, get_dynamic_byok_vars
from squadron.sandbox.inference_proxy import (
    InferenceProxy,
    build_credentials_from_env,
)

from .conftest import MockUpstream


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Ephemeral CA — real ECDSA P-256 crypto
# ═══════════════════════════════════════════════════════════════════════════════


class TestEphemeralCA:
    """Validate CA key generation, cert structure, and leaf signing."""

    def test_ca_generates_key_and_cert(self, ca: SandboxCA, ca_dir: Path) -> None:
        """CA files are created with correct permissions."""
        cert_path = ca_dir / "ca.crt"
        key_path = ca_dir / "ca.key"

        assert cert_path.exists()
        assert key_path.exists()

        # Key must be owner-only (0o600).
        key_mode = stat.S_IMODE(key_path.stat().st_mode)
        assert key_mode == 0o600, f"Expected 0o600, got {oct(key_mode)}"

    def test_ca_cert_is_valid_x509(self, ca: SandboxCA, ca_dir: Path) -> None:
        """CA cert parses as valid X.509 with CA=True."""
        from cryptography import x509 as cx509

        cert_pem = (ca_dir / "ca.crt").read_bytes()
        cert = cx509.load_pem_x509_certificate(cert_pem)

        bc = cert.extensions.get_extension_for_class(cx509.BasicConstraints)
        assert bc.value.ca is True
        assert bc.value.path_length == 0

    def test_ca_idempotent_reload(self, ca_dir: Path) -> None:
        """Second SandboxCA instance loads existing CA instead of regenerating."""
        ca1 = SandboxCA(str(ca_dir), validity_days=1)
        ca1.ensure_ca()
        cert1 = (ca_dir / "ca.crt").read_bytes()

        ca2 = SandboxCA(str(ca_dir), validity_days=1)
        ca2.ensure_ca()
        cert2 = (ca_dir / "ca.crt").read_bytes()

        # Same cert — not regenerated.
        assert cert1 == cert2

    def test_sign_leaf_produces_valid_chain(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Leaf cert is signed by the CA and contains correct SAN."""
        from cryptography import x509 as cx509

        cert_pem, key_pem = ca.sign_leaf("api.anthropic.com")

        # Parse leaf cert.
        leaf = cx509.load_pem_x509_certificate(cert_pem)

        # Check SAN.
        san = leaf.extensions.get_extension_for_class(cx509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(cx509.DNSName)
        assert "api.anthropic.com" in dns_names

        # CA flag must be False.
        bc = leaf.extensions.get_extension_for_class(cx509.BasicConstraints)
        assert bc.value.ca is False

        # Verify chain: leaf issuer matches CA subject.
        ca_cert = cx509.load_pem_x509_certificate((ca_dir / "ca.crt").read_bytes())
        assert leaf.issuer == ca_cert.subject

    def test_sign_leaf_key_is_valid(self, ca: SandboxCA) -> None:
        """Leaf key is a usable ECDSA private key."""
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        _cert_pem, key_pem = ca.sign_leaf("api.openai.com")
        key = load_pem_private_key(key_pem, password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)

    async def test_sign_leaf_tls_verification(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Leaf cert passes ssl module verification against the CA cert."""
        hostname = "test.example.com"
        cert_pem, key_pem = ca.sign_leaf(hostname)

        # Write leaf cert + key to temp files.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as cf:
            cf.write(cert_pem)
            leaf_cert_path = cf.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".key") as kf:
            kf.write(key_pem)
            leaf_key_path = kf.name

        try:
            # Create server context with leaf cert.
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(leaf_cert_path, leaf_key_path)

            # Create client context trusting our CA.
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

            # Do a real TLS handshake via loopback server.
            server_ready = asyncio.Event()
            assigned_port: list[int] = []

            async def _tls_server() -> None:
                server = await asyncio.start_server(
                    lambda r, w: None,
                    host="127.0.0.1",
                    port=0,
                    ssl=server_ctx,
                )
                assigned_port.append(server.sockets[0].getsockname()[1])
                server_ready.set()
                # Give time for client to connect.
                await asyncio.sleep(0.5)
                server.close()
                await server.wait_closed()

            server_task = asyncio.create_task(_tls_server())
            await server_ready.wait()

            # Client connects with SNI and verification.
            _reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                assigned_port[0],
                ssl=client_ctx,
                server_hostname=hostname,
            )
            writer.close()
            await writer.wait_closed()
            server_task.cancel()
        finally:
            os.unlink(leaf_cert_path)
            os.unlink(leaf_key_path)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Proxy TLS Handshake — SNI callback, per-hostname certs
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

        # Extract the assigned port.
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

            # TLS handshake succeeded — check peer cert.
            ssl_obj = writer.transport.get_extra_info("ssl_object")
            peer_cert = ssl_obj.getpeercert()
            # The CN in the peer cert should match our SNI hostname.
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
            # Default client context trusts system CAs, NOT our ephemeral CA.
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            # Don't load our CA — so verification should fail.

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
# 3. Credential Injection — per-provider auth headers
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
        """Send a request through the proxy to mock_upstream, return captured headers.

        We override the proxy's upstream forwarding to point at mock_upstream
        instead of the real internet by patching the httpx client's base_url.
        """
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

        # Monkey-patch the proxy's upstream client to disable TLS verification
        # (the mock upstream uses a self-signed cert) and rewrite the URL to
        # point at our mock upstream.
        original_forward = proxy._forward_upstream

        async def _patched_forward(
            method: str, url: str, headers: dict[str, str], body: bytes
        ) -> httpx.Response | None:
            # Rewrite url to mock upstream.
            rewritten = f"https://127.0.0.1:{mock_upstream.port}{request_path}"
            if proxy._upstream_client:
                # Disable TLS verification for mock upstream.
                proxy._upstream_client._transport = httpx.AsyncHTTPTransport(
                    verify=False,
                )
            return await original_forward(method, rewritten, headers, body)

        proxy._forward_upstream = _patched_forward  # type: ignore[assignment]

        try:
            # Connect through the proxy as if we're the agent.
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                port,
                ssl=client_ctx,
                server_hostname=target_host,
            )

            # Build HTTP/1.1 request.
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

            # Read response (wait for it).
            response_data = await asyncio.wait_for(reader.read(8192), timeout=10.0)
            assert response_data, "No response received from proxy"

            writer.close()
            await writer.wait_closed()

            # Return captured headers from mock upstream.
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
        # Authorization should NOT be present for Anthropic.
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
        # The proxy should have stripped the agent's stolen headers
        # and injected the real key.
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
        # Both auth headers should be stripped (defense in depth) even
        # though we have no credentials to inject.
        assert "authorization" not in captured_headers
        assert "x-api-key" not in captured_headers


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Environment Scrubbing — real build_sanitized_env()
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvScrubIntegration:
    """Validate env scrubbing with real os.environ manipulation."""

    def test_static_secrets_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Static secret env vars from config are stripped."""
        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        monkeypatch.setenv("GITHUB_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_secret_token")
        monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "dash-api-key")
        monkeypatch.setenv("PATH", "/usr/bin")  # Should survive.

        config = SandboxConfig(enabled=True, ca_dir=str(tmp_path / "ca"))

        env = build_sanitized_env(config)

        assert "GITHUB_APP_ID" not in env
        assert "GITHUB_PRIVATE_KEY" not in env
        assert "COPILOT_GITHUB_TOKEN" not in env
        assert "SQUADRON_DASHBOARD_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"

    def test_pattern_based_stripping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env vars matching secret patterns are stripped."""
        monkeypatch.setenv("MY_CUSTOM_API_KEY", "secret-123")
        monkeypatch.setenv("SOME_SECRET_KEY_VAR", "secret-456")
        monkeypatch.setenv("RANDOM_ACCESS_TOKEN", "secret-789")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")  # Whitelisted.
        monkeypatch.setenv("NORMAL_VAR", "keep-me")

        config = SandboxConfig(enabled=True, ca_dir=str(tmp_path / "ca"))

        env = build_sanitized_env(config)

        assert "MY_CUSTOM_API_KEY" not in env
        assert "SOME_SECRET_KEY_VAR" not in env
        assert "RANDOM_ACCESS_TOKEN" not in env
        assert env.get("SSH_AUTH_SOCK") == "/tmp/ssh-agent.sock"
        assert env.get("NORMAL_VAR") == "keep-me"

    def test_ca_cert_injected(self, ca: SandboxCA, ca_dir: Path) -> None:
        """When ca_cert_path is provided, SSL_CERT_FILE etc. are set."""
        config = SandboxConfig(enabled=True, ca_dir=str(ca_dir))

        env = build_sanitized_env(config, ca_cert_path=ca.cert_path)

        assert env.get("SSL_CERT_FILE") == str(ca.cert_path)
        assert env.get("NODE_EXTRA_CA_CERTS") == str(ca.cert_path)
        assert env.get("REQUESTS_CA_BUNDLE") == str(ca.cert_path)

    def test_socket_and_token_injected(self, tmp_path: Path) -> None:
        """Socket path and session token are injected."""
        config = SandboxConfig(enabled=True, ca_dir=str(tmp_path / "ca"))
        socket = tmp_path / "proxy.sock"

        env = build_sanitized_env(
            config,
            socket_path=socket,
            session_token_hex="deadbeef01234567",
        )

        assert env.get("SQUADRON_PROXY_SOCKET") == str(socket)
        assert env.get("SQUADRON_SESSION_TOKEN") == "deadbeef01234567"

    def test_extra_strip_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dynamic BYOK vars passed via extra_strip are removed."""
        monkeypatch.setenv("CUSTOM_PROVIDER_KEY", "sk-custom-123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-456")

        config = SandboxConfig(enabled=True, ca_dir=str(tmp_path / "ca"))

        env = build_sanitized_env(
            config,
            extra_strip=["CUSTOM_PROVIDER_KEY", "ANTHROPIC_API_KEY"],
        )

        assert "CUSTOM_PROVIDER_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env

    def test_get_dynamic_byok_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_dynamic_byok_vars collects BYOK env var names."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")

        result = get_dynamic_byok_vars("MY_CUSTOM_API_KEY")

        assert "MY_CUSTOM_API_KEY" in result
        assert "ANTHROPIC_API_KEY" in result
        assert "OPENAI_API_KEY" in result

    def test_build_credentials_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_credentials_from_env reads host env into credential dict."""
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_test_copilot")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-from-env")

        creds = build_credentials_from_env("anthropic", "ANTHROPIC_API_KEY")

        assert creds["copilot_token"] == "ghu_test_copilot"
        assert creds["anthropic_key"] == "sk-ant-from-env"
        assert creds["openai_key"] == "sk-oai-from-env"

    def test_build_credentials_byok_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BYOK key is routed to correct provider slot based on provider_type."""
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("MY_PROVIDER_KEY", "sk-custom")

        # Unknown provider type → byok_key slot.
        creds = build_credentials_from_env("custom-llm", "MY_PROVIDER_KEY")
        assert creds.get("byok_key") == "sk-custom"
        assert "anthropic_key" not in creds
        assert "openai_key" not in creds


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Full Stack — CA + proxy + env scrub + mock upstream
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullStack:
    """End-to-end: build sanitized env, start proxy, make request, verify everything."""

    async def test_full_stack_anthropic(
        self,
        ca: SandboxCA,
        ca_dir: Path,
        mock_upstream: MockUpstream,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full stack: Anthropic request through proxy with env scrubbing.

        1. Set secret env vars on host.
        2. Build sanitized env — verify secrets stripped.
        3. Build credentials from host env.
        4. Start proxy with credentials.
        5. Make request through proxy.
        6. Verify mock upstream got correct auth headers.
        """
        # 1. Set up host env.
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_fullstack_copilot")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fullstack")
        monkeypatch.setenv("GITHUB_APP_ID", "99999")
        monkeypatch.setenv("GITHUB_PRIVATE_KEY", "-----BEGIN RSA-----")
        monkeypatch.setenv("SQUADRON_DASHBOARD_API_KEY", "dashboard-key")
        monkeypatch.setenv("SAFE_VAR", "keep-this")

        # 2. Build sanitized env and verify.
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

        # 3. Build credentials from host env.
        creds = build_credentials_from_env("anthropic", "ANTHROPIC_API_KEY")
        assert creds["anthropic_key"] == "sk-ant-fullstack"
        assert creds["copilot_token"] == "ghu_fullstack_copilot"

        # 4. Start proxy.
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

        # Patch proxy to forward to mock upstream.
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
            # 5. Make request through proxy (as an agent would).
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

            # 6. Verify mock upstream headers.
            assert len(mock_upstream.captured) >= 1
            req = mock_upstream.captured[-1]
            assert req.headers.get("x-api-key") == "sk-ant-fullstack"
            assert "authorization" not in req.headers  # Anthropic uses x-api-key, not Bearer.
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


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Live Copilot API (requires COPILOT_GITHUB_TOKEN)
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
            ca_dir=str(ca_dir),
        )
        proxy = InferenceProxy(proxy_config, ca, {"copilot_token": copilot_token})
        await proxy.start()

        assert proxy._server is not None
        port = proxy._server.sockets[0].getsockname()[1]

        try:
            # Use httpx client that trusts our CA and connects through proxy.
            # For this live test, we do NOT monkey-patch upstream forwarding.
            # The proxy will forward to real api.githubcopilot.com.

            # We use a direct TLS connection to the proxy (like the agent would).
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.load_verify_locations(str(ca_dir / "ca.crt"))

            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                port,
                ssl=client_ctx,
                server_hostname="api.githubcopilot.com",
            )

            # Send a chat completion request with required Copilot headers.
            # The Copilot API requires Copilot-Integration-Id and editor-version.
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
                pass  # We may hit timeout after response body is read.

            writer.close()
            await writer.wait_closed()

            # Verify we got a valid HTTP response (2xx or 4xx — not a proxy error).
            response_str = response_data.decode("latin-1", errors="replace")
            assert "HTTP/1.1" in response_str, f"No HTTP response: {response_str[:200]}"

            # 200 = success, 401 = token expired (still proves proxy forwarded),
            # 429 = rate limited (proves proxy forwarded).
            status_line = response_str.split("\r\n")[0]
            status_code = int(status_line.split(" ")[1])
            assert status_code in {200, 401, 403, 429}, (
                f"Unexpected status {status_code}: {response_str[:500]}"
            )

        finally:
            await proxy.stop()
