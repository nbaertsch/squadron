"""Linux namespace and seccomp-bpf isolation for sandbox processes.

Uses native Linux kernel interfaces only - no third-party dependencies.
Requires Linux kernel >= 3.8 for namespaces, >= 3.5 for seccomp-bpf.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform
import shutil
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from squadron.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)

# BPF / seccomp constants
_BPF_LD = 0x00
_BPF_W = 0x00
_BPF_ABS = 0x20
_BPF_JMP = 0x05
_BPF_JEQ = 0x10
_BPF_K = 0x00
_BPF_RET = 0x06
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_SET_MODE_FILTER = 1
_AUDIT_ARCH_X86_64 = 0xC000003E
_PR_SET_NO_NEW_PRIVS = 38
_NR_SECCOMP_X86_64 = 317
_ARCH_OFFSET = 4
_SYSCALL_NR_OFFSET = 0

# Allowlist of safe syscalls for agent worktree processes (x86_64).
_SAFE_SYSCALLS_X86_64: frozenset[int] = frozenset([
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28,
    32, 33, 35, 39, 41, 42, 43, 44, 45, 46, 47, 48, 49,
    50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62,
    63, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83,
    84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96,
    97, 98, 99, 100, 102, 104, 107, 108, 110, 111,
    158, 186, 217, 218, 228, 229, 230, 231, 232, 233,
    234, 257, 258, 259, 260, 261, 262, 263, 264, 265,
    266, 267, 268, 269, 270, 271, 280, 281, 282, 283,
    284, 285, 286, 287, 288, 290, 291, 292, 293, 302,
    316, 318, 332,
])


def is_linux() -> bool:
    return platform.system() == "Linux"


def unshare_available() -> bool:
    return is_linux() and shutil.which("unshare") is not None


class SandboxNamespace:
    """Wraps a command in Linux namespace isolation.

    When namespace isolation is unavailable (non-Linux host or unshare
    not found), methods degrade gracefully: wrap_command() returns the
    original command unchanged.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._available = unshare_available() and config.enabled

    def wrap_command(self, cmd: list[str]) -> list[str]:
        """Wrap a command to run inside the configured namespaces.

        Uses unshare from util-linux. Falls back to the unwrapped
        command if namespace isolation is not available.
        """
        if not self._available:
            if self._config.enabled:
                logger.warning(
                    "Namespace isolation requested but unshare not available "
                    "-- running without namespace isolation"
                )
            return cmd

        unshare_args = ["unshare"]

        if self._config.namespace_mount:
            unshare_args.append("--mount")
        if self._config.namespace_pid:
            unshare_args.append("--pid")
            unshare_args.append("--fork")
        if self._config.namespace_net:
            unshare_args.append("--net")
        if self._config.namespace_ipc:
            unshare_args.append("--ipc")
        if self._config.namespace_uts:
            unshare_args.append("--uts")

        unshare_args.append("--map-root-user")

        return unshare_args + ["--"] + cmd

    def apply_seccomp_filter(self) -> bool:
        """Apply seccomp-bpf allowlist filter to the current process.

        Must be called in the subprocess (after fork, before exec) or in
        a process already in its final privilege state.

        Returns True if filter was installed, False if not applicable.
        """
        if not self._config.enabled or not self._config.seccomp_enabled:
            return False

        if not is_linux():
            return False

        try:
            return _install_seccomp_allowlist(_SAFE_SYSCALLS_X86_64)
        except Exception:
            logger.warning("Failed to install seccomp filter", exc_info=True)
            return False


def _bpf_stmt(code: int, k: int) -> bytes:
    """Encode a BPF statement instruction (jt=0, jf=0)."""
    return struct.pack("<HBBI", code, 0, 0, k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> bytes:
    """Encode a BPF jump instruction."""
    return struct.pack("<HBBI", code, jt, jf, k)


def _install_seccomp_allowlist(allowed: frozenset[int]) -> bool:
    """Install a BPF seccomp allowlist filter via the seccomp(2) syscall."""
    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        raise OSError("libc not found")
    libc = ctypes.CDLL(libc_name, use_errno=True)

    instructions: list[bytes] = []

    instructions.append(_bpf_stmt(_BPF_LD | _BPF_W | _BPF_ABS, _ARCH_OFFSET))
    instructions.append(_bpf_jump(_BPF_JMP | _BPF_JEQ | _BPF_K, _AUDIT_ARCH_X86_64, 1, 0))
    instructions.append(_bpf_stmt(_BPF_RET | _BPF_K, _SECCOMP_RET_KILL_PROCESS))

    instructions.append(_bpf_stmt(_BPF_LD | _BPF_W | _BPF_ABS, _SYSCALL_NR_OFFSET))

    sorted_allowed = sorted(allowed)
    n_allowed = len(sorted_allowed)

    for i, nr in enumerate(sorted_allowed):
        jt = n_allowed - i
        instructions.append(_bpf_jump(_BPF_JMP | _BPF_JEQ | _BPF_K, nr, jt - 1, 0))

    instructions.append(_bpf_stmt(_BPF_RET | _BPF_K, _SECCOMP_RET_KILL_PROCESS))
    instructions.append(_bpf_stmt(_BPF_RET | _BPF_K, _SECCOMP_RET_ALLOW))

    n_instructions = len(instructions)
    filter_bytes = b"".join(instructions)

    FilterArr = ctypes.c_uint8 * len(filter_bytes)
    filter_arr = FilterArr(*filter_bytes)

    class sock_fprog(ctypes.Structure):
        _fields_ = [
            ("len", ctypes.c_ushort),
            ("filter", ctypes.POINTER(ctypes.c_uint8)),
        ]

    prog = sock_fprog(n_instructions, filter_arr)

    ret = libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))

    ret = libc.syscall(_NR_SECCOMP_X86_64, _SECCOMP_SET_MODE_FILTER, 0, ctypes.byref(prog))
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))

    logger.info("seccomp-bpf allowlist filter installed (%d syscalls allowed)", len(allowed))
    return True
