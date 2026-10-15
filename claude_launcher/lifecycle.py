"""Explicit local lifecycle operations; never connect to or uninstall from a VPS."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .config import Config
from .profile_reset import _profile_is_running
from .wire import RouteError


# Archives written by pre-0.4 releases; removed with the other tool backups.
OLD_ARCHIVES = Path(".local/share/claude-launcher/legacy-archives")


def _safe_path(home: Path, path: Path, *, expect_directory: bool | None = None) -> bool:
    """Validate an existing user-owned path without following a symlink."""
    if not path.is_relative_to(home):
        raise RouteError("path_outside_home")
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
            raise RouteError("unsafe_tool_path")
        current = current.parent
    if not present:
        return False
    mode = path.lstat().st_mode
    if expect_directory is True and not stat.S_ISDIR(mode):
        raise RouteError("unsafe_tool_path")
    if expect_directory is False and not stat.S_ISREG(mode):
        raise RouteError("unsafe_tool_path")
    if expect_directory is None and not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise RouteError("unsafe_tool_path")
    if path.is_dir() and os.path.ismount(path):
        raise RouteError("tool_path_is_mount")
    return True


def _home() -> Path:
    root = Path.home()
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise RouteError("unsafe_home")
    return root.absolute()


def confirm_operation(message: str, *, yes: bool = False,
                      prompt: Callable[[str], str] = input) -> None:
    if yes:
        return
    try:
        answer = prompt(f"{message} [y/N]: ").strip().lower()
    except (EOFError, OSError):
        raise RouteError("confirmation_required; inspect_--dry-run_then_use_-y") from None
    if answer not in {"y", "yes"}:
        raise RouteError("operation_cancelled; no_changes_made")


def _validate_tree(path: Path) -> None:
    """Validate all candidates before deleting anything, with no link traversal."""
    if path.is_symlink() or not path.is_dir():
        return
    if os.path.ismount(path):
        raise RouteError("cleanup_refuses_mount")
    for base, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            child = Path(base) / name
            info = child.lstat()
            if info.st_uid != os.getuid():
                raise RouteError("cleanup_refuses_foreign_owned_file")
            if not stat.S_ISLNK(info.st_mode) and os.path.ismount(child):
                raise RouteError("cleanup_refuses_mount")


def _package_command() -> list[str] | None:
    """Use the package manager, never manually remove Python or its environment."""
    try:
        importlib.metadata.distribution("vps-route")
    except importlib.metadata.PackageNotFoundError:
        return None
    metadata = Path(sys.prefix) / "pipx_metadata.json"
    if metadata.is_file():
        try:
            package = json.loads(metadata.read_text())["main_package"]["package"]
        except (OSError, ValueError, KeyError, TypeError):
            raise RouteError("cannot_identify_package_manager") from None
        if package != "vps-route":
            raise RouteError("refuse_uninstall_from_unrelated_pipx_environment")
        pipx = shutil.which("pipx")
        if not pipx:
            raise RouteError("pipx_required_for_package_uninstall; or_use_--keep-package")
        return [pipx, "uninstall", "vps-route"]
    if sys.prefix == sys.base_prefix:
        raise RouteError("refuse_system_python_uninstall; use_--keep-package")
    return [sys.executable, "-m", "pip", "uninstall", "-y", "vps-route"]


def uninstall_inventory(config_path: Path, *, keep_profiles: bool = False,
                        keep_backups: bool = False, keep_logs: bool = False,
                        keep_config: bool = False, keep_package: bool = False) -> dict[str, object]:
    home = _home()
    paths: list[Path] = []
    profiles = home / ".local/share/claude-launcher/profiles"
    if not keep_profiles and _safe_path(home, profiles, expect_directory=True):
        for entry in profiles.iterdir():
            if entry.name != ".archives":
                _safe_path(home, entry, expect_directory=True)
                paths.append(entry)
    if not keep_backups:
        paths.extend([profiles / ".archives", home / OLD_ARCHIVES,
                      home / ".local/state/claude-launcher/updates"])
    if not keep_logs:
        paths.append(home / ".local/state/claude-launcher/logs")
    paths.extend([home / ".local/state/claude-launcher/sessions",
                  home / ".local/state/claude-launcher/.maintenance.lock"])
    if not keep_config:
        paths.append(home / ".config/vps-route")
        supplied = config_path.expanduser().absolute()
        if not supplied.is_relative_to(home / ".config/vps-route"):
            if _safe_path(home, supplied, expect_directory=False) and not any(
                supplied.is_relative_to(path) for path in paths
            ):
                try:
                    Config.load(supplied)
                except RouteError:
                    raise RouteError("custom_config_not_identifiable; remove_it_manually") from None
            paths.append(supplied)
    present: list[Path] = []
    for path in paths:
        if _safe_path(home, path):
            _validate_tree(path)
            if not any(path.is_relative_to(parent) for parent in present):
                present = [item for item in present if not item.is_relative_to(path)]
                present.append(path)
    # Users may keep their SSH material in custom directories. Do not delete a
    # configured key/host pin merely because it resides below a tool data root.
    supplied = config_path.expanduser().absolute()
    if _safe_path(home, supplied, expect_directory=False):
        try:
            configured = Config.load(supplied)
        except RouteError:
            configured = None  # Corrupt/absent configuration must not block bounded cleanup.
        if configured is not None:
            for protected in (configured.identity_file, configured.known_hosts):
                if any(protected.resolve().is_relative_to(path) for path in present):
                    raise RouteError("ssh_material_inside_cleanup_target; move_it_or_keep_category")
    command = None if keep_package else _package_command()
    from .sessions import list_sessions, maintenance_in_use

    tracked_running = any(item["state"] == "running" for item in list_sessions())
    return {
        "delete_paths": [str(path) for path in present],
        "package_command": command,
        # Only processes started from tool profiles; an unrelated claude CLI may keep running.
        "process_running": tracked_running or maintenance_in_use()
        or _profile_is_running(profiles),
        "local_only": True,
        "permanent_deletion": True,
        "secure_physical_erasure": False,
        "ssh_keys_projects_shared_skills_and_vps_preserved": True,
    }


def uninstall_local(config_path: Path, *, yes: bool = False,
                    prompt: Callable[[str], str] = input, **options: bool) -> dict[str, object]:
    inventory = uninstall_inventory(config_path, **options)
    print(json.dumps(inventory, indent=2))
    if inventory["process_running"]:
        raise RouteError("vps_route_sessions_still_running; close_them_before_uninstall")
    confirm_operation("Permanently delete these local files and backups?", yes=yes, prompt=prompt)
    # Recheck after the user-facing wait. -y never bypasses ownership or process checks.
    fresh = uninstall_inventory(config_path, **options)
    if fresh != inventory:
        raise RouteError("cleanup_state_changed; review_again")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RouteError("safe_directory_removal_unavailable")
    deleted: list[str] = []
    try:
        for item in inventory["delete_paths"]:
            path = Path(item)
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)
            deleted.append(item)
    except OSError as exc:
        raise RouteError(f"uninstall_incomplete; deleted_paths={deleted}; {exc}") from None
    command = inventory["package_command"]
    if command is not None:
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise RouteError("local_data_removed; package_uninstall_failed; retry_package_manager")
    return {"deleted_paths": deleted, "package_removed": command is not None,
            "vps_changed": False}
