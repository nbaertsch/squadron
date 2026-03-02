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
- GitHub API endpoints (``api.github.com``, ``github.com``): inject
  ``COPILOT_GITHUB_TOKEN`` as Bearer token (needed for CLI auth exchange)
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
        "api.individual.githubcopilot.com",
        "api.business.githubcopilot.com",
        "api.enterprise.githubcopilot.com",
        "copilot-proxy.githubusercontent.com",
    }
)
# GitHub API hosts used by the Copilot CLI during auth token exchange.
# The CLI connects to api.github.com to swap the GitHub PAT for a
# short-lived Copilot session token.  These need the same copilot_token.
_GITHUB_AUTH_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
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
            follow_redirects=False,
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
        """Handle a proxied HTTPS connection from an agent.

        Supports HTTP/1.1 keep-alive: loops reading requests on the same
        TCP+TLS connection until the client sends ``Connection: close``,
        the connection is idle too long, or EOF is reached.  This is
        critical because the Copilot CLI binary (undici) reuses
        connections for multiple requests (auth → GraphQL → inference).
        """
        try:
            while True:
                # Read the next HTTP request (with idle timeout).
                try:
                    request_data = await asyncio.wait_for(
                        self._read_http_request(reader), timeout=60.0
                    )
                except asyncio.TimeoutError:
                    break  # Idle timeout — close the connection.
                except ValueError as exc:
                    # Malformed / oversized request — return 4xx and close.
                    logger.warning("InferenceProxy: bad request: %s", exc)
                    try:
                        await self._send_error(writer, 400, str(exc))
                    except Exception:
                        pass
                    break

                if not request_data:
                    break  # EOF / client closed.

                method, path, headers, body = request_data

                # Determine upstream host from Host header.
                host = self._extract_host(headers)
                if not host:
                    await self._send_error(writer, 400, "Missing Host header")
                    break

                # Check if client wants to close after this request.
                connection_header = headers.get("connection", "").lower()
                close_after = connection_header == "close"

                # Inject credentials based on destination.
                injected_headers = self._inject_credentials(host, headers)

                # Forward to upstream.
                upstream_url = f"https://{host}{path}"
                response = await self._forward_upstream(
                    method, upstream_url, injected_headers, body
                )

                if response:
                    await self._send_response(writer, response, keep_alive=not close_after)
                else:
                    await self._send_error(writer, 502, "Upstream connection failed")
                    break  # Can't reliably continue after a proxy error.

                if close_after:
                    break

        except (ConnectionResetError, BrokenPipeError):
            pass  # Client disconnected — normal for keep-alive.
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

    # Maximum allowed request body size (50 MB).
    _MAX_BODY_SIZE = 50 * 1024 * 1024
    # Maximum number of request headers.
    _MAX_HEADERS = 200
    # Maximum length of a single header line (16 KB).
    _MAX_HEADER_LINE = 16 * 1024

    async def _read_http_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str], bytes] | None:
        """Read and parse an HTTP/1.1 request.

        Returns (method, path, headers, body) or None on EOF.
        Raises ValueError for malformed/oversized requests.
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

        # Read headers (bounded by count and line length).
        headers: dict[str, str] = {}
        for _ in range(self._MAX_HEADERS):
            line = await reader.readline()
            if not line or line == b"\r\n" or line == b"\n":
                break
            if len(line) > self._MAX_HEADER_LINE:
                raise ValueError(f"Header line too long ({len(line)} bytes)")
            decoded = line.decode("latin-1").strip()
            if ":" in decoded:
                key, _, value = decoded.partition(":")
                headers[key.strip().lower()] = value.strip()
        else:
            raise ValueError(f"Too many headers (>{self._MAX_HEADERS})")

        # Reject chunked Transfer-Encoding — we only support Content-Length.
        if "chunked" in headers.get("transfer-encoding", "").lower():
            raise ValueError("Chunked Transfer-Encoding not supported")

        # Read body based on content-length.
        body = b""
        content_length = headers.get("content-length")
        if content_length:
            try:
                cl = int(content_length)
            except ValueError:
                raise ValueError(f"Invalid Content-Length: {content_length}")
            if cl > self._MAX_BODY_SIZE:
                raise ValueError(f"Request body too large ({cl} bytes, max {self._MAX_BODY_SIZE})")
            try:
                body = await reader.readexactly(cl)
            except asyncio.IncompleteReadError as exc:
                logger.warning(
                    "InferenceProxy: client disconnected mid-body (%d/%d bytes received)",
                    len(exc.partial),
                    cl,
                )
                return None  # Client disconnected — abort this request.

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

        elif host in _GITHUB_AUTH_HOSTS:
            # GitHub API hosts — the CLI uses these during auth token
            # exchange.  Inject the same copilot_token as Bearer.
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
        """Forward the request to the real upstream provider.

        Redirects are followed manually (up to 5 hops) so we can strip
        sensitive headers (Authorization, x-api-key) on cross-origin
        redirects.  This prevents credential leakage if an upstream
        returns a 3xx to a different host.
        """
        if not self._upstream_client:
            return None

        _MAX_REDIRECTS = 5

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

            # Manually follow redirects, stripping auth on cross-origin.
            for _ in range(_MAX_REDIRECTS):
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break

                redirect_url = response.headers.get("location")
                if not redirect_url:
                    break

                # Resolve relative URLs.
                if redirect_url.startswith("/"):
                    from urllib.parse import urlparse

                    parsed = urlparse(url)
                    redirect_url = f"{parsed.scheme}://{parsed.netloc}{redirect_url}"

                # Strip auth headers if the redirect goes to a different host.
                from urllib.parse import urlparse

                orig_host = urlparse(url).netloc
                new_host = urlparse(redirect_url).netloc
                if orig_host != new_host:
                    fwd_headers.pop("authorization", None)
                    fwd_headers.pop("x-api-key", None)
                    logger.info(
                        "InferenceProxy: cross-origin redirect %s → %s, stripping auth headers",
                        orig_host,
                        new_host,
                    )

                # 303: always GET with no body; 307/308: preserve method+body.
                if response.status_code == 303:
                    method = "GET"
                    body = b""

                url = redirect_url
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

    async def _send_response(
        self, writer: asyncio.StreamWriter, response: httpx.Response, *, keep_alive: bool = True
    ) -> None:
        """Send the upstream response back to the agent.

        Important: httpx automatically decompresses gzip/br/deflate bodies,
        so ``response.content`` is always uncompressed.  We must strip the
        upstream ``Content-Encoding`` and ``Content-Length`` headers and
        set our own ``Content-Length`` based on the actual (decompressed)
        body size.  Forwarding the original ``Content-Encoding: gzip``
        with a decompressed body would cause the client to either fail
        decompression or hang waiting for more data.
        """
        status_line = f"HTTP/1.1 {response.status_code} {response.reason_phrase}\r\n"
        writer.write(status_line.encode("latin-1"))

        # Hop-by-hop and content-encoding headers that must NOT be forwarded.
        # httpx decodes content-encoding transparently, so we send the raw body.
        _skip_headers = {
            "transfer-encoding",
            "connection",
            "keep-alive",
            "content-encoding",
            "content-length",
        }

        # Forward response headers.
        for key, value in response.headers.items():
            if key.lower() in _skip_headers:
                continue
            writer.write(f"{key}: {value}\r\n".encode("latin-1"))

        body = response.content
        writer.write(f"content-length: {len(body)}\r\n".encode("latin-1"))

        # Signal keep-alive or close to the client.
        if keep_alive:
            writer.write(b"connection: keep-alive\r\n")
        else:
            writer.write(b"connection: close\r\n")

        writer.write(b"\r\n")
        writer.write(body)
        await writer.drain()

    async def _send_error(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        """Send an error response to the agent and signal connection close."""
        import json as _json

        body = _json.dumps({"error": message}).encode()
        writer.write(f"HTTP/1.1 {status} Error\r\n".encode("latin-1"))
        writer.write(b"content-type: application/json\r\n")
        writer.write(f"content-length: {len(body)}\r\n".encode("latin-1"))
        writer.write(b"connection: close\r\n")
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
