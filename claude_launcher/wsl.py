"""WSL platform discovery and Windows mount policy, without account access."""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .wire import RouteError


@dataclass(frozen=True)
class Mount:
    target: Path
    root: Path
    device: str
    kind: str
    windows: bool


def version(proc: Path = Path("/proc")) -> int | None:
    try:
        release = (proc / "sys/kernel/osrelease").read_text().lower()
    except OSError:
        return None
    if "microsoft" not in release and "wsl" not in release:
        return None
    return 2 if "wsl2" in release or "microsoft-standard" in release else 1


def registrations(proc: Path = Path("/proc")) -> list[dict]:
    result = []
    for name in ("WSLInterop", "WSLInterop-late"):
        try:
            lines = (proc / "sys/fs/binfmt_misc" / name).read_text().splitlines()
        except OSError:
            continue
        flags = next((line.partition(":")[2].strip() for line in lines
                      if line.startswith("flags:")), "")
        result.append({"name": name, "enabled": "enabled" in lines,
                       "persistent_interpreter": "F" in flags})
    return result


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def mounts(proc: Path = Path("/proc")) -> list[Mount]:
    result = []
    try:
        lines = (proc / "self/mountinfo").read_text().splitlines()
    except OSError:
        raise RouteError("wsl_mount_inventory_unavailable") from None
    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            fields, filesystem = left.split(), right.split()
            kind = filesystem[0]
            windows = kind in {"drvfs", "virtiofs"} or (
                kind == "9p" and "aname=drvfs" in right)
            result.append(Mount(Path(_unescape(fields[4])), Path(_unescape(fields[3])),
                                fields[2], kind, windows))
        except (ValueError, IndexError):
            raise RouteError("wsl_mount_inventory_unavailable") from None
    if not result:
        raise RouteError("wsl_mount_inventory_unavailable")
    return result


def windows_mounts(inventory: list[Mount]) -> list[Mount]:
    return [mount for mount in inventory if mount.windows]


def runtime_paths(inventory: list[Mount]) -> list[Path]:
    """Include bind aliases of shared runtime trees using mount roots/devices."""
    paths = {Path("/run/WSL"), Path("/mnt/wsl"), Path("/mnt/wslg")}
    for path in tuple(paths):
        parents = [m for m in inventory if path.is_relative_to(m.target)]
        if not parents:
            continue
        origin = max(parents, key=lambda m: len(m.target.parts))
        source = origin.root / path.relative_to(origin.target)
        for mount in inventory:
            if mount.device == origin.device and source.is_relative_to(mount.root):
                paths.add(mount.target / source.relative_to(mount.root))
            elif mount.device == origin.device and mount.root.is_relative_to(source):
                paths.add(mount.target)
    return sorted(paths, key=lambda p: (len(p.parts), str(p)))


def network_mode(run=subprocess.run) -> str:
    try:
        result = run(["wslinfo", "--networking-mode"], capture_output=True,
                     text=True, timeout=3, check=False)
        mode = result.stdout.strip().lower()
        return mode if result.returncode == 0 and mode in {
            "nat", "mirrored", "virtioproxy", "none", "bridged"} else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def default_gateway(proc: Path = Path("/proc")) -> str:
    try:
        choices = []
        for line in (proc / "net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1] == "00000000" and int(fields[3], 16) & 3 == 3:
                address = ipaddress.IPv4Address(struct.pack("<I", int(fields[2], 16)))
                if not address.is_unspecified:
                    choices.append((int(fields[6]), str(address)))
        if choices:
            return min(choices)[1]
    except (OSError, ValueError, IndexError):
        pass
    raise RouteError("wsl_default_gateway_unavailable")


def proxy_host(hostname: str, proc: Path = Path("/proc")) -> str:
    if hostname != "windows-host":
        return hostname
    if version(proc) != 2:
        raise RouteError("windows_host_proxy_requires_wsl2")
    # In mirrored mode the default gateway is the LAN router, not Windows.
    mode = network_mode()
    if mode != "nat":
        raise RouteError("windows_host_proxy_requires_nat")
    return default_gateway(proc)


SENSITIVE = (
    "appdata", ".ssh", ".aws", ".azure", ".kube", ".docker", ".gnupg", ".config",
    ".claude*", ".codex*", "ntuser.dat*", ".git-credentials", ".netrc", ".npmrc",
    ".pypirc", ".agents", ".local", ".cache", ".env*", ".bash*", ".zsh*",
    "application data", "local settings", "start menu", "startup",
)


def containing_mount(path: Path, inventory: list[Mount]) -> Mount | None:
    # Case folding mirrors DrvFS lookup; resolve also handles Linux symlinks.
    name = str(path).casefold().rstrip("/") + "/"
    candidates = [m for m in windows_mounts(inventory)
                  if name.startswith(str(m.target).casefold().rstrip("/") + "/")]
    return max(candidates, key=lambda m: len(m.target.parts), default=None)


def validate_workspace(path: Path, inventory: list[Mount]) -> None:
    for candidate in (path.absolute(), path.resolve()):
        for runtime in runtime_paths(inventory):
            if candidate.is_relative_to(runtime) or runtime.is_relative_to(candidate):
                raise RouteError("wsl_runtime_directory_is_not_a_workspace")
        mount = containing_mount(candidate, inventory)
        if mount is None:
            # A broad parent of a Windows mount also exposes the whole drive.
            if any(m.target.is_relative_to(candidate) for m in windows_mounts(inventory)):
                raise RouteError("windows_broad_directory_is_not_a_workspace")
            continue
        relative = candidate.parts[len(mount.target.parts):]
        parts = tuple(part.casefold() for part in (*mount.root.parts[1:], *relative))
        if (not relative or not parts or parts[-1] in {"users", "documents and settings"}
                or any(part in {"users", "documents and settings"} and len(parts) <= i + 2
                       for i, part in enumerate(parts))):
            raise RouteError("windows_broad_directory_is_not_a_workspace")
        if any(re.search(r"~[0-9]+(?:\.|$)", part) for part in parts):
            raise RouteError("windows_private_directory_is_not_a_workspace")
        # Metadata-only checks recognize relocated profile roots without opening
        # registry hives, browser stores or credential files.
        if ((candidate / "NTUSER.DAT").exists() or (candidate / "AppData").is_dir()):
            raise RouteError("windows_broad_directory_is_not_a_workspace")
        if any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in SENSITIVE):
            raise RouteError("windows_private_directory_is_not_a_workspace")


def mask_paths(inventory: list[Mount]) -> list[Path]:
    candidates = {m.target for m in windows_mounts(inventory)} | set(runtime_paths(inventory))
    return [p for p in sorted(candidates, key=lambda p: (len(p.parts), str(p)))
            if not any(parent in candidates for parent in p.parents)]


def windows_environment(environ: dict[str, str]) -> dict[str, str]:
    return {key: environ[key] for key in (
        "WSL_INTEROP", "WSL_DISTRO_NAME", "DISPLAY", "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR", "PULSE_SERVER") if key in environ}


def windows_path(inventory: list[Mount], environ: dict[str, str]) -> list[str]:
    entries = [item for item in environ.get("PATH", "").split(":")
               if item.startswith("/") and containing_mount(Path(item), inventory)]
    if not entries:
        for mount in windows_mounts(inventory):
            system = mount.target / "Windows/System32"
            if system.is_dir():
                entries.extend([str(system), str(system / "WindowsPowerShell/v1.0")])
    return list(dict.fromkeys(entries))


def summary() -> dict:
    detected = version()
    if detected is None:
        return {"platform": "linux", "version": None}
    inventory = mounts()
    mode = network_mode()
    return {"platform": "wsl", "version": detected, "interop": registrations(),
            "network_mode": mode, "windows_mount_count": len(windows_mounts(inventory)),
            "proxy_hint": ("http://windows-host:PORT (allow Windows proxy access from WSL)"
                           if mode == "nat" else "http://127.0.0.1:PORT"
                           if mode == "mirrored" else "Check the WSL network mode first")}
