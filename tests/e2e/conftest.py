"""Shared fixtures and infrastructure for E2E tests.

Provides:
- Ephemeral CA (real ECDSA P-256 key generation)
- Sandbox config for bridge-based testing
- NamespaceFixture: manages network namespace + veth bridge + iptables DNAT
- Skip conditions for platform requirements

All E2E tests run as root inside a container (mirroring production deployment).
They use REAL network namespaces, REAL iptables DNAT, REAL TLS, and REAL
external APIs — no mocks, no test doubles, no fake upstreams.

Platform requirements:
- Linux (not WSL2) with root privileges
- iproute2 (ip command)
- iptables
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from squadron.sandbox.ca import SandboxCA
from squadron.sandbox.config import SandboxConfig


# ── Platform detection ────────────────────────────────────────────────────────

_IS_LINUX = sys.platform == "linux"
_HAS_IP = shutil.which("ip") is not None
_HAS_IPTABLES = shutil.which("iptables") is not None
_IS_ROOT = os.getuid() == 0 if _IS_LINUX else False
_IN_CONTAINER = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
# WSL2 kernel string leaks into Docker containers (shared kernel).
# Only treat as WSL2 if we're NOT in a container.
_IS_WSL2 = _IS_LINUX and not _IN_CONTAINER and "microsoft" in os.uname().release.lower()

CAN_RUN_NAMESPACE_TESTS = _IS_LINUX and _HAS_IP and _HAS_IPTABLES and _IS_ROOT and not _IS_WSL2


# ── Network namespace constants ──────────────────────────────────────────────

NS_NAME = "sq-ns-e2e"
BRIDGE_NAME = "sq-br-e2e"
HOST_VETH = "sq-ve2e-h"
AGENT_VETH = "sq-ve2e-a"
BRIDGE_IP = "10.147.0.1"
AGENT_IP = "10.147.1.2"
SUBNET = "10.147.0.0/16"
SUBNET_BITS = "16"


# ── Shell helpers ────────────────────────────────────────────────────────────


def run_sync(cmd: str, check: bool = False) -> tuple[int, str, str]:
    """Run a shell command synchronously."""
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\nstderr: {proc.stderr}")
    return proc.returncode, proc.stdout, proc.stderr


def run_in_ns(cmd: str, ns: str = NS_NAME) -> tuple[int, str, str]:
    """Run a command inside the network namespace."""
    return run_sync(f"ip netns exec {ns} {cmd}")


async def run_async(cmd: str, timeout: float = 30) -> tuple[int, str, str]:
    """Run a shell command asynchronously."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def run_in_ns_async(cmd: str, ns: str = NS_NAME, timeout: float = 30) -> tuple[int, str, str]:
    """Run a command inside the network namespace, without blocking the event loop."""
    return await run_async(f"ip netns exec {ns} {cmd}", timeout=timeout)


# ── NamespaceFixture ─────────────────────────────────────────────────────────


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
        run_sync(f"ip link add name {BRIDGE_NAME} type bridge", check=True)
        run_sync(f"ip addr add {BRIDGE_IP}/{SUBNET_BITS} dev {BRIDGE_NAME}")
        run_sync(f"ip link set {BRIDGE_NAME} up", check=True)

        # Enable IP forwarding.
        run_sync("sysctl -w net.ipv4.ip_forward=1")

        self._bridge_up = True

    def setup_namespace(self, proxy_port: int) -> None:
        """Phase 2: Create namespace + veth + DNAT (after proxy is running)."""
        assert self._bridge_up, "Must call setup_bridge() first"
        self.proxy_port = proxy_port

        # Create namespace.
        run_sync(f"ip netns add {NS_NAME}", check=True)

        # Create veth pair.
        run_sync(
            f"ip link add {HOST_VETH} type veth peer name {AGENT_VETH}",
            check=True,
        )

        # Attach host end to bridge + bring up.
        run_sync(f"ip link set {HOST_VETH} master {BRIDGE_NAME}")
        run_sync(f"ip link set {HOST_VETH} up")

        # Move agent end into namespace.
        run_sync(f"ip link set {AGENT_VETH} netns {NS_NAME}")

        # Configure namespace networking.
        run_in_ns(f"ip addr add {AGENT_IP}/{SUBNET_BITS} dev {AGENT_VETH}")
        run_in_ns(f"ip link set {AGENT_VETH} up")
        run_in_ns("ip link set lo up")
        run_in_ns(f"ip route add default via {BRIDGE_IP}")

        # DNS inside namespace.
        ns_dir = Path(f"/etc/netns/{NS_NAME}")
        ns_dir.mkdir(parents=True, exist_ok=True)
        (ns_dir / "resolv.conf").write_text("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")

        # iptables rules.
        # DNAT: redirect all :443 from namespace → proxy.
        run_sync(
            f"iptables -t nat -A PREROUTING -s {SUBNET} "
            f"-p tcp --dport 443 -j DNAT --to-destination {BRIDGE_IP}:{self.proxy_port}"
        )
        # MASQUERADE: allow namespace traffic to reach the internet.
        run_sync(f"iptables -t nat -A POSTROUTING -s {SUBNET} ! -d {SUBNET} -j MASQUERADE")
        # FORWARD: explicitly allow traffic from/to namespace subnet.
        run_sync(f"iptables -I FORWARD 1 -s {SUBNET} -j ACCEPT")
        run_sync(f"iptables -I FORWARD 1 -d {SUBNET} -j ACCEPT")
        # INPUT: allow traffic from namespace to host (bridge IP services).
        run_sync(f"iptables -I INPUT 1 -s {SUBNET} -j ACCEPT")

        self._ns_up = True

    def teardown(self) -> None:
        """Remove all infrastructure (best-effort)."""
        if self._ns_up:
            # Remove iptables rules (best-effort, ignore errors).
            run_sync(
                f"iptables -t nat -D PREROUTING -s {SUBNET} "
                f"-p tcp --dport 443 -j DNAT --to-destination {BRIDGE_IP}:{self.proxy_port}"
            )
            run_sync(f"iptables -t nat -D POSTROUTING -s {SUBNET} ! -d {SUBNET} -j MASQUERADE")
            run_sync(f"iptables -D FORWARD -s {SUBNET} -j ACCEPT")
            run_sync(f"iptables -D FORWARD -d {SUBNET} -j ACCEPT")
            run_sync(f"iptables -D INPUT -s {SUBNET} -j ACCEPT")

            # Delete namespace (also removes agent end of veth).
            run_sync(f"ip netns delete {NS_NAME}")
            # Delete host veth (may already be gone).
            run_sync(f"ip link delete {HOST_VETH}")

            # Clean up DNS config.
            ns_dir = Path(f"/etc/netns/{NS_NAME}")
            if ns_dir.exists():
                run_sync(f"rm -rf {ns_dir}")

            self._ns_up = False

        if self._bridge_up:
            # Delete bridge.
            run_sync(f"ip link set {BRIDGE_NAME} down")
            run_sync(f"ip link delete {BRIDGE_NAME} type bridge")
            self._bridge_up = False

    def _cleanup_stale(self) -> None:
        """Remove leftover resources from previous test runs."""
        # Flush stale iptables rules referencing our subnet (try multiple ports).
        for port in [0, 8443, self.proxy_port]:
            run_sync(
                f"iptables -t nat -D PREROUTING -s {SUBNET} "
                f"-p tcp --dport 443 -j DNAT --to-destination {BRIDGE_IP}:{port}"
            )
        run_sync(f"iptables -t nat -D POSTROUTING -s {SUBNET} ! -d {SUBNET} -j MASQUERADE")
        run_sync(f"iptables -D FORWARD -s {SUBNET} -j ACCEPT")
        run_sync(f"iptables -D FORWARD -d {SUBNET} -j ACCEPT")
        run_sync(f"iptables -D INPUT -s {SUBNET} -j ACCEPT")

        # Delete stale namespace / bridge.
        run_sync(f"ip netns delete {NS_NAME}")
        run_sync(f"ip link delete {HOST_VETH}")
        run_sync(f"ip link set {BRIDGE_NAME} down")
        run_sync(f"ip link delete {BRIDGE_NAME} type bridge")

        # Clean up DNS config.
        ns_dir = Path(f"/etc/netns/{NS_NAME}")
        if ns_dir.exists():
            run_sync(f"rm -rf {ns_dir}")

        self._bridge_up = False


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


# ── Sandbox config ────────────────────────────────────────────────────────────


@pytest.fixture
def proxy_config(ca_dir: Path) -> SandboxConfig:
    """SandboxConfig for bridge-based proxy testing."""
    return SandboxConfig(
        enabled=True,
        bridge_ip=BRIDGE_IP,
        proxy_port=0,  # OS assigns free port
        ca_dir=str(ca_dir),
    )
