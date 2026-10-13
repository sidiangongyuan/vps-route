"""Rotate only the launcher's isolated Claude profile, keeping a private archive."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from .config import Config
from .wire import RouteError


def _profile_is_running(profile_home: Path) -> bool:
    """Refuse rotation while a same-user process was started from this profile."""
    needle = os.fsencode(str(profile_home))
    proc = Path("/proc")
    try:
        entries = tuple(proc.iterdir())
    except OSError:
        raise RouteError("cannot_confirm_profile_process_state") from None

    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            if needle in (entry / "cmdline").read_bytes():
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            raise RouteError("cannot_confirm_profile_process_state") from None
    return False


def reset_profile(config: Config) -> Path | None:
    """Archive the tool-owned profile and create a blank replacement.

    The archive is a sibling of the active profile and is never mounted into
    the Claude sandbox. No path outside the current user's home is accepted.
    """
    home = Path.home().resolve()
    configured = config.profile_home
    profiles = configured.parent.resolve()
    active = profiles / configured.name
    if not profiles.is_relative_to(home):
        raise RouteError("profile_path_outside_home")
    if active.is_symlink() or (active.exists() and not active.is_dir()):
        raise RouteError("unsafe_profile_path")
    if active.exists() and _profile_is_running(active):
        raise RouteError("profile_process_running")

    archive_root = profiles / ".archives"
    if archive_root.is_symlink():
        raise RouteError("unsafe_archive_path")
    archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive_root.chmod(0o700)

    archive = None
    if active.exists():
        active.chmod(0o700)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive = archive_root / f"{config.profile}-{stamp}-{secrets.token_hex(4)}"
        os.replace(active, archive)

    try:
        active.mkdir(mode=0o700)
        active.chmod(0o700)
    except OSError:
        if archive is not None and not active.exists():
            os.replace(archive, active)
        raise

    return archive


def profile_reset_inventory(config: Config) -> dict[str, object]:
    """Only the selected profile; auth reset must not inspect legacy account stores."""
    active = config.profile_home
    home = Path.home().resolve()
    for path in (active, *active.parents):
        if path == home:
            break
        if (path.is_symlink() or (path.exists() and
                (not path.is_dir() or path.stat().st_uid != os.getuid()))):
            raise RouteError("unsafe_profile_path")
    return {"profile": str(active), "profile_present": active.exists(),
            "profile_process_running": _profile_is_running(active),
            "archive_not_delete": True, "account_files_read": False,
            "vps_and_project_files_unchanged": True}


def full_reset_inventory(config: Config, *, include_hicc: bool = True) -> dict[str, object]:
    """Preview exact state paths, without reading credentials or session contents."""
    from .legacy_cutover import legacy_inventory

    home = Path.home().resolve()
    active = config.profile_home
    # Unlike resolve-only validation, reject links in every managed ancestor.
    if not active.is_relative_to(home):
        raise RouteError("profile_path_outside_home")
    for path in (active, *active.parents):
        if path == home:
            break
        if path.is_symlink():
            raise RouteError("unsafe_profile_path")
        if path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()):
            raise RouteError("unsafe_profile_path")
    if (active.parent / ".archives").is_symlink():
        raise RouteError("unsafe_archive_path")
    legacy = legacy_inventory(include_hicc=include_hicc)
    return {
        "profile": str(active),
        "profile_present": active.exists(),
        "profile_process_running": _profile_is_running(active),
        "legacy_present": legacy["present"],
        "legacy_process_running": legacy["legacy_process_running"],
        "projects_and_shared_skills_preserved": True,
        "route_and_ssh_configuration_preserved": True,
        "backup_contains_old_credentials": True,
        "physical_erasure": False,
        "login_performed": False,
    }


def reset_local_state(config: Config) -> dict[str, object]:
    """Compose the existing archive operations; never destroy their backups.

    Cross-directory moves are not a single transaction. If a later step fails,
    preserve and report completed archives rather than pretending to roll back
    newly created state or discarding the user's data.
    """
    from .legacy_cutover import archive_legacy_state

    inventory = full_reset_inventory(config)
    if inventory["profile_process_running"] or inventory["legacy_process_running"]:
        raise RouteError("claude_or_hicc_still_running; close_sessions_before_reset")
    legacy = archive_legacy_state() if inventory["legacy_present"] else None
    try:
        profile = reset_profile(config)
    except (RouteError, OSError) as exc:
        raise RouteError(f"reset_incomplete; legacy_archive={legacy}; {exc}") from None

    # Recreate only a neutral update preference, never restore old settings.
    settings = config.profile_home / ".claude/settings.json"
    settings.parent.mkdir(mode=0o700)
    with settings.open("x", encoding="utf-8") as stream:
        os.chmod(settings, 0o600)
        json.dump({"autoUpdatesChannel": "latest"}, stream)
        stream.write("\n")
    return {
        "profile_archive": str(profile) if profile else None,
        "legacy_archive": str(legacy) if legacy else None,
        "fresh_profile": str(config.profile_home),
        "login_performed": False,
        "projects_and_shared_skills_preserved": True,
    }
