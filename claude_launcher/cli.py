"""CLI entry points. Live account stores are never used by this launcher."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .audit import AuditLog, log_tail
from .config import Config
from .customizations import skills_arguments
from .profile_reset import full_reset_inventory, reset_local_state, reset_profile
from .onboarding import complete_authenticated_onboarding
from .setup import default_config_path, initialize_config
from .sessions import SessionLease, list_sessions, maintenance_barrier, stop_session
from .runner import (
    ENTRY_DIR, RuntimeConnectivityMonitor, SSHTunnel, connectivity_check, receipt, run_supervised,
    sandbox_command, ssh_arguments,
)
from .wire import RouteError


def namespace_available() -> bool:
    if not shutil.which("bwrap"):
        return False
    code = (
        "from pathlib import Path; "
        "rows=Path('/proc/net/dev').read_text().splitlines()[2:]; "
        "assert all(r.split(':')[0].strip()=='lo' for r in rows); "
        "assert len(Path('/proc/net/route').read_text().splitlines())==1"
    )
    try:
        result = subprocess.run(
            ["bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/",
             "--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/python3", "-c", code],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


MACOS = sys.platform == "darwin"
# Linux-only features; reported instead of attempted on macOS.
MACOS_UNSUPPORTED = {"reset", "status", "sessions", "shell", "network-test"}


def doctor(config: Config | None, connect: bool, report: Path | None = None) -> int:
    checks = {
        "ssh_installed": shutil.which("ssh") is not None,
        "curl_installed": shutil.which("curl") is not None,
        "bash_installed": shutil.which("bash") is not None,
    }
    if MACOS:
        from .macos import check_failures, isolation_report

        failures = check_failures(isolation_report(
            config.private_home_entries if config else (),
            (config.identity_file, config.known_hosts) if config else (),
        ))
        checks.update(platform="macos", seatbelt_network_isolation=not failures,
                      seatbelt_check_failures=failures,
                      home_access="profile_and_workspace_only", host_mach_ipc=False,
                      macos_configuration_supported=(config is None or (
                          not config.research_filesystem and config.texlive_root is None)))
        isolation = ("seatbelt_network_isolation", "macos_configuration_supported")
    else:
        checks.update(system_python_compatible=system_python_compatible(),
                      private_network_namespace=namespace_available())
        isolation = ("private_network_namespace", "system_python_compatible")
    checks.update({
        "config_loaded": config is not None,
        "live_account_files_read": False,
        "network_probe_executed": connect,
        "launcher_version": __version__,
    })
    if config is not None:
        executable = config.claude_executable
        resolved = executable.resolve()
        native = executable.is_file() and resolved.is_relative_to(config.profile_home.resolve())
        settings_path = config.profile_home / ".claude/settings.json"
        try:
            settings = json.loads(settings_path.read_text())
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        update_env = settings.get("env", {})
        if not isinstance(update_env, dict):
            update_env = {}
        checks.update({
            "ssh_identity_present": config.identity_file.is_file(),
            "known_hosts_present": config.known_hosts.is_file(),
            "separate_claude_install_present": executable.is_file(),
            "native_launcher_inside_profile": native,
            "isolated_claude_version": (
                resolved.name if native and re.fullmatch(r"\d+\.\d+\.\d+", resolved.name)
                else None
            ),
            "stable_update_channel_configured": settings.get("autoUpdatesChannel") == "stable",
            "auto_update_channel": settings.get("autoUpdatesChannel", "latest"),
            "auto_updates_not_disabled": not any(
                str(update_env.get(name, "")) == "1"
                for name in ("DISABLE_AUTOUPDATER", "DISABLE_UPDATES")
            ),
            "isolated_timezone_source": "process_override" if config.timezone else "host_system_default",
            "isolated_timezone": config.timezone,
            **({} if MACOS else {"research_filesystem_enabled": config.research_filesystem}),
            "runtime_connectivity_monitor_enabled": True,
            "exit_ip_pinning_enabled": False,
            "runtime_probe_interval_seconds": config.runtime_probe_interval_seconds,
            "runtime_probe_failures": config.runtime_probe_failures,
        })
    ready = all(checks[name] for name in ("ssh_installed", *isolation, "curl_installed",
                                        "bash_installed"))
    if connect:
        if config is None:
            raise RouteError("connect_requires_config")
        from .diagnostics import connect_diagnostics

        if ready:
            checks.update(connect_diagnostics(config))
        else:
            checks.update(connectivity_ok=False, network_probe_executed=False,
                          route_result="local_dependencies_unavailable")
    if report is not None:
        from .management import private_json

        private_json(report.expanduser().absolute(), checks)
    print(json.dumps(checks, indent=2))
    return 0 if ready and (not connect or checks["connectivity_ok"]) else 69


def system_python_compatible() -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-c", "import sys; sys.exit(sys.version_info < (3, 10))"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch(config: Config, workspace: Path, arguments: list[str], install: bool = False,
           login: bool = False, safe_mode: bool = False,
           verify_after: bool = False, skills_mode: bool = False) -> int:
    if install:
        from .install import installation_present

        if installation_present(config):
            print("vps-route: Claude is already installed; kept unchanged. "
                  "Use vps-route update to upgrade.")
            return 0
    operation = "install" if install else "run"
    if arguments[:1] == ["auth"] and arguments[1:2] in (["login"], ["status"]):
        operation = "auth_" + arguments[1]
    elif arguments == ["update"]:
        operation = "update"
    elif arguments == ["--version"]:
        operation = "version"
    audit = AuditLog(operation, config.timezone)
    audit.write("start")
    previous_term = signal.getsignal(signal.SIGTERM)

    def cancelled(_number: int, _frame: object) -> None:
        # Also clean up SSH when stop is requested during preflight/post-update,
        # before/after run_supervised installs its own terminal signal handlers.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancelled)
    try:
        with maintenance_barrier(exclusive=install), \
                SessionLease(audit.run_id, config.profile, operation):
            return _launch(config, workspace, arguments, audit, install=install, login=login,
                           safe_mode=safe_mode, verify_after=verify_after, skills_mode=skills_mode)
    except RouteError as exc:
        audit.exit_code, audit.reason = 69, str(exc)
        raise
    except OSError:
        audit.exit_code, audit.reason = 70, "operation_failed"
        raise
    except KeyboardInterrupt:
        audit.exit_code, audit.reason = 130, "interrupted"
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        try:
            audit.write("exit")
        except (OSError, RouteError):
            # Never hide a route failure or change the completed child's result.
            print("vps-route: usage_log_write_failed", file=sys.stderr)


def _isolated(config: Config, socket_path: Path, profile_home: Path, workspace: Path,
              command: list[str], **options: object):
    """Linux returns a ready namespace command; macOS also owns a loopback relay."""
    if MACOS:
        from .macos import seatbelt_session

        return seatbelt_session(socket_path, profile_home, workspace, command,
                                private_home_entries=config.private_home_entries,
                                protected_files=(config.identity_file, config.known_hosts),
                                timezone=config.timezone)
    return contextlib.nullcontext(
        sandbox_command(socket_path, profile_home, workspace, command, **options)
    )


def _launch(config: Config, workspace: Path, arguments: list[str], audit: AuditLog,
            install: bool = False, login: bool = False, safe_mode: bool = False,
            verify_after: bool = False, skills_mode: bool = False) -> int:
    if install:
        from .install import installation_present

        if installation_present(config):
            print("vps-route: Claude is already installed; kept unchanged. "
                  "Use vps-route update to upgrade.")
            audit.exit_code, audit.reason = 0, "already_installed"
            return 0
    if MACOS:
        from .macos import seatbelt_present

        if config.texlive_root is not None:
            raise RouteError("texlive_root_unsupported_on_macos")
        if config.research_filesystem:
            raise RouteError("research_filesystem_unsupported_on_macos")
        if not seatbelt_present():
            raise RouteError("seatbelt_unavailable")
    else:
        if not namespace_available():
            raise RouteError("network_isolation_unavailable")
        if not system_python_compatible():
            raise RouteError("system_python_3_10_required")
    profile_home = config.profile_home
    executable = config.claude_executable
    profile_base = Path.home() / ".local/share/claude-launcher/profiles"
    if not profile_home.resolve().is_relative_to(profile_base.absolute()):
        raise RouteError("profile_location_must_not_redirect_outside_tool_storage")
    if install:
        profile_home.mkdir(parents=True, mode=0o700, exist_ok=True)
    elif not executable.is_file() or not os.access(executable, os.X_OK):
        raise RouteError("install_official_claude_in_separate_profile_first")
    if not install and not executable.resolve().is_relative_to(profile_home.resolve()):
        raise RouteError("claude_executable_must_stay_inside_separate_profile")
    if profile_home.resolve() == Path.home().resolve():
        raise RouteError("profile_must_not_be_live_home")
    code, reason = 69, "route_not_started"
    keep_session = audit.operation == "run" and not install and not verify_after and not login
    with tempfile.TemporaryDirectory(prefix="claude-route-") as directory:
        route_directory = Path(directory) / "route"
        route_directory.mkdir(mode=0o700)
        tunnel = SSHTunnel(config, route_directory / "proxy.sock")
        try:
            tunnel.start()
            reason = connectivity_check(config, tunnel.socket_path, audit.connection_check)
            if reason:
                raise RouteError(reason)
            audit.write("preflight_passed")
            if (safe_mode or skills_mode) and complete_authenticated_onboarding(profile_home, executable):
                print("vps-route: existing login kept; first-run UI setup repaired", flush=True)
            if login:
                print("Open the official login URL shown below on a browser using the VPS exit."
                      " Paste the one-time code back into this terminal when prompted.",
                      flush=True)
            if not install:
                executable_args = [str(executable), *(["--safe-mode"] if safe_mode else []),
                                   *(skills_arguments(arguments) if skills_mode else arguments)]
            elif MACOS:
                from .macos import INSTALL_COMMAND

                executable_args = INSTALL_COMMAND
            else:
                executable_args = ["/usr/bin/python3", str(ENTRY_DIR / "install_entry.py")]
            with _isolated(
                config, tunnel.socket_path, profile_home, workspace,
                executable_args,
                login=login, project_instructions=skills_mode,
                texlive_root=config.texlive_root,
                research_filesystem=config.research_filesystem,
                private_home_entries=config.private_home_entries,
                timezone=config.timezone,
                reconnectable=keep_session,
            ) as (command, environment):
                code, reason = run_supervised(
                    command, environment,
                    None if keep_session else RuntimeConnectivityMonitor(
                        config, tunnel.socket_path, audit.connection_check,
                    ),
                    tunnel.close, tunnel.alive,
                    config.runtime_probe_interval_seconds,
                    reconnect=tunnel.reconnect if keep_session else None,
                    connection_event=audit.connection_state if keep_session else None,
                )
            if (login and code == 0 and MACOS
                    and not (profile_home / ".claude/.credentials.json").is_file()):
                # The Keychain tool is blocked; a login stored elsewhere is not isolated.
                code, reason = 69, "isolated_login_credentials_not_in_profile"
            if login and code == 0:
                complete_authenticated_onboarding(profile_home, executable)
            if code == 0 and verify_after:
                if (not executable.is_file()
                        or not executable.resolve().is_relative_to(profile_home.resolve())):
                    code, reason = 69, "native_launcher_missing_after_update"
                else:
                    # The first supervised child closes its owned SSH route.
                    # Use a new verified route for the post-update binary check.
                    verify_tunnel = SSHTunnel(config, Path(directory) / "verify.sock")
                    try:
                        verify_tunnel.start()
                        if connectivity_check(config, verify_tunnel.socket_path, audit.connection_check):
                            code, reason = 69, "update_verification_route_failed"
                        else:
                            with _isolated(
                                config, verify_tunnel.socket_path, profile_home, workspace,
                                [str(executable), "--version"],
                                timezone=config.timezone,
                            ) as (verify_command, verify_environment):
                                verify_code, _ = run_supervised(
                                    verify_command, verify_environment,
                                    RuntimeConnectivityMonitor(config, verify_tunnel.socket_path,
                                                               audit.connection_check),
                                    verify_tunnel.close, verify_tunnel.alive,
                                    config.runtime_probe_interval_seconds,
                                )
                            if verify_code:
                                code, reason = 69, "updated_binary_unusable"
                    except RouteError:
                        code, reason = 69, "update_verification_route_failed"
                    finally:
                        verify_tunnel.dispose()
        except RouteError as exc:
            reason = str(exc)
            raise
        finally:
            tunnel.dispose()
            audit.exit_code, audit.reason = code, reason
            receipt(profile_home, code, reason)
    print(f"\nvps-route: {reason}; exit={code}")
    return code


def network_test(config: Config) -> int:
    """Exercise the actual isolated route without installing or starting an AI CLI."""
    if not namespace_available():
        raise RouteError("network_isolation_unavailable")
    with tempfile.TemporaryDirectory(prefix="claude-network-test-") as temporary:
        root = Path(temporary)
        profile = root / "profile"
        workspace = root / "workspace"
        profile.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        tunnel = SSHTunnel(config, root / "proxy.sock")
        try:
            tunnel.start()
            reason = connectivity_check(config, tunnel.socket_path)
            if reason:
                raise RouteError(reason)
            command, environment = sandbox_command(
                tunnel.socket_path, profile, workspace,
                ["/usr/bin/python3", str(ENTRY_DIR / "network_test_entry.py"),
                 *config.connectivity_urls],
                research_filesystem=config.research_filesystem,
                private_home_entries=config.private_home_entries,
                timezone=config.timezone,
            )
            code, reason = run_supervised(
                command, environment, None,
                tunnel.close, tunnel.alive, config.interval_seconds,
            )
        finally:
            tunnel.dispose()
    print(f"network-test: {reason}; exit={code}")
    return code


def private_workspace(config: Config) -> Path:
    """Keep auth and maintenance commands away from projects and the live HOME."""
    workspace = config.profile_home / "auth-workspace"
    workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
    return workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    inspect.add_argument("--report", type=Path, help="write private sanitized JSON; no account contents")
    subparsers.add_parser("macos-check",
                          help="macOS: secret-free sandbox self-test report for troubleshooting")
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
    auth = subparsers.add_parser("auth", help="authentication in the separate profile only")
    auth.add_argument("operation", choices=["login", "status", "reset"])
    auth.add_argument("--text", action="store_true", help="forward --text for auth status")
    auth.add_argument("--apply", action="store_true", help="auth reset only: archive after confirmation")
    auth.add_argument("--dry-run", action="store_true", help="auth reset only: preview without changes")
    auth.add_argument("-y", "--yes", action="store_true", help="auth reset only: skip confirmation")
    reset = subparsers.add_parser(
        "reset", help="preview tool profile reset; --apply archives it and reinstalls via VPS",
    )
    reset.add_argument("--apply", action="store_true",
                       help="archive the tool profile and create a fresh one; ~/.claude is untouched")
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
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    upgrade = subparsers.add_parser("update", help="run official claude update through the same route")
    upgrade.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    try:
        if MACOS and (args.action in MACOS_UNSUPPORTED
                      or (args.action == "auth" and args.operation == "reset")):
            raise RouteError("command_unsupported_on_macos")
        if args.action == "init":
            path = args.config or default_config_path()
            config = initialize_config(path, args.from_file, keep_existing=args.keep_existing,
                                       dry_run=args.dry_run, yes=args.yes)
            if args.dry_run:
                print("Preview only; no configuration or account state changed.")
                return 0
            print(f"Private config ready (existing state preserved): {path}")
            from .install import installation_present

            if installation_present(config):
                print("Claude already installed. Next: vps-route auth status --text, "
                      "then vps-route run")
            else:
                print("Claude not installed. Next: vps-route setup-vps --apply "
                      "(only for a new VPS service), then vps-route install")
            return 0
        if args.action == "uninstall":
            from .lifecycle import uninstall_inventory, uninstall_local

            path = args.config or default_config_path()
            options = {f"keep_{name}": getattr(args, f"keep_{name}")
                       for name in ("profiles", "backups", "logs", "config", "package")}
            if args.dry_run:
                print(json.dumps(uninstall_inventory(path, **options), indent=2))
            else:
                print(json.dumps(uninstall_local(path, yes=args.yes, **options), indent=2))
            return 0
        if args.action == "logs":
            print(log_tail(args.tail))
            return 0
        if args.action in {"status", "sessions"}:
            if args.action == "sessions" and args.sessions_action == "stop":
                if args.apply:
                    result = stop_session(args.run_id)
                else:
                    matches = [item for item in list_sessions() if item["run_id"] == args.run_id]
                    if not matches:
                        raise RouteError("session_not_found")
                    result = {"session": matches[0], "apply_required": True}
            else:
                result = {"sessions": list_sessions(),
                          "scope": "processes_started_after_tracking_was_enabled"}
            print(json.dumps(result, indent=2))
            return 0
        if args.action == "self-update":
            from .updates import self_update

            if args.check and args.apply:
                raise RouteError("check_and_apply_are_mutually_exclusive")
            print(json.dumps(self_update(version=args.to, apply=args.apply,
                                         rollback=args.rollback), indent=2))
            return 0
        path = args.config or default_config_path()
        config = Config.load(path) if args.config or path.exists() else None
        if args.action == "doctor":
            return doctor(config, args.connect, args.report)
        if args.action == "macos-check":
            if not MACOS:
                raise RouteError("macos_check_runs_only_on_macos")
            from .macos_check import report

            result = report(config)
            print(json.dumps(result, indent=2))
            return 0 if result["ready"] else 69
        if config is None:
            raise RouteError("config_required; run_vps-route_init")
        if args.action == "install":
            from .install import installation_present

            if installation_present(config):
                # Do not even create the maintenance workspace for a no-op install.
                return launch(config, config.profile_home, [], install=True, verify_after=True)
        if args.action == "config":
            from .management import config_values, set_config

            if args.config_action == "show":
                result = config_values(config, reveal=args.reveal)
            elif args.config_action == "validate":
                result = {"valid": True, "account_files_read": False, "network_probe_executed": False}
            else:
                result = set_config(path, args.key, args.value, apply=args.apply)
            print(json.dumps(result, indent=2))
            return 0
        if args.action == "server-version":
            from .diagnostics import inspect_server

            result = inspect_server(config)
            print(json.dumps(result, indent=2))
            return 0 if result["compatible"] is True else 69
        if args.action == "setup-vps":
            if not args.apply:
                print(json.dumps({"apply_required": True, "service": "vps-route-connect.service",
                                  "verified_ssh_required": True,
                                  "account_or_vpn_configuration_changed": False}, indent=2))
                return 0
            from .provision import deploy_server
            return deploy_server(config)
        if args.action == "shell":
            if not sys.stdin.isatty():
                raise RouteError("shell_requires_an_interactive_terminal")
            command = ssh_arguments(config)
            command[command.index("-T")] = "-tt"
            if args.password:
                command[command.index("BatchMode=yes")] = "BatchMode=no"
                command.extend(["-o", "PubkeyAuthentication=no"])
            return subprocess.call([*command, config.ssh_host])
        if args.action == "reset":
            if args.apply and args.dry_run:
                raise RouteError("dry_run_and_apply_are_mutually_exclusive")
            if not args.apply:
                print(json.dumps(full_reset_inventory(config), indent=2))
                return 0
            from .lifecycle import confirm_operation

            print(json.dumps(full_reset_inventory(config), indent=2))
            confirm_operation("Archive this tool profile (login and history) and create a fresh one?",
                              yes=args.yes)
            with maintenance_barrier(exclusive=True):
                result = reset_local_state(config)
            print(json.dumps(result, indent=2), flush=True)
            if args.no_install:
                print("fresh state ready; run vps-route install before login")
                return 0
            code = launch(config, private_workspace(config), [], install=True, verify_after=True)
            if code:
                print("reset completed; install failed; backups kept; retry: vps-route install")
            else:
                print("fresh state and CLI ready; not logged in; next: vps-route auth login")
            return code
        if args.action == "auth" and args.operation == "reset":
            from .profile_reset import profile_reset_inventory
            from .lifecycle import confirm_operation

            if args.text or (args.apply and args.dry_run):
                raise RouteError("invalid_auth_reset_options")
            inventory = profile_reset_inventory(config)
            print(json.dumps(inventory, indent=2))
            if not args.apply:
                return 0
            if inventory["profile_process_running"]:
                raise RouteError("profile_process_running")
            confirm_operation("Archive this isolated profile and create a blank replacement?",
                              yes=args.yes)
            with maintenance_barrier(exclusive=True):
                archive = reset_profile(config)
            if archive is None:
                print("fresh profile created; no previous tool profile was present")
            else:
                print(f"fresh profile created; previous tool profile archived at {archive}")
            print("reinstall the isolated Claude CLI before running auth login")
            return 0
        if args.action == "network-test":
            return network_test(config)
        if args.action == "native-version":
            return launch(config, private_workspace(config), ["--version"])
        if args.action == "auth":
            if args.apply or args.dry_run or args.yes:
                raise RouteError("reset_options_only_valid_for_auth_reset")
            if args.text and args.operation != "status":
                raise RouteError("auth_text_only_valid_for_status")
            # Login cannot read project-level hooks or MCP settings.
            if not config.claude_executable.is_file():
                raise RouteError("install_official_claude_in_separate_profile_first")
            workspace = private_workspace(config)
            auth_args = ["auth", args.operation, *(["--text"] if args.text else [])]
            return launch(config, workspace, auth_args,
                          login=args.operation == "login")
        arguments = ["update"] if args.action == "update" else getattr(args, "arguments", [])
        if arguments and arguments[0] == "--":
            arguments = arguments[1:]
        if args.action == "run" and arguments[:1] in (["auth"], ["update"], ["install"]):
            raise RouteError("use_the_dedicated_auth_update_or_install_command")
        safe_mode = args.action == "run" and (args.safe_mode or "--safe-mode" in arguments)
        workspace = args.workspace if args.workspace is not None else private_workspace(config)
        if args.action == "run" and workspace.resolve() == Path.home().resolve():
            workspace = config.profile_home / "default-workspace"
            workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
            print(f"vps-route: home directory isolated; using {workspace}", flush=True)
        return launch(config, workspace, arguments, install=args.action == "install",
                      safe_mode=safe_mode, skills_mode=args.action == "run" and not safe_mode,
                      verify_after=args.action in {"install", "update"})
    except RouteError as exc:
        print(f"vps-route: {exc}")
        from .diagnostics import HINTS

        if str(exc) in HINTS:
            print(f"hint: {HINTS[str(exc)]}")
        return 69
    except OSError:
        print("vps-route: local_operation_failed")
        return 70
    except (EOFError, KeyboardInterrupt):
        print("vps-route: cancelled")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
