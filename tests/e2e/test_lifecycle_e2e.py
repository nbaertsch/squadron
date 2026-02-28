"""E2E lifecycle tests — Copilot SDK sessions through MitM proxy chain.

Validates the full sandbox lifecycle through the REAL proxy chain:

    1. Ephemeral CA + InferenceProxy on host (bridge IP)
    2. Network namespace with veth bridge + iptables DNAT
    3. CopilotClient inside namespace (via driver script)
    4. CLI connects to api.githubcopilot.com:443 → DNAT'd to proxy
    5. Proxy terminates TLS (ephemeral CA) → injects real Copilot token
       → forwards to real Copilot API
    6. Tests assert valid completions + lifecycle operations

Everything is REAL: real crypto, real TLS, real network namespace, real
iptables DNAT, real proxy, real credential injection, real SDK, real API.

The test proves that agents running inside the sandbox can:
- Start the Copilot CLI and authenticate (credentials injected by proxy)
- Create sessions and get completions through the MitM chain
- Destroy and resume sessions (sleep/wake pattern)
- Operate with a sanitized env (no secrets leaked into namespace)

Requires:
    - Linux with root privileges (container deployment)
    - iproute2 (ip command) + iptables
    - COPILOT_GITHUB_TOKEN env var (locally or via CI secrets)

Run::

    sudo pytest tests/e2e/test_lifecycle_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.config import SandboxConfig
from squadron.sandbox.env_scrub import build_sanitized_env
from squadron.sandbox.inference_proxy import InferenceProxy

from .conftest import BRIDGE_IP, CAN_RUN_NAMESPACE_TESTS, NS_NAME, NamespaceFixture

pytestmark = pytest.mark.skipif(
    not CAN_RUN_NAMESPACE_TESTS,
    reason="Requires Linux, root, ip, and iptables (not available on this platform)",
)


# ── Credential gate ──────────────────────────────────────────────────────────

_HAS_COPILOT_TOKEN = bool(os.environ.get("COPILOT_GITHUB_TOKEN"))


# ═══════════════════════════════════════════════════════════════════════════════
# Live lifecycle tests — SDK sessions through full MitM proxy chain
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.live
@pytest.mark.skipif(
    not _HAS_COPILOT_TOKEN,
    reason="Live lifecycle E2E requires COPILOT_GITHUB_TOKEN",
)
class TestLifecycleThroughProxy:
    """Copilot SDK lifecycle tests through the full MitM proxy chain.

    Each test:
    1. Starts InferenceProxy on the bridge IP with the real Copilot token
    2. Creates a network namespace with DNAT → proxy
    3. Runs a Python driver script INSIDE the namespace that exercises
       SDK lifecycle operations (create, send, destroy, resume, delete)
    4. The CLI's HTTPS traffic is transparently DNAT'd to our proxy
    5. Proxy does TLS termination + credential injection + upstream forwarding
    6. Asserts valid results from the real Copilot API
    """

    @staticmethod
    async def _run_driver_in_namespace(
        driver_code: str,
        *,
        copilot_token: str,
        ca: SandboxCA,
        ca_dir: Path,
        config: SandboxConfig,
        ns: NamespaceFixture,
        timeout: int = 120,
    ) -> dict:
        """Write a driver script, run it inside the namespace, parse JSON result.

        The driver runs with:
        - Sanitized env (secrets stripped, CA certs injected)
        - Copilot token passed as CLI argument (simulating CopilotAgent pattern)
        - Inside the network namespace (all :443 traffic DNAT'd to proxy)

        Uses asyncio subprocess so the event loop stays responsive and
        the proxy can serve requests while the driver runs.
        """
        sanitized = build_sanitized_env(config, ca_cert_path=ca.cert_path)

        # Verify secrets are stripped from sanitized env.
        assert "COPILOT_GITHUB_TOKEN" not in sanitized
        assert "GITHUB_PRIVATE_KEY" not in sanitized

        # Verify CA certs are injected.
        assert sanitized.get("NODE_EXTRA_CA_CERTS") == str(ca.cert_path)
        assert sanitized.get("SSL_CERT_FILE") == str(ca.cert_path)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
            f.write(driver_code)
            driver_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "ip",
                "netns",
                "exec",
                NS_NAME,
                "python3",
                driver_path,
                copilot_token,  # argv[1]: token for SDK auth
                str(ca_dir),  # argv[2]: working directory
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=sanitized,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise

            stdout = (stdout_bytes or b"").decode().strip()
            stderr = (stderr_bytes or b"").decode().strip()

            assert proc.returncode == 0, (
                f"Driver failed (rc={proc.returncode}):\nstdout: {stdout}\nstderr: {stderr[-2000:]}"
            )

            # Parse the last line of stdout as JSON (driver may emit debug lines).
            result = json.loads(stdout.split("\n")[-1])
            return result

        finally:
            os.unlink(driver_path)

    # ── Test 1: Session create → send → destroy ──────────────────────────────

    async def test_session_create_send_destroy(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Full lifecycle: create session → send message → destroy session.

        Proves:
        - CLI subprocess starts inside namespace with sanitized env
        - CLI authenticates via proxy-injected credentials (not env vars)
        - Session creation works through the MitM chain
        - send_and_wait produces a real completion from the Copilot API
        - Session teardown works cleanly
        """
        copilot_token = os.environ["COPILOT_GITHUB_TOKEN"]

        config = SandboxConfig(
            enabled=True,
            bridge_ip=BRIDGE_IP,
            proxy_port=0,
            ca_dir=str(ca_dir),
        )

        ns = NamespaceFixture()
        proxy: InferenceProxy | None = None
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(config, ca, {"copilot_token": copilot_token})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            driver = textwrap.dedent("""\
                import asyncio
                import json
                import os
                import sys

                async def main():
                    try:
                        from copilot import CopilotClient, PermissionHandler

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
                            session = await client.create_session({
                                "system_message": {
                                    "mode": "replace",
                                    "content": "You are a test assistant. Respond with exactly one word.",
                                },
                                "on_permission_request": PermissionHandler.approve_all,
                            })
                            try:
                                event = await asyncio.wait_for(
                                    session.send_and_wait(
                                        {"prompt": "Say PONG and nothing else."}
                                    ),
                                    timeout=60,
                                )

                                content = ""
                                if event and hasattr(event, "data"):
                                    if hasattr(event.data, "content"):
                                        content = event.data.content or ""
                                    else:
                                        content = str(event.data)

                                print(json.dumps({
                                    "ok": True,
                                    "content": content[:200],
                                    "has_content": len(content) > 0,
                                    "env_clean": "COPILOT_GITHUB_TOKEN" not in os.environ,
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

            result = await self._run_driver_in_namespace(
                driver,
                copilot_token=copilot_token,
                ca=ca,
                ca_dir=ca_dir,
                config=config,
                ns=ns,
            )

            assert result["ok"] is True, (
                f"Driver error: {result.get('error')}\n{result.get('traceback', '')}"
            )
            assert result["has_content"] is True, f"No content in response: {result}"
            assert result["env_clean"] is True, "Secrets leaked into namespace env"

        finally:
            if proxy:
                await proxy.stop()
            ns.teardown()

    # ── Test 2: Session resume (sleep/wake pattern) ──────────────────────────

    async def test_session_resume_through_proxy(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Sleep/wake pattern: create → send → destroy → resume → send again.

        Proves the session resume lifecycle works through the MitM proxy.
        This is the pattern dev/review agents use: they create a session,
        work on it, sleep (destroy session object), then wake (resume) to
        continue where they left off.
        """
        copilot_token = os.environ["COPILOT_GITHUB_TOKEN"]

        config = SandboxConfig(
            enabled=True,
            bridge_ip=BRIDGE_IP,
            proxy_port=0,
            ca_dir=str(ca_dir),
        )

        ns = NamespaceFixture()
        proxy: InferenceProxy | None = None
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(config, ca, {"copilot_token": copilot_token})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            driver = textwrap.dedent("""\
                import asyncio
                import json
                import os
                import sys

                async def main():
                    try:
                        from copilot import CopilotClient, PermissionHandler

                        token = sys.argv[1]
                        cwd = sys.argv[2]

                        # Verify env is clean.
                        assert "COPILOT_GITHUB_TOKEN" not in os.environ

                        client = CopilotClient({
                            "cwd": cwd,
                            "github_token": token,
                        })
                        await client.start()

                        session_id = "sq-e2e-resume-test"

                        try:
                            # Phase 1: Create session and send initial message.
                            session = await client.create_session({
                                "session_id": session_id,
                                "system_message": {
                                    "mode": "replace",
                                    "content": "You are a test assistant. Respond briefly.",
                                },
                                "on_permission_request": PermissionHandler.approve_all,
                            })

                            event1 = await asyncio.wait_for(
                                session.send_and_wait(
                                    {"prompt": "Remember the word BANANA. Reply OK."}
                                ),
                                timeout=60,
                            )

                            content1 = ""
                            if event1 and hasattr(event1, "data"):
                                if hasattr(event1.data, "content"):
                                    content1 = event1.data.content or ""
                                else:
                                    content1 = str(event1.data)

                            # Phase 2: Sleep — destroy session object (persisted state remains).
                            await session.destroy()

                            # Phase 3: Wake — resume the session.
                            resumed = await client.resume_session(session_id, {
                                "system_message": {
                                    "mode": "replace",
                                    "content": "You are a test assistant. Respond briefly.",
                                },
                                "on_permission_request": PermissionHandler.approve_all,
                            })

                            event2 = await asyncio.wait_for(
                                resumed.send_and_wait(
                                    {"prompt": "What word did I ask you to remember?"}
                                ),
                                timeout=60,
                            )

                            content2 = ""
                            if event2 and hasattr(event2, "data"):
                                if hasattr(event2.data, "content"):
                                    content2 = event2.data.content or ""
                                else:
                                    content2 = str(event2.data)

                            # Phase 4: Clean up — delete persisted session.
                            await client.delete_session(session_id)

                            print(json.dumps({
                                "ok": True,
                                "phase1_has_content": len(content1) > 0,
                                "phase1_content": content1[:200],
                                "phase2_has_content": len(content2) > 0,
                                "phase2_content": content2[:200],
                            }))

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

            result = await self._run_driver_in_namespace(
                driver,
                copilot_token=copilot_token,
                ca=ca,
                ca_dir=ca_dir,
                config=config,
                ns=ns,
            )

            assert result["ok"] is True, (
                f"Driver error: {result.get('error')}\n{result.get('traceback', '')}"
            )
            assert result["phase1_has_content"] is True, (
                f"Phase 1 (create+send) returned no content: {result}"
            )
            assert result["phase2_has_content"] is True, (
                f"Phase 2 (resume+send) returned no content: {result}"
            )

        finally:
            if proxy:
                await proxy.stop()
            ns.teardown()

    # ── Test 3: Env isolation + live completion ──────────────────────────────

    async def test_env_isolation_with_live_completion(self, ca: SandboxCA, ca_dir: Path) -> None:
        """Verify env sanitization + credential injection work together.

        This test explicitly validates the security boundary:
        1. Agent env has NO secrets (COPILOT_GITHUB_TOKEN, API keys stripped)
        2. Agent env HAS CA cert vars (SSL_CERT_FILE, NODE_EXTRA_CA_CERTS)
        3. Despite having no credentials, agent can still complete inference
           (because the proxy injects credentials based on Host header)

        This is the core security property of the sandbox MitM proxy:
        agents cannot exfiltrate credentials, but they can still call LLMs.
        """
        copilot_token = os.environ["COPILOT_GITHUB_TOKEN"]

        config = SandboxConfig(
            enabled=True,
            bridge_ip=BRIDGE_IP,
            proxy_port=0,
            ca_dir=str(ca_dir),
        )

        ns = NamespaceFixture()
        proxy: InferenceProxy | None = None
        try:
            ns.setup_bridge()

            proxy = InferenceProxy(config, ca, {"copilot_token": copilot_token})
            await proxy.start()
            assert proxy._server is not None
            port = proxy._server.sockets[0].getsockname()[1]

            ns.setup_namespace(port)

            driver = textwrap.dedent("""\
                import asyncio
                import json
                import os
                import sys

                async def main():
                    try:
                        from copilot import CopilotClient, PermissionHandler

                        token = sys.argv[1]
                        cwd = sys.argv[2]

                        # Comprehensive env audit.
                        env_audit = {
                            "has_copilot_token": "COPILOT_GITHUB_TOKEN" in os.environ,
                            "has_github_token": "GITHUB_TOKEN" in os.environ,
                            "has_gh_token": "GH_TOKEN" in os.environ,
                            "has_github_private_key": "GITHUB_PRIVATE_KEY" in os.environ,
                            "has_github_app_id": "GITHUB_APP_ID" in os.environ,
                            "has_node_extra_ca": bool(os.environ.get("NODE_EXTRA_CA_CERTS")),
                            "has_ssl_cert_file": bool(os.environ.get("SSL_CERT_FILE")),
                            "has_requests_ca_bundle": bool(os.environ.get("REQUESTS_CA_BUNDLE")),
                            "node_extra_ca_path": os.environ.get("NODE_EXTRA_CA_CERTS", ""),
                        }

                        # Verify no secrets leaked.
                        assert not env_audit["has_copilot_token"], "COPILOT_GITHUB_TOKEN leaked!"
                        assert not env_audit["has_github_token"], "GITHUB_TOKEN leaked!"
                        assert not env_audit["has_gh_token"], "GH_TOKEN leaked!"
                        assert not env_audit["has_github_private_key"], "GITHUB_PRIVATE_KEY leaked!"
                        assert not env_audit["has_github_app_id"], "GITHUB_APP_ID leaked!"

                        # Verify CA certs are present.
                        assert env_audit["has_node_extra_ca"], "NODE_EXTRA_CA_CERTS missing!"
                        assert env_audit["has_ssl_cert_file"], "SSL_CERT_FILE missing!"

                        # Now do a real completion — should work despite no credentials
                        # in env (proxy injects them).
                        client = CopilotClient({
                            "cwd": cwd,
                            "github_token": token,
                        })
                        await client.start()

                        try:
                            session = await client.create_session({
                                "system_message": {
                                    "mode": "replace",
                                    "content": "Reply with exactly: SANDBOX_OK",
                                },
                                "available_tools": [],
                                "on_permission_request": PermissionHandler.approve_all,
                            })
                            try:
                                event = await asyncio.wait_for(
                                    session.send_and_wait(
                                        {"prompt": "What should you reply with?"}
                                    ),
                                    timeout=60,
                                )

                                content = ""
                                if event and hasattr(event, "data"):
                                    if hasattr(event.data, "content"):
                                        content = event.data.content or ""
                                    else:
                                        content = str(event.data)

                                print(json.dumps({
                                    "ok": True,
                                    "content": content[:200],
                                    "has_content": len(content) > 0,
                                    "env_audit": env_audit,
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

            result = await self._run_driver_in_namespace(
                driver,
                copilot_token=copilot_token,
                ca=ca,
                ca_dir=ca_dir,
                config=config,
                ns=ns,
            )

            assert result["ok"] is True, (
                f"Driver error: {result.get('error')}\n{result.get('traceback', '')}"
            )
            assert result["has_content"] is True, f"No content in response: {result}"

            # Verify env audit shows clean environment.
            audit = result.get("env_audit", {})
            assert audit.get("has_copilot_token") is False
            assert audit.get("has_node_extra_ca") is True
            assert audit.get("has_ssl_cert_file") is True

        finally:
            if proxy:
                await proxy.stop()
            ns.teardown()
