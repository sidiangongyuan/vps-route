"""Production-sandbox isolation diagnostics using disposable local fixtures."""

from __future__ import annotations

import json
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
from pathlib import Path

from . import seccomp, wsl
from .config import Config
from .runner import ENTRY_DIR, sandbox_command
from .wire import RouteError


class FixtureHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            self.request.recv(4096)
            self.request.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 17\r\n\r\nvps-route-fixture")
        except OSError:
            pass


class FixtureServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def _host_filters() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("Seccomp_filters:"):
            return int(line.split(":")[1])
    return 0


def _run(config: Config | None, windows: bool, inventory: list[wsl.Mount]) -> dict:
    with tempfile.TemporaryDirectory(prefix="vps-route-isolation-") as temporary:
        root = Path(temporary)
        profile, workspace = root / "profile", root / "project"
        profile.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        fixture = None
        host_ok = False
        if wsl.version() == 2:
            for mount in wsl.windows_mounts(inventory):
                source = mount.target / "Windows/System32/cmd.exe"
                if source.is_file():
                    fixture = workspace / "cmd.exe"
                    shutil.copyfile(source, fixture)
                    fixture.chmod(0o700)
                    try:
                        control = subprocess.run([str(fixture), "/c", "echo", "vps-route-probe"],
                                                 capture_output=True, timeout=5)
                        host_ok = control.returncode == 0 and b"vps-route-probe" in control.stdout
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    break
        with socket.socket() as parent, socket.socket(socket.AF_INET6) as parent6:
            parent.bind(("127.0.0.1", 0))
            parent.listen()
            parent_port6 = 0
            try:
                parent6.bind(("::1", 0))
                parent6.listen()
                parent_port6 = parent6.getsockname()[1]
            except OSError:
                pass
            # Establish the canary control before interpreting a child refusal.
            with socket.create_connection(parent.getsockname(), timeout=2):
                connection, _ = parent.accept()
                connection.close()
            server = FixtureServer(str(root / "upstream.sock"), FixtureHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            spec = {"wsl": wsl.version(), "windows": windows, "host_filters": _host_filters(),
                    "host_fixture_ok": host_ok, "fixture": str(fixture) if fixture else None,
                    "parent_port": parent.getsockname()[1], "parent_port6": parent_port6,
                    "windows_mounts": [str(m.target) for m in wsl.windows_mounts(inventory)],
                    "runtime_paths": [str(p) for p in wsl.runtime_paths(inventory)]}
            try:
                args, environment = sandbox_command(
                    root / "upstream.sock", profile, workspace,
                    ["/usr/bin/python3", str(ENTRY_DIR / "isolation_entry.py"), json.dumps(spec)],
                    research_filesystem=config.research_filesystem if config else False,
                    private_home_entries=config.private_home_entries if config else (),
                    texlive_root=config.texlive_root if config else None,
                    timezone=config.timezone if config else None,
                    windows_interop=windows,
                )
                child = subprocess.run(args, env=environment, capture_output=True,
                                       text=True, timeout=25, close_fds=True)
                if child.returncode:
                    return {"result": "inconclusive", "reason": "sandbox_probe_failed"}
                probes = json.loads(child.stdout)
                if not isinstance(probes, dict):
                    raise ValueError
            except subprocess.TimeoutExpired:
                return {"result": "inconclusive", "reason": "sandbox_probe_timeout"}
            except (OSError, ValueError):
                return {"result": "inconclusive", "reason": "sandbox_probe_unavailable"}
            except RouteError as exc:
                return {"result": "inconclusive", "reason": str(exc)}
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        expected = {key: "blocked_by_policy" for key in (
            "no_new_privileges", "capabilities", "interfaces", "ipv4_routes", "ipv6_routes",
            "direct_tcp", "parent_tcp", "io_uring", "new_mount",
            "filter_inherited", "inherited_capability_fds")}
        expected.update(seccomp_filter="reachable", approved_relay="reachable")
        if parent_port6:
            expected["parent_ipv6"] = "blocked_by_policy"
        if wsl.version() == 2:
            expected.update({key: "reachable" if windows else "blocked_by_policy" for key in (
                "windows_execution", "interop_runtime", "windows_drives", "environment")})
            if not windows:
                expected.update({key: "blocked_by_policy" for key in (
                    "vsock", "vsock_high_bits", "vsock_socketpair", "wslg", "shared_runtime_aliases")})
        elif seccomp.supported():
            expected.update(vsock="blocked_by_policy", vsock_high_bits="blocked_by_policy",
                            vsock_socketpair="blocked_by_policy")
        passed = all(probes.get(key) == value for key, value in expected.items())
        return {"passed": passed, "checks": probes, "expected": expected,
                "host_windows_fixture": "reachable" if host_ok else
                "inconclusive" if wsl.version() else "unsupported"}


def inspect(config: Config | None) -> dict:
    try:
        detected = wsl.version()
        inventory = wsl.mounts() if detected else []
        windows = bool(config and config.windows_interop)
        current = _run(config, windows, inventory)
        result = {"production_builder": True, "current": current,
                  "windows_interop_enabled": windows,
                  "host_execution_allowed": windows,
                  "host_control_enabled": bool(config and config.orca_control),
                  "vps_only_guarantee": not (windows or bool(config and config.orca_control)),
                  "shared_workspace_writable": True}
        if detected == 2 and not windows:
            result["windows_opt_in"] = _run(config, True, inventory)
        result["passed"] = current.get("passed", False) and (
            result.get("windows_opt_in", {}).get("passed", True))
        return result
    except (OSError, RouteError):
        return {"passed": False, "result": "inconclusive", "reason": "isolation_fixture_unavailable"}
