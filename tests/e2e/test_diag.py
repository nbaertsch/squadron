"""Diagnostic test: verify DNS + DNAT + TLS + HTTP works from namespace."""

import asyncio
import json
import os
import tempfile

import pytest

from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.config import SandboxConfig
from squadron.sandbox.env_scrub import build_sanitized_env
from squadron.sandbox.inference_proxy import InferenceProxy
from tests.e2e.conftest import (
    BRIDGE_IP,
    NS_NAME,
    CAN_RUN_NAMESPACE_TESTS,
    NamespaceFixture,
)

pytestmark = pytest.mark.skipif(not CAN_RUN_NAMESPACE_TESTS, reason="Needs root + netns")


class TestDiagnostic:
    @pytest.fixture(autouse=True)
    def _require_copilot_token(self) -> None:
        if not os.environ.get("COPILOT_GITHUB_TOKEN"):
            pytest.skip("COPILOT_GITHUB_TOKEN not set — skipping diagnostic test")

    async def test_dns_dnat_tls_http_from_namespace(self, ca: SandboxCA, ca_dir) -> None:
        """End-to-end: DNS resolve → DNAT → TLS → HTTP from namespace."""
        copilot_token = os.environ["COPILOT_GITHUB_TOKEN"]
        config = SandboxConfig(enabled=True, bridge_ip=BRIDGE_IP, proxy_port=0, ca_dir=str(ca_dir))

        proxy: InferenceProxy | None = None
        ns = NamespaceFixture()
        try:
            ns.setup_bridge()
            proxy = InferenceProxy(config, ca, {"copilot_token": copilot_token})
            await proxy.start()
            port = proxy._server.sockets[0].getsockname()[1]
            ns.setup_namespace(port)
            sanitized = build_sanitized_env(config, ca_cert_path=ca.cert_path)

            driver = """\
import ssl, socket, json, os, sys

cert_file = os.environ.get('SSL_CERT_FILE', '')
results = {}

# 1) DNS
try:
    addrs = socket.getaddrinfo('api.github.com', 443)
    ip = addrs[0][4][0]
    results['dns_ip'] = ip
    results['dns_ok'] = True
except Exception as e:
    results['dns_ok'] = False
    results['dns_error'] = str(e)
    print(json.dumps(results))
    sys.exit(1)

# 2) TCP + TLS + HTTP via hostname (DNAT should intercept)
try:
    sock = socket.create_connection(('api.github.com', 443), timeout=10)
    results['tcp_peer'] = str(sock.getpeername())

    ctx = ssl.create_default_context()
    if cert_file:
        ctx.load_verify_locations(cert_file)

    wrapped = ctx.wrap_socket(sock, server_hostname='api.github.com')
    peer_cert = wrapped.getpeercert()
    cn = dict(x[0] for x in peer_cert['subject'])['commonName']
    results['tls_cn'] = cn
    results['tls_ok'] = True

    req = b'GET /copilot_internal/user HTTP/1.1\\r\\nHost: api.github.com\\r\\nUser-Agent: diag\\r\\nConnection: close\\r\\n\\r\\n'
    wrapped.sendall(req)
    resp = b''
    while True:
        chunk = wrapped.recv(4096)
        if not chunk:
            break
        resp += chunk
    wrapped.close()
    lines = resp.decode('latin-1', errors='replace').split('\\r\\n')
    results['http_status_line'] = lines[0]
    results['http_ok'] = '200' in lines[0]
    results['resp_length'] = len(resp)
except Exception as e:
    import traceback
    results['connect_ok'] = False
    results['connect_error'] = str(e)
    results['traceback'] = traceback.format_exc()

# 3) Also try connecting to api.githubcopilot.com:443
try:
    sock2 = socket.create_connection(('api.githubcopilot.com', 443), timeout=10)
    results['copilot_tcp_peer'] = str(sock2.getpeername())
    ctx2 = ssl.create_default_context()
    if cert_file:
        ctx2.load_verify_locations(cert_file)
    wrapped2 = ctx2.wrap_socket(sock2, server_hostname='api.githubcopilot.com')
    cn2 = dict(x[0] for x in wrapped2.getpeercert()['subject'])['commonName']
    results['copilot_tls_cn'] = cn2
    results['copilot_tls_ok'] = True
    wrapped2.close()
except Exception as e:
    results['copilot_tls_ok'] = False
    results['copilot_tls_error'] = str(e)

print(json.dumps(results))
"""
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
                f.write(driver)
                driver_path = f.name

            # Print diagnostic info about iptables and routing
            ipt_proc = await asyncio.create_subprocess_exec(
                "iptables",
                "-t",
                "nat",
                "-L",
                "-n",
                "-v",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            ipt_out, _ = await ipt_proc.communicate()
            print(f"=== iptables nat rules ===\n{ipt_out.decode()}")

            route_proc = await asyncio.create_subprocess_exec(
                "ip",
                "netns",
                "exec",
                NS_NAME,
                "ip",
                "route",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            route_out, _ = await route_proc.communicate()
            print(f"=== namespace routes ===\n{route_out.decode()}")
            print(f"=== proxy port: {port}, bridge IP: {BRIDGE_IP} ===")

            try:
                proc = await asyncio.create_subprocess_exec(
                    "ip",
                    "netns",
                    "exec",
                    NS_NAME,
                    "python3",
                    driver_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=sanitized,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                out = stdout.decode().strip()
                err = stderr.decode().strip()

                print(f"STDOUT: {out}")
                if err:
                    print(f"STDERR: {err[:2000]}")

                result = json.loads(out)
                print(f"Result: {json.dumps(result, indent=2)}")

                assert result.get("dns_ok"), f"DNS failed: {result}"
                assert result.get("tls_ok"), f"TLS failed: {result}"
                assert result.get("http_ok"), f"HTTP failed: {result}"
                # If DNAT works, the TLS CN should be from our CA (api.github.com)
                assert result.get("tls_cn") == "api.github.com", (
                    f"Expected CN=api.github.com but got {result.get('tls_cn')}"
                )
                # Also verify Copilot endpoint TLS works through our proxy
                assert result.get("copilot_tls_ok"), (
                    f"Copilot TLS failed: {result.get('copilot_tls_error', 'unknown')}"
                )
                assert result.get("copilot_tls_cn") == "api.githubcopilot.com", (
                    f"Expected CN=api.githubcopilot.com but got {result.get('copilot_tls_cn')}"
                )
            finally:
                os.unlink(driver_path)

        finally:
            if proxy:
                await proxy.stop()
            ns.teardown()
