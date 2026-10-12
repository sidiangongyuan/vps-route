"""Prepare a private, explicit HICC-to-isolated-CLI cutover.

This module is deliberately inert until the operator uses both ``--apply``
and ``--confirm-hicc-retired``. It never reads credential contents.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import stat
import time
from pathlib import Path

from .wire import RouteError


LEGACY_ITEMS = {
    "claude-home": Path(".claude"),
    "claude-global": Path(".claude.json"),
    "hicc-home": Path(".hicc"),
    "claude-cli-cache": Path(".cache/claude-cli-nodejs"),
    "claude-cache": Path(".cache/claude"),
    "claude-url-handler": Path(".local/share/applications/claude-code-url-handler.desktop"),
}
ARCHIVE_PARENT = Path(".local/share/claude-launcher/legacy-archives")
WRAPPER = Path(".local/bin/claude")
ROUTE = Path(".local/bin/vps-route")


def _same_user_legacy_process_running() -> bool:
    """Fail closed if /proc cannot establish whether a legacy client is live."""
    try:
        processes = Path("/proc").iterdir()
        for process in processes:
            if not process.name.isdecimal():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                name = (process / "comm").read_text().strip().lower()
                if name in {"hicc", "hiccrun", "claude", "claude.exe"}:
                    return True
            except FileNotFoundError:
                continue
        return False
    except (OSError, UnicodeError):
        raise RouteError("cannot_confirm_legacy_process_state") from None


def _safe_path(home: Path, path: Path, *, expect_directory: bool | None = None) -> bool:
    """Validate an existing user-owned path without following a symlink."""
    if not path.is_relative_to(home):
        raise RouteError("legacy_path_outside_home")
    current = path
    present = True
    while current != home:
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current == path:
                present = False
            current = current.parent
            continue
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise RouteError("unsafe_legacy_path")
        current = current.parent
    if not present:
        return False
    mode = path.lstat().st_mode
    if expect_directory is True and not stat.S_ISDIR(mode):
        raise RouteError("unsafe_legacy_path")
    if expect_directory is False and not stat.S_ISREG(mode):
        raise RouteError("unsafe_legacy_path")
    if expect_directory is None and not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise RouteError("unsafe_legacy_path")
    if path.is_dir() and os.path.ismount(path):
        raise RouteError("legacy_path_is_mount")
    return True


def _home(home: Path | None) -> Path:
    root = Path.home() if home is None else home
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise RouteError("unsafe_home")
    return root.absolute()


def legacy_inventory(home: Path | None = None, *, include_hicc: bool = True) -> dict[str, object]:
    """List path labels only; never inspect old account file contents."""
    root = _home(home)
    present = [name for name, relative in LEGACY_ITEMS.items()
               if (include_hicc or name != "hicc-home") and _safe_path(root, root / relative)]
    return {"present": present,
            "legacy_process_running": _same_user_legacy_process_running()}


def _archive_parent(home: Path) -> Path:
    parent = home / ARCHIVE_PARENT
    _safe_path(home, parent, expect_directory=True)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    return parent


def archive_legacy_state(home: Path | None = None, *, include_hicc: bool = True) -> Path:
    """Move exact legacy paths into one private archive after HICC is retired."""
    root = _home(home)
    inventory = legacy_inventory(root, include_hicc=include_hicc)
    if inventory["legacy_process_running"]:
        raise RouteError("legacy_claude_or_hicc_still_running")
    if not inventory["present"]:
        raise RouteError("no_legacy_state_present")
    parent = _archive_parent(root)
    if _same_user_legacy_process_running():
        raise RouteError("legacy_claude_or_hicc_still_running")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = parent / f"cutover-{stamp}-{secrets.token_hex(4)}"
    archive.mkdir(mode=0o700)
    moved: list[tuple[Path, Path]] = []
    try:
        for name in inventory["present"]:
            original = root / LEGACY_ITEMS[name]
            destination = archive / name
            os.replace(original, destination)
            moved.append((original, destination))
        manifest = archive / "manifest.json"
        descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"created_utc": stamp, "items": inventory["present"]}, stream)
            stream.write("\n")
    except OSError:
        for original, destination in reversed(moved):
            if destination.exists() and not original.exists():
                os.replace(destination, original)
        raise RouteError("legacy_archive_failed") from None
    return archive


def _validated_archive(home: Path, archive: Path) -> Path:
    parent = home / ARCHIVE_PARENT
    target = archive.absolute()
    if (target.name in {".", ".."} or target.parent != parent
            or target.resolve().parent != parent.resolve()
            or not _safe_path(home, target, expect_directory=True)):
        raise RouteError("invalid_legacy_archive")
    marker = target / "manifest.json"
    if not _safe_path(home, marker, expect_directory=False):
        raise RouteError("invalid_legacy_archive")
    return target


def wrapper_script(route: Path) -> str:
    """Dispatch ordinary CLI use, login, and updates through the owned route."""
    # The route path is a validated absolute path under the current user's home.
    return f'''#!/bin/sh
route={shlex.quote(str(route))}
case "$1" in
  auth)
    shift
    case "$1" in
      login)
        [ "$#" -eq 1 ] || exit 64
        exec "$route" auth login
        ;;
      status)
        shift
        if [ "$#" -gt 0 ]; then
          [ "$#" -eq 1 ] && [ "$1" = "--text" ] || exit 64
        fi
        exec "$route" auth status "$@"
        ;;
      *) echo "Use vps-route auth login or status" >&2; exit 64 ;;
    esac
    ;;
  update)
    shift
    [ "$#" -eq 0 ] || exit 64
    exec "$route" update
    ;;
  install)
    shift
    [ "$#" -eq 0 ] || exit 64
    exec "$route" install
    ;;
  --version|-v)
    [ "$#" -eq 1 ] || exit 64
    exec "$route" native-version
    ;;
  *) exec "$route" run --workspace "$PWD" -- "$@" ;;
esac
'''


def activation_inventory(archive: Path, home: Path | None = None) -> dict[str, bool]:
    root = _home(home)
    _validated_archive(root, archive)
    route, wrapper = root / ROUTE, root / WRAPPER
    return {
        "legacy_process_running": _same_user_legacy_process_running(),
        "new_route_command_ready": (
            _safe_path(root, route, expect_directory=False) and os.access(route, os.X_OK)
        ),
        "old_claude_wrapper_present": _safe_path(root, wrapper, expect_directory=False),
    }


def activate_wrapper(archive: Path, home: Path | None = None) -> Path:
    """Back up the user's old wrapper and atomically install the prepared one."""
    root = _home(home)
    target = _validated_archive(root, archive)
    if _same_user_legacy_process_running():
        raise RouteError("legacy_claude_or_hicc_still_running")
    route, wrapper = root / ROUTE, root / WRAPPER
    if not _safe_path(root, route, expect_directory=False) or not os.access(route, os.X_OK):
        raise RouteError("new_route_command_missing")
    if not _safe_path(root, wrapper, expect_directory=False):
        raise RouteError("old_claude_wrapper_missing")
    backup = target / "original-claude-wrapper"
    if backup.exists() or backup.is_symlink():
        raise RouteError("wrapper_already_backed_up")
    temporary = wrapper.parent / f".claude-cutover-{secrets.token_hex(4)}"
    moved = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(wrapper_script(route))
        os.replace(wrapper, backup)
        moved = True
        os.replace(temporary, wrapper)
    except OSError:
        if moved and not wrapper.exists():
            os.replace(backup, wrapper)
        raise RouteError("wrapper_activation_failed") from None
    finally:
        if temporary.exists():
            temporary.unlink()
    return backup
