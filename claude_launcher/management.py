"""Explicit config edits; no account files, automatic route changes, or secret output."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path

from .config import Config
from .setup import _private_parent
from .wire import RouteError


PRIVATE_FIELDS = {
    "ssh_host", "ssh_user", "identity_file", "known_hosts", "remote_socket",
    "ssh_http_proxy", "texlive_root", "profile", "probe_url", "connectivity_urls",
}


def config_values(config: Config, *, reveal: bool = False) -> dict:
    values = asdict(config)
    values.pop("expected_ip", None)
    values.pop("expected_country", None)
    for name, value in values.items():
        if isinstance(value, Path):
            values[name] = str(value)
        if not reveal and name in PRIVATE_FIELDS and value is not None:
            values[name] = "xxx"
    return values


def private_json(path: Path, value: dict) -> None:
    """Replace only a private, owned, regular file without following links."""
    _private_parent(path)
    before = None
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise RouteError("unsafe_management_file; use_an_owned_0600_file")
        before = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    fd, name = tempfile.mkstemp(prefix=".route-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if before is None:
            # Exclusive publication also protects against concurrent first writers.
            os.link(temporary, path)
        else:
            info = path.lstat()
            if before != (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size):
                raise RouteError("management_file_changed; retry_after_review")
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_config(path: Path, key: str, text: str, *, apply: bool = False) -> dict:
    current = Config.load(path)
    values = config_values(current, reveal=True)
    if key not in values:
        raise RouteError("unknown_config_field")
    # Strings may be unquoted; numbers, booleans and null follow JSON syntax.
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    values[key] = value
    candidate = Config.from_values(values)
    result = {"field": key, "value": config_values(candidate)[key], "applied": apply,
              "existing_sessions_unchanged": True, "takes_effect": "next_launch"}
    if apply:
        private_json(path.expanduser().absolute(), config_values(candidate, reveal=True))
    return result
