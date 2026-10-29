"""Classic BPF seccomp filters for the sandbox, built without libseccomp.

A network namespace does not cover AF_VSOCK, which reaches the Windows host
directly under WSL2, and io_uring can create sockets without the socket
syscall. The filter closes both paths for every process in the sandbox.
"""

from __future__ import annotations

import errno
import platform
import struct

# struct seccomp_data offsets.
_NR = 0
_ARCH = 4
_ARG0_LOW = 16  # Little-endian low word of args[0]; the kernel truncates int args.

_LD_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ = 0x15     # BPF_JMP | BPF_JEQ | BPF_K
_JGE = 0x35     # BPF_JMP | BPF_JGE | BPF_K
_RET = 0x06     # BPF_RET | BPF_K

_ALLOW = 0x7FFF0000
_KILL_PROCESS = 0x80000000
_ERRNO = 0x00050000

AF_VSOCK = 40
_X32_SYSCALL_BIT = 0x40000000

# machine: (audit arch, socket, socketpair). io_uring numbers are shared.
_ARCHES = {
    "x86_64": (0xC000003E, 41, 53),
    "aarch64": (0xC00000B7, 198, 199),
}
_IO_URING = (425, 426, 427)  # io_uring_setup, io_uring_enter, io_uring_register


def supported(machine: str | None = None) -> bool:
    return (machine or platform.machine()) in _ARCHES


def _instruction(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def build(*, block_vsock: bool = True, machine: str | None = None) -> bytes:
    """Return a filter program for bwrap --seccomp.

    Foreign-ABI syscalls (i386, x32, arm32) are killed so they cannot reach
    socket through a different syscall table.
    """
    machine = machine or platform.machine()
    if machine not in _ARCHES:
        raise ValueError(machine)
    audit_arch, socket_nr, socketpair_nr = _ARCHES[machine]
    program = [
        _instruction(_LD_ABS, 0, 0, _ARCH),
        _instruction(_JEQ, 1, 0, audit_arch),
        _instruction(_RET, 0, 0, _KILL_PROCESS),
        _instruction(_LD_ABS, 0, 0, _NR),
    ]
    if machine == "x86_64":
        program += [
            _instruction(_JGE, 0, 1, _X32_SYSCALL_BIT),
            _instruction(_RET, 0, 0, _KILL_PROCESS),
        ]
    if block_vsock:
        for number in (socket_nr, socketpair_nr):
            program += [
                _instruction(_JEQ, 0, 4, number),
                _instruction(_LD_ABS, 0, 0, _ARG0_LOW),
                _instruction(_JEQ, 0, 1, AF_VSOCK),
                _instruction(_RET, 0, 0, _ERRNO | errno.EAFNOSUPPORT),
                _instruction(_RET, 0, 0, _ALLOW),
            ]
    for number in _IO_URING:
        program += [
            _instruction(_JEQ, 0, 1, number),
            _instruction(_RET, 0, 0, _ERRNO | errno.ENOSYS),
        ]
    program.append(_instruction(_RET, 0, 0, _ALLOW))
    return b"".join(program)
