"""Shared argument metadata; constructing it never loads config or credentials."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import NoReturn

from . import __version__


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # A mistakenly pasted credential must also stay out of parser diagnostics.
        message = re.sub(r"sk-ant-[A-Za-z0-9_-]+", "[redacted token]", message)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = PrivateArgumentParser(
        prog="vps-route",
        description="Run Claude Code through your own VPS in a separate local environment.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="action", required=True)
    initialize = subparsers.add_parser(
        "init", help="create or reuse private config and profile; never reset account state",
    )
    initialize.add_argument("--from-file", type=Path, help="import a validated connection config")
    initialize.add_argument("--keep-existing", action="store_true",
                            help="compatibility option; init always preserves existing state")
    initialize.add_argument("--dry-run", action="store_true", help="preview without writing or moving")
    initialize.add_argument("-y", "--yes", action="store_true",
                            help="compatibility option; init never resets existing state")
    uninstall = subparsers.add_parser(
        "uninstall", help="permanently remove local profiles, backups, logs, config and package",
    )
    uninstall.add_argument("--dry-run", action="store_true", help="preview; do not delete")
    uninstall.add_argument("-y", "--yes", action="store_true", help="skip deletion confirmation only")
    for category in ("profiles", "backups", "logs", "config", "package"):
        uninstall.add_argument(f"--keep-{category}", action="store_true",
                               help=f"preserve {category} during local cleanup")
    provision = subparsers.add_parser("setup-vps", help="deploy only the private VPS CONNECT service")
    provision.add_argument("--apply", action="store_true", help="install through verified SSH")
    inspect = subparsers.add_parser("doctor", help="offline checks; optional VPS route probe")
    inspect.add_argument("--connect", action="store_true")
    inspect.add_argument("--observe-hicc", action="store_true", help="passive same-user socket metadata")
    inspect.add_argument("--report", type=Path, help="write private sanitized JSON; no account contents")
    configuration = subparsers.add_parser("config", help="view, validate or explicitly edit route config")
    config_commands = configuration.add_subparsers(dest="config_action", required=True)
    show = config_commands.add_parser("show", help="redacted values by default")
    show.add_argument("--reveal", action="store_true", help="show local endpoint/path values")
    config_commands.add_parser("validate", help="offline syntax and policy check; no login or network")
    edit = config_commands.add_parser("set", help="preview one change; --apply writes atomically")
    edit.add_argument("key")
    edit.add_argument("value", help="string, JSON number, true/false, or null")
    edit.add_argument("--apply", action="store_true")
    subparsers.add_parser("status", help="list tracked tool-owned processes, not Claude conversation history")
    sessions = subparsers.add_parser("sessions", help="list or safely stop one tracked tool process")
    session_commands = sessions.add_subparsers(dest="sessions_action", required=True)
    session_commands.add_parser("list")
    stop = session_commands.add_parser("stop")
    stop.add_argument("run_id", help="exact run_id printed by status; never an arbitrary PID")
    stop.add_argument("--apply", action="store_true", help="request graceful stop; default previews")
    subparsers.add_parser("server-version", help="read VPS service protocol/version; never deploys")
    self_upgrade = subparsers.add_parser("self-update", help="manual launcher update from official releases")
    self_upgrade.add_argument("--to", metavar="X.Y.Z", help="select a formal published release")
    self_upgrade.add_argument("--rollback", action="store_true", help="restore previous launcher code only")
    self_upgrade.add_argument("--apply", action="store_true", help="update only while all sessions are idle")
    self_upgrade.add_argument("--check", action="store_true", help="check/preview only (the default)")
    logs = subparsers.add_parser("logs", help="read private operational logs; no accounts or prompts")
    logs.add_argument("--tail", type=int, default=20)
    shell = subparsers.add_parser("shell", help="open an interactive VPS SSH shell")
    shell.add_argument("--password", action="store_true",
                       help="prompt in the terminal for the VPS password; keep restricted keys unchanged")
    subparsers.add_parser("network-test", help="isolated HTTPS connectivity and direct-route blocking test")
    archive = subparsers.add_parser("legacy-archive", help="preview or archive legacy Claude/HICC state")
    archive.add_argument("--apply", action="store_true")
    archive.add_argument("--confirm-hicc-retired", action="store_true")
    activate = subparsers.add_parser("cutover-activate", help="preview or install an optional Claude wrapper")
    activate.add_argument("--archive", type=Path, required=True)
    activate.add_argument("--apply", action="store_true")
    activate.add_argument("--confirm-hicc-retired", action="store_true")
    auth = subparsers.add_parser("auth", help="authentication in the separate profile only")
    auth.add_argument("operation", choices=["login", "status", "import-token", "remove-token", "reset"])
    auth.add_argument("--text", action="store_true", help="forward --text for auth status")
    auth.add_argument("--apply", action="store_true",
                      help="apply auth reset or local token removal after confirmation")
    auth.add_argument("--dry-run", action="store_true",
                      help="preview reset or local token removal without changes")
    auth.add_argument("-y", "--yes", action="store_true",
                      help="skip confirmation for reset or local token removal")
    auth.add_argument("--replace", action="store_true",
                      help="import-token only: explicitly replace an existing local token")
    reset = subparsers.add_parser(
        "reset", help="preview full local-state reset; --apply archives and reinstalls via VPS",
    )
    reset.add_argument("--apply", action="store_true", help="archive old state and create fresh state")
    reset.add_argument("--no-install", action="store_true", help="leave fresh profile uninstalled")
    reset.add_argument("--dry-run", action="store_true", help="preview without moving/installing")
    reset.add_argument("-y", "--yes", action="store_true", help="skip confirmation, not safety checks")
    subparsers.add_parser("native-version", help="show the isolated official Claude CLI version")
    installer = subparsers.add_parser("install", help="official latest install into a separate home")
    installer.add_argument("--workspace", type=Path)
    run = subparsers.add_parser("run", help="launch the separately installed official CLI")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--safe-mode", action="store_true",
                     help="disable all customizations for troubleshooting")
    run.add_argument("--orca", action="store_true",
                     help="allow local Orca runtime control (including host-side operations)")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    upgrade = subparsers.add_parser("update", help="run official claude update through the same route")
    upgrade.add_argument("--workspace", type=Path)
    completion = subparsers.add_parser("completion", help="print Bash or Zsh Tab completion")
    completion.add_argument("shell", choices=["bash", "zsh"])
    return parser
