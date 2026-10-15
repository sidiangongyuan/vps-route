"""Secret-free macOS self-test report for testers.

Uses only loopback listeners and documentation addresses; reads no account files,
keys or Keychain secrets. Run `vps-route macos-check`, or from a checkout
`python3 -m claude_launcher.macos_check`.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .config import Config
from .macos import (
    SANDBOX_EXEC, check_failures, child_environment, private_paths, run_self_check,
    seatbelt_present, seatbelt_profile,
)
from .wire import RouteError


DNS_CHECK = r"""
import socket
try:
    socket.getaddrinfo("example.com", 443)
    print("resolved")
except OSError:
    print("failed")
"""


def keychain_claude_items() -> list[str] | None:
    """Service names and modification times of Claude items; never secrets."""
    try:
        result = subprocess.run(["/usr/bin/security", "dump-keychain"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, errors="replace",
                                timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    items: list[str] = []
    service = modified = None

    def flush() -> None:
        if service is not None and "claude" in service.lower():
            items.append(f"{service} | modified {modified or 'unknown'}")

    value = r'(?:0x[0-9A-Fa-f]+\s+)?"(.*?)(?:\\000)?"\s*$'
    for line in result.stdout.splitlines():
        if line.startswith("keychain:"):
            flush()
            service = modified = None
        elif match := re.match(r'\s*"svce"<blob>=' + value, line):
            service = match.group(1)
        elif match := re.match(r'\s*"mdat"<timedate>=' + value, line):
            modified = match.group(1)
    flush()
    return sorted(items)


def _sandboxed(profile: str, code: str) -> str:
    try:
        result = subprocess.run(
            [SANDBOX_EXEC, "-p", profile, os.path.realpath(sys.executable), "-I", "-S", "-c", code],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
            timeout=30, cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "not_started"
    return result.stdout.strip() or f"exit_{result.returncode}"


def _pty_check(profile: str, profile_home: Path, port: int) -> str:
    """The real PTY helper must give the sandboxed child a controlling terminal."""
    from .runner import run_supervised

    command = [SANDBOX_EXEC, "-p", profile, "/bin/sh", "-c",
               "tty >/dev/null && : </dev/tty && exit 7"]
    with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
        try:
            code, _ = run_supervised(command, child_environment(profile_home, port, None), None,
                                     lambda: None, lambda: True, 1,
                                     input_fd=empty.fileno(), output_fd=output.fileno())
        except (OSError, RouteError):
            return "not_started"
    return "ok" if code == 7 else f"exit_{code}"


def report(config: Config | None) -> dict:
    home = Path.home().resolve()
    entries = config.private_home_entries if config else ()
    protected = (config.identity_file, config.known_hosts) if config else ()
    result: dict = {
        "launcher_version": __version__,
        "macos_version": platform.mac_ver()[0],
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sandbox_exec_present": seatbelt_present(),
        "config_loaded": config is not None,
    }
    private = private_paths(home, entries, protected)
    result["private_path_count"] = len(private)
    with tempfile.TemporaryDirectory(prefix="vps-route-check-") as directory, \
            socket.socket() as stand_in:
        stand_in.bind(("127.0.0.1", 0))
        stand_in.listen(8)
        port = stand_in.getsockname()[1]
        profile_home = Path(directory).resolve()
        profile = seatbelt_profile(port, profile_home, private)
        isolation = run_self_check(profile, port, home)
        failures = check_failures(isolation)
        result["isolation"] = isolation
        result["isolation_failures"] = failures
        if not isolation.get("sandbox_started"):
            result["profile"] = profile
        else:
            result["dns_inside_sandbox"] = _sandboxed(profile, DNS_CHECK)
            result["pty_in_sandbox"] = _pty_check(profile, profile_home, port)
    result["keychain_claude_items"] = keychain_claude_items()
    if config is not None:
        from .install import installation_present

        executable = config.claude_executable
        try:
            installed = installation_present(config)
        except RouteError as exc:
            installed = str(exc)
        result["tool_profile"] = {
            "claude_installed": installed,
            "claude_version": executable.resolve().name if installed is True else None,
            "credentials_file_present": (config.profile_home
                                         / ".claude/.credentials.json").is_file(),
        }
    result["ready"] = not failures and result.get("pty_in_sandbox") == "ok"
    # Paths name the tester's account; keep the report shareable.
    return json.loads(json.dumps(result).replace(json.dumps(str(home))[1:-1], "~"))


def main() -> int:
    from .setup import default_config_path

    path = default_config_path()
    try:
        config = Config.load(path) if path.exists() else None
    except RouteError:
        config = None
    if sys.platform != "darwin":
        print("macos-check runs only on macOS", file=sys.stderr)
        return 69
    value = report(config)
    print(json.dumps(value, indent=2))
    return 0 if value["ready"] else 69


if __name__ == "__main__":
    raise SystemExit(main())
