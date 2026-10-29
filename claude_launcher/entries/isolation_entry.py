"""Account-free probes executed inside the production sandbox."""

import ctypes
import errno
import json
import os
import platform
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path


def blocked(path):
    try:
        info = Path(path).stat()
        if stat.S_ISCHR(info.st_mode):
            return info.st_rdev == Path("/dev/null").stat().st_rdev
        return Path(path).is_dir() and not any(Path(path).iterdir())
    except FileNotFoundError:
        return True


def route_state(path, interface_index):
    try:
        rows = path.read_text().splitlines()
    except FileNotFoundError:
        return "unsupported"
    except OSError:
        return "inconclusive"
    if interface_index == 0:
        rows = rows[1:]
    return ("blocked_by_policy" if not any(
        row.split()[interface_index] != "lo" for row in rows if row.strip()) else "reachable")


def socket_result(family, address):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(address)
        return "reachable"
    except OSError as exc:
        if exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}:
            return "unsupported"
        return ("blocked_by_policy" if exc.errno in {
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES, errno.EPERM,
            errno.ECONNREFUSED} else "inconclusive")


def mask_locked(paths):
    """Exercise inherited masks in a new user/mount namespace, with a control."""
    targets = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields, filesystem = line.split(" - ", 1)
        target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields.split()[4])
        if target in paths and filesystem.split()[0] == "tmpfs":
            targets.append(target)
    if not targets:
        return "unsupported"
    code = """
import ctypes,errno,os,sys,tempfile
c=ctypes.CDLL(None,use_errno=True)
with tempfile.TemporaryDirectory() as own:
    assert c.mount(b'none',os.fsencode(own),b'tmpfs',0,None)==0
    assert c.umount2(os.fsencode(own),0)==0
for target in sys.argv[1:]:
    rc=c.umount2(os.fsencode(target),2)
    assert rc==-1 and ctypes.get_errno() in (errno.EINVAL,errno.EPERM)
print('MASK_LOCKED')
"""
    try:
        child = subprocess.run(["unshare", "-rm", "/usr/bin/python3", "-c", code, *targets],
                               capture_output=True, timeout=5)
        return ("blocked_by_policy" if child.returncode == 0 and
                b"MASK_LOCKED" in child.stdout else "inconclusive")
    except (OSError, subprocess.TimeoutExpired):
        return "inconclusive"


def filter_checks(spec, status):
    names = ("seccomp_filter", "vsock", "vsock_high_bits", "vsock_socketpair",
             "io_uring", "filter_inherited")
    if not spec["seccomp_supported"]:
        return dict.fromkeys(names, "unsupported"), False
    active = (status.get("Seccomp", "").strip() == "2" and
              int(status.get("Seccomp_filters", "0")) > spec["host_filters"])
    result = {"seccomp_filter": "reachable" if active else "inconclusive"}
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
    errors = [syscall(number, 1, ctypes.create_string_buffer(256), 0)[1]
              for number in (425, 426, 427)]
    result["io_uring"] = ("blocked_by_policy" if active and errors == [errno.ENOSYS] * 3
                         else "inconclusive")
    inherited = subprocess.run([sys.executable, "-c",
        "import ctypes,errno; c=ctypes.CDLL(None,use_errno=True); "
        "v=c.syscall(425,1,ctypes.create_string_buffer(256)); "
        "raise SystemExit(0 if v==-1 and ctypes.get_errno()==errno.ENOSYS else 1)"],
        capture_output=True, timeout=3)
    result["filter_inherited"] = ("blocked_by_policy" if inherited.returncode == 0 and active
                                  else "inconclusive")
    return result, active


def wslg_details(windows):
    paths = {"x11": Path("/tmp/.X11-unix/X0"), "pulse": Path("/mnt/wslg/PulseServer")}
    runtime, wayland = os.environ.get("XDG_RUNTIME_DIR"), os.environ.get("WAYLAND_DISPLAY")
    if runtime and wayland and "/" not in wayland and wayland not in {".", ".."}:
        paths["wayland"] = Path(runtime) / wayland
    result = {"tree_path": "reachable" if Path("/mnt/wslg").exists() else "blocked_by_policy",
              "application": "inconclusive", "application_reason": "not_exercised",
              "sockets": {}}
    for name, path in paths.items():
        state = {"path": "reachable" if path.exists() else "blocked_by_policy",
                 "connection": "inconclusive"}
        if not windows:
            state["connection"] = "blocked_by_policy" if not path.exists() else "inconclusive"
        else:
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(1)
                    client.connect(str(path))
                state["connection"] = "reachable"
            except OSError:
                state["connection"] = "inconclusive"
        result["sockets"][name] = state
    return result


def main():
    spec = json.loads(sys.argv[1])
    result = {}

    def check(name, passed, success="blocked_by_policy"):
        result[name] = success if passed else ("inconclusive" if success == "reachable" else "reachable")

    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
                  if ":" in line)
    filtered, active = filter_checks(spec, status)
    result.update(filtered)
    check("no_new_privileges", status.get("NoNewPrivs", "").strip() == "1")
    check("capabilities", int(status.get("CapEff", "1"), 16) == 0)
    interfaces = [row.split(":")[0].strip() for row in
                  Path("/proc/net/dev").read_text().splitlines()[2:]]
    check("interfaces", interfaces == ["lo"])
    result["ipv4_routes"] = route_state(Path("/proc/net/route"), 0)
    result["ipv6_routes"] = route_state(Path("/proc/net/ipv6_route"), -1)
    result["direct_tcp"] = socket_result(socket.AF_INET, ("203.0.113.1", 443))
    result["parent_tcp"] = socket_result(socket.AF_INET, ("127.0.0.1", spec["parent_port"]))
    result["parent_ipv6"] = socket_result(socket.AF_INET6, ("::1", spec["parent_port6"])) \
        if spec["parent_port6"] else "unsupported"
    result["mask_locked"] = mask_locked(spec["mask_paths"]) if not spec["windows"] else "unsupported"
    try:
        nested = subprocess.run(["bwrap", "--unshare-user", "--ro-bind", "/", "/",
                                 "--", "/bin/true"], capture_output=True, timeout=5)
        result["nested_bwrap"] = "reachable" if nested.returncode == 0 else "inconclusive"
    except (OSError, subprocess.TimeoutExpired):
        result["nested_bwrap"] = "inconclusive"
    fd_targets = []
    for descriptor in Path("/proc/self/fd").iterdir():
        if int(descriptor.name) <= 2:
            continue
        try:
            fd_targets.append(os.readlink(descriptor))
        except FileNotFoundError:
            pass
    check("inherited_capability_fds", not any(
        name.startswith(("socket:", "anon_inode:[io_uring]", "/memfd:")) for name in fd_targets))
    check("interop_runtime", blocked("/run/WSL"))
    check("wslg", blocked("/mnt/wslg"))
    result["wslg_details"] = wslg_details(spec["windows"])
    check("shared_runtime_aliases", all(blocked(p) for p in spec["runtime_paths"]
                                      if p not in {"/run/WSL", "/mnt/wslg"}))
    check("windows_drives", all(blocked(p) for p in spec["windows_mounts"])
          if not spec["windows"] else False)
    check("environment", not any(name in os.environ for name in (
        "WSL_INTEROP", "WSL_DISTRO_NAME", "WSLENV", "DISPLAY", "WAYLAND_DISPLAY")))
    if spec["windows"]:
        result["windows_drives"] = "reachable" if all(Path(p).exists() for p in spec["windows_mounts"]) else "inconclusive"
        result["interop_runtime"] = "reachable" if Path("/run/WSL").is_dir() else "inconclusive"
        result["wslg"] = "reachable" if Path("/mnt/wslg").is_dir() else "unsupported"
        result["environment"] = "reachable" if os.environ.get("WSL_INTEROP") else "inconclusive"
    if not spec["fixture"] or not spec["host_fixture_ok"]:
        result["windows_execution"] = "unsupported"
    else:
        try:
            child = subprocess.run([spec["fixture"], "/d", "/c", "echo", "vps-route-probe"],
                                   capture_output=True, timeout=5)
            executed = child.returncode == 0 and b"vps-route-probe" in child.stdout
            result["windows_execution"] = ("reachable" if executed else
                "blocked_by_policy" if active else "inconclusive")
        except subprocess.TimeoutExpired:
            result["windows_execution"] = "inconclusive"
        except OSError:
            result["windows_execution"] = "blocked_by_policy" if active else "inconclusive"
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
