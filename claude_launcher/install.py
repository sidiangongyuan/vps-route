"""Explicit official native install inside the launcher's separate home."""

import os
import stat
import subprocess
import tempfile
from pathlib import Path

from .config import Config
from .wire import RouteError


def installation_present(config: Config) -> bool:
    """Inspect paths only; never run Claude or read account files."""
    home = config.profile_home
    for path in (home, *home.parents):
        if path.is_symlink():
            raise RouteError("profile_path_must_not_be_symlinked")
    if home.exists():
        info = home.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise RouteError("profile_must_be_a_private_owned_directory")
    executable = config.claude_executable
    for path in executable.parents:
        if path == home:
            break
        if path.is_symlink():
            raise RouteError("claude_install_directory_must_not_be_symlinked")
    if not executable.exists() and not executable.is_symlink():
        return False
    try:
        resolved = executable.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RouteError("claude_installation_invalid; inspect_entry_before_retrying") from None
    if not resolved.is_relative_to(home):
        raise RouteError("claude_executable_must_stay_inside_separate_profile")
    info = resolved.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or not os.access(executable, os.X_OK)):
        raise RouteError("claude_installation_invalid; inspect_entry_before_retrying")
    return True


def main() -> int:
    cache = Path.home() / ".cache/claude-launcher"
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="installer-", dir=cache) as temporary:
        script = Path(temporary) / "install.sh"
        downloaded = subprocess.run([
            "curl", "--fail", "--silent", "--show-error", "--location",
            "--proto", "=https", "--proto-redir", "=https",
            "https://claude.ai/install.sh", "--output", str(script),
        ], check=False)
        if downloaded.returncode:
            return downloaded.returncode
        return subprocess.call(["bash", str(script), "latest"])
