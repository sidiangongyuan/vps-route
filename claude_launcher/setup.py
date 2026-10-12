"""Create explicit per-user configuration without touching any account or service."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import Config
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
                      prompt: Callable[[str], str] = input) -> Config:
    """Write a validated config exclusively; never overwrite an existing file."""
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise RouteError("config_already_exists; use_--config_for_another_file")
    if source is not None:
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
    _private_parent(destination)
    payload = asdict(config)
    for name in ("identity_file", "known_hosts", "texlive_root"):
        if payload[name] is not None:
            payload[name] = str(payload[name])
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    return config
