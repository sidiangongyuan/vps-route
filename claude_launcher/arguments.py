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
        description="Run Claude Code in an isolated local environment that reaches the "
                    "internet only through your own VPS.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path,
                        help="use this config file instead of ~/.config/vps-route/config.json")
    subparsers = parser.add_subparsers(dest="action", required=True)
    initialize = subparsers.add_parser(
        "init", help="create or reuse the config and separate profile; never resets login",
    )
    initialize.add_argument("--from-file", type=Path,
                            help="import connection settings from a JSON file")
    initialize.add_argument("--keep-existing", action="store_true",
                            help="accepted for compatibility; init always keeps existing state")
    initialize.add_argument("--dry-run", action="store_true",
                            help="show what would be created without writing anything")
    initialize.add_argument("-y", "--yes", action="store_true",
                            help="accepted for compatibility; init never resets existing state")
    uninstall = subparsers.add_parser(
        "uninstall", help="permanently remove local profiles, backups, logs, config and package",
    )
    uninstall.add_argument("--dry-run", action="store_true",
                           help="list what would be deleted without deleting")
    uninstall.add_argument("-y", "--yes", action="store_true", help="skip the deletion prompt")
    for category in ("profiles", "backups", "logs", "config", "package"):
        uninstall.add_argument(f"--keep-{category}", action="store_true", help=f"keep {category}")
    provision = subparsers.add_parser("setup-vps",
                                      help="deploy the private connection service on your VPS")
    provision.add_argument("--apply", action="store_true",
                           help="deploy over verified SSH; without it, only show the plan")
    inspect = subparsers.add_parser("doctor",
                                    help="check local requirements; --connect also tests the VPS")
    inspect.add_argument("--connect", action="store_true",
                         help="also test SSH and HTTPS through your VPS")
    inspect.add_argument("--report", type=Path,
                         help="write a sanitized JSON report for troubleshooting; no account data")
    configuration = subparsers.add_parser("config", help="show, validate or change the config")
    config_commands = configuration.add_subparsers(dest="config_action", required=True)
    show = config_commands.add_parser("show", help="print the config with private values redacted")
    show.add_argument("--reveal", action="store_true", help="include local endpoints and paths")
    config_commands.add_parser("validate", help="check the config offline")
    edit = config_commands.add_parser("set", help="preview a change to one key; --apply saves it")
    edit.add_argument("key", help="config key")
    edit.add_argument("value", help="string, JSON number, true/false or null")
    edit.add_argument("--apply", action="store_true", help="save the change")
    subparsers.add_parser("status", help="list running vps-route processes, not conversations")
    sessions = subparsers.add_parser("sessions", help="list tracked processes or stop one")
    session_commands = sessions.add_subparsers(dest="sessions_action", required=True)
    session_commands.add_parser("list", help="list tracked processes")
    stop = session_commands.add_parser("stop", help="stop one tracked process")
    stop.add_argument("run_id", help="run_id shown by status; never an arbitrary PID")
    stop.add_argument("--apply", action="store_true",
                      help="send a graceful stop request; without it, only preview")
    subparsers.add_parser("server-version", help="show the VPS service version without deploying")
    self_upgrade = subparsers.add_parser("self-update",
                                         help="update vps-route from its published GitHub releases")
    self_upgrade.add_argument("--to", metavar="X.Y.Z", help="select this release instead of the latest")
    self_upgrade.add_argument("--rollback", action="store_true",
                              help="restore the previous vps-route code")
    self_upgrade.add_argument("--apply", action="store_true",
                              help="install the update; every session must be idle")
    self_upgrade.add_argument("--check", action="store_true",
                              help="only check and preview (the default)")
    logs = subparsers.add_parser("logs", help="show recent connection logs; no prompts or credentials")
    logs.add_argument("--tail", type=int, default=20, help="number of entries (default 20)")
    shell = subparsers.add_parser("shell", help="open an interactive SSH shell on your VPS")
    shell.add_argument("--password", action="store_true",
                       help="log in with a password typed in the terminal; key settings stay unchanged")
    subparsers.add_parser("network-test",
                          help="confirm HTTPS works and direct connections are blocked in the sandbox")
    auth = subparsers.add_parser("auth", help="log in, check status or manage a token in the profile")
    auth.add_argument("operation", choices=["login", "status", "import-token", "remove-token", "reset"])
    auth.add_argument("--text", action="store_true", help="plain-text output for auth status")
    auth.add_argument("--apply", action="store_true",
                      help="carry out reset or local token removal after confirmation")
    auth.add_argument("--dry-run", action="store_true",
                      help="preview reset or local token removal without changes")
    auth.add_argument("-y", "--yes", action="store_true",
                      help="skip confirmation for reset or local token removal")
    auth.add_argument("--replace", action="store_true",
                      help="import-token only: replace an existing local token")
    reset = subparsers.add_parser(
        "reset", help="preview a profile reset; --apply archives it and reinstalls through the VPS",
    )
    reset.add_argument("--apply", action="store_true",
                       help="archive the tool profile and create a fresh one; ~/.claude is untouched")
    reset.add_argument("--no-install", action="store_true",
                       help="leave the fresh profile without Claude Code")
    reset.add_argument("--dry-run", action="store_true", help="preview without changes")
    reset.add_argument("-y", "--yes", action="store_true",
                       help="skip confirmation; safety checks still run")
    subparsers.add_parser("native-version", help="show the installed Claude Code version")
    installer = subparsers.add_parser("install",
                                      help="install the latest official Claude Code into the profile")
    installer.add_argument("--workspace", type=Path, help="working directory for the installer")
    run = subparsers.add_parser("run", help="start Claude Code in the isolated environment")
    run.add_argument("--workspace", type=Path, default=Path.cwd(),
                     help="project directory (default: current directory)")
    run.add_argument("--safe-mode", action="store_true",
                     help="disable all customizations, for troubleshooting")
    run.add_argument("--windows", action="store_true",
                     help="allow Windows host programs and WSLg during daily run")
    run.add_argument("--orca", action="store_true",
                     help="allow local Orca runtime control, including host-side operations")
    run.add_argument("arguments", nargs=argparse.REMAINDER,
                     help="arguments for Claude Code, after --")
    upgrade = subparsers.add_parser("update", help="update Claude Code through the same VPS route")
    upgrade.add_argument("--workspace", type=Path, help="working directory for the update")
    completion = subparsers.add_parser("completion", help="print a Bash or Zsh completion script")
    completion.add_argument("shell", choices=["bash", "zsh"])
    return parser
