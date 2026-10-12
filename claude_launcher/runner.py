"""Own one SSH tunnel and one isolated process tree; terminate only those."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import platform
import pty
import queue
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .config import Config
from .customizations import project_instruction_mounts, shared_skill_mounts
from .filesystem import host_socket_mounts, private_mounts, private_paths, research_mounts
from .wire import RouteError, proxy_json


PROXY_NAMES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
ENTRY_DIR = PACKAGE_DIR / "entries"
# Tolerate brief transport stalls; a dead SSH process still terminates its child.
SSH_KEEPALIVE_OPTIONS = (
    "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
)

# termios restoration does not reset terminal-emulator modes enabled by a TUI.
TERMINAL_RESET = (
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l"
    b"\x1b[?1015l\x1b[?1016l\x1b[?1004l\x1b[?2004l\x1b[?2026l"
    b"\x1b[?1049l\x1b[0m\x1b[?25h"
)


def prepare_browser_hook(profile: Path) -> Path:
    """Wheel installers need not preserve execute bits on Python package files."""
    root = profile / ".launcher-runtime"
    if root.is_symlink():
        raise RouteError("unsafe_browser_helper_directory")
    root.mkdir(mode=0o700, exist_ok=True)
    if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise RouteError("unsafe_browser_helper_directory")
    path = root / "browser-hook"
    contents = (ENTRY_DIR / "browser_hook.py").read_bytes()
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or path.read_bytes() != contents):
            raise RouteError("unsafe_or_outdated_browser_helper")
        path.chmod(0o700)
        return path
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(contents)
    return path


def clean_environment() -> dict[str, str]:
    # The isolated profile must not inherit unrelated API keys, SSH peer
    # addresses, IDE sockets or language-runtime injection settings.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM",
               "LANG", "LANGUAGE"}
    environment = {name: value for name, value in os.environ.items()
                   if name in allowed or name.startswith("LC_")}
    for name in tuple(environment):
        if (
            name.lower() in PROXY_NAMES
            or name.startswith("ANTHROPIC_")
            or name.startswith("CLAUDE_")
            or name.startswith("HICC_")
            or name in {
                "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS",
                "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV",
                "DISABLE_AUTOUPDATER", "DISABLE_UPDATES",
                "BROWSER", "DISPLAY", "WAYLAND_DISPLAY", "VPS_ROUTE_AUTH_DIR",
                "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
            }
        ):
            environment.pop(name)
    return environment


def bootstrap_proxy_options(config: Config) -> list[str]:
    """Use only an explicitly configured loopback HTTP CONNECT proxy; no fallback."""
    if config.ssh_http_proxy is None:
        return []
    executable = shutil.which("nc")
    if executable is None:
        raise RouteError("openbsd_netcat_required_for_ssh_http_proxy")
    address = urlsplit(config.ssh_http_proxy)
    proxy_command = shlex.join([
        executable, "-n", "-X", "connect", "-x",
        f"{address.hostname}:{address.port}", "%h", "%p",
    ])
    return ["-o", "ProxyCommand=" + proxy_command]


def ssh_arguments(config: Config) -> list[str]:
    """Common verified-host options for owned SSH connections."""
    return [
        "ssh", "-F", "/dev/null", "-T", "-a", "-p", str(config.ssh_port),
        "-l", config.ssh_user, "-i", str(config.identity_file),
        "-o", f"UserKnownHostsFile={config.known_hosts}",
        "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "ControlMaster=no", "-o", "ControlPath=none",
        *SSH_KEEPALIVE_OPTIONS,
        "-o", f"ConnectTimeout={int(config.timeout_seconds)}",
        *bootstrap_proxy_options(config),
    ]


class SSHTunnel:
    def __init__(self, config: Config, socket_path: Path):
        self.config = config
        self.socket_path = socket_path
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._errors = tempfile.TemporaryFile()

    def start(self) -> None:
        cfg = self.config
        if not cfg.identity_file.is_file() or not cfg.known_hosts.is_file():
            raise RouteError("ssh_key_or_verified_known_hosts_missing")
        args = [
            "ssh", "-F", "/dev/null", "-N", "-T", "-n", "-a",
            "-p", str(cfg.ssh_port), "-l", cfg.ssh_user,
            "-i", str(cfg.identity_file),
            "-o", f"UserKnownHostsFile={cfg.known_hosts}",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=no",
            *SSH_KEEPALIVE_OPTIONS,
            "-o", f"ConnectTimeout={int(cfg.timeout_seconds)}",
            "-L", f"{self.socket_path}:{cfg.remote_socket}",
            *bootstrap_proxy_options(cfg), cfg.ssh_host,
        ]
        ssh_environment = clean_environment()
        if os.environ.get("SSH_AUTH_SOCK"):
            # Let local OpenSSH use an unlocked key; never forward the agent or
            # expose its socket to the isolated Claude process.
            ssh_environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        self.process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=self._errors, env=ssh_environment, start_new_session=True,
        )
        deadline = time.monotonic() + cfg.timeout_seconds + 2
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._errors.seek(0)
                message = self._errors.read(8192).lower()
                if b"host key verification" in message or b"host identification has changed" in message:
                    raise RouteError("ssh_host_key_verification_failed")
                if b"permission denied" in message:
                    raise RouteError("ssh_authentication_failed")
                raise RouteError("ssh_tunnel_failed")
            if self.socket_path.exists():
                return
            time.sleep(0.05)
        raise RouteError("ssh_tunnel_start_timeout")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    return
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait()

    def dispose(self) -> None:
        self.close()
        self._errors.close()


def sandbox_command(
    socket_path: Path, profile_home: Path, workspace: Path, command: list[str],
    *, login: bool = False, project_instructions: bool = False,
    texlive_root: Path | None = None,
    research_filesystem: bool = False,
    timezone: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Create only mount/namespace changes scoped to the launched child."""
    profile_home = profile_home.resolve()
    workspace = workspace.resolve()
    live_home = Path.home().resolve()
    if workspace == live_home or workspace in live_home.parents:
        raise RouteError("choose_a_project_directory_not_whole_home")
    protected = (".claude", ".hicc", ".codex", ".ssh", ".local", ".config", ".cache")
    private_workspace = (
        profile_home.parent == live_home / ".local/share/claude-launcher/profiles"
        and workspace in {profile_home / "auth-workspace", profile_home / "default-workspace"}
    )
    if not private_workspace and any(
        workspace.is_relative_to(live_home / name) for name in protected
    ):
        raise RouteError("account_or_key_directory_is_not_a_workspace")
    hidden = private_paths(live_home, REPO_ROOT) if research_filesystem else []
    if not private_workspace and any(workspace.is_relative_to(p) for p in hidden):
        raise RouteError("account_or_key_directory_is_not_a_workspace")
    args = [
        "bwrap", "--unshare-user", "--unshare-net", "--unshare-pid", "--unshare-ipc",
        "--die-with-parent",
    ]
    # Expose runtime libraries, not the host's whole filesystem and Unix sockets.
    for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        path = Path(directory)
        if path.is_symlink():
            args.extend(["--symlink", os.readlink(path), directory])
        elif path.is_dir():
            args.extend(["--ro-bind", directory, directory])
    args.extend(["--ro-bind", "/etc", "/etc"])
    args.extend([
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
        "--tmpfs", "/sys",
    ])
    if research_filesystem:
        args.extend(research_mounts(live_home))
    args.extend([
        "--dir", "/run/claude-launcher",
        "--ro-bind", str(socket_path), "/run/claude-launcher/upstream.sock",
        "--bind", str(profile_home), str(profile_home),
        "--bind", str(workspace), str(workspace),
    ])
    if project_instructions:
        args.extend(project_instruction_mounts(workspace, include_ancestors=research_filesystem))
    if research_filesystem:
        args.extend(host_socket_mounts(live_home, profile_home, socket_path))
        args.extend(private_mounts(hidden))
        # .local is masked above. Expose only this profile again, never its archive siblings.
        args.extend(["--bind", str(profile_home), str(profile_home)])
        # A pipx/venv package may itself live under a masked .local directory.
        args.extend(["--ro-bind", str(PACKAGE_DIR), str(PACKAGE_DIR)])
        # Create the profile mountpoint first; remounting parents is not recursive.
        for path in hidden:
            if path.is_dir():
                args.extend(["--remount-ro", str(path)])
    else:
        args.extend(["--ro-bind", str(PACKAGE_DIR), str(PACKAGE_DIR)])
    if project_instructions:
        # Rebind only skill contents after masking the host's account directories.
        args.extend(shared_skill_mounts(profile_home, live_home))
    texlive_bin = None
    if texlive_root is not None:
        texlive_root = texlive_root.resolve()
        texlive_bin = texlive_root / "bin" / f"{platform.machine()}-linux"
        # This opt-in is for a TeX installation, not a generic home-directory mount.
        if (texlive_root == live_home or texlive_root in live_home.parents
                or not (texlive_root / "texmf-dist/web2c/texmf.cnf").is_file()
                or not (texlive_bin / "latexmk").is_file()
                or not (texlive_bin / "pdflatex").is_file()):
            raise RouteError("invalid_texlive_installation")
        args.extend(["--ro-bind", str(texlive_root), str(texlive_root)])
    args.extend([
        "--chdir", str(workspace), "--", "/usr/bin/python3",
        str(ENTRY_DIR / "sandbox_entry.py"), "/run/claude-launcher/upstream.sock", *command,
    ])
    environment = clean_environment()
    environment["HOME"] = str(profile_home)
    environment["CLAUDE_CONFIG_DIR"] = str(profile_home / ".claude")
    environment["XDG_CONFIG_HOME"] = str(profile_home / ".config")
    environment["XDG_CACHE_HOME"] = str(profile_home / ".cache")
    environment["XDG_DATA_HOME"] = str(profile_home / ".local/share")
    if timezone is not None:
        # Explicit per-process choice, never an inherited token-bearing environment.
        environment["TZ"] = timezone
    path_entries = [str(profile_home / ".local/bin")]
    if texlive_bin is not None:
        path_entries.append(str(texlive_bin))
        # Keep generated formats/fonts in the persistent private profile, not TeX itself.
        environment["TEXMFVAR"] = str(profile_home / ".cache/texlive/texmf-var")
        environment["TEXMFCONFIG"] = str(profile_home / ".config/texlive/texmf-config")
        environment["TEXMFHOME"] = str(profile_home / "texmf")
        environment["TEXMFCACHE"] = environment["TEXMFVAR"]
    environment["PATH"] = ":".join([*path_entries, "/usr/local/bin", "/usr/bin", "/bin"])
    if login:
        environment["BROWSER"] = str(prepare_browser_hook(profile_home))
    return args, environment


def run_supervised(
    args: list[str],
    environment: dict[str, str],
    check: Callable[[], str | None] | None,
    close_route: Callable[[], None],
    route_alive: Callable[[], bool],
    interval: float,
    *,
    input_fd: int | None = None,
    output_fd: int | None = None,
) -> tuple[int, str]:
    """Monitor SSH liveness; optionally run a probe for diagnostic callers."""
    input_fd = sys.stdin.fileno() if input_fd is None else input_fd
    output_fd = sys.stdout.fileno() if output_fd is None else output_fd
    failure: list[str] = []
    finished = threading.Event()
    failed = threading.Event()
    failure_lock = threading.Lock()
    master, slave = pty.openpty()
    try:
        child = subprocess.Popen(
            ["/usr/bin/python3", str(ENTRY_DIR / "pty_entry.py"), *args],
            stdin=slave, stdout=slave, stderr=slave, env=environment,
            start_new_session=True, close_fds=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    pid = child.pid

    def fail(reason: str) -> None:
        with failure_lock:
            if failure:
                return
            failure.append(reason)
            # Closing SSH tears down established tunnels before the process kill.
            close_route()
            failed.set()

    def watch() -> None:
        assert check is not None
        while not finished.wait(interval):
            try:
                reason = check()
            except Exception:
                reason = "egress_check_failed"
            if finished.is_set():
                return
            if reason:
                fail(reason)
                return

    def resize(*_unused: object) -> None:
        if os.isatty(input_fd):
            try:
                size = fcntl.ioctl(input_fd, termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(master, termios.TIOCSWINSZ, size)
            except OSError:
                pass

    def interrupted(signum: int, _frame: object) -> None:
        fail("launcher_interrupted")

    old_handlers = {
        number: signal.getsignal(number)
        for number in (signal.SIGWINCH, signal.SIGINT, signal.SIGTERM)
    }
    old_tty = termios.tcgetattr(input_fd) if os.isatty(input_fd) else None
    worker = threading.Thread(target=watch, daemon=True) if check is not None else None
    reaped = False
    status = 0
    try:
        signal.signal(signal.SIGWINCH, resize)
        signal.signal(signal.SIGINT, interrupted)
        signal.signal(signal.SIGTERM, interrupted)
        resize()
        if old_tty is not None:
            tty.setraw(input_fd)
        if worker is not None:
            worker.start()
        inputs = [master, input_fd]
        while True:
            observed, status = os.waitpid(pid, os.WNOHANG)
            if observed:
                reaped = True
                # Drain output already buffered by the PTY after normal exit.
                while select.select([master], [], [], 0)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    os.write(output_fd, data)
                break
            if not route_alive():
                fail("ssh_tunnel_lost")
            if failed.is_set():
                break
            readable, _, _ = select.select(inputs, [], [], 0.1)
            for source in readable:
                try:
                    data = os.read(source, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO and source == master:
                        inputs.remove(master)
                        continue
                    raise
                if not data:
                    inputs.remove(source)
                    if source == input_fd:
                        os.write(master, b"\x04")
                    continue
                os.write(output_fd if source == master else master, data)
    finally:
        finished.set()
        close_route()
        if not reaped:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                observed, status = os.waitpid(pid, os.WNOHANG)
                if observed:
                    reaped = True
                    break
                time.sleep(0.05)
            if not reaped:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, status = os.waitpid(pid, 0)
        if worker is not None:
            worker.join(timeout=1)
        if os.isatty(output_fd):
            try:
                os.write(output_fd, TERMINAL_RESET)
            except OSError:
                pass  # A disconnected terminal must not prevent process cleanup.
        if old_tty is not None:
            # Drop queued mouse reports before handing input back to the shell.
            termios.tcsetattr(input_fd, termios.TCSAFLUSH, old_tty)
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
        os.close(master)
        child.returncode = os.waitstatus_to_exitcode(status)
    if failure:
        return 69, failure[0]
    code = os.waitstatus_to_exitcode(status)
    return (code if code >= 0 else 128 - code), "child_exited"


def egress_check(config: Config, socket_path: Path) -> str | None:
    outcome: queue.Queue = queue.Queue(maxsize=1)

    def inspect() -> None:
        try:
            value = proxy_json(str(socket_path), config.probe_url, config.timeout_seconds)
            outcome.put(config.judge(value))
        except RouteError as exc:
            outcome.put(str(exc))
        except Exception:
            outcome.put("egress_check_failed")

    # A socket timeout alone is per read, not a total deadline. Bound the whole
    # check so a slow response cannot keep an unverified session running.
    threading.Thread(target=inspect, daemon=True).start()
    try:
        return outcome.get(timeout=config.timeout_seconds)
    except queue.Empty:
        return "egress_check_timeout"


def receipt(profile_home: Path, code: int, reason: str) -> None:
    """Store only outcome metadata, never terminal output or account fields."""
    payload = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "exit_code": code, "reason": reason}
    path = profile_home / "last-run.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle)
        handle.write("\n")
