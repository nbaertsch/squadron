"""Host-side MitM HTTPS proxy for sandbox inference traffic (Issue #146).

Intercepts all outbound HTTPS traffic from agent namespaces (redirected
by iptables DNAT rules configured by ``net_bridge.py``), decrypts TLS
using the ephemeral CA, injects API credentials based on the destination
host, and forwards to the upstream provider.

This proxy runs on the bridge IP (10.146.0.1) on the configured port
(default 8443).  Agents see their traffic transparently proxied — they
connect to ``api.anthropic.com:443`` which gets DNAT'd to our proxy.

Credential injection rules:
- Copilot endpoints: inject ``COPILOT_GITHUB_TOKEN`` as Bearer token
- Anthropic endpoints: inject ``x-api-key`` header
- OpenAI endpoints: inject ``Authorization: Bearer <key>`` header
- Custom providers: inject ``Authorization: Bearer <key>`` (fallback)

The proxy holds credentials in memory (host-side only) — they never
enter the agent namespace.  This follows the same security pattern as
the existing AuthBroker (see ``broker.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import tempfile
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from squadron.sandbox.ca import SandboxCA
    from squadron.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)

# Known provider host patterns → credential injection strategy.
_ANTHROPIC_HOSTS = frozenset({"api.anthropic.com"})
_OPENAI_HOSTS = frozenset({"api.openai.com"})
_COPILOT_HOSTS = frozenset(
    {
        "api.githubcopilot.com",
        "copilot-proxy.githubusercontent.com",
    }
)


class InferenceProxy:
    """Transparent MitM HTTPS proxy for agent inference traffic.

    Architecture::

        Agent namespace                   Host namespace
        ─────────────                     ──────────────
        HTTPS :443 ──DNAT──► InferenceProxy (bridge_ip:8443)
                              │ TLS terminate (ephemeral CA)
                              │ Read SNI / Host header
                              │ Inject credentials
                              │ Forward to upstream (real TLS)
                              ▼
                         upstream provider (api.anthropic.com, etc.)

    Lifecycle::

        proxy = InferenceProxy(config, ca, credentials)
        await proxy.start()
        ...
        await proxy.stop()
    """

    def __init__(
        self,
        config: SandboxConfig,
        ca: SandboxCA,
        credentials: dict[str, str],
    ) -> None:
        """
        Args:
            config: Sandbox configuration.
            ca: SandboxCA with an initialised ephemeral CA.
            credentials: Map of credential keys to values.  Expected keys:
                - "copilot_token": GitHub Copilot token
                - "anthropic_key": Anthropic API key
                - "openai_key": OpenAI API key
                (Missing keys are fine — requests to those providers
                will be forwarded without credential injection.)
        """
        self._config = config
        self._ca = ca
        self._credentials = credentials
        self._server: asyncio.AbstractServer | None = None
        self._listen_ip = config.bridge_ip
        self._listen_port = config.proxy_port
        # Cache of hostname → (ssl_context) for TLS termination.
        self._tls_contexts: dict[str, ssl.SSLContext] = {}
        # httpx client for upstream connections.
        self._upstream_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Start the proxy server on the bridge IP."""
        # Use HTTP/2 if the h2 package is available; fall back to HTTP/1.1.
        try:
            import h2 as _h2  # noqa: F401

            _use_http2 = True
        except ImportError:
            _use_http2 = False
        self._upstream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0),
            follow_redirects=True,
            http2=_use_http2,
        )

        # Create a default SSL context for incoming connections.
        # Register an SNI callback so we can dynamically switch to a
        # per-hostname certificate during the TLS handshake.
        default_ctx = self._make_ssl_context("localhost")
        default_ctx.sni_callback = self._sni_callback

        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._listen_ip,
            port=self._listen_port,
            ssl=default_ctx,
        )

        logger.info(
            "InferenceProxy: listening on %s:%d",
            self._listen_ip,
            self._listen_port,
        )

    async def stop(self) -> None:
        """Stop the proxy server."""
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        if self._upstream_client:
            await self._upstream_client.aclose()
        # Clean up temporary cert files.
        for hostname in list(self._tls_contexts):
            self._tls_contexts.pop(hostname, None)
        logger.info("InferenceProxy: stopped")

    def _make_ssl_context(self, hostname: str) -> ssl.SSLContext:
        """Create an SSL context with a leaf cert for the given hostname."""
        if hostname in self._tls_contexts:
            return self._tls_contexts[hostname]

        cert_pem, key_pem = self._ca.sign_leaf(hostname)

        # Write to temporary files (ssl.SSLContext needs file paths).
        cert_file = tempfile.NamedTemporaryFile(
            delete=False, suffix=".crt", prefix=f"sq-{hostname}-"
        )
        cert_file.write(cert_pem)
        cert_file.close()

        key_file = tempfile.NamedTemporaryFile(
            delete=False, suffix=".key", prefix=f"sq-{hostname}-"
        )
        key_file.write(key_pem)
        key_file.close()

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file.name, key_file.name)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        self._tls_contexts[hostname] = ctx

        # Clean up temp files (already loaded into SSLContext).
        os.unlink(cert_file.name)
        os.unlink(key_file.name)

        return ctx

    def _sni_callback(
        self,
        ssl_socket: ssl.SSLSocket,
        server_name: str | None,
        ssl_context: ssl.SSLContext,
    ) -> int | None:
        """TLS SNI callback — switch to a per-hostname SSL context.

        Called by the ssl module during the TLS handshake when the client
        sends a Server Name Indication extension.  We generate (or retrieve
        from cache) a leaf certificate for the requested hostname so the
        agent sees a valid cert chain for the host it thinks it is
        connecting to (e.g. api.anthropic.com).

        Returns ``None`` to continue the handshake with the new context,
        or ``ssl.ALERT_DESCRIPTION_INTERNAL_ERROR`` on failure.
        """
        if not server_name:
            return None  # No SNI — keep default "localhost" context.
        try:
            ctx = self._make_ssl_context(server_name)
            ssl_socket.context = ctx
            return None
        except Exception:
            logger.exception("InferenceProxy: failed to generate cert for %s", server_name)
            return ssl.ALERT_DESCRIPTION_INTERNAL_ERROR

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one proxied HTTPS connection from an agent."""
        try:
            # Read the HTTP request.
            request_data = await asyncio.wait_for(self._read_http_request(reader), timeout=30.0)
            if not request_data:
                return

            method, path, headers, body = request_data

            # Determine upstream host from Host header or original destination.
            host = self._extract_host(headers)
            if not host:
                await self._send_error(writer, 400, "Missing Host header")
                return

            # Inject credentials based on destination.
            injected_headers = self._inject_credentials(host, headers)

            # Forward to upstream.
            upstream_url = f"https://{host}{path}"
            response = await self._forward_upstream(method, upstream_url, injected_headers, body)

            if response:
                await self._send_response(writer, response)
            else:
                await self._send_error(writer, 502, "Upstream connection failed")

        except asyncio.TimeoutError:
            await self._send_error(writer, 408, "Request timeout")
        except Exception:
            logger.exception("InferenceProxy: error handling connection")
            try:
                await self._send_error(writer, 500, "Internal proxy error")
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_http_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str], bytes] | None:
        """Read and parse an HTTP/1.1 request.

        Returns (method, path, headers, body) or None on EOF.
        """
        # Read request line.
        request_line = await reader.readline()
        if not request_line:
            return None

        request_str = request_line.decode("latin-1").strip()
        parts = request_str.split(" ", 2)
        if len(parts) < 2:
            return None

        method = parts[0]
        path = parts[1]

        # Read headers.
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n" or line == b"\n":
                break
            decoded = line.decode("latin-1").strip()
            if ":" in decoded:
                key, _, value = decoded.partition(":")
                headers[key.strip().lower()] = value.strip()

        # Read body based on content-length.
        body = b""
        content_length = headers.get("content-length")
        if content_length:
            try:
                body = await reader.readexactly(int(content_length))
            except (asyncio.IncompleteReadError, ValueError):
                pass

        return method, path, headers, body

    def _extract_host(self, headers: dict[str, str]) -> str:
        """Extract the target hostname from request headers."""
        host = headers.get("host", "")
        # Strip port if present.
        if ":" in host:
            host = host.split(":")[0]
        return host

    def _inject_credentials(self, host: str, headers: dict[str, str]) -> dict[str, str]:
        """Inject API credentials into headers based on destination host.

        Returns a new headers dict with credentials added.
        The original Authorization / x-api-key headers from the agent
        are stripped (the agent should not have any, but defense in depth).
        """
        result = {k: v for k, v in headers.items()}

        # Strip any existing auth headers (defense in depth).
        result.pop("authorization", None)
        result.pop("x-api-key", None)

        if host in _COPILOT_HOSTS:
            token = self._credentials.get("copilot_token")
            if token:
                result["authorization"] = f"Bearer {token}"

        elif host in _ANTHROPIC_HOSTS:
            key = self._credentials.get("anthropic_key")
            if key:
                result["x-api-key"] = key

        elif host in _OPENAI_HOSTS:
            key = self._credentials.get("openai_key")
            if key:
                result["authorization"] = f"Bearer {key}"

        else:
            # Unknown host — try a generic Bearer token (BYOK fallback).
            # If the agent was configured with a custom provider, inject
            # whatever key we have for it.
            byok_key = self._credentials.get("byok_key")
            if byok_key:
                result["authorization"] = f"Bearer {byok_key}"

        logger.debug(
            "InferenceProxy: %s → credentials %s",
            host,
            "injected" if result.get("authorization") or result.get("x-api-key") else "none",
        )
        return result

    async def _forward_upstream(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
    ) -> httpx.Response | None:
        """Forward the request to the real upstream provider."""
        if not self._upstream_client:
            return None

        try:
            # Build httpx-compatible headers (remove hop-by-hop headers).
            fwd_headers = {
                k: v
                for k, v in headers.items()
                if k not in {"transfer-encoding", "connection", "keep-alive", "host"}
            }

            response = await self._upstream_client.request(
                method=method,
                url=url,
                headers=fwd_headers,
                content=body,
            )
            return response
        except Exception:
            logger.exception("InferenceProxy: upstream request failed for %s", url)
            return None

    async def _send_response(self, writer: asyncio.StreamWriter, response: httpx.Response) -> None:
        """Send the upstream response back to the agent."""
        status_line = f"HTTP/1.1 {response.status_code} {response.reason_phrase}\r\n"
        writer.write(status_line.encode("latin-1"))

        # Forward response headers.
        for key, value in response.headers.items():
            if key.lower() in {"transfer-encoding", "connection"}:
                continue
            writer.write(f"{key}: {value}\r\n".encode("latin-1"))

        body = response.content
        writer.write(f"content-length: {len(body)}\r\n".encode("latin-1"))
        writer.write(b"\r\n")
        writer.write(body)
        await writer.drain()

    async def _send_error(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        """Send an error response to the agent."""
        body = f'{{"error": "{message}"}}'.encode()
        writer.write(f"HTTP/1.1 {status} Error\r\n".encode("latin-1"))
        writer.write(b"content-type: application/json\r\n")
        writer.write(f"content-length: {len(body)}\r\n".encode("latin-1"))
        writer.write(b"\r\n")
        writer.write(body)
        await writer.drain()


def build_credentials_from_env(
    provider_type: str,
    provider_api_key_env: str,
) -> dict[str, str]:
    """Collect API credentials from the host environment.

    Called once at SandboxManager startup.  The returned dict is passed
    to InferenceProxy — credentials never enter agent namespaces.
    """
    creds: dict[str, str] = {}

    # Copilot token.
    copilot_token = os.environ.get("COPILOT_GITHUB_TOKEN")
    if copilot_token:
        creds["copilot_token"] = copilot_token

    # BYOK key (from provider config env var).
    if provider_api_key_env:
        byok_val = os.environ.get(provider_api_key_env)
        if byok_val:
            # Route to the appropriate provider slot.
            if provider_type == "anthropic":
                creds["anthropic_key"] = byok_val
            elif provider_type == "openai":
                creds["openai_key"] = byok_val
            else:
                creds["byok_key"] = byok_val

    # Also check common env vars directly (user may set both).
    for env_var, cred_key in [
        ("ANTHROPIC_API_KEY", "anthropic_key"),
        ("OPENAI_API_KEY", "openai_key"),
    ]:
        val = os.environ.get(env_var)
        if val and cred_key not in creds:
            creds[cred_key] = val

    return creds
