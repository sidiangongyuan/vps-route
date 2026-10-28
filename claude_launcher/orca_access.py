"""Opt-in local Orca control, separate from the child's Internet route."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .wire import RouteError


SANDBOX_ROOT = "/run/claude-launcher/orca"
CONTEXT_NAMES = {
    "ORCA_TERMINAL_HANDLE", "ORCA_PANE_KEY", "ORCA_TAB_ID", "ORCA_WORKTREE_ID",
    "ORCA_AGENT_LAUNCH", "ORCA_AGENT_LAUNCH_TOKEN", "ORCA_AGENT_PANE",
    "ORCA_RUNTIME_SOURCE_ID", "ORCA_RUNTIME_SOURCE_INCARNATION",
    "ORCA_RUNTIME_SOURCE_PROFILE_PATH",
}


@dataclass(frozen=True)
class OrcaAccess:
    files: Path
    socket: Path
    resources: tuple[Path, ...]
    context: dict[str, str]

    def mounts(self) -> list[str]:
        args = ["--ro-bind", str(self.files), SANDBOX_ROOT,
                "--ro-bind", str(self.socket), SANDBOX_ROOT + "/runtime.sock"]
        for source in self.resources:
            args.extend(["--ro-bind", str(source), str(source)])
        return args

    def environment(self) -> dict[str, str]:
        return {
            **self.context,
            "ORCA_USER_DATA_PATH": SANDBOX_ROOT,
            "ORCA_CLI_COMMAND": SANDBOX_ROOT + "/bin/orca-ide",
            "ORCA_CLI_BIN_DIR": SANDBOX_ROOT + "/bin",
        }


def _cli_layout() -> tuple[list[str], Path, tuple[Path, ...]]:
    """Resolve the selected CLI once; never fall through to a different build.

    Orca's Linux remote installer writes a small shell wrapper around a private
    Node executable and its bundled CLI. Only that literal layout is accepted;
    arbitrary shell expressions must not be evaluated while building mounts.
    """
    selected = os.environ.get("ORCA_CLI_COMMAND") or "orca-ide"
    found = shutil.which(selected)
    if found is None:
        raise RouteError("orca_cli_not_found")
    cli = Path(found).resolve()
    try:
        if not cli.is_file() or not os.access(cli, os.X_OK) or cli.stat().st_size > 16384:
            raise RouteError("unsupported_orca_cli_layout")
        text = cli.read_text()
        command = None
        data = os.environ.get("ORCA_USER_DATA_PATH")
        for line in text.splitlines():
            words = shlex.split(line, comments=True)
            if len(words) == 2 and words[0] == "export":
                if words[1].startswith("ORCA_USER_DATA_PATH="):
                    data = words[1].partition("=")[2]
            if words[:1] == ["exec"]:
                if command is not None or words[-1:] != ["$@"]:
                    raise RouteError("unsupported_orca_cli_layout")
                command = words[1:-1]
        if (not command or len(command) != 2 or not data
                or any(not Path(value).is_absolute() or "$" in value for value in command)):
            raise RouteError("unsupported_orca_cli_layout")
        node, script = [Path(value).resolve() for value in command]
        if (node.name != "node" or not node.is_file() or not os.access(node, os.X_OK)
                or script.name != "index.js" or script.parent.name != "cli"
                or not script.is_file()):
            raise RouteError("unsupported_orca_cli_layout")
        # Bind only the Node executable and CLI bundle, not the user's data or
        # other remote runtimes, agents, hook endpoints or relay sockets.
        return [str(node), str(script)], Path(data), (node, script.parent)
    except (OSError, UnicodeError, ValueError):
        raise RouteError("unsupported_orca_cli_layout") from None


def prepare_orca_access(destination: Path) -> OrcaAccess:
    """Build a private ephemeral bootstrap containing only the local Unix route.

    The auth token grants Orca runtime control, including host-side operations;
    this is an explicit opt-in, not an Internet/network permission for Claude.
    No agent account/profile database is mounted or migrated.
    """
    command, data, resources = _cli_layout()
    try:
        info = data.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise RouteError("unsafe_orca_runtime")
        bootstrap_path = data / "orca-runtime.json"
        info = bootstrap_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 65536):
            raise RouteError("unsafe_orca_runtime")
        bootstrap = json.loads(bootstrap_path.read_text())
        transports = bootstrap.get("transports", [])
        if not transports and isinstance(bootstrap.get("transport"), dict):
            transports = [bootstrap["transport"]]
        transport = next((item for item in transports
                          if isinstance(item, dict) and item.get("kind") == "unix"), None)
        if not transport or not isinstance(transport.get("endpoint"), str):
            raise RouteError("orca_local_unix_runtime_required")
        socket = Path(transport["endpoint"])
        if (not socket.is_absolute() or socket.is_symlink()
                or not socket.resolve().is_relative_to(data.resolve())):
            raise RouteError("unsafe_orca_runtime")
        info = socket.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RouteError("unsafe_orca_runtime")
        token = bootstrap.get("authToken")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise RouteError("unsafe_orca_runtime")
        # Discard websocket/remote transports. The isolated CLI must never use
        # loopback ports belonging to Claude or fall back to another endpoint.
        exposed = {key: bootstrap[key] for key in ("pid", "runtimeId", "startedAt")
                   if key in bootstrap}
        exposed.update(authToken=token, transports=[{
            "kind": "unix", "endpoint": SANDBOX_ROOT + "/runtime.sock",
        }])
        destination.mkdir(mode=0o700)
        bootstrap_copy = destination / "orca-runtime.json"
        bootstrap_copy.write_text(json.dumps(exposed) + "\n")
        bootstrap_copy.chmod(0o600)
        # Create the mountpoint before the directory becomes read-only.
        (destination / "runtime.sock").touch(mode=0o600)
        bin_dir = destination / "bin"
        bin_dir.mkdir(mode=0o700)
        wrapper = "#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n'
        for name in ("orca-ide", "orca"):
            path = bin_dir / name
            path.write_text(wrapper)
            path.chmod(0o700)
        context = {key: value for key, value in os.environ.items() if key in CONTEXT_NAMES
                   and len(value) <= 4096 and not any(c in value for c in "\r\n\0")}
        return OrcaAccess(destination, socket, resources, context)
    except (OSError, ValueError, TypeError, StopIteration):
        raise RouteError("orca_runtime_unavailable") from None
