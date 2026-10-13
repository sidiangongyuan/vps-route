"""Private process leases, PID-reuse-safe stop, and a local maintenance barrier."""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import stat
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .wire import RouteError


def state_directory(*, create: bool = True) -> Path:
    root = Path.home() / ".local/state/claude-launcher"
    for path in (root, *root.parents):
        if path.is_symlink():
            raise RouteError("unsafe_session_directory")
    if create:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.exists() and root.stat().st_uid != os.getuid():
        raise RouteError("unsafe_session_directory")
    if create:
        root.chmod(0o700)
    return root


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(fd)
        raise RouteError("unsafe_session_file")
    return fd


@contextmanager
def maintenance_barrier(*, exclusive: bool = False) -> Iterator[None]:
    fd = _open_private(state_directory() / ".maintenance.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RouteError("active_sessions_or_maintenance; retry_when_idle") from None
        yield
    finally:
        os.close(fd)


def maintenance_in_use() -> bool:
    """Read-only check, also covering upgrades/preflight before a lease exists."""
    path = state_directory(create=False) / ".maintenance.lock"
    try:
        fd = _open_private(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def process_identity(pid: int) -> str | None:
    """Start ticks uniquely identify a PID on this boot; never read its environment."""
    try:
        root = Path("/proc") / str(pid)
        if root.stat().st_uid != os.getuid():
            return None
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]  # field 22 after pid and parenthesized comm
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, IndexError):
        raise RouteError("cannot_confirm_session_process") from None


class SessionLease:
    def __init__(self, run_id: str, profile: str, operation: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{16}", run_id):
            raise RouteError("invalid_session_id")
        self.root = state_directory() / "sessions"
        if self.root.is_symlink():
            raise RouteError("unsafe_session_directory")
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.stat().st_uid != os.getuid():
            raise RouteError("unsafe_session_directory")
        self.root.chmod(0o700)
        self.path = self.root / f"{run_id}.json"
        self.fd: int | None = None
        self.payload = {"run_id": run_id, "profile": profile, "operation": operation,
                        "pid": os.getpid(), "start_ticks": process_identity(os.getpid()),
                        "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def __enter__(self) -> SessionLease:
        self.fd = _open_private(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(self.fd, json.dumps(self.payload).encode())
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_unused: object) -> None:
        if self.fd is not None:
            try:
                info = self.path.lstat()
                own = os.fstat(self.fd)
                if (info.st_ino, info.st_dev) == (own.st_ino, own.st_dev):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            finally:
                os.close(self.fd)
                self.fd = None


def _read_record(path: Path) -> tuple[dict, bool]:
    fd = _open_private(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = False
        except BlockingIOError:
            locked = True
        value = json.loads(os.read(fd, 4097))
        if (not isinstance(value, dict) or value.get("run_id") != path.stem
                or not isinstance(value.get("pid"), int) or value["pid"] <= 1
                or not isinstance(value.get("start_ticks"), str)):
            raise ValueError
        alive = locked and process_identity(value["pid"]) == value["start_ticks"]
        if alive:
            # A copied/forged lease must not authorize signaling another process.
            info = os.fstat(fd)
            descriptors = Path("/proc") / str(value["pid"]) / "fd"
            alive = False
            for item in descriptors.iterdir():
                try:
                    observed = item.stat()
                except FileNotFoundError:
                    continue
                if (observed.st_dev, observed.st_ino) == (info.st_dev, info.st_ino):
                    alive = True
                    break
        return value, alive
    except (ValueError, TypeError):
        raise RouteError("invalid_session_record") from None
    finally:
        os.close(fd)


def list_sessions() -> list[dict]:
    root = state_directory(create=False) / "sessions"
    if root.is_symlink():
        raise RouteError("unsafe_session_directory")
    if not root.exists():
        return []
    result = []
    for path in sorted(root.glob("*.json")):
        try:
            value, alive = _read_record(path)
        except FileNotFoundError:
            continue
        result.append({**value, "state": "running" if alive else "stale"})
    return result


def stop_session(run_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{16}", run_id):
        raise RouteError("invalid_session_id")
    root = state_directory() / "sessions"
    if root.is_symlink():
        raise RouteError("unsafe_session_directory")
    path = root / f"{run_id}.json"
    try:
        value, alive = _read_record(path)
    except FileNotFoundError:
        raise RouteError("session_not_found") from None
    if not alive:
        raise RouteError("session_stale; no_process_signaled")
    # pidfd prevents a process exiting/reusing its PID between validation and signal.
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        # Some Conda builds omit pidfd bindings. Use the already-required system
        # Python, rechecking start ticks after opening the PID-pinned descriptor.
        helper = (
            "import os,signal,sys; from pathlib import Path; "
            "pid=int(sys.argv[1]); p=Path('/proc')/str(pid); "
            "fd=os.pidfd_open(pid); "
            "assert p.stat().st_uid==os.getuid(); "
            "assert p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]==sys.argv[2]; "
            "signal.pidfd_send_signal(fd,signal.SIGTERM); os.close(fd)"
        )
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", helper, str(value["pid"]), value["start_ticks"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        if result.returncode:
            raise RouteError("safe_stop_failed; process_exited_or_system_pidfd_unavailable")
        return {"run_id": run_id, "stop_requested": True, "other_sessions_unchanged": True}
    try:
        fd = os.pidfd_open(value["pid"])
        try:
            again, alive = _read_record(path)
            if not alive or again != value:
                raise RouteError("session_changed; no_process_signaled")
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        finally:
            os.close(fd)
    except ProcessLookupError:
        raise RouteError("session_already_exited") from None
    return {"run_id": run_id, "stop_requested": True, "other_sessions_unchanged": True}
