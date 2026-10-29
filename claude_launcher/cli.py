"""CLI entry points. Live account stores are never used by this launcher."""

from __future__ import annotations

import argparse
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
from .arguments import build_parser
from .credentials import has_token, prompt_token, read_token, remove_token, store_token, token_status
from .customizations import skills_arguments
from .profile_reset import full_reset_inventory, reset_local_state, reset_profile
from .onboarding import complete_authenticated_onboarding
from .orca_access import prepare_orca_access
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


def doctor(config: Config | None, connect: bool, report: Path | None = None) -> int:
    checks = {
        "ssh_installed": shutil.which("ssh") is not None,
        "system_python_compatible": system_python_compatible(),
        "curl_installed": shutil.which("curl") is not None,
        "bash_installed": shutil.which("bash") is not None,
        "private_network_namespace": namespace_available(),
        "config_loaded": config is not None,
        "live_account_files_read": False,
        "network_probe_executed": connect,
        "launcher_version": __version__,
    }
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
            "research_filesystem_enabled": config.research_filesystem,
            "runtime_connectivity_monitor_enabled": True,
            "exit_ip_pinning_enabled": False,
            "runtime_probe_interval_seconds": config.runtime_probe_interval_seconds,
            "runtime_probe_failures": config.runtime_probe_failures,
        })
    from . import isolation, wsl

    try:
        checks["wsl"] = wsl.summary()
    except RouteError:
        checks["wsl"] = {"platform": "wsl", "result": "inconclusive"}
    checks["isolation"] = isolation.inspect(config)
    ready = all(checks[name] for name in ("ssh_installed", "private_network_namespace",
                                        "system_python_compatible", "curl_installed",
                                        "bash_installed"))
    ready = ready and checks["isolation"]["passed"]
    if connect:
        if config is None:
            raise RouteError("connect_requires_config")
        from .diagnostics import connect_diagnostics, connection_diagnosis

        if ready:
            checks.update(connect_diagnostics(config))
        else:
            checks.update(connectivity_ok=False, network_probe_executed=False,
                          route_result="local_dependencies_unavailable",
                          diagnosis=connection_diagnosis("local_dependencies_unavailable", []))
    checks["ready"] = ready and (not connect or checks.get("connectivity_ok", False))
    if report is not None:
        from .management import private_json

        private_json(report.expanduser().absolute(), checks)
    print(json.dumps(checks, indent=2, ensure_ascii=False))
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
           verify_after: bool = False, skills_mode: bool = False,
           orca: bool = False, windows: bool = False) -> int:
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
                           safe_mode=safe_mode, verify_after=verify_after, skills_mode=skills_mode,
                           orca=orca, windows=windows)
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


def _launch(config: Config, workspace: Path, arguments: list[str], audit: AuditLog,
            install: bool = False, login: bool = False, safe_mode: bool = False,
            verify_after: bool = False, skills_mode: bool = False,
            orca: bool = False, windows: bool = False) -> int:
    if install:
        from .install import installation_present

        if installation_present(config):
            print("vps-route: Claude is already installed; kept unchanged. "
                  "Use vps-route update to upgrade.")
            audit.exit_code, audit.reason = 0, "already_installed"
            return 0
    from . import wsl

    if wsl.version() == 1:
        raise RouteError("wsl1_unsupported")
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
    if windows and (not keep_session or arguments[:1] == ["setup-token"]):
        raise RouteError("windows_interop_requires_daily_run")
    if windows and wsl.version() != 2:
        raise RouteError("windows_interop_requires_wsl2")
    if windows:
        print("vps-route: Windows programs and WSLg enabled; host programs use Windows "
              "network and user permissions; Linux processes retain the VPS route.", flush=True)
    oauth_token = (read_token(profile_home)
                   if keep_session and arguments[:1] != ["setup-token"] else None)
    with tempfile.TemporaryDirectory(prefix="claude-route-") as directory:
        if orca and not keep_session:
            raise RouteError("orca_requires_daily_run")
        orca_access = prepare_orca_access(Path(directory) / "orca") if orca else None
        route_directory = Path(directory) / "route"
        route_directory.mkdir(mode=0o700)
        tunnel = SSHTunnel(config, route_directory / "proxy.sock")
        try:
            tunnel.start()
            reason = connectivity_check(config, tunnel.socket_path, audit.connection_check)
            if reason:
                raise RouteError(reason)
            audit.write("preflight_passed")
            if (oauth_token is None and (safe_mode or skills_mode)
                    and complete_authenticated_onboarding(profile_home, executable)):
                print("vps-route: existing login kept; first-run UI setup repaired", flush=True)
            if login:
                print("Open the official login URL shown below on a browser using the VPS exit."
                      " Paste the one-time code back into this terminal when prompted.",
                      flush=True)
            executable_args = (
                ["/usr/bin/python3", str(ENTRY_DIR / "install_entry.py")]
                if install else [str(executable), *(["--safe-mode"] if safe_mode else []),
                                 *(skills_arguments(arguments) if skills_mode else arguments)]
            )
            command, environment = sandbox_command(
                tunnel.socket_path, profile_home, workspace,
                executable_args,
                login=login, project_instructions=skills_mode,
                texlive_root=config.texlive_root,
                research_filesystem=config.research_filesystem,
                private_home_entries=config.private_home_entries,
                timezone=config.timezone,
                reconnectable=keep_session,
                orca_access=orca_access,
                windows_interop=windows,
            )
            if oauth_token is not None:
                environment["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
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
                            verify_command, verify_environment = sandbox_command(
                                verify_tunnel.socket_path, profile_home, workspace,
                                [str(executable), "--version"],
                                timezone=config.timezone,
                            )
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
    from . import wsl

    if wsl.version() == 1:
        raise RouteError("wsl1_unsupported")
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


def token_auth(config: Config, args: argparse.Namespace) -> int | None:
    """Explicit local token lifecycle; no account API calls or terminal recording."""
    operation = args.operation
    if args.replace and operation != "import-token":
        raise RouteError("replace_only_valid_for_token_import")
    if operation == "import-token":
        if args.text or args.apply or args.dry_run or args.yes:
            raise RouteError("invalid_token_import_options")
        if has_token(config.profile_home) and not args.replace:
            raise RouteError("setup_token_exists; use_--replace")
        token = prompt_token()
        with maintenance_barrier():
            store_token(config.profile_home, token, replace=args.replace)
        print("Setup token saved privately for this profile. "
              "New sessions will use it; validity has not been checked. Next: vps-route run")
        return 0
    if operation == "remove-token":
        if args.text or (args.apply and args.dry_run):
            raise RouteError("invalid_token_removal_options")
        present = has_token(config.profile_home)
        if not args.apply:
            print(json.dumps({"token_stored": present, "apply_required": present,
                              "local_copy_only": True, "upstream_revoked": False}, indent=2))
            return 0
        from .lifecycle import confirm_operation

        if present:
            confirm_operation("Remove the local token? Future sessions use saved login, if any.",
                              yes=args.yes)
            with maintenance_barrier():
                remove_token(config.profile_home)
        print("Local setup token removed; upstream token and running sessions unchanged.")
        return 0
    if operation == "status" and not (args.apply or args.dry_run or args.yes):
        result = token_status(config.profile_home)
        if result is not None:
            if args.text:
                print("Authentication: setup-token (stored locally; validity not checked)")
            else:
                print(json.dumps(result, indent=2))
            return 0
    if operation == "login" and has_token(config.profile_home):
        raise RouteError("setup_token_active; remove_local_token_before_browser_login")
    return None


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.action == "completion":
            from .completions import shell_script

            print(shell_script(args.shell), end="")
            return 0
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
        if config is None:
            raise RouteError("config_required; run_vps-route_init")
        if args.action == "auth":
            handled = token_auth(config, args)
            if handled is not None:
                return handled
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
                      verify_after=args.action in {"install", "update"},
                      windows=args.action == "run" and (args.windows or config.windows_interop),
                      orca=args.action == "run" and (
                          args.orca or (config.orca_control
                                        and bool(os.environ.get("ORCA_CLI_COMMAND")))
                      ))
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
