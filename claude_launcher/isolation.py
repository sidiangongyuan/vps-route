"""Production-sandbox isolation diagnostics using disposable local fixtures."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
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
                        control = subprocess.run([str(fixture), "/d", "/c", "echo", "vps-route-probe"],
                                                 capture_output=True, timeout=5)
                        host_ok = control.returncode == 0 and b"vps-route-probe" in control.stdout
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    break
        with ExitStack() as sockets:
            parent = sockets.enter_context(socket.socket())
            parent.bind(("127.0.0.1", 0))
            parent.listen()
            parent_port6 = 0
            try:
                parent6 = sockets.enter_context(socket.socket(socket.AF_INET6))
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
                    "seccomp_supported": seccomp.supported(),
                    "mask_paths": [str(p) for p in wsl.mask_paths(inventory)] if inventory else [],
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
        expected = expected_checks(spec, probes)
        passed = all(probes.get(key) == value for key, value in expected.items())
        return {"passed": passed, "checks": probes, "expected": expected,
                "host_windows_fixture": "reachable" if host_ok else
                "unsupported",
                "host_fixture_reason": None if host_ok else "host_execution_unavailable"}


def expected_checks(spec: dict, probes: dict) -> dict:
    expected = {key: "blocked_by_policy" for key in (
        "no_new_privileges", "capabilities", "interfaces", "ipv4_routes",
        "direct_tcp", "parent_tcp", "inherited_capability_fds")}
    expected.update(approved_relay="reachable", nested_bwrap="reachable")
    if probes.get("ipv6_routes") != "unsupported":
        expected["ipv6_routes"] = "blocked_by_policy"
    if spec["parent_port6"] and probes.get("parent_ipv6") != "unsupported":
        expected["parent_ipv6"] = "blocked_by_policy"
    if spec["seccomp_supported"]:
        expected.update(seccomp_filter="reachable", io_uring="blocked_by_policy",
                        filter_inherited="blocked_by_policy")
        if not spec["windows"]:
            expected.update({key: "blocked_by_policy" for key in (
                "vsock", "vsock_high_bits", "vsock_socketpair")})
    if spec["wsl"] == 2:
        expected.update({key: "reachable" if spec["windows"] else "blocked_by_policy"
                         for key in ("interop_runtime", "windows_drives", "environment")})
        if spec["windows"] or spec["host_fixture_ok"]:
            expected["windows_execution"] = "reachable" if spec["windows"] else "blocked_by_policy"
        if not spec["windows"]:
            expected.update(mask_locked="blocked_by_policy", wslg="blocked_by_policy",
                            shared_runtime_aliases="blocked_by_policy")
    return expected


def inspect(config: Config | None, *, windows: bool = False) -> dict:
    try:
        detected = wsl.version()
        inventory = wsl.mounts() if detected else []
        windows = windows or bool(config and config.windows_interop)
        current = _run(config, windows, inventory)
        return {"production_builder": True, "scope": "temporary_fixture", "current": current,
                "windows_interop_enabled": windows, "host_execution_allowed": windows,
                "host_control_enabled": bool(config and config.orca_control),
                "vps_only_guarantee": not (windows or bool(config and config.orca_control)),
                "shared_workspace_writable": True, "passed": current.get("passed", False)}
    except (OSError, RouteError):
        return {"passed": False, "result": "inconclusive", "reason": "isolation_fixture_unavailable"}


def workspace_plan(config: Config | None, workspace: Path, *, windows: bool = False) -> dict:
    """Build daily-run mounts without executing a child or writing the project."""
    result = {"scope": "workspace_mount_plan", "executed": False,
              "project_instructions": True, "skills": True, "reconnectable": True,
              "passed": False}
    if config is None:
        return dict(result, result="inconclusive", reason="workspace_requires_config")
    workspace = workspace.expanduser().absolute()
    if not workspace.is_dir():
        return dict(result, result="inconclusive", reason="workspace_directory_missing")
    if not config.profile_home.is_dir():
        return dict(result, result="inconclusive", reason="workspace_profile_missing")
    # Orca bootstrap contains a live control token. An account-free doctor keeps
    # this plan explicitly incomplete instead of reading that credential.
    orca = config.orca_control and bool(os.environ.get("ORCA_CLI_COMMAND"))
    result["orca_control_requested"] = orca
    if orca:
        return dict(result, result="inconclusive", reason="workspace_orca_plan_requires_review")
    try:
        with tempfile.TemporaryDirectory(prefix="vps-route-workspace-plan-") as directory:
            route = Path(directory) / "route"
            route.mkdir(mode=0o700)
            address = route / "upstream.sock"
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(address))
                args, _ = sandbox_command(
                    address, config.profile_home, workspace, ["/bin/true"],
                    project_instructions=True, texlive_root=config.texlive_root,
                    research_filesystem=config.research_filesystem,
                    private_home_entries=config.private_home_entries, timezone=config.timezone,
                    reconnectable=True, windows_interop=windows or config.windows_interop)
        inventory = wsl.mounts() if wsl.version() else []
        sources = []
        for index, argument in enumerate(args):
            if argument in {"--bind", "--ro-bind"}:
                source = Path(args[index + 1]).resolve()
                if wsl.containing_mount(source, inventory):
                    sources.append({"source": str(source), "destination": args[index + 2],
                                    "access": "read_write" if argument == "--bind" else "read_only"})
        return dict(result, passed=True, result="reachable", windows_sources=sources,
                    windows_interop_enabled=windows or config.windows_interop)
    except RouteError as exc:
        return dict(result, result="blocked_by_policy", reason=str(exc))
    except OSError:
        return dict(result, result="inconclusive", reason="workspace_plan_unavailable")
