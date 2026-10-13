"""Initialize or reuse a private route without resetting any Claude state."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import Config
from .install import installation_present
from .wire import RouteError


def default_config_path() -> Path:
    return Path.home() / ".config/vps-route/config.json"


def _private_parent(path: Path) -> None:
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise RouteError("config_parent_must_not_be_symlinked")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid():
        raise RouteError("config_parent_must_be_owned")
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise RouteError("config_parent_must_be_private; choose_a_0700_directory")


def initialize_config(destination: Path, source: Path | None = None,
                      prompt: Callable[[str], str] = input, *, keep_existing: bool = True,
                      dry_run: bool = False, yes: bool = False) -> Config:
    """Reuse valid state, create missing profile directories, never overwrite config.

    keep_existing and yes remain accepted for compatibility; init always preserves data.
    """
    destination = destination.expanduser().absolute()
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise RouteError("config_path_must_not_be_symlinked")
    existing = destination.exists()
    if existing:
        info = destination.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise RouteError("config_must_be_a_private_owned_file")
        config = Config.load(destination)
        if source is not None and Config.load(source.expanduser()) != config:
            raise RouteError("config_already_exists_and_differs; existing_config_preserved")
    elif source is not None:
        config = Config.load(source.expanduser())
    else:
        def ask(label: str, default: str = "") -> str:
            answer = prompt(f"{label}{f' [{default}]' if default else ''}: ").strip()
            return answer or default

        user = ask("VPS SSH user", "vpsuser")
        values = {
            "ssh_user": user,
            "ssh_host": ask("VPS numeric IP (not a hostname)"),
            "ssh_port": ask("VPS SSH port", "22"),
            "identity_file": ask("Existing SSH private key path", "~/.ssh/id_ed25519"),
            "known_hosts": ask("Verified known_hosts file", "~/.ssh/known_hosts"),
            "remote_socket": ask("Private socket on VPS", "/root/.local/state/claude-launcher/connect.sock"
                                 if user == "root" else
                                 str(Path("/home") / user /
                                     ".local/state/claude-launcher/connect.sock")),
            "expected_ip": ask("Expected final public exit IP"),
            "expected_country": ask("Expected two-letter country", "US").upper(),
            "profile": ask("Independent profile name", "default"),
            "ssh_http_proxy": ask("Local HTTP proxy for SSH; blank means direct SSH") or None,
            "timezone": ask("Process timezone; blank means host default") or None,
        }
        try:
            values["ssh_port"] = int(values["ssh_port"])
        except ValueError:
            raise RouteError("invalid_ssh_port") from None
        with tempfile.TemporaryDirectory(prefix="vps-route-init-") as temporary:
            candidate = Path(temporary) / "candidate.json"
            candidate.write_text(json.dumps(values))
            config = Config.load(candidate)
    if not config.identity_file.is_file() or not config.known_hosts.is_file():
        raise RouteError("ssh_key_or_verified_known_hosts_missing")
    if destination.is_relative_to(config.profile_home):
        raise RouteError("config_must_stay_outside_claude_profile")
    installed = installation_present(config)
    print(json.dumps({"config_path": str(destination), "config_reused": existing,
                      "profile_present": config.profile_home.is_dir(),
                      "claude_installed": installed, "existing_state_preserved": True,
                      "archive_paths": []}, indent=2))
    if dry_run:
        return config
    if existing:
        config.profile_home.mkdir(parents=True, mode=0o700, exist_ok=True)
        return config
    _private_parent(destination)
    payload = asdict(config)
    for name in ("identity_file", "known_hosts", "texlive_root"):
        if payload[name] is not None:
            payload[name] = str(payload[name])
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        # Another init may have finished while the wizard was open. Revalidate it.
        reused = initialize_config(destination, source, prompt, dry_run=dry_run)
        if reused != config:
            raise RouteError("config_already_exists_and_differs; existing_config_preserved")
        return reused
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    config.profile_home.mkdir(parents=True, mode=0o700, exist_ok=True)
    return config
