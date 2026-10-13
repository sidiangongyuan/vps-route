"""CLI entry points. Live account stores are never used by this launcher."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .audit import AuditLog, log_tail
from .config import Config
from .customizations import skills_arguments
from .observe import observe_hicc
from .profile_reset import full_reset_inventory, reset_local_state, reset_profile
from .onboarding import complete_authenticated_onboarding
from .setup import default_config_path, initialize_config
from .runner import (
    ENTRY_DIR, RuntimeEgressMonitor, SSHTunnel, egress_check, receipt, run_supervised,
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


def doctor(config: Config | None, connect: bool, hicc: bool = False) -> int:
    checks = {
        "ssh_installed": shutil.which("ssh") is not None,
        "system_python_compatible": system_python_compatible(),
        "curl_installed": shutil.which("curl") is not None,
        "bash_installed": shutil.which("bash") is not None,
        "private_network_namespace": namespace_available(),
        "config_loaded": config is not None,
        "live_account_files_read": False,
        "network_probe_executed": connect,
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
            "runtime_egress_monitor_enabled": True,
            "runtime_probe_interval_seconds": config.runtime_probe_interval_seconds,
            "runtime_probe_failures": config.runtime_probe_failures,
        })
    if hicc:
        checks["hicc_passive_snapshot"] = observe_hicc()
    ready = all(checks[name] for name in ("ssh_installed", "private_network_namespace",
                                        "system_python_compatible", "curl_installed",
                                        "bash_installed"))
    if connect:
        if config is None:
            raise RouteError("connect_requires_config")
        with tempfile.TemporaryDirectory(prefix="claude-route-") as directory:
            tunnel = SSHTunnel(config, Path(directory) / "proxy.sock")
            try:
                tunnel.start()
                reason = egress_check(config, tunnel.socket_path)
                checks["configured_egress_matches"] = reason is None
                checks["route_result"] = reason or "configured_ip_and_country_match"
            finally:
                tunnel.dispose()
        print(json.dumps(checks, indent=2))
        return 0 if ready and checks["configured_egress_matches"] else 69
    print(json.dumps(checks, indent=2))
    return 0 if ready else 69


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
    operation = "install" if install else "run"
    if arguments[:1] == ["auth"] and arguments[1:2] in (["login"], ["status"]):
        operation = "auth_" + arguments[1]
    elif arguments == ["update"]:
        operation = "update"
    elif arguments == ["--version"]:
        operation = "version"
    audit = AuditLog(operation, config.timezone)
    audit.write("start")
    try:
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
        try:
            audit.write("exit")
        except (OSError, RouteError):
            # Never hide a route failure or change the completed child's result.
            print("claude-launcher: usage_log_write_failed", file=sys.stderr)


def _launch(config: Config, workspace: Path, arguments: list[str], audit: AuditLog,
            install: bool = False, login: bool = False, safe_mode: bool = False,
            verify_after: bool = False, skills_mode: bool = False) -> int:
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
    with tempfile.TemporaryDirectory(prefix="claude-route-") as directory:
        tunnel = SSHTunnel(config, Path(directory) / "proxy.sock")
        try:
            tunnel.start()
            reason = egress_check(config, tunnel.socket_path)
            if reason:
                raise RouteError(reason)
            audit.write("preflight_passed", country=config.expected_country)
            if (safe_mode or skills_mode) and complete_authenticated_onboarding(profile_home, executable):
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
                timezone=config.timezone,
            )
            code, reason = run_supervised(
                command, environment,
                RuntimeEgressMonitor(config, tunnel.socket_path),
                tunnel.close, tunnel.alive,
                config.runtime_probe_interval_seconds,
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
                        if egress_check(config, verify_tunnel.socket_path):
                            code, reason = 69, "update_verification_route_failed"
                        else:
                            verify_command, verify_environment = sandbox_command(
                                verify_tunnel.socket_path, profile_home, workspace,
                                [str(executable), "--version"],
                                timezone=config.timezone,
                            )
                            verify_code, _ = run_supervised(
                                verify_command, verify_environment,
                                RuntimeEgressMonitor(config, verify_tunnel.socket_path),
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
    print(f"\nclaude-launcher: {reason}; exit={code}")
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
            reason = egress_check(config, tunnel.socket_path)
            if reason:
                raise RouteError(reason)
            command, environment = sandbox_command(
                tunnel.socket_path, profile, workspace,
                ["/usr/bin/python3", str(ENTRY_DIR / "network_test_entry.py"),
                 config.expected_ip, config.expected_country],
                research_filesystem=config.research_filesystem,
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
        "init", help="archive old local Claude state and create private config; no login",
    )
    initialize.add_argument("--from-file", type=Path, help="import a validated connection config")
    initialize.add_argument("--keep-existing", action="store_true",
                            help="create config only; do not archive existing Claude state")
    initialize.add_argument("--dry-run", action="store_true", help="preview without writing or moving")
    initialize.add_argument("-y", "--yes", action="store_true", help="skip archive confirmation only")
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
    logs = subparsers.add_parser("logs", help="read private operational logs; no accounts or prompts")
    logs.add_argument("--tail", type=int, default=20)
    shell = subparsers.add_parser("shell", help="open an interactive VPS SSH shell")
    shell.add_argument("--password", action="store_true",
                       help="prompt in the terminal for the VPS password; keep restricted keys unchanged")
    subparsers.add_parser("network-test", help="isolated egress and unauthenticated Gemini HTTPS test")
    archive = subparsers.add_parser("legacy-archive", help="preview or archive legacy Claude/HICC state")
    archive.add_argument("--apply", action="store_true")
    archive.add_argument("--confirm-hicc-retired", action="store_true")
    activate = subparsers.add_parser("cutover-activate", help="preview or install an optional Claude wrapper")
    activate.add_argument("--archive", type=Path, required=True)
    activate.add_argument("--apply", action="store_true")
    activate.add_argument("--confirm-hicc-retired", action="store_true")
    auth = subparsers.add_parser("auth", help="authentication in the separate profile only")
    auth.add_argument("operation", choices=["login", "status", "reset"])
    auth.add_argument("--text", action="store_true", help="forward --text for auth status")
    reset = subparsers.add_parser(
        "reset", help="preview full local-state reset; --apply archives and reinstalls via VPS",
    )
    reset.add_argument("--apply", action="store_true", help="archive old state and create fresh state")
    reset.add_argument("--no-install", action="store_true", help="leave fresh profile uninstalled")
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
        if args.action == "init":
            path = args.config or default_config_path()
            initialize_config(path, args.from_file, keep_existing=args.keep_existing,
                              dry_run=args.dry_run, yes=args.yes)
            if args.dry_run:
                print("Preview only; no configuration or account state changed.")
                return 0
            print(f"Private config created: {path}")
            print("Next: vps-route setup-vps --apply (or use an existing CONNECT service)")
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
        path = args.config or default_config_path()
        config = Config.load(path) if args.config or path.exists() else None
        if args.action == "doctor":
            return doctor(config, args.connect, args.observe_hicc)
        if args.action in {"legacy-archive", "cutover-activate"}:
            from .legacy_cutover import (
                activate_wrapper, activation_inventory, archive_legacy_state, legacy_inventory,
            )
            if args.apply and not args.confirm_hicc_retired:
                raise RouteError("explicit_hicc_retirement_confirmation_required")
            if args.action == "legacy-archive":
                if args.apply:
                    archived = archive_legacy_state()
                    print(f"legacy state archived privately at {archived}")
                else:
                    print(json.dumps(legacy_inventory(), indent=2))
                return 0
            if args.apply:
                backup = activate_wrapper(args.archive)
                print(f"old Claude wrapper backed up privately at {backup}")
            else:
                print(json.dumps(activation_inventory(args.archive), indent=2))
            return 0
        if config is None:
            raise RouteError("config_required; run_vps-route_init")
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
            if not args.apply:
                print(json.dumps(full_reset_inventory(config), indent=2))
                return 0
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
        print(f"claude-launcher: {exc}")
        return 69
    except OSError:
        print("claude-launcher: local_operation_failed")
        return 70
    except (EOFError, KeyboardInterrupt):
        print("vps-route: cancelled")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
