"""Explicit route configuration; never read Claude or HICC account files."""

from __future__ import annotations

import ipaddress
import json
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
    expected_ip: str
    expected_country: str
    profile: str = "default"
    probe_url: str = "https://ipinfo.io/json"
    interval_seconds: float = 5.0
    timeout_seconds: float = 8.0
    ssh_http_proxy: str | None = None
    texlive_root: Path | None = None
    research_filesystem: bool = False
    timezone: str | None = None

    @property
    def profile_home(self) -> Path:
        return Path.home() / ".local/share/claude-launcher/profiles" / self.profile

    @property
    def claude_executable(self) -> Path:
        return self.profile_home / ".local/bin/claude"

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            values = json.loads(path.read_text())
            if not isinstance(values, dict):
                raise ValueError
            # Accept old config files while ignoring the retired browser worker.
            values.pop("browser_agent", None)
            values["identity_file"] = Path(values["identity_file"]).expanduser().absolute()
            values["known_hosts"] = Path(values["known_hosts"]).expanduser().absolute()
            if values.get("texlive_root") is not None:
                values["texlive_root"] = Path(values["texlive_root"]).expanduser().absolute()
            config = cls(**values)
            if not isinstance(config.research_filesystem, bool):
                raise ValueError
            if config.timezone is not None:
                if not isinstance(config.timezone, str):
                    raise ValueError
                ZoneInfo(config.timezone)
            # Numeric bootstrap address avoids local DNS for the SSH destination.
            ipaddress.ip_address(config.ssh_host)
            expected = ipaddress.ip_address(config.expected_ip)
            if not expected.is_global:
                raise ValueError
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}", config.ssh_user):
                raise ValueError
            if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", config.remote_socket):
                raise ValueError
            if ".." in Path(config.remote_socket).parts:
                raise ValueError
            if not re.fullmatch(r"[A-Z]{2}", config.expected_country):
                raise ValueError
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", config.profile):
                raise ValueError
            if not 1 <= config.ssh_port <= 65535:
                raise ValueError
            if not 1 <= config.interval_seconds <= 60 or not 1 <= config.timeout_seconds <= 30:
                raise ValueError
            if config.ssh_http_proxy is not None:
                bootstrap = urlsplit(config.ssh_http_proxy)
                if (
                    bootstrap.scheme != "http" or bootstrap.hostname != "127.0.0.1"
                    or bootstrap.port is None or not 1 <= bootstrap.port <= 65535
                    or bootstrap.username or bootstrap.password
                    or bootstrap.path not in ("", "/") or bootstrap.query or bootstrap.fragment
                ):
                    raise ValueError
            url = urlsplit(config.probe_url)
            if (
                url.scheme != "https" or not url.hostname or url.port not in (None, 443)
                or url.username or url.password or url.fragment
            ):
                raise ValueError
            return config
        except (OSError, ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
            raise RouteError("invalid_or_unreadable_config") from None

    def judge(self, reading: dict) -> str | None:
        try:
            observed = str(ipaddress.ip_address(reading["ip"]))
            expected = str(ipaddress.ip_address(self.expected_ip))
        except (KeyError, ValueError, TypeError):
            return "egress_ip_unknown"
        if observed != expected:
            return "egress_ip_changed"
        country = reading.get("country")
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
            return "egress_country_unknown"
        if country != self.expected_country:
            return "egress_country_changed"
        return None
