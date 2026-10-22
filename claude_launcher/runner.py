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
from .wire import RouteError, proxy_reachable


PROXY_NAMES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
ENTRY_DIR = PACKAGE_DIR / "entries"
# The PTY helper runs outside the sandbox. macOS may lack /usr/bin/python3.
PTY_PYTHON = [sys.executable, "-I"] if sys.platform == "darwin" else ["/usr/bin/python3"]
# Tolerate brief transport stalls before declaring the SSH transport dead.
SSH_KEEPALIVE_OPTIONS = (
    "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
)

# termios restoration does not reset terminal-emulator modes enabled by a TUI.
TERMINAL_RESET = (
    b"\x1b[?9l\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l"
    b"\x1b[?1015l\x1b[?1016l\x1b[?1004l\x1b[?2004l\x1b[?2026l"
    b"\x1b[?1049l\x1b[0m\x1b[?25h"
)
# Cancel a partial control string, then reset emulator state on forced exit.
# Normal exits keep scrollback; forced exits prioritize a usable shell.
TERMINAL_HARD_RESET = b"\x18\x1b\\\x1bc" + TERMINAL_RESET


def _write_bounded(descriptor: int, data: bytes) -> None:
    """Best-effort UI write; a stopped reader must not block cleanup."""
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    try:
        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        deadline = time.monotonic() + 0.1
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [descriptor], [], remaining)[1]:
                break
            try:
                written = os.write(descriptor, data)
            except BlockingIOError:
                continue
            if not written:
                break
            data = data[written:]
    finally:
        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags)


def write_exit_notice(message: str, *, file=None) -> None:
    """Report an exit without blocking on a full terminal or redirected pipe."""
    stream = sys.stdout if file is None else file
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        # In-memory streams used by embedders and tests have no descriptor.
        try:
            print(message, file=stream)
        except (OSError, ValueError):
            pass
        return
    try:
        _write_bounded(descriptor, (message + "\n").encode(
            getattr(stream, "encoding", None) or "utf-8", errors="replace"))
    except OSError:
        pass


def _restore_terminal(input_fd: int, output_fd: int, attributes: list | None,
                      *, hard_reset: bool = False, rendered_tui: bool = False) -> None:
    """Stop mouse tracking and absorb in-flight input before restoring shell echo.

    A remote terminal may send its last mouse packets after the first reset.
    Do not request a terminal reply: a delayed reply can itself pollute the shell.
    """
    terminal_fd = output_fd if os.isatty(output_fd) else (
        input_fd if attributes is not None else (output_fd if rendered_tui else None)
    )
    reset = TERMINAL_HARD_RESET if hard_reset else TERMINAL_RESET
    owned_fd = None
    try:
        if (terminal_fd == input_fd
                and fcntl.fcntl(input_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY):
            owned_fd = os.open(os.ttyname(input_fd), os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
            terminal_fd = owned_fd
        if attributes is not None:
            tty.setraw(input_fd, termios.TCSANOW)
        if terminal_fd is not None:
            _write_bounded(terminal_fd, reset)
        if attributes is not None and terminal_fd is not None:
            deadline = time.monotonic() + 1.0
            quiet_until = time.monotonic() + 0.35
            while True:
                remaining = min(deadline, quiet_until) - time.monotonic()
                if remaining <= 0 or not select.select([input_fd], [], [], remaining)[0]:
                    break
                if not os.read(input_fd, 65536):
                    break
                quiet_until = max(quiet_until, time.monotonic() + 0.15)
    except (OSError, termios.error):
        pass  # A disconnected terminal must not interfere with process cleanup.
    finally:
        if terminal_fd is not None:
            try:
                _write_bounded(terminal_fd, reset)
            except OSError:
                pass
        if attributes is not None:
            try:
                # TCSAFLUSH waits for output to drain and can hang indefinitely
                # on macOS when the PTY peer no longer reads. Restore now and
                # then flush input, including BSD's pending canonical retype.
                # Leave output queued so the terminal can still receive resets.
                termios.tcsetattr(input_fd, termios.TCSANOW, attributes)
            except (OSError, termios.error):
                pass
            try:
                termios.tcflush(input_fd, termios.TCIFLUSH)
            except (OSError, termios.error):
                pass
        if owned_fd is not None:
            os.close(owned_fd)


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
        self._lock = threading.RLock()
        self._errors = tempfile.TemporaryFile()
        self._socket_identity: tuple[int, int] | None = None

    def start(self) -> None:
        with self._lock:
            self._start()

    def _start(self, cancelled: threading.Event | None = None) -> None:
        if cancelled is not None and cancelled.is_set():
            return
        cfg = self.config
        if not cfg.identity_file.is_file() or not cfg.known_hosts.is_file():
            raise RouteError("ssh_key_or_verified_known_hosts_missing")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise RouteError("ssh_forward_socket_changed")
        self._errors.seek(0)
        self._errors.truncate()
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
            if cancelled is not None and cancelled.is_set():
                self.close()
                return
            if self.process.poll() is not None:
                self._errors.seek(0)
                message = self._errors.read(8192).lower()
                if b"host key verification" in message or b"host identification has changed" in message:
                    raise RouteError("ssh_host_key_verification_failed")
                if b"permission denied" in message:
                    raise RouteError("ssh_authentication_failed")
                raise RouteError("ssh_tunnel_failed")
            if self.socket_path.exists():
                info = self.socket_path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise RouteError("ssh_forward_socket_changed")
                self._socket_identity = (info.st_dev, info.st_ino)
                return
            time.sleep(0.05)
        raise RouteError("ssh_tunnel_start_timeout")

    def reconnect(self, cancelled: threading.Event) -> None:
        """Replace only this tunnel's socket; retain host verification and bootstrap."""
        with self._lock:
            if cancelled.is_set():
                return
            self.close()
            try:
                info = self.socket_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                        or (info.st_dev, info.st_ino) != self._socket_identity):
                    raise RouteError("ssh_forward_socket_changed")
                self.socket_path.unlink()
            self._socket_identity = None
            try:
                self._start(cancelled)
            except BaseException:
                self.close()
                raise

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
    private_home_entries: tuple[str, ...] = (),
    timezone: str | None = None,
    reconnectable: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Create only mount/namespace changes scoped to the launched child."""
    profile_home = profile_home.resolve()
    workspace = workspace.resolve()
    live_home = Path.home().resolve()
    if workspace == live_home or workspace in live_home.parents:
        raise RouteError("choose_a_project_directory_not_whole_home")
    protected = (".claude", ".codex", ".ssh", ".local", ".config", ".cache")
    private_workspace = (
        profile_home.parent == live_home / ".local/share/claude-launcher/profiles"
        and workspace in {profile_home / "auth-workspace", profile_home / "default-workspace"}
    )
    if not private_workspace and any(
        workspace.is_relative_to(live_home / name) for name in protected
    ):
        raise RouteError("account_or_key_directory_is_not_a_workspace")
    hidden = (private_paths(live_home, REPO_ROOT, private_home_entries)
              if research_filesystem else [])
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
    upstream = "/run/claude-launcher/upstream.sock"
    args.extend(["--dir", "/run/claude-launcher"])
    if reconnectable:
        # A socket bind pins its inode. A private, socket-only directory lets an
        # existing namespace see the replacement after SSH reconnects.
        parent = socket_path.parent
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or set(parent.iterdir()) != {socket_path}):
            raise RouteError("unsafe_reconnect_directory")
        upstream = "/run/claude-launcher/route/" + socket_path.name
        args.extend(["--ro-bind", str(parent), "/run/claude-launcher/route"])
    else:
        args.extend(["--ro-bind", str(socket_path), upstream])
    args.extend([
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
        str(ENTRY_DIR / "sandbox_entry.py"), upstream, *command,
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
    reconnect: Callable[[threading.Event], None] | None = None,
    connection_event: Callable[[str, str | None], None] | None = None,
) -> tuple[int, str]:
    """Recover daily SSH sessions, or apply fatal checks to maintenance commands."""
    input_fd = sys.stdin.fileno() if input_fd is None else input_fd
    output_fd = sys.stdout.fileno() if output_fd is None else output_fd
    failure: list[str] = []
    finished = threading.Event()
    failed = threading.Event()
    failure_lock = threading.Lock()
    notices: queue.Queue = queue.Queue()
    master, slave = pty.openpty()
    try:
        child = subprocess.Popen(
            [*PTY_PYTHON, str(ENTRY_DIR / "pty_entry.py"), *args],
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
            finished.set()  # Cancel an in-flight reconnect before waiting for its lock.
            failure.append(reason)
            # Closing SSH tears down established tunnels before the process kill.
            try:
                close_route()
            finally:
                failed.set()

    def watch() -> None:
        assert check is not None
        while not finished.wait(interval):
            try:
                reason = check()
            except Exception:
                reason = "connectivity_check_failed"
            if finished.is_set():
                return
            if reason:
                fail(reason)
                return

    def watch_recovering() -> None:
        # One worker owns reconnects. The main thread remains responsive to input
        # and termination. Daily runs leave HTTPS errors/retries to Claude.
        next_retry = 0.0
        backoff = 5.0
        state = "online"
        blocked = False
        stable_since = time.monotonic()

        def transition(new_state: str, reason: str | None = None) -> None:
            nonlocal state
            if state != new_state:
                state = new_state
                notices.put((new_state, reason))

        while not finished.wait(0.1):
            if route_alive():
                if state != "online":
                    transition("connection_restored")
                    state = "online"
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= interval:
                    # A briefly revived SSH process must not cause rapid retries.
                    backoff = 5.0
                    next_retry = 0.0
                continue
            if not route_alive():
                if state == "online":
                    transition("connection_lost", "ssh_tunnel_lost")
                if blocked or time.monotonic() < next_retry:
                    continue
                try:
                    reconnect(finished)
                    if finished.is_set():
                        return
                    if not route_alive():
                        raise RouteError("ssh_tunnel_failed")
                except Exception as exc:
                    if finished.is_set():
                        return
                    reason = str(exc) if isinstance(exc, RouteError) else "ssh_tunnel_failed"
                    if reason in {"ssh_host_key_verification_failed", "ssh_authentication_failed",
                                  "ssh_key_or_verified_known_hosts_missing",
                                  "ssh_forward_socket_changed"}:
                        blocked = True
                        transition("reconnect_blocked", reason)
                    next_retry = time.monotonic() + backoff
                    backoff = min(backoff * 2, 30.0)
                    continue
                next_retry = time.monotonic() + backoff
                backoff = min(backoff * 2, 30.0)

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
    worker = (threading.Thread(target=watch_recovering if reconnect is not None else watch,
                               daemon=True)
              if check is not None or reconnect is not None else None)
    reaped = False
    status = 0
    rendered_tui = False
    pending_output = bytearray()
    pending_input = bytearray()
    output_flags = None
    exit_deadline = None

    def render(data: bytes) -> None:
        nonlocal rendered_tui
        # Relay clients can expose pipes rather than a local TTY. Only append
        # terminal cleanup to such output if the child actually rendered ANSI.
        rendered_tui = rendered_tui or b"\x1b" in data
        pending_output.extend(data)

    try:
        signal.signal(signal.SIGWINCH, resize)
        signal.signal(signal.SIGINT, interrupted)
        signal.signal(signal.SIGTERM, interrupted)
        resize()
        if old_tty is not None:
            tty.setraw(input_fd, termios.TCSANOW)
        output_flags = fcntl.fcntl(output_fd, fcntl.F_GETFL)
        fcntl.fcntl(output_fd, fcntl.F_SETFL, output_flags | os.O_NONBLOCK)
        os.set_blocking(master, False)
        if worker is not None:
            worker.start()
        inputs = [master, input_fd]
        while True:
            if not reaped:
                observed, status = os.waitpid(pid, os.WNOHANG)
                if observed:
                    reaped = True
                    finished.set()  # Late connectivity results cannot replace the child's exit.
                    pending_input.clear()
                    # Bound inactivity after exit, not total drain time: a slow
                    # reader must still receive all remaining output.
                    exit_deadline = time.monotonic() + 1.0
            if reaped and ((master not in inputs and not pending_output)
                           or time.monotonic() >= exit_deadline):
                break
            if not reaped and reconnect is None and not route_alive():
                fail("ssh_tunnel_lost")
            if failed.is_set():
                break
            while not notices.empty():
                event, detail = notices.get_nowait()
                # Claude owns request errors/retries. Only a blocked SSH recovery
                # needs a launcher notice because waiting alone cannot fix it.
                if event == "reconnect_blocked":
                    render(b"\r\nvps-route: SSH could not be safely reconnected; automatic "
                           b"reconnect stopped. Check configuration and restart; "
                           b"session kept open, no direct fallback.\r\n")
                if connection_event is not None:
                    try:
                        connection_event(event, detail)
                    except (OSError, RouteError):
                        pass
            # Apply backpressure to the child, but keep reading user input and
            # checking process/route state while terminal output is blocked.
            readers = [fd for fd in inputs
                       if (fd != master or len(pending_output) < 65536)
                       and (fd == master or (not reaped and len(pending_input) < 65536))]
            readable, writable, _ = select.select(
                readers, ([output_fd] if pending_output else [])
                + ([master] if pending_input and not reaped else []), [], 0.1,
            )
            if output_fd in writable:
                try:
                    written = os.write(output_fd, pending_output)
                    del pending_output[:written]
                    if reaped and written:
                        exit_deadline = time.monotonic() + 1.0
                except BlockingIOError:
                    pass
            if master in writable:
                try:
                    written = os.write(master, pending_input)
                    del pending_input[:written]
                except BlockingIOError:
                    pass
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    pending_input.clear()  # PTY slave closed; reap on the next iteration.
            for source in readable:
                try:
                    data = os.read(source, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue
                    if exc.errno == errno.EIO and source == master:
                        inputs.remove(master)
                        continue
                    raise
                if not data:
                    inputs.remove(source)
                    if source == input_fd:
                        pending_input.extend(b"\x04")
                    continue
                if source == master:
                    render(data)
                else:
                    pending_input.extend(data)
    finally:
        finished.set()
        try:
            try:
                close_route()
            finally:
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
        finally:
            try:
                if output_flags is not None:
                    try:
                        fcntl.fcntl(output_fd, fcntl.F_SETFL, output_flags)
                    except OSError:
                        pass
                _restore_terminal(input_fd, output_fd, old_tty,
                                  hard_reset=bool(failure), rendered_tui=rendered_tui)
            finally:
                for number, handler in old_handlers.items():
                    signal.signal(number, handler)
                os.close(master)
                child.returncode = os.waitstatus_to_exitcode(status)
    if failure:
        return 69, failure[0]
    code = os.waitstatus_to_exitcode(status)
    return (code if code >= 0 else 128 - code), "child_exited"


def connectivity_check(config: Config, socket_path: Path,
                       record: Callable[[list[dict]], None] | None = None) -> str | None:
    outcome: queue.Queue = queue.Queue()
    started = time.monotonic()
    deadline = started + config.timeout_seconds

    def inspect(index: int, url: str) -> None:
        began = time.monotonic()
        try:
            proxy_reachable(str(socket_path), url, config.timeout_seconds)
            reason = "reachable"
        except RouteError as exc:
            reason = str(exc)
        except Exception:
            reason = "connectivity_check_failed"
        outcome.put({"target_index": index, "reason": reason,
                     "elapsed_seconds": round(time.monotonic() - began, 3)})

    # Each target gets the full timeout concurrently, with the same total bound.
    # A slow primary no longer consumes the alternate's time budget.
    for index, url in enumerate(config.connectivity_urls):
        threading.Thread(target=inspect, args=(index, url), daemon=True).start()
    results: dict[int, dict] = {}
    reason = "connectivity_check_timeout"
    while len(results) < len(config.connectivity_urls):
        try:
            result = outcome.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            break
        results[result["target_index"]] = result
        reason = result["reason"]
        if reason == "reachable":
            reason = None
            break
        if len(results) < len(config.connectivity_urls):
            reason = "connectivity_check_timeout"
    if record is not None:
        details = [results.get(index, {
            "target_index": index,
            "reason": "pending" if reason is None else "connectivity_check_timeout",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }) for index in range(len(config.connectivity_urls))]
        try:
            record(details)
        except OSError:
            pass  # Logging failure must not turn a reachable route into an outage.
    return reason


class RuntimeConnectivityMonitor:
    """Stop after consecutive unreachable checks; exit IP is not a condition."""

    def __init__(self, config: Config, socket_path: Path,
                 record: Callable[[list[dict]], None] | None = None) -> None:
        self.config = config
        self.socket_path = socket_path
        self.consecutive_failures = 0
        self.record = record

    def __call__(self) -> str | None:
        reason = connectivity_check(self.config, self.socket_path, self.record)
        if reason is None:
            self.consecutive_failures = 0
            return None
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.config.runtime_probe_failures:
            return reason
        # The network namespace remains fail-closed during this grace period.
        return None


def receipt(profile_home: Path, code: int, reason: str) -> None:
    """Store only outcome metadata, never terminal output or account fields."""
    payload = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "exit_code": code, "reason": reason}
    path = profile_home / "last-run.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle)
        handle.write("\n")
