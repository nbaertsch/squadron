"""Network bridge for sandbox namespace isolation (Issue #146).

Creates a host-side Linux bridge (``sq-br0``) and per-agent veth pairs so
that every agent namespace has outbound connectivity *only* through the
host bridge.  Iptables DNAT rules redirect all HTTPS (port 443) traffic
from agent namespaces to the MitM inference proxy running on the bridge IP.

Architecture::

    ┌─────────────────────────────────────────┐
    │  Host network namespace                 │
    │                                         │
    │  sq-br0 (10.146.0.1/16)                │
    │    ├── sq-veth-<id>-h  ←→  sq-veth-<id>-a (agent ns)  10.146.<idx>.2/16
    │    └── sq-veth-<id>-h  ←→  sq-veth-<id>-a (agent ns)  10.146.<idx>.2/16
    │                                         │
    │  iptables: DNAT :443 → 10.146.0.1:8443 │
    │  InferenceProxy listens on :8443        │
    └─────────────────────────────────────────┘

All ``ip`` / ``iptables`` commands are executed as async subprocesses.
On non-Linux hosts (dev/test), all methods degrade gracefully (no-op).
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from squadron.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _tools_available() -> bool:
    """Check that ip and iptables are on PATH."""
    return _is_linux() and shutil.which("ip") is not None and shutil.which("iptables") is not None


@dataclass
class VethPair:
    """Metadata for one agent's veth pair."""

    agent_id: str
    agent_index: int  # 1-based; used to derive IP: 10.146.<idx>.2
    host_iface: str  # e.g. sq-veth-abc-h
    agent_iface: str  # e.g. sq-veth-abc-a
    agent_ip: str  # e.g. 10.146.1.2
    netns_name: str  # e.g. sq-ns-abc


async def _run(cmd: str) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


class NetworkBridge:
    """Manages the host bridge and per-agent veth pairs.

    Lifecycle (managed by SandboxManager)::

        bridge = NetworkBridge(config)
        await bridge.setup_bridge()          # once at startup
        veth = await bridge.create_veth(agent_id, index)   # per agent
        ...
        await bridge.destroy_veth(veth)      # per agent teardown
        await bridge.teardown_bridge()       # on shutdown
    """

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._available = _tools_available() and config.enabled
        self._bridge_name = config.bridge_name
        self._bridge_ip = config.bridge_ip
        self._bridge_subnet = config.bridge_subnet
        self._proxy_port = config.proxy_port
        self._bridge_up = False
        # Track next available agent index (1-based).
        self._next_index = 1

    @property
    def is_available(self) -> bool:
        return self._available

    # ── Bridge Lifecycle ─────────────────────────────────────────────────────

    async def setup_bridge(self) -> bool:
        """Create the host bridge and configure IP + iptables.

        Returns True if bridge was created, False if unavailable/skipped.
        """
        if not self._available:
            if self._config.enabled:
                logger.warning(
                    "NetworkBridge: requested but ip/iptables not available "
                    "— running without network isolation"
                )
            return False

        br = self._bridge_name
        ip = self._bridge_ip
        subnet_bits = self._bridge_subnet.split("/")[1]

        # Create bridge interface.
        rc, _, err = await _run(f"ip link add name {br} type bridge")
        if rc != 0 and "File exists" not in err:
            logger.error("Failed to create bridge %s: %s", br, err)
            return False

        # Assign IP to bridge.
        await _run(f"ip addr add {ip}/{subnet_bits} dev {br}")
        # Bring bridge up.
        rc, _, err = await _run(f"ip link set {br} up")
        if rc != 0:
            logger.error("Failed to bring up bridge %s: %s", br, err)
            return False

        # Enable IP forwarding for bridge traffic.
        await _run("sysctl -w net.ipv4.ip_forward=1")

        # NAT: masquerade outbound traffic from bridge subnet (so agent
        # traffic can reach the internet via the host).
        await _run(
            f"iptables -t nat -A POSTROUTING -s {self._bridge_subnet} "
            f"! -d {self._bridge_subnet} -j MASQUERADE"
        )

        # DNAT: redirect all HTTPS (:443) from bridge subnet to our proxy.
        await _run(
            f"iptables -t nat -A PREROUTING -s {self._bridge_subnet} "
            f"-p tcp --dport 443 -j DNAT --to-destination {ip}:{self._proxy_port}"
        )

        self._bridge_up = True
        logger.info(
            "NetworkBridge: bridge %s up at %s/%s, HTTPS→proxy:%d",
            br,
            ip,
            subnet_bits,
            self._proxy_port,
        )
        return True

    async def teardown_bridge(self) -> None:
        """Remove bridge, iptables rules, and cleanup."""
        if not self._bridge_up:
            return

        br = self._bridge_name
        ip = self._bridge_ip

        # Remove iptables rules (best-effort).
        await _run(
            f"iptables -t nat -D PREROUTING -s {self._bridge_subnet} "
            f"-p tcp --dport 443 -j DNAT --to-destination {ip}:{self._proxy_port}"
        )
        await _run(
            f"iptables -t nat -D POSTROUTING -s {self._bridge_subnet} "
            f"! -d {self._bridge_subnet} -j MASQUERADE"
        )

        # Bring bridge down and delete.
        await _run(f"ip link set {br} down")
        await _run(f"ip link delete {br} type bridge")

        self._bridge_up = False
        logger.info("NetworkBridge: bridge %s torn down", br)

    # ── Per-Agent Veth Lifecycle ─────────────────────────────────────────────

    def allocate_index(self) -> int:
        """Allocate and return the next agent index (1-based)."""
        idx = self._next_index
        self._next_index += 1
        return idx

    async def create_veth(self, agent_id: str, agent_index: int) -> VethPair | None:
        """Create a veth pair and network namespace for one agent.

        Returns VethPair metadata, or None if network isolation is unavailable.
        """
        if not self._available or not self._bridge_up:
            return None

        # Derive short suffix from agent_id (max 8 chars for interface name limits).
        suffix = agent_id[-8:] if len(agent_id) > 8 else agent_id
        host_iface = f"sq-vh-{suffix}"
        agent_iface = f"sq-va-{suffix}"
        netns_name = f"sq-ns-{suffix}"
        agent_ip = f"10.146.{agent_index}.2"
        subnet_bits = self._bridge_subnet.split("/")[1]

        # Create named network namespace.
        rc, _, err = await _run(f"ip netns add {netns_name}")
        if rc != 0:
            logger.error("Failed to create netns %s: %s", netns_name, err)
            return None

        # Create veth pair.
        rc, _, err = await _run(f"ip link add {host_iface} type veth peer name {agent_iface}")
        if rc != 0:
            logger.error("Failed to create veth pair: %s", err)
            await _run(f"ip netns delete {netns_name}")
            return None

        # Attach host end to bridge.
        await _run(f"ip link set {host_iface} master {self._bridge_name}")
        await _run(f"ip link set {host_iface} up")

        # Move agent end into namespace.
        await _run(f"ip link set {agent_iface} netns {netns_name}")

        # Configure agent namespace networking.
        ns = netns_name
        await _run(f"ip netns exec {ns} ip addr add {agent_ip}/{subnet_bits} dev {agent_iface}")
        await _run(f"ip netns exec {ns} ip link set {agent_iface} up")
        await _run(f"ip netns exec {ns} ip link set lo up")
        await _run(f"ip netns exec {ns} ip route add default via {self._bridge_ip}")

        veth = VethPair(
            agent_id=agent_id,
            agent_index=agent_index,
            host_iface=host_iface,
            agent_iface=agent_iface,
            agent_ip=agent_ip,
            netns_name=netns_name,
        )
        logger.info(
            "NetworkBridge: created veth for %s (ns=%s, ip=%s)",
            agent_id,
            netns_name,
            agent_ip,
        )
        return veth

    async def destroy_veth(self, veth: VethPair) -> None:
        """Tear down one agent's veth pair and network namespace."""
        if not self._available:
            return

        # Delete the namespace (which also removes the veth agent end).
        await _run(f"ip netns delete {veth.netns_name}")
        # The host end is auto-deleted when the peer is removed, but clean
        # up explicitly in case it lingers.
        await _run(f"ip link delete {veth.host_iface}")

        logger.info("NetworkBridge: destroyed veth for %s", veth.agent_id)

    def wrap_command_in_netns(self, veth: VethPair, cmd: list[str]) -> list[str]:
        """Wrap a command to execute inside the agent's network namespace.

        This replaces the bare ``unshare --net`` from the old SandboxNamespace.
        Other namespaces (mount, pid, ipc, uts) are still applied via unshare.
        """
        if not self._available:
            return cmd
        return ["ip", "netns", "exec", veth.netns_name] + cmd
