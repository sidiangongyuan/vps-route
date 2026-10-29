"""Account-free probes executed inside the production sandbox."""

import ctypes
import errno
import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path


def blocked(path):
    return not Path(path).exists() or (Path(path).is_dir() and not any(Path(path).iterdir()))


def socket_result(family, address):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(address)
        return "reachable"
    except OSError as exc:
        return ("blocked_by_policy" if exc.errno in {
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES, errno.EPERM,
            errno.ECONNREFUSED, errno.EAFNOSUPPORT} else "inconclusive")


def main():
    spec = json.loads(sys.argv[1])
    result = {}
    def check(name, passed, success="blocked_by_policy"):
        result[name] = success if passed else ("inconclusive" if success == "reachable" else "reachable")

    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
                  if ":" in line)
    filters = int(status.get("Seccomp_filters", "0").strip())
    active = status.get("Seccomp", "").strip() == "2" and filters > spec["host_filters"]
    check("seccomp_filter", active, "reachable")
    check("no_new_privileges", status.get("NoNewPrivs", "").strip() == "1")
    check("capabilities", int(status.get("CapEff", "1"), 16) == 0)
    interfaces = [row.split(":")[0].strip() for row in
                  Path("/proc/net/dev").read_text().splitlines()[2:]]
    check("interfaces", interfaces == ["lo"])
    routes4 = Path("/proc/net/route").read_text().splitlines()[1:]
    routes6 = Path("/proc/net/ipv6_route").read_text().splitlines()
    check("ipv4_routes", not any(line.split()[0] != "lo" for line in routes4))
    check("ipv6_routes", not any(line.split()[-1] != "lo" for line in routes6))
    result["direct_tcp"] = socket_result(socket.AF_INET, ("203.0.113.1", 443))
    result["parent_tcp"] = socket_result(socket.AF_INET, ("127.0.0.1", spec["parent_port"]))
    result["parent_ipv6"] = socket_result(socket.AF_INET6, ("::1", spec["parent_port6"])) \
        if spec["parent_port6"] else "unsupported"

    libc = ctypes.CDLL(None, use_errno=True)
    socket_nr, pair_nr = (41, 53) if platform.machine() == "x86_64" else (198, 199)
    def syscall(number, *args):
        ctypes.set_errno(0)
        value = libc.syscall(number, *args)
        error = ctypes.get_errno()
        if value >= 0 and number in (socket_nr, 425):
            os.close(value)
        return value, error

    for name, domain in (("vsock", 40), ("vsock_high_bits", 40 | 1 << 32)):
        errors = [syscall(socket_nr, ctypes.c_long(domain), kind, 0)
                  for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_SEQPACKET)]
        if spec["windows"]:
            result[name] = "reachable" if any(v >= 0 for v, e in errors) else "unsupported"
        else:
            result[name] = ("blocked_by_policy" if active and
                            all(v < 0 and e == errno.EAFNOSUPPORT for v, e in errors)
                            else "inconclusive")
    descriptors = (ctypes.c_int * 2)()
    value, error = syscall(pair_nr, ctypes.c_long(40 | 1 << 32), 1, 0, descriptors)
    if value >= 0:
        for descriptor in descriptors:
            os.close(descriptor)
    result["vsock_socketpair"] = ("blocked_by_policy" if active and value < 0
                                 and error == errno.EAFNOSUPPORT else "unsupported")
    ring_errors = [syscall(number, 1, ctypes.create_string_buffer(256), 0)[1]
                   for number in (425, 426, 427)]
    result["io_uring"] = ("blocked_by_policy" if active and
                         ring_errors == [errno.ENOSYS] * 3 else "inconclusive")
    mount_target = Path("/tmp/vps-route-mount-probe")
    mount_target.mkdir()
    value = libc.mount(b"none", os.fsencode(mount_target), b"tmpfs", 0, None)
    check("new_mount", value < 0 and ctypes.get_errno() == errno.EPERM and active)
    # A fresh exec must retain the same filter. No networking or mount changes.
    inherited = subprocess.run([sys.executable, "-c",
        "import ctypes,errno; c=ctypes.CDLL(None,use_errno=True); "
        "v=c.syscall(425,1,ctypes.create_string_buffer(256)); "
        "raise SystemExit(0 if v==-1 and ctypes.get_errno()==errno.ENOSYS else 1)"],
        capture_output=True, timeout=3)
    check("filter_inherited", inherited.returncode == 0 and active)
    # Only stdio survives exec; ctypes and transient /proc listing FDs are ignored.
    fd_targets = []
    for descriptor in Path("/proc/self/fd").iterdir():
        if int(descriptor.name) <= 2:
            continue
        try:
            fd_targets.append(os.readlink(descriptor))
        except FileNotFoundError:
            pass
    check("inherited_capability_fds", not any(
        name.startswith(("socket:", "anon_inode:[io_uring]", "/memfd:"))
        for name in fd_targets))
    check("interop_runtime", blocked("/run/WSL"))
    check("wslg", blocked("/mnt/wslg"))
    check("shared_runtime_aliases", all(blocked(p) or
          (Path(p).is_dir() and not any(Path(p).iterdir())) for p in spec["runtime_paths"]
          if p not in {"/run/WSL", "/mnt/wslg"}))
    # On research roots, masked mountpoints exist as empty, read-only directories.
    check("windows_drives", all(blocked(p) or (
        Path(p).is_dir() and not any(Path(p).iterdir())) for p in spec["windows_mounts"])
        if not spec["windows"] else False)
    check("environment", not any(name in os.environ for name in (
        "WSL_INTEROP", "WSL_DISTRO_NAME", "WSLENV", "DISPLAY", "WAYLAND_DISPLAY")))
    if spec["windows"]:
        result["windows_drives"] = "reachable" if all(Path(p).is_dir() for p in spec["windows_mounts"]) else "inconclusive"
        result["interop_runtime"] = "reachable" if Path("/run/WSL").is_dir() else "inconclusive"
        result["wslg"] = "reachable" if Path("/mnt/wslg").is_dir() else "unsupported"
        result["environment"] = "reachable" if os.environ.get("WSL_INTEROP") else "inconclusive"
    if spec["fixture"]:
        try:
            child = subprocess.run([spec["fixture"], "/c", "echo", "vps-route-probe"],
                                   capture_output=True, timeout=5)
            executed = child.returncode == 0 and b"vps-route-probe" in child.stdout
            result["windows_execution"] = ("inconclusive" if not spec["host_fixture_ok"] else "reachable" if executed else
                "blocked_by_policy" if spec["host_fixture_ok"] and active else "inconclusive")
        except subprocess.TimeoutExpired:
            result["windows_execution"] = "inconclusive"
        except OSError:
            result["windows_execution"] = (
                "blocked_by_policy" if spec["host_fixture_ok"] and active else "inconclusive")
    else:
        result["windows_execution"] = "inconclusive" if spec["wsl"] else "unsupported"
    try:
        with socket.create_connection(("127.0.0.1", 17878), timeout=2) as client:
            client.sendall(b"GET /fixture HTTP/1.0\r\n\r\n")
            data = b""
            while b"vps-route-fixture" not in data and len(data) < 4096:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
        result["approved_relay"] = "reachable" if b"vps-route-fixture" in data else "inconclusive"
    except OSError:
        result["approved_relay"] = "inconclusive"
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
