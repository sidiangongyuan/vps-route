"""Explicit route configuration; never read Claude account files."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .wire import RouteError


@dataclass(frozen=True)
class Config:
    ssh_host: str
    ssh_user: str
    ssh_port: int
    identity_file: Path
    known_hosts: Path
    remote_socket: str
    # Retained only for callers using the old positional constructor. Never enforced.
    expected_ip: str | None = None
    expected_country: str | None = None
    profile: str = "default"
    probe_url: str = "https://ipinfo.io/json"
    interval_seconds: float = 5.0
    timeout_seconds: float = 8.0
    ssh_http_proxy: str | None = None
    texlive_root: Path | None = None
    research_filesystem: bool = False
    timezone: str | None = None
    runtime_probe_interval_seconds: float = 30.0
    runtime_probe_failures: int = 2
    connectivity_urls: tuple[str, ...] = (
        "https://www.gstatic.com/generate_204",
        "https://www.cloudflare.com/cdn-cgi/trace",
    )
    orca_control: bool = False
    # Extra HOME entry names or fnmatch patterns hidden when research_filesystem is on.
    private_home_entries: tuple[str, ...] = ()

    @property
    def profile_home(self) -> Path:
        return Path.home() / ".local/share/claude-launcher/profiles" / self.profile

    @property
    def claude_executable(self) -> Path:
        return self.profile_home / ".local/bin/claude"

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            return cls.from_values(json.loads(path.read_text()))
        except (OSError, ValueError):
            raise RouteError("invalid_or_unreadable_config") from None

    @classmethod
    def from_values(cls, values: dict) -> Config:
        """Validate file and CLI input with exactly the same policy."""
        try:
            if not isinstance(values, dict):
                raise ValueError
            values = dict(values)
            # Accept old config files while ignoring the retired browser worker.
            values.pop("browser_agent", None)
            values.pop("expected_ip", None)
            values.pop("expected_country", None)
            if "connectivity_urls" in values:
                urls = values["connectivity_urls"]
                if not isinstance(urls, (list, tuple)) or not 1 <= len(urls) <= 4:
                    raise ValueError
                values["connectivity_urls"] = tuple(urls)
            if "private_home_entries" in values:
                entries = values["private_home_entries"]
                if not isinstance(entries, (list, tuple)) or len(entries) > 64:
                    raise ValueError
                for entry in entries:
                    # One name directly under HOME; never a path that could reach elsewhere.
                    if (not isinstance(entry, str) or not 1 <= len(entry) <= 128
                            or entry in {".", ".."} or "/" in entry
                            or any(ord(char) < 32 for char in entry)):
                        raise ValueError
                values["private_home_entries"] = tuple(entries)
            values["identity_file"] = Path(values["identity_file"]).expanduser().absolute()
            values["known_hosts"] = Path(values["known_hosts"]).expanduser().absolute()
            if values.get("texlive_root") is not None:
                values["texlive_root"] = Path(values["texlive_root"]).expanduser().absolute()
            config = cls(**values)
            for name in ("ssh_host", "ssh_user", "remote_socket", "profile", "probe_url"):
                if not isinstance(getattr(config, name), str):
                    raise ValueError
            if not isinstance(config.research_filesystem, bool):
                raise ValueError
            if not isinstance(config.orca_control, bool):
                raise ValueError
            if config.timezone is not None:
                if not isinstance(config.timezone, str):
                    raise ValueError
                ZoneInfo(config.timezone)
            # Numeric bootstrap address avoids local DNS for the SSH destination.
            ipaddress.ip_address(config.ssh_host)
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}", config.ssh_user):
                raise ValueError
            if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", config.remote_socket):
                raise ValueError
            if ".." in Path(config.remote_socket).parts:
                raise ValueError
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", config.profile):
                raise ValueError
            if (isinstance(config.ssh_port, bool) or not isinstance(config.ssh_port, int)
                    or not 1 <= config.ssh_port <= 65535):
                raise ValueError
            for value, maximum in ((config.interval_seconds, 60), (config.timeout_seconds, 30)):
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not 1 <= value <= maximum):
                    raise ValueError
            if (isinstance(config.runtime_probe_interval_seconds, bool)
                    or not isinstance(config.runtime_probe_interval_seconds, (int, float))
                    or not 5 <= config.runtime_probe_interval_seconds <= 300
                    or isinstance(config.runtime_probe_failures, bool)
                    or not isinstance(config.runtime_probe_failures, int)
                    or not 1 <= config.runtime_probe_failures <= 10):
                raise ValueError
            if config.ssh_http_proxy is not None:
                if not isinstance(config.ssh_http_proxy, str):
                    raise ValueError
                bootstrap = urlsplit(config.ssh_http_proxy)
                if (
                    bootstrap.scheme != "http" or bootstrap.hostname != "127.0.0.1"
                    or bootstrap.port is None or not 1 <= bootstrap.port <= 65535
                    or bootstrap.username or bootstrap.password
                    or bootstrap.path not in ("", "/") or bootstrap.query or bootstrap.fragment
                ):
                    raise ValueError
            for endpoint in (config.probe_url, *config.connectivity_urls):
                if not isinstance(endpoint, str) or any(ch.isspace() for ch in endpoint):
                    raise ValueError
                url = urlsplit(endpoint)
                if (
                    url.scheme != "https" or not url.hostname or url.port not in (None, 443)
                    or url.username or url.password or url.fragment
                ):
                    raise ValueError
            return config
        except (OSError, ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
            raise RouteError("invalid_or_unreadable_config") from None
