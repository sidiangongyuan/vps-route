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
from .legacy_cutover import (
    ARCHIVE_PARENT, LEGACY_ITEMS, ROUTE, WRAPPER, _home, _safe_path,
    _same_user_legacy_process_running, archive_legacy_state, legacy_inventory, wrapper_script,
)
from .profile_reset import _profile_is_running, reset_profile
from .wire import RouteError


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


def initialization_inventory(config: Config) -> dict[str, object]:
    """Inspect names only, without reading credentials or following directory links."""
    from .profile_reset import full_reset_inventory

    inventory = full_reset_inventory(config, include_hicc=False)
    legacy = legacy_inventory(include_hicc=False)
    return {
        "archive_paths": [str(Path.home() / LEGACY_ITEMS[name]) for name in legacy["present"]]
        + ([str(config.profile_home)] if inventory["profile_present"] else []),
        "process_running": inventory["profile_process_running"]
        or legacy["legacy_process_running"],
        "permanent_deletion": False,
        "project_files_shared_skills_ssh_and_vps_preserved": True,
    }


def archive_for_initialization(config: Config) -> dict[str, str | None]:
    inventory = initialization_inventory(config)
    if inventory["process_running"]:
        raise RouteError("claude_or_hicc_still_running; close_sessions_before_init")
    legacy = legacy_inventory(include_hicc=False)
    archive = archive_legacy_state(include_hicc=False) if legacy["present"] else None
    try:
        profile = reset_profile(config)
    except (RouteError, OSError) as exc:
        raise RouteError(f"init_incomplete; legacy_archive={archive}; {exc}") from None
    return {"legacy_archive": str(archive) if archive else None,
            "profile_archive": str(profile) if profile else None}


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
    home = _home(None)
    paths: list[Path] = []
    profiles = home / ".local/share/claude-launcher/profiles"
    if not keep_profiles and _safe_path(home, profiles, expect_directory=True):
        for entry in profiles.iterdir():
            if entry.name != ".archives":
                _safe_path(home, entry, expect_directory=True)
                paths.append(entry)
    if not keep_backups:
        paths.extend([profiles / ".archives", home / ARCHIVE_PARENT,
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
    # Remove only the exact wrapper we generated; do not restore a direct CLI.
    wrapper = home / WRAPPER
    if not wrapper.is_symlink() and _safe_path(home, wrapper, expect_directory=False):
        if wrapper.stat().st_size < 65536 and wrapper.read_text() == wrapper_script(home / ROUTE):
            paths.append(wrapper)
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
        "process_running": tracked_running or maintenance_in_use()
        or _same_user_legacy_process_running()
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
        raise RouteError("claude_or_hicc_still_running; close_sessions_before_uninstall")
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
