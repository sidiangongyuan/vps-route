"""Small private operational logs, never terminal output or account material."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .wire import RouteError


LOG_LIMIT = 1024 * 1024
LOG_BACKUPS = 4
SAFE_REASONS = {
    "child_exited", "ssh_tunnel_lost", "ssh_tunnel_failed", "ssh_tunnel_start_timeout",
    "ssh_host_key_verification_failed", "ssh_authentication_failed",
    "ssh_key_or_verified_known_hosts_missing", "network_isolation_unavailable",
    "egress_check_timeout", "egress_check_failed", "egress_ip_changed", "egress_ip_unknown",
    "egress_country_changed", "egress_country_unknown", "route_not_started",
    "install_official_claude_in_separate_profile_first", "native_launcher_missing_after_update",
    "updated_binary_unusable", "update_verification_route_failed", "operation_failed",
    "interrupted",
    "system_python_3_10_required",
}


def log_directory() -> Path:
    return Path.home() / ".local/state/claude-launcher/logs"


def _private_file(path: Path, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise RouteError("unsafe_audit_file")
    os.fchmod(descriptor, 0o600)
    return descriptor


class AuditLog:
    def __init__(self, operation: str, process_timezone: str | None) -> None:
        self.operation = operation
        self.process_timezone = process_timezone
        self.run_id = secrets.token_hex(8)
        self.started = time.monotonic()
        self.exit_code = 69
        self.reason = "operation_failed"

    def write(self, event: str, *, country: str | None = None) -> None:
        root = log_directory()
        if root.is_symlink():
            raise RouteError("unsafe_audit_directory")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if root.stat().st_uid != os.getuid():
            raise RouteError("unsafe_audit_directory")
        root.chmod(0o700)
        now = datetime.now(timezone.utc)
        payload = {
            "time": now.isoformat(timespec="seconds"),
            "local_time": now.astimezone(
                ZoneInfo(self.process_timezone) if self.process_timezone else None
            ).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "operation": self.operation,
            "event": event,
            "process_timezone": self.process_timezone or "host_system_default",
        }
        if event == "preflight_passed":
            payload.update(network_isolated=True, ssh_host_verified=True,
                           egress_matches=True, country=country)
        if event == "exit":
            payload.update(exit_code=self.exit_code,
                           reason=self.reason if self.reason in SAFE_REASONS else "operation_failed",
                           duration_seconds=round(time.monotonic() - self.started, 3))
        # Serialize append/rotation across simultaneous CLI sessions.
        lock = _private_file(root / ".lock", os.O_WRONLY | os.O_CREAT)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = root / "usage.jsonl"
            if path.is_symlink():
                raise RouteError("unsafe_audit_file")
            if path.exists() and path.stat().st_size >= LOG_LIMIT:
                for number in range(LOG_BACKUPS, 0, -1):
                    source = path if number == 1 else root / f"usage.jsonl.{number - 1}"
                    target = root / f"usage.jsonl.{number}"
                    if source.is_symlink() or target.is_symlink():
                        raise RouteError("unsafe_audit_file")
                    if source.exists():
                        os.replace(source, target)
            descriptor = _private_file(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
        finally:
            os.close(lock)


def log_tail(count: int = 20) -> str:
    if not 1 <= count <= 200:
        raise RouteError("log_tail_must_be_between_1_and_200")
    path = log_directory() / "usage.jsonl"
    if not path.exists():
        return "No usage log yet."
    descriptor = _private_file(path, os.O_RDONLY)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        return "".join(stream.readlines()[-count:]).rstrip()
