"""macOS isolation with Seatbelt (sandbox-exec): no direct network, credentials masked.

Claude runs under a profile that allows outbound connections only to one loopback
port. A relay in the launcher process, outside the sandbox, forwards that port to
the SSH-owned Unix socket, so SSH reconnects keep the same port. Every launch first
proves inside the same profile that direct connections are refused.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator

from .filesystem import PRIVATE_NAMES, PRIVATE_PATTERNS
from .inside import ShimHandler, ShimServer
from .wire import RouteError


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# The Keychain tool would share logins with the user's normal Claude; `open`
# hands URLs to a browser on the host network.
BLOCKED_EXECUTABLES = ("/usr/bin/security", "/usr/bin/open")
LIBRARY_PRIVATE = (
    "Library/Keychains", "Library/Cookies", "Library/Safari", "Library/Mail",
    "Library/Messages", "Library/Containers", "Library/Group Containers",
    "Library/Application Support/Claude", "Library/Application Support/Google",
    "Library/Application Support/Chromium", "Library/Application Support/Firefox",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/BraveSoftware",
    "Library/Application Support/Arc", "Library/Application Support/Code",
    "Library/Application Support/Cursor",
)
# Proxy clients keep subscription URLs under Application Support.
PROXY_CLIENT_WORDS = ("clash", "mihomo", "v2ray", "xray", "shadowsocks", "sing-box",
                      "surge", "hiddify", "nekoray")
PROTECTED_WORKSPACES = (".claude", ".codex", ".ssh", ".local", ".config", ".cache")
INSTALL_SCRIPT = r"""set -eu
cache="$HOME/.cache/claude-launcher"
mkdir -p "$cache"
chmod 700 "$cache"
work=$(mktemp -d "$cache/installer-XXXXXX")
trap 'rm -rf "$work"' EXIT
curl --fail --silent --show-error --location --proto =https --proto-redir =https \
  https://claude.ai/install.sh --output "$work/install.sh"
bash "$work/install.sh" latest
"""
INSTALL_COMMAND = ["/bin/bash", "-c", INSTALL_SCRIPT]

# Runs inside the profile under test; standard library only, Python 3.9 compatible.
SELF_CHECK = r"""
import errno, json, os, socket, subprocess, sys
proxy, decoy, home = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
def attempt(family, kind, address):
    try:
        s = socket.socket(family, kind)
    except OSError:
        return "unsupported"
    s.settimeout(3)
    try:
        if kind == socket.SOCK_DGRAM:
            s.sendto(b"x", address)
            return "sent"
        s.connect(address)
        return "connected"
    except PermissionError:
        return "blocked"
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, "error")
    finally:
        s.close()
def execute(path):
    try:
        subprocess.run([path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)
        return "executed"
    except PermissionError:
        return "blocked"
    except (OSError, subprocess.SubprocessError):
        return "error"
def listing(path):
    try:
        os.listdir(path)
        return "readable"
    except PermissionError:
        return "blocked"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "error"
print(json.dumps({
    "tcp4_public": attempt(socket.AF_INET, socket.SOCK_STREAM, ("203.0.113.1", 443)),
    "tcp6_public": attempt(socket.AF_INET6, socket.SOCK_STREAM, ("2001:db8::1", 443)),
    "udp4_public": attempt(socket.AF_INET, socket.SOCK_DGRAM, ("203.0.113.1", 53)),
    "loopback_other_port": attempt(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", decoy)),
    "loopback_proxy_port": attempt(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", proxy)),
    "keychain_tool": execute("/usr/bin/security"),
    "open_tool": execute("/usr/bin/open"),
    "ssh_directory": listing(os.path.join(home, ".ssh")),
}))
"""
# Errors that mean no packet left the host are acceptable where IPv6 is absent.
REQUIRED = {
    "tcp4_public": {"blocked"},
    "tcp6_public": {"blocked", "unsupported", "ENETUNREACH", "EADDRNOTAVAIL", "EAFNOSUPPORT"},
    "udp4_public": {"blocked"},
    "loopback_other_port": {"blocked"},
    "loopback_proxy_port": {"connected"},
    "keychain_tool": {"blocked"},
    "open_tool": {"blocked"},
    "ssh_directory": {"blocked", "absent"},
}


def seatbelt_present() -> bool:
    return os.access(SANDBOX_EXEC, os.X_OK)


def _quote(value: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise RouteError("path_unsupported_by_macos_sandbox")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def private_paths(home: Path, extra: tuple[str, ...] = (),
                  protected_files: tuple[Path, ...] = ()) -> list[Path]:
    """Credential locations to deny, as given and resolved; Seatbelt matches real paths.

    HOME stays open otherwise, including ~/.local apart from this tool's own data.
    """
    candidates = [home / name for name in PRIVATE_NAMES if name != ".local"]
    candidates += [home / ".local/share/claude-launcher", home / ".local/state/claude-launcher",
                   home / ".cargo/credentials", home / ".cargo/credentials.toml"]
    candidates += [home / name for name in LIBRARY_PRIVATE]
    patterns = (*PRIVATE_PATTERNS, *extra)
    try:
        candidates += [path for path in home.iterdir()
                       if any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)]
    except OSError:
        raise RouteError("cannot_list_home_for_private_paths") from None
    support = home / "Library/Application Support"
    if support.is_dir() and not support.is_symlink():
        candidates += [path for path in support.iterdir()
                       if any(word in path.name.lower() for word in PROXY_CLIENT_WORDS)]
    candidates += list(protected_files)
    result: set[Path] = set()
    for path in candidates:
        for item in {path.absolute(), path.resolve()}:
            if item == home or item in home.parents:
                raise RouteError("private_path_resolves_to_broad_filesystem_root")
            result.add(item)
    return sorted(result)


def seatbelt_profile(proxy_port: int, profile_home: Path, private: list[Path]) -> str:
    """Later SBPL rules take precedence over earlier ones for the same operation."""
    lines = [
        "(version 1)",
        "(allow default)",
        # TCP, UDP, Unix sockets and mDNSResponder DNS; nothing leaves directly.
        "(deny network*)",
        # Local listeners (login callback, dev servers) may accept loopback clients.
        '(allow network-bind network-inbound (local ip "localhost:*"))',
        # The sole way out: the launcher's relay into the verified SSH route.
        f'(allow network-outbound (remote ip "localhost:{int(proxy_port)}"))',
    ]
    if private:
        # Metadata stays readable so path resolution through denied parents works.
        lines.append("(deny file-read-data file-read-xattr file-write*")
        lines.extend(f"    (subpath {_quote(str(path))})" for path in private)
        lines.append(")")
    lines.append(f"(allow file-read* file-write* (subpath {_quote(str(profile_home))}))")
    lines.append("(deny process-exec "
                 + " ".join(f"(literal {_quote(path)})" for path in BLOCKED_EXECUTABLES) + ")")
    # Apple Events would let another app act on the host network.
    lines.append("(deny appleevent-send)")
    return "\n".join(lines) + "\n"


def run_self_check(profile: str, proxy_port: int, home: Path) -> dict:
    """Run the probes under ``profile``; values are short outcome names only."""
    python = os.path.realpath(sys.executable)
    with socket.socket() as decoy:
        decoy.bind(("127.0.0.1", 0))
        decoy.listen(1)
        try:
            result = subprocess.run(
                [SANDBOX_EXEC, "-p", profile, python, "-I", "-S", "-c", SELF_CHECK,
                 str(proxy_port), str(decoy.getsockname()[1]), str(home)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                timeout=60,
                cwd="/", env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"sandbox_started": False}
    try:
        observed = json.loads(result.stdout)
        if not isinstance(observed, dict):
            raise ValueError
    except ValueError:
        return {"sandbox_started": False, "exit_code": result.returncode,
                "stderr": result.stderr.replace(str(home), "~")[-600:]}
    return {"sandbox_started": True,
            **{name: str(value) for name, value in observed.items() if name in REQUIRED}}


def check_failures(observed: dict) -> list[str]:
    if not observed.get("sandbox_started"):
        return ["sandbox_started"]
    return [name for name, allowed in REQUIRED.items() if observed.get(name) not in allowed]


def _validated_paths(profile_home: Path, workspace: Path, private_home_entries: tuple[str, ...],
                     protected_files: tuple[Path, ...]) -> tuple[Path, Path, list[Path]]:
    profile_home = profile_home.resolve()
    workspace = workspace.resolve()
    home = Path.home().resolve()
    if workspace == home or workspace in home.parents:
        raise RouteError("choose_a_project_directory_not_whole_home")
    private = private_paths(home, private_home_entries, protected_files)
    private_workspace = (
        profile_home.parent == home / ".local/share/claude-launcher/profiles"
        and workspace in {profile_home / "auth-workspace", profile_home / "default-workspace"}
    )
    if not private_workspace and (
        any(workspace.is_relative_to(home / name) for name in PROTECTED_WORKSPACES)
        or any(workspace.is_relative_to(path) for path in private)
    ):
        raise RouteError("account_or_key_directory_is_not_a_workspace")
    return profile_home, workspace, private


def child_environment(profile_home: Path, proxy_port: int, timezone: str | None) -> dict[str, str]:
    from .runner import clean_environment

    environment = clean_environment()
    environment["HOME"] = str(profile_home)
    environment["CLAUDE_CONFIG_DIR"] = str(profile_home / ".claude")
    environment["XDG_CONFIG_HOME"] = str(profile_home / ".config")
    environment["XDG_CACHE_HOME"] = str(profile_home / ".cache")
    environment["XDG_DATA_HOME"] = str(profile_home / ".local/share")
    if os.environ.get("TMPDIR", "").startswith("/"):
        environment["TMPDIR"] = os.environ["TMPDIR"]  # per-user, unlike the shared /tmp
    if timezone is not None:
        environment["TZ"] = timezone
    proxy = f"http://127.0.0.1:{int(proxy_port)}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment[name] = proxy
    environment["NO_PROXY"] = environment["no_proxy"] = "localhost,127.0.0.1,::1"
    own = str(profile_home / ".local/bin")
    inherited = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":")
    # Homebrew and user tools remain usable; Claude's own entry comes first.
    environment["PATH"] = ":".join(
        [own, *(entry for entry in inherited if entry.startswith("/") and entry != own)]
    )
    return environment


@contextlib.contextmanager
def seatbelt_session(
    socket_path: Path, profile_home: Path, workspace: Path, command: list[str], *,
    private_home_entries: tuple[str, ...] = (), protected_files: tuple[Path, ...] = (),
    timezone: str | None = None,
) -> Iterator[tuple[list[str], dict[str, str]]]:
    """Own the loopback relay for the duration of one supervised command."""
    profile_home, workspace, private = _validated_paths(
        profile_home, workspace, private_home_entries, protected_files,
    )
    if not seatbelt_present():
        raise RouteError("seatbelt_unavailable")
    with ShimServer(("127.0.0.1", 0), ShimHandler) as server:
        server.upstream_path = str(socket_path)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            profile = seatbelt_profile(port, profile_home, private)
            if check_failures(run_self_check(profile, port, Path.home().resolve())):
                raise RouteError("network_isolation_unverified")
            args = ["/bin/sh", "-c", 'cd -- "$1" && shift && exec "$@"', "vps-route",
                    str(workspace), SANDBOX_EXEC, "-p", profile, *command]
            yield args, child_environment(profile_home, port, timezone)
        finally:
            server.shutdown()


def isolation_report(private_home_entries: tuple[str, ...] = (),
                     protected_files: tuple[Path, ...] = ()) -> dict:
    """Self-check with a stand-in proxy listener; used by doctor and macos-check."""
    if not seatbelt_present():
        return {"sandbox_started": False, "sandbox_exec_present": False}
    import tempfile

    home = Path.home().resolve()
    with tempfile.TemporaryDirectory(prefix="vps-route-check-") as directory, \
            socket.socket() as stand_in:
        stand_in.bind(("127.0.0.1", 0))
        stand_in.listen(4)
        port = stand_in.getsockname()[1]
        profile = seatbelt_profile(port, Path(directory).resolve(),
                                   private_paths(home, private_home_entries, protected_files))
        return run_self_check(profile, port, home)


def _ps(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/ps", *arguments], stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=10, check=False,
                          env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})


def profile_is_running(path: Path) -> bool:
    """Any other same-user process whose arguments name ``path`` or a file inside it."""
    try:
        result = _ps("-axww", "-o", "pid=,uid=,command=")
    except (OSError, subprocess.SubprocessError):
        raise RouteError("cannot_confirm_profile_process_state") from None
    if result.returncode:
        raise RouteError("cannot_confirm_profile_process_state")
    needle = os.fsencode(str(path))
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        if int(fields[0]) == os.getpid() or int(fields[1]) != os.getuid():
            continue
        start = fields[2].find(needle)
        while start >= 0:
            end = start + len(needle)
            if end == len(fields[2]) or fields[2][end:end + 1] in (b"/", b" "):
                return True
            start = fields[2].find(needle, end)
    return False


def process_identity(pid: int) -> str | None:
    """Start time (second resolution) of a live same-user process; ps reads no environment."""
    try:
        result = _ps("-o", "uid=,stat=,lstart=", "-p", str(pid))
    except (OSError, subprocess.SubprocessError):
        raise RouteError("cannot_confirm_session_process") from None
    fields = result.stdout.decode(errors="replace").split(None, 2)
    if result.returncode or len(fields) < 3:
        return None
    if not fields[0].isdigit() or int(fields[0]) != os.getuid() or fields[1].startswith("Z"):
        return None
    return " ".join(fields[2].split())
