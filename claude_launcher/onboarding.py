"""Complete CLI UI setup for a profile that already has local OAuth state.

This does not authenticate, refresh tokens, accept project trust, or change permissions.
The official CLI still validates credentials normally when it uses the account.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .wire import RouteError


def complete_authenticated_onboarding(profile: Path, executable: Path) -> bool:
    """Repair missing UI state, preserving account data and an exclusive backup."""
    directory = profile / ".claude"
    config_path = directory / ".claude.json"
    credentials = directory / ".credentials.json"
    if directory.is_symlink() or config_path.is_symlink() or credentials.is_symlink():
        raise RouteError("onboarding_state_must_not_be_symlinked")
    if not config_path.is_file() or not credentials.is_file() or not credentials.stat().st_size:
        return False
    original = config_path.read_bytes()
    try:
        config = json.loads(original)
        if not isinstance(config, dict):
            raise ValueError
    except ValueError:
        raise RouteError("onboarding_config_invalid") from None
    if config.get("hasCompletedOnboarding") is True or not config.get("oauthAccount"):
        return False
    # Do not race a running CLI that has cached the unfinished onboarding state.
    target = executable.resolve()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid == os.getuid() and (process / "exe").resolve() == target:
                raise RouteError("exit_running_claude_before_onboarding_repair")
        except (FileNotFoundError, PermissionError):
            continue
    config["hasCompletedOnboarding"] = True
    if re.fullmatch(r"\d+\.\d+\.\d+", target.name):
        config["lastOnboardingVersion"] = target.name
    backup = directory / ".claude.json.before-onboarding-repair"
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original)
    descriptor, temporary = tempfile.mkstemp(prefix=".onboarding-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if config_path.read_bytes() != original:
            raise RouteError("onboarding_config_changed_retry_when_cli_exits")
        os.replace(temporary, config_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True
