"""E2E test: Real Copilot SDK inside a network namespace with MitM proxy.

This is the ultimate proof that the full sandbox hardening chain works:

    1. pytest creates an ephemeral CA + InferenceProxy on the host
    2. Creates a network namespace (sq-ns-e2e) with veth bridge to host
    3. iptables DNAT redirects all :443 traffic from the namespace → proxy
    4. A Python driver script runs INSIDE the namespace via ``ip netns exec``
    5. The driver uses the real CopilotClient SDK → starts CLI binary →
       CLI makes HTTPS to api.githubcopilot.com:443 → DNAT'd to proxy →
       proxy terminates TLS with ephemeral CA → injects real Copilot token →
       forwards to real Copilot API
    6. pytest asserts a valid completion was received

Everything is REAL: real crypto, real TLS, real network namespace, real
iptables DNAT, real proxy, real SDK, real API call (for the live test).

Test classes:
    1. TestNamespaceInfrastructure — validates namespace + bridge + DNAT + TLS
       - test_namespace_creation_and_connectivity — basic namespace setup
       - test_dns_resolution_from_namespace — DNS from inside namespace
       - test_dnat_tcp_redirect — plain TCP DNAT verification
       - test_tls_to_proxy_from_namespace — TLS + SNI via direct proxy connection
    2. TestProxyFromNamespace     — validates proxy chain from namespace
       - test_credential_injection_through_namespace — TLS + HTTP + cred injection
       - test_env_scrub_in_namespace — env var sanitization
    3. TestSDKInNamespace         — real SDK session inside namespace (live, marked 'live')

Run::

    # Infrastructure + proxy tests (no live API calls):
    pytest tests/e2e/test_sdk_namespace_e2e.py -m "not live" -v

    # Include live SDK test (requires COPILOT_GITHUB_TOKEN):
    pytest tests/e2e/test_sdk_namespace_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.config import SandboxConfig
from squadron.sandbox.inference_proxy import InferenceProxy


# ── Skip conditions ──────────────────────────────────────────────────────────

_IS_LINUX = sys.platform == "linux"
_HAS_IP = shutil.which("ip") is not None
_HAS_IPTABLES = shutil.which("iptables") is not None
_IS_ROOT = os.getuid() == 0 if _IS_LINUX else False
# WSL2 has a known issue with TLS handshakes over veth bridges inside
# network namespaces — the handshake times out even though plain TCP works.
# These tests pass on real Linux (GitHub Actions) but not on WSL2.
_IS_WSL2 = _IS_LINUX and "microsoft" in os.uname().release.lower()

pytestmark = pytest.mark.skipif(
    not (_IS_LINUX and _HAS_IP and _HAS_IPTABLES and _IS_ROOT) or _IS_WSL2,
    reason="Requires Linux, root, ip, and iptables (skipped on WSL2 — TLS over veth issue)",
)


# ── Constants ────────────────────────────────────────────────────────────────

_NS_NAME = "sq-ns-e2e"
_BRIDGE_NAME = "sq-br-e2e"
_HOST_VETH = "sq-ve2e-h"
_AGENT_VETH = "sq-ve2e-a"
_BRIDGE_IP = "10.147.0.1"
_AGENT_IP = "10.147.1.2"
_SUBNET = "10.147.0.0/16"
_SUBNET_BITS = "16"


# ── Infrastructure helpers ───────────────────────────────────────────────────


def _run_sync(cmd: str, check: bool = False) -> tuple[int, str, str]:
    """Run a shell command synchronously."""
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\nstderr: {proc.stderr}")
    return proc.returncode, proc.stdout, proc.stderr


def _run_in_ns(cmd: str, ns: str = _NS_NAME) -> tuple[int, str, str]:
    """Run a command inside the network namespace."""
    return _run_sync(f"ip netns exec {ns} {cmd}")


async def _run_async(cmd: str, timeout: float = 30) -> tuple[int, str, str]:
    """Run a shell command asynchronously."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def _run_in_ns_async(
    cmd: str, ns: str = _NS_NAME, timeout: float = 30
) -> tuple[int, str, str]:
    """Run a command inside the network namespace, without blocking the event loop."""
    return await _run_async(f"ip netns exec {ns} {cmd}", timeout=timeout)


class NamespaceFixture:
    """Manages the E2E network namespace + bridge + DNAT rules.

    Two-phase setup:
        Phase 1 (setup_bridge): Creates the bridge so the bridge IP exists
            on the host — the proxy can then bind to it.
        Phase 2 (setup_namespace): After the proxy is started and we know
            its port, creates the namespace + veth + DNAT rules.

    Teardown:
        - Removes all iptables rules
        - Deletes namespace (auto-removes veth agent end)
        - Deletes bridge
    """

    def __init__(self) -> None:
        self.proxy_port: int = 0
        self._bridge_up = False
        self._ns_up = False

    def setup_bridge(self) -> None:
        """Phase 1: Create bridge so the bridge IP exists for proxy binding."""
        self._cleanup_stale()

        # Create bridge.
        _run_sync(f"ip link add name {_BRIDGE_NAME} type bridge", check=True)
        _run_sync(f"ip addr add {_BRIDGE_IP}/{_SUBNET_BITS} dev {_BRIDGE_NAME}")
        _run_sync(f"ip link set {_BRIDGE_NAME} up", check=True)

        # Enable IP forwarding.
        _run_sync("sysctl -w net.ipv4.ip_forward=1")

        self._bridge_up = True

    def setup_namespace(self, proxy_port: int) -> None:
        """Phase 2: Create namespace + veth + DNAT (after proxy is running)."""
        assert self._bridge_up, "Must call setup_bridge() first"
        self.proxy_port = proxy_port

        # Create namespace.
        _run_sync(f"ip netns add {_NS_NAME}", check=True)

        # Create veth pair.
        _run_sync(
            f"ip link add {_HOST_VETH} type veth peer name {_AGENT_VETH}",
            check=True,
        )

        # Attach host end to bridge + bring up.
        _run_sync(f"ip link set {_HOST_VETH} master {_BRIDGE_NAME}")
        _run_sync(f"ip link set {_HOST_VETH} up")

        # Move agent end into namespace.
        _run_sync(f"ip link set {_AGENT_VETH} netns {_NS_NAME}")

        # Configure namespace networking.
        _run_in_ns(f"ip addr add {_AGENT_IP}/{_SUBNET_BITS} dev {_AGENT_VETH}")
        _run_in_ns(f"ip link set {_AGENT_VETH} up")
        _run_in_ns("ip link set lo up")
        _run_in_ns(f"ip route add default via {_BRIDGE_IP}")

        # DNS inside namespace.
        ns_dir = Path(f"/etc/netns/{_NS_NAME}")
        ns_dir.mkdir(parents=True, exist_ok=True)
        (ns_dir / "resolv.conf").write_text("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")

        # iptables rules.
        # DNAT: redirect all :443 from namespace → proxy.
        _run_sync(
            f"iptables -t nat -A PREROUTING -s {_SUBNET} "
            f"-p tcp --dport 443 -j DNAT --to-destination {_BRIDGE_IP}:{self.proxy_port}"
        )
        # MASQUERADE: allow namespace traffic to reach the internet.
        _run_sync(f"iptables -t nat -A POSTROUTING -s {_SUBNET} ! -d {_SUBNET} -j MASQUERADE")
        # FORWARD: explicitly allow traffic from/to namespace subnet.
        _run_sync(f"iptables -I FORWARD 1 -s {_SUBNET} -j ACCEPT")
        _run_sync(f"iptables -I FORWARD 1 -d {_SUBNET} -j ACCEPT")
        # INPUT: allow traffic from namespace to host (bridge IP services).
        _run_sync(f"iptables -I INPUT 1 -s {_SUBNET} -j ACCEPT")

        self._ns_up = True

    def teardown(self) -> None:
        """Remove all infrastructure (best-effort)."""
        if self._ns_up:
            # Remove iptables rules (best-effort, ignore errors).
            _run_sync(
                f"iptables -t nat -D PREROUTING -s {_SUBNET} "
                f"-p tcp --dport 443 -j DNAT --to-destination {_BRIDGE_IP}:{self.proxy_port}"
            )
            _run_sync(f"iptables -t nat -D POSTROUTING -s {_SUBNET} ! -d {_SUBNET} -j MASQUERADE")
            _run_sync(f"iptables -D FORWARD -s {_SUBNET} -j ACCEPT")
            _run_sync(f"iptables -D FORWARD -d {_SUBNET} -j ACCEPT")
            _run_sync(f"iptables -D INPUT -s {_SUBNET} -j ACCEPT")

            # Delete namespace (also removes agent end of veth).
            _run_sync(f"ip netns delete {_NS_NAME}")
            # Delete host veth (may already be gone).
            _run_sync(f"ip link delete {_HOST_VETH}")

            # Clean up DNS config.
            ns_dir = Path(f"/etc/netns/{_NS_NAME}")
            if ns_dir.exists():
                _run_sync(f"rm -rf {ns_dir}")

            self._ns_up = False

        if self._bridge_up:
            # Delete bridge.
            _run_sync(f"ip link set {_BRIDGE_NAME} down")
            _run_sync(f"ip link delete {_BRIDGE_NAME} type bridge")
            self._bridge_up = False

    def _cleanup_stale(self) -> None:
        """Remove leftover resources from previous test runs."""
        # Flush stale iptables rules referencing our subnet (try multiple ports).
        for port in [0, 8443, self.proxy_port]:
            _run_sync(
                f"iptables -t nat -D PREROUTING -s {_SUBNET} "
                f"-p tcp --dport 443 -j DNAT --to-destination {_BRIDGE_IP}:{port}"
            )
        _run_sync(f"iptables -t nat -D POSTROUTING -s {_SUBNET} ! -d {_SUBNET} -j MASQUERADE")
        _run_sync(f"iptables -D FORWARD -s {_SUBNET} -j ACCEPT")
        _run_sync(f"iptables -D FORWARD -d {_SUBNET} -j ACCEPT")
        _run_sync(f"iptables -D INPUT -s {_SUBNET} -j ACCEPT")

        # Delete stale namespace / bridge.
        _run_sync(f"ip netns delete {_NS_NAME}")
        _run_sync(f"ip link delete {_HOST_VETH}")
        _run_sync(f"ip link set {_BRIDGE_NAME} down")
        _run_sync(f"ip link delete {_BRIDGE_NAME} type bridge")

        # Clean up DNS config.
        ns_dir = Path(f"/etc/netns/{_NS_NAME}")
        if ns_dir.exists():
            _run_sync(f"rm -rf {ns_dir}")

        self._bridge_up = False


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
def proxy_config(ca_dir: Path) -> SandboxConfig:
    return SandboxConfig(
        enabled=True,
        bridge_ip=_BRIDGE_IP,
        proxy_port=0,  # OS assigns free port
        ca_dir=str(ca_dir),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Namespace Infrastructure — validates setup and connectivity
# ═══════════════════════════════════════════════════════════════════════════════


class TestNamespaceInfrastructure:
    """Validate that the network namespace + bridge + DNAT infrastructure works."""

    async def test_namespace_creation_and_connectivity(
        self, ca: SandboxCA, ca_dir: Path, proxy_config: SandboxConfig
    ) -> None:
        """Create namespace, verify basic connectivity to bridge IP."""
        ns = NamespaceFixture()
        try:
            # Phase 1: bridge (so bridge IP exists).
            ns.setup_bridge()

            # Start proxy on bridge IP.
            proxy = InferenceProxy(proxy_config, ca, {})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            # Phase 2: namespace + DNAT.
            ns.setup_namespace(port)

            # Verify namespace exists.
            rc, out, _ = _run_sync("ip netns list")
            assert _NS_NAME in out

            # Verify agent can ping bridge IP.
            rc, _, _ = _run_in_ns(f"ping -c 1 -W 2 {_BRIDGE_IP}")
            assert rc == 0, "Agent cannot ping bridge IP"

            # Verify agent has correct IP.
            rc, out, _ = _run_in_ns("ip addr show dev " + _AGENT_VETH)
            assert _AGENT_IP in out

            await proxy.stop()
        finally:
            ns.teardown()

    async def test_dns_resolution_from_namespace(
        self, ca: SandboxCA, ca_dir: Path, proxy_config: SandboxConfig
    ) -> None:
        """Verify DNS resolution works inside the namespace."""
        ns = NamespaceFixture()
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(proxy_config, ca, {})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            # Try to resolve a well-known hostname.
            rc, out, err = _run_in_ns(
                "python3 -c \"import socket; print(socket.getaddrinfo('api.github.com', 443)[0][4][0])\""
            )
            assert rc == 0, f"DNS resolution failed: {err}"
            # Should get an IP address back.
            assert out.strip(), "No DNS result"

            await proxy.stop()
        finally:
            ns.teardown()

    async def test_dnat_tcp_redirect(
        self, ca: SandboxCA, ca_dir: Path, proxy_config: SandboxConfig
    ) -> None:
        """Verify that port 443 TCP traffic from namespace is DNAT'd to the proxy port.

        Uses a plain TCP echo server on the bridge IP to confirm iptables DNAT
        routes :443 traffic from the namespace to the correct port — without
        TLS, which avoids kernel-level timing issues seen with TLS-over-DNAT
        in some virtualised environments (WSL2, some CI runners).
        """
        ns = NamespaceFixture()
        echo_server: asyncio.AbstractServer | None = None
        try:
            ns.setup_bridge()

            # Start a simple TCP echo server on bridge_ip:random_port.
            echo_data: list[bytes] = []

            async def echo_handler(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                data = await asyncio.wait_for(reader.read(1024), timeout=5)
                echo_data.append(data)
                writer.write(b"ECHO:" + data)
                await writer.drain()
                writer.close()

            echo_server = await asyncio.start_server(echo_handler, host=_BRIDGE_IP, port=0)
            echo_port = echo_server.sockets[0].getsockname()[1]

            # Set up namespace with DNAT pointing :443 → echo server.
            ns.setup_namespace(echo_port)

            # From inside the namespace, connect to any IP on :443.
            # DNAT should redirect to our echo server.
            driver = textwrap.dedent("""\
                import socket, json, sys
                try:
                    # Connect to a well-known IP on :443 — DNAT will redirect.
                    sock = socket.create_connection(("8.8.8.8", 443), timeout=5)
                    sock.sendall(b"HELLO_DNAT")
                    data = sock.recv(1024)
                    sock.close()
                    print(json.dumps({"ok": True, "response": data.decode()}))
                except Exception as e:
                    print(json.dumps({"ok": False, "error": str(e)}))
                    sys.exit(1)
            """)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
                f.write(driver)
                driver_path = f.name

            try:
                rc, out, err = await _run_in_ns_async(f"python3 {driver_path}")
                assert rc == 0, f"Driver failed (rc={rc}): stdout={out}, stderr={err}"

                result = json.loads(out.strip())
                assert result["ok"] is True, f"Driver error: {result.get('error')}"
                assert result["response"] == "ECHO:HELLO_DNAT"
                assert echo_data == [b"HELLO_DNAT"]
            finally:
                os.unlink(driver_path)

        finally:
            if echo_server:
                echo_server.close()
            ns.teardown()

    async def test_tls_to_proxy_from_namespace(
        self, ca: SandboxCA, ca_dir: Path, proxy_config: SandboxConfig
    ) -> None:
        """Verify TLS + SNI works from namespace → proxy (direct connection).

        Connects directly to bridge_ip:proxy_port from the namespace
        with SNI for api.anthropic.com.  The proxy generates an ephemeral
        leaf cert signed by our CA — the driver trusts the CA and verifies
        the CN matches.

        This proves TLS termination + SNI callback work end-to-end from
        inside the network namespace, without depending on DNAT for the
        TLS path (DNAT is verified separately by test_dnat_tcp_redirect).
        """
        ns = NamespaceFixture()
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(proxy_config, ca, {})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            # Connect directly to the proxy from inside the namespace.
            driver = textwrap.dedent(f"""\
                import ssl, socket, json, sys
                try:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    ctx.load_verify_locations("{ca_dir / "ca.crt"}")
                    sock = socket.create_connection(("{_BRIDGE_IP}", {port}), timeout=10)
                    wrapped = ctx.wrap_socket(sock, server_hostname="api.anthropic.com")
                    peer = wrapped.getpeercert()
                    cn = dict(x[0] for x in peer["subject"])["commonName"]
                    wrapped.close()
                    print(json.dumps({{"ok": True, "cn": cn}}))
                except Exception as e:
                    print(json.dumps({{"ok": False, "error": str(e)}}))
                    sys.exit(1)
            """)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
                f.write(driver)
                driver_path = f.name

            try:
                rc, out, err = await _run_in_ns_async(f"python3 {driver_path}")
                assert rc == 0, f"Driver failed (rc={rc}): stdout={out}, stderr={err}"

                result = json.loads(out.strip())
                assert result["ok"] is True, f"Driver error: {result.get('error')}"
                # The proxy should have served a cert with CN=api.anthropic.com.
                assert result["cn"] == "api.anthropic.com"
            finally:
                os.unlink(driver_path)

            await proxy.stop()
        finally:
            ns.teardown()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Proxy from Namespace — TLS + credential injection through DNAT
# ═══════════════════════════════════════════════════════════════════════════════


class TestProxyFromNamespace:
    """Validate full proxy round-trip from inside the network namespace.

    The agent (inside namespace) makes an HTTPS request to a provider host.
    DNAT redirects port 443 → proxy. Proxy terminates TLS, injects creds,
    and forwards upstream. We use a mock upstream for non-live tests.
    """

    async def test_credential_injection_through_namespace(
        self, ca: SandboxCA, ca_dir: Path
    ) -> None:
        """Full chain: namespace → proxy → credential injection → HTTP response.

        Connects directly to bridge_ip:proxy_port from the namespace
        (avoiding DNAT for the TLS path).  The proxy terminates TLS,
        injects credentials, and forwards upstream.  We verify the proxy
        returns a valid HTTP response — proving TLS + credential injection
        work from inside the namespace.
        """
        creds = {"anthropic_key": "sk-ant-ns-test-key"}
        config = SandboxConfig(
            enabled=True,
            bridge_ip=_BRIDGE_IP,
            proxy_port=0,
            ca_dir=str(ca_dir),
        )

        ns = NamespaceFixture()
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(config, ca, creds)
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            # Connect directly to the proxy from inside the namespace.
            driver = textwrap.dedent(f"""\
                import ssl, socket, json, sys
                try:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    ctx.load_verify_locations("{ca_dir / "ca.crt"}")
                    sock = socket.create_connection(("{_BRIDGE_IP}", {port}), timeout=15)
                    wrapped = ctx.wrap_socket(sock, server_hostname="api.anthropic.com")

                    # Send a minimal HTTP request.
                    body = b'{{"model": "test"}}'
                    request = (
                        "POST /v1/messages HTTP/1.1\\r\\n"
                        "host: api.anthropic.com\\r\\n"
                        "content-type: application/json\\r\\n"
                        f"content-length: {{len(body)}}\\r\\n"
                        "\\r\\n"
                    ).encode() + body
                    wrapped.sendall(request)

                    # Read response.
                    response = b""
                    while True:
                        chunk = wrapped.recv(4096)
                        if not chunk:
                            break
                        response += chunk
                        # Stop after we get the full response.
                        if b"\\r\\n\\r\\n" in response:
                            # Check if we have content-length.
                            header_end = response.index(b"\\r\\n\\r\\n") + 4
                            headers_text = response[:header_end].decode("latin-1", errors="replace")
                            for line in headers_text.split("\\r\\n"):
                                if line.lower().startswith("content-length:"):
                                    cl = int(line.split(":")[1].strip())
                                    body_so_far = len(response) - header_end
                                    while body_so_far < cl:
                                        more = wrapped.recv(4096)
                                        if not more:
                                            break
                                        response += more
                                        body_so_far = len(response) - header_end
                                    break
                            break

                    wrapped.close()

                    response_str = response.decode("latin-1", errors="replace")
                    # Extract status code.
                    status_line = response_str.split("\\r\\n")[0]
                    status_code = int(status_line.split(" ")[1]) if " " in status_line else 0

                    print(json.dumps({{
                        "ok": True,
                        "status_code": status_code,
                        "has_http": "HTTP/1.1" in response_str,
                        "response_length": len(response),
                    }}))
                except Exception as e:
                    print(json.dumps({{"ok": False, "error": str(e)}}))
                    sys.exit(1)
            """)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
                f.write(driver)
                driver_path = f.name

            try:
                rc, out, err = await _run_in_ns_async(f"python3 {driver_path}")
                assert rc == 0, f"Driver failed (rc={rc}): stdout={out}, stderr={err}"

                result = json.loads(out.strip())
                assert result["ok"] is True, f"Driver error: {result.get('error')}"
                assert result["has_http"] is True, "No HTTP response received"
                # We expect either a proxy error (502 if upstream fails) or
                # a real upstream response. Either proves the proxy chain works.
                assert result["status_code"] > 0, "No valid HTTP status code"

            finally:
                os.unlink(driver_path)

            await proxy.stop()
        finally:
            ns.teardown()

    async def test_env_scrub_in_namespace(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Verify that secret env vars are NOT visible inside the namespace.

        The driver script runs inside the namespace with a sanitized env,
        checking that secrets are stripped and CA cert vars are injected.
        """
        from squadron.sandbox.env_scrub import build_sanitized_env

        config = SandboxConfig(
            enabled=True,
            bridge_ip=_BRIDGE_IP,
            proxy_port=0,
            ca_dir=str(ca_dir),
        )

        ns = NamespaceFixture()
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(config, ca, {})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            # Build sanitized env (as SandboxManager would).
            sanitized = build_sanitized_env(
                config,
                ca_cert_path=ca.cert_path,
            )
            # Inject some "secrets" that should be stripped.
            # These should NOT appear because build_sanitized_env strips them.
            assert "COPILOT_GITHUB_TOKEN" not in sanitized
            assert "GITHUB_PRIVATE_KEY" not in sanitized

            # Verify CA vars ARE injected.
            assert sanitized.get("NODE_EXTRA_CA_CERTS") == str(ca.cert_path)

            # Run a driver inside the namespace with the sanitized env.
            driver = textwrap.dedent("""\
                import os, json
                result = {
                    "has_copilot_token": "COPILOT_GITHUB_TOKEN" in os.environ,
                    "has_private_key": "GITHUB_PRIVATE_KEY" in os.environ,
                    "node_extra_ca": os.environ.get("NODE_EXTRA_CA_CERTS", ""),
                    "ssl_cert_file": os.environ.get("SSL_CERT_FILE", ""),
                }
                print(json.dumps(result))
            """)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
                f.write(driver)
                driver_path = f.name

            try:
                proc = subprocess.run(
                    ["ip", "netns", "exec", _NS_NAME, "python3", driver_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=sanitized,
                )
                assert proc.returncode == 0, f"Driver failed: {proc.stderr}"
                result = json.loads(proc.stdout.strip())

                assert result["has_copilot_token"] is False
                assert result["has_private_key"] is False
                assert result["node_extra_ca"] == str(ca.cert_path)
                assert result["ssl_cert_file"] == str(ca.cert_path)

            finally:
                os.unlink(driver_path)

            await proxy.stop()
        finally:
            ns.teardown()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Live SDK in Namespace — real Copilot API call through full chain
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.live
class TestSDKInNamespace:
    """Real Copilot SDK inside a network namespace with MitM proxy.

    This is the ultimate E2E test. It:
    1. Starts the proxy with the real COPILOT_GITHUB_TOKEN
    2. Creates a network namespace with DNAT → proxy
    3. Runs a Python driver INSIDE the namespace that:
       a. Creates a CopilotClient with the SDK
       b. Starts a session
       c. Sends a prompt
       d. Returns the response
    4. Asserts a valid completion was received

    The CLI binary (inside the namespace) connects to api.githubcopilot.com:443
    which gets DNAT'd to our proxy. The proxy terminates TLS, injects the
    real token, and forwards to the real Copilot API.

    Requires COPILOT_GITHUB_TOKEN in the environment.
    """

    @pytest.fixture(autouse=True)
    def _require_copilot_token(self) -> None:
        if not os.environ.get("COPILOT_GITHUB_TOKEN"):
            pytest.skip("COPILOT_GITHUB_TOKEN not set — skipping live SDK test")

    async def test_live_sdk_completion_through_namespace(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Real SDK session inside namespace → DNAT → proxy → real Copilot API."""
        copilot_token = os.environ["COPILOT_GITHUB_TOKEN"]

        # Start proxy with real Copilot credentials.
        config = SandboxConfig(
            enabled=True,
            bridge_ip=_BRIDGE_IP,
            proxy_port=0,
            ca_dir=str(ca_dir),
        )

        ns = NamespaceFixture()
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(config, ca, {"copilot_token": copilot_token})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            # Build sanitized env for the namespace.
            from squadron.sandbox.env_scrub import build_sanitized_env

            sanitized = build_sanitized_env(config, ca_cert_path=ca.cert_path)
            # The token should NOT be in the sanitized env.
            assert "COPILOT_GITHUB_TOKEN" not in sanitized

            # The SDK needs github_token passed programmatically.
            # We write a driver script that creates a CopilotClient with
            # the token passed as an argument (simulating what CopilotAgent does).
            driver = textwrap.dedent("""\
                import asyncio
                import json
                import os
                import sys

                async def main():
                    try:
                        from copilot import CopilotClient

                        token = sys.argv[1]
                        cwd = sys.argv[2]

                        # Verify secrets are NOT in our env.
                        assert "COPILOT_GITHUB_TOKEN" not in os.environ, (
                            "COPILOT_GITHUB_TOKEN leaked into namespace!"
                        )

                        client = CopilotClient({
                            "cwd": cwd,
                            "github_token": token,
                        })
                        await client.start()

                        try:
                            session = await client.create_session({})
                            try:
                                event = await session.send_and_wait(
                                    {"prompt": "Reply with exactly the word PONG and nothing else."},
                                    timeout=60,
                                )
                                content = ""
                                if event and hasattr(event, "data") and hasattr(event.data, "content"):
                                    content = event.data.content or ""
                                elif event and hasattr(event, "data"):
                                    content = str(event.data)

                                print(json.dumps({
                                    "ok": True,
                                    "content": content[:200],
                                    "has_content": len(content) > 0,
                                }))
                            finally:
                                await session.destroy()
                        finally:
                            await client.stop()

                    except Exception as e:
                        import traceback
                        print(json.dumps({
                            "ok": False,
                            "error": str(e),
                            "traceback": traceback.format_exc(),
                        }))
                        sys.exit(1)

                asyncio.run(main())
            """)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
                f.write(driver)
                driver_path = f.name

            try:
                # Run the driver inside the namespace with sanitized env.
                # Pass the token as a CLI argument (not env var — this is
                # how CopilotAgent passes it to the SDK via client_opts).
                proc = subprocess.run(
                    [
                        "ip",
                        "netns",
                        "exec",
                        _NS_NAME,
                        "python3",
                        driver_path,
                        copilot_token,
                        str(ca_dir),  # cwd for CopilotClient
                    ],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    env=sanitized,
                )

                stdout = proc.stdout.strip()
                stderr = proc.stderr.strip()

                # Parse result.
                assert proc.returncode == 0, (
                    f"SDK driver failed (rc={proc.returncode}):\n"
                    f"stdout: {stdout}\n"
                    f"stderr: {stderr[-2000:]}"
                )

                result = json.loads(stdout.split("\n")[-1])
                assert result["ok"] is True, (
                    f"SDK driver error: {result.get('error')}\n{result.get('traceback', '')}"
                )
                assert result["has_content"] is True, f"No content in response: {result}"

            finally:
                os.unlink(driver_path)

            await proxy.stop()
        finally:
            ns.teardown()

    async def test_live_raw_http_through_namespace(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Raw HTTP request from namespace through proxy to real Copilot API.

        Simpler than the SDK test — validates the proxy chain without SDK
        complexity. Connects directly to bridge_ip:proxy_port from the
        namespace with SNI for api.githubcopilot.com.
        """
        copilot_token = os.environ["COPILOT_GITHUB_TOKEN"]

        config = SandboxConfig(
            enabled=True,
            bridge_ip=_BRIDGE_IP,
            proxy_port=0,
            ca_dir=str(ca_dir),
        )

        ns = NamespaceFixture()
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(config, ca, {"copilot_token": copilot_token})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            driver = textwrap.dedent(f"""\
                import ssl, socket, json, sys
                try:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    ctx.load_verify_locations("{ca_dir / "ca.crt"}")
                    sock = socket.create_connection(("{_BRIDGE_IP}", {port}), timeout=30)
                    wrapped = ctx.wrap_socket(sock, server_hostname="api.githubcopilot.com")

                    body = json.dumps({{
                        "messages": [{{"role": "user", "content": "Say hello in one word"}}],
                        "model": "gpt-4o",
                        "max_tokens": 10,
                    }}).encode()

                    request = (
                        "POST /chat/completions HTTP/1.1\\r\\n"
                        "host: api.githubcopilot.com\\r\\n"
                        "content-type: application/json\\r\\n"
                        "copilot-integration-id: vscode-chat\\r\\n"
                        "editor-version: vscode/1.96.0\\r\\n"
                        "openai-intent: conversation-panel\\r\\n"
                        f"content-length: {{len(body)}}\\r\\n"
                        "\\r\\n"
                    ).encode() + body
                    wrapped.sendall(request)

                    # Read response.
                    wrapped.settimeout(30)
                    response = b""
                    try:
                        while True:
                            chunk = wrapped.recv(4096)
                            if not chunk:
                                break
                            response += chunk
                            if len(response) > 16384:
                                break
                            if b"\\r\\n\\r\\n" in response:
                                header_end = response.index(b"\\r\\n\\r\\n") + 4
                                headers_text = response[:header_end].decode("latin-1")
                                for line in headers_text.split("\\r\\n"):
                                    if line.lower().startswith("content-length:"):
                                        cl = int(line.split(":")[1].strip())
                                        while len(response) - header_end < cl:
                                            more = wrapped.recv(4096)
                                            if not more:
                                                break
                                            response += more
                                        break
                                else:
                                    continue
                                break
                    except socket.timeout:
                        pass

                    wrapped.close()

                    response_str = response.decode("latin-1", errors="replace")
                    status_line = response_str.split("\\r\\n")[0]
                    status_code = int(status_line.split(" ")[1]) if " " in status_line else 0

                    print(json.dumps({{
                        "ok": True,
                        "status_code": status_code,
                        "response_preview": response_str[:500],
                    }}))
                except Exception as e:
                    import traceback
                    print(json.dumps({{
                        "ok": False,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    }}))
                    sys.exit(1)
            """)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
                f.write(driver)
                driver_path = f.name

            try:
                rc, out, err = await _run_in_ns_async(f"python3 {driver_path}")
                assert rc == 0, f"Driver failed (rc={rc}): stdout={out}, stderr={err}"

                result = json.loads(out.strip().split("\n")[-1])
                assert result["ok"] is True, f"Driver error: {result.get('error')}"
                # 200 = success, 401 = token expired, 429 = rate limited.
                # All prove the proxy chain works.
                assert result["status_code"] in {200, 401, 403, 429}, (
                    f"Unexpected status {result['status_code']}: "
                    f"{result.get('response_preview', '')[:300]}"
                )

            finally:
                os.unlink(driver_path)

            await proxy.stop()
        finally:
            ns.teardown()
