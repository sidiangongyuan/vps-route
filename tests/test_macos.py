"""macOS isolation logic that can be checked without a Mac: rules, paths, relay, gating."""

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_launcher import cli, macos, macos_check
from claude_launcher.config import Config
from claude_launcher.customizations import skills_arguments
from claude_launcher.runner import run_supervised
from claude_launcher.wire import RouteError


GOOD = {"sandbox_started": True, "tcp4_public": "blocked", "tcp6_public": "blocked",
        "udp4_public": "blocked", "loopback_other_port": "blocked",
        "loopback_proxy_port": "connected", "keychain_tool": "blocked",
        "open_tool": "blocked", "ssh_directory": "blocked", "udp_proxy_port": "blocked",
        "host_file_read": "blocked", "host_file_write": "blocked", "host_signal": "blocked",
        "launchservices": "blocked", "securityd": "blocked", "trustd": "blocked"}


def balanced(profile: str) -> bool:
    depth, quoted, escaped = 0, False, False
    for char in profile:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


class SeatbeltProfileTests(unittest.TestCase):
    def test_only_the_relay_port_is_reachable_and_profile_overrides_denials(self):
        profile = macos.seatbelt_profile(
            43210, Path("/Users/a/.local/share/claude-launcher/profiles/default"),
            [Path("/Users/a/.local/share/claude-launcher"), Path('/Users/a/we"ird\\name')],
        )
        self.assertTrue(balanced(profile))
        lines = profile.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(deny default)"])
        outbound = [line for line in lines if "network-outbound" in line]
        self.assertEqual(outbound, ['(allow network-outbound (remote tcp "localhost:43210"))'])
        self.assertIn('(subpath "/Users/a/we\\"ird\\\\name")', profile)
        deny = profile.index("(deny file-read-data file-read-xattr file-write*")
        allow = profile.index('(allow file-read* file-write* (subpath '
                              '"/Users/a/.local/share/claude-launcher/profiles/default"))')
        self.assertLess(allow, deny)  # Deny credentials even inside an allowed root.
        self.assertIn('(require-not (subpath '
                      '"/Users/a/.local/share/claude-launcher/profiles/default"))', profile)
        self.assertIn('(deny process-exec (literal "/usr/bin/security") '
                      '(literal "/usr/bin/open"))', profile)
        self.assertIn("(deny appleevent-send)", profile)

    def test_control_characters_are_rejected(self):
        with self.assertRaisesRegex(RouteError, "path_unsupported_by_macos_sandbox"):
            macos.seatbelt_profile(1, Path("/Users/a/p"), [Path("/Users/a/x\ny")])

    def test_private_paths_cover_credentials_links_proxy_clients_and_config_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            outside = root / "vault"
            (home / ".ssh").mkdir(parents=True)
            outside.mkdir()
            (home / ".aws").symlink_to(outside)
            (home / "clash-verge").mkdir()
            (home / ".tokentracker-cache").mkdir()
            (home / "projects").mkdir()
            support = home / "Library/Application Support"
            (support / "io.github.clash-verge-rev.clash-verge-rev").mkdir(parents=True)
            (support / "Notes").mkdir()
            key = root / "keys/vps"
            paths = macos.private_paths(home, (".tokentracker*",), (key,))
            for expected in (home / ".ssh", home / ".aws", outside, home / "clash-verge",
                             home / ".tokentracker-cache", home / ".config",
                             home / ".local/share/claude-launcher",
                             home / ".local/state/claude-launcher", home / "Library/Keychains",
                             support / "io.github.clash-verge-rev.clash-verge-rev", key):
                self.assertIn(expected, paths)
            for absent in (home / ".local", home / "projects", support / "Notes"):
                self.assertNotIn(absent, paths)

            (home / ".gnupg").symlink_to(home)
            with self.assertRaisesRegex(RouteError, "broad_filesystem_root"):
                macos.private_paths(home)

    def test_failures_require_denial_not_merely_unreachable_networks(self):
        self.assertEqual(macos.check_failures(GOOD), [])
        self.assertEqual(macos.check_failures({"sandbox_started": False}), ["sandbox_started"])
        for name, value, ok in [("tcp6_public", "ENETUNREACH", True),
                                ("tcp6_public", "unsupported", True),
                                ("tcp6_public", "EHOSTUNREACH", False),
                                ("tcp4_public", "timeout", False),
                                ("udp4_public", "sent", False),
                                ("loopback_other_port", "connected", False),
                                ("loopback_proxy_port", "blocked", False),
                                ("host_file_write", "allowed", False),
                                ("launchservices", "0", False),
                                ("trustd", "1102", False),
                                ("keychain_tool", "executed", False),
                                ("ssh_directory", "absent", True),
                                ("ssh_directory", "readable", False)]:
            with self.subTest(name=name, value=value):
                self.assertEqual(macos.check_failures({**GOOD, name: value}) == [], ok)

    def test_self_check_script_runs_and_reports_every_probe(self):
        with tempfile.TemporaryDirectory() as directory, \
                socket.socket() as proxy, socket.socket() as decoy:
            probe = Path(directory) / "host-probe"
            probe.write_text("test data")
            for listener in (proxy, decoy):
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", macos.SELF_CHECK,
                 str(proxy.getsockname()[1]), str(decoy.getsockname()[1]), "/nonexistent",
                 str(probe), str(os.getpid())],
                capture_output=True, text=True, timeout=60, check=True,
            )
        observed = json.loads(result.stdout)
        self.assertEqual(set(observed), set(macos.REQUIRED))
        self.assertEqual(observed["loopback_proxy_port"], "connected")
        # Without Seatbelt the decoy is reachable, so the gate must fail.
        self.assertEqual(observed["loopback_other_port"], "connected")
        self.assertIn("loopback_other_port",
                      macos.check_failures({"sandbox_started": True, **observed}))
        self.assertEqual(observed["ssh_directory"], "absent")

    def test_install_script_is_valid_bash_and_uses_https_only(self):
        subprocess.run(["bash", "-n", "-c", macos.INSTALL_SCRIPT], check=True)
        self.assertIn("--proto =https --proto-redir =https", macos.INSTALL_SCRIPT)
        self.assertEqual(macos.INSTALL_COMMAND[:2], ["/bin/bash", "-c"])


class SeatbeltSessionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / "home"
        self.profile = self.home / ".local/share/claude-launcher/profiles/default"
        (self.profile / "auth-workspace").mkdir(parents=True)
        (self.home / "project").mkdir()
        (self.home / ".ssh").mkdir()
        (self.home / "Library/Keychains").mkdir(parents=True)
        for patcher in (patch("claude_launcher.macos.Path.home", return_value=self.home),
                        patch("claude_launcher.macos.seatbelt_present", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_relay_forwards_to_the_current_ssh_socket_and_command_is_sandboxed(self):
        upstream = self.root / "proxy.sock"
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(upstream))
        server.listen(1)
        self.addCleanup(server.close)

        def echo():
            connection, _ = server.accept()
            with connection:
                connection.sendall(connection.recv(64).upper())

        threading.Thread(target=echo, daemon=True).start()
        with patch.dict(os.environ, {"PATH": "/opt/homebrew/bin:relative:/usr/bin",
                                     "ANTHROPIC_API_KEY": "secret", "TMPDIR": "/var/tmp/x/"}), \
                patch("claude_launcher.macos.run_self_check", return_value=GOOD) as check, \
                macos.seatbelt_session(upstream, self.profile, self.home / "project",
                                       ["/claude", "--flag"], timezone="UTC") as (args, env):
            port = int(env["HTTPS_PROXY"].rsplit(":", 1)[1])
            self.assertEqual(env["HTTP_PROXY"], f"http://127.0.0.1:{port}")
            self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1")
            self.assertEqual(env["HOME"], str(self.profile))
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.profile / ".claude"))
            self.assertEqual(env["TZ"], "UTC")
            self.assertNotEqual(env["TMPDIR"], "/var/tmp/x/")
            self.assertTrue(Path(env["TMPDIR"]).is_dir())
            self.assertEqual(env["PATH"], f"{self.profile}/.local/bin:/opt/homebrew/bin:/usr/bin")
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertEqual(args[:6], ["/bin/sh", "-c", 'cd -- "$1" && shift && exec "$@"',
                                        "vps-route", str(self.home / "project"),
                                        macos.SANDBOX_EXEC])
            self.assertEqual(args[6], "-p")
            self.assertEqual(args[8:], ["/claude", "--flag"])
            profile = args[7]
            self.assertEqual(check.call_args.args[:2], (profile, port))
            self.assertIn(f'(remote tcp "localhost:{port}")', profile)
            self.assertIn(f'(subpath "{self.home}/.ssh")', profile)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(b"connect")
                self.assertEqual(client.recv(64), b"CONNECT")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        self.assertFalse(Path(env["TMPDIR"]).exists())

    def test_command_runs_in_workspace_through_pty_helper_with_stand_in_sandbox(self):
        # A stand-in for sandbox-exec drops "-p PROFILE"; the rest of the chain is real.
        stand_in = self.root / "sandbox-exec"
        stand_in.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
        stand_in.chmod(0o700)
        script = 'pwd; echo "$HTTPS_PROXY"; tty >/dev/null && : </dev/tty && exit 7'
        input_fd, writer = os.pipe()  # Keep input open; do not echo EOF into the PTY.
        self.addCleanup(os.close, input_fd)
        self.addCleanup(os.close, writer)
        with patch("claude_launcher.macos.SANDBOX_EXEC", str(stand_in)), \
                patch("claude_launcher.runner.PTY_PYTHON", [sys.executable, "-I"]), \
                patch("claude_launcher.macos.run_self_check", return_value=GOOD), \
                macos.seatbelt_session(self.root / "x.sock", self.profile,
                                       self.home / "project", ["/bin/sh", "-c", script]) \
                as (args, env), \
                tempfile.TemporaryFile() as output:
            result = run_supervised(args, env, None, lambda: None, lambda: True, 1,
                                    input_fd=input_fd, output_fd=output.fileno())
            output.seek(0)
            lines = output.read().decode().splitlines()
        self.assertEqual(result, (7, "child_exited"))
        self.assertEqual(lines[:2], [str(self.home / "project"), env["HTTPS_PROXY"]])

    def test_failed_self_check_refuses_start_and_closes_relay(self):
        with patch("claude_launcher.macos.run_self_check",
                   return_value={**GOOD, "tcp4_public": "timeout"}):
            with self.assertRaisesRegex(RouteError, "network_isolation_unverified"):
                with macos.seatbelt_session(self.root / "x.sock", self.profile,
                                            self.home / "project", ["/claude"]):
                    self.fail("must not start")

    def test_credential_and_home_workspaces_are_refused_private_workspace_allowed(self):
        for workspace in (self.home, self.home / ".ssh", self.home / "Library/Keychains",
                          self.home / ".local/share/claude-launcher"):
            with self.subTest(workspace=workspace), self.assertRaises(RouteError):
                with macos.seatbelt_session(self.root / "x.sock", self.profile, workspace,
                                            ["/claude"]):
                    pass
        with patch("claude_launcher.macos.run_self_check", return_value=GOOD), \
                macos.seatbelt_session(self.root / "x.sock", self.profile,
                                       self.profile / "auth-workspace", ["/claude"]) as (args, _):
            self.assertEqual(args[4], str(self.profile / "auth-workspace"))

    def test_missing_sandbox_exec_refuses(self):
        with patch("claude_launcher.macos.seatbelt_present", return_value=False), \
                self.assertRaisesRegex(RouteError, "seatbelt_unavailable"):
            with macos.seatbelt_session(self.root / "x.sock", self.profile,
                                        self.home / "project", ["/claude"]):
                pass


class ProcessTests(unittest.TestCase):
    def ps(self, output: bytes, code: int = 0):
        return patch("claude_launcher.macos._ps", return_value=subprocess.CompletedProcess(
            [], code, output, b""))

    def test_profile_detection_matches_whole_path_components_of_own_processes(self):
        uid, profile = os.getuid(), "/Users/a/.local/share/claude-launcher/profiles/default"
        cases = [
            (f"  7 {uid} {profile}/.local/bin/claude --resume", True),
            (f"  7 {uid} /bin/zsh -c cd {profile} && ls", True),
            (f"  7 {uid} {profile}", True),
            (f"  7 {uid} {profile}2/.local/bin/claude", False),
            (f"  7 {uid + 1} {profile}/.local/bin/claude", False),
            (f"  {os.getpid()} {uid} vps-route run --workspace {profile}/default-workspace", False),
        ]
        for line, expected in cases:
            with self.subTest(line=line), self.ps(line.encode() + b"\n"):
                self.assertEqual(macos.profile_is_running(Path(profile)), expected)
        with self.ps(b"", 1), self.assertRaisesRegex(RouteError, "process_state"):
            macos.profile_is_running(Path(profile))

    def test_process_identity_uses_start_time_of_live_own_processes(self):
        uid = os.getuid()
        with self.ps(f"{uid} Ss   Wed Oct 15 10:00:00 2026\n".encode()):
            self.assertEqual(macos.process_identity(9), "Wed Oct 15 10:00:00 2026")
        for output, code in [(f"{uid} Z Wed Oct 15 10:00:00 2026", 0),
                             (f"{uid + 1} S Wed Oct 15 10:00:00 2026", 0), ("", 1)]:
            with self.subTest(output=output), self.ps(output.encode(), code):
                self.assertIsNone(macos.process_identity(9))

    def test_sessions_use_ps_identity_on_macos(self):
        from claude_launcher import sessions

        with patch.object(sessions.sys, "platform", "darwin"), \
                patch("claude_launcher.macos.process_identity", return_value="t") as identity:
            self.assertEqual(sessions.process_identity(5), "t")
            identity.assert_called_once_with(5)


class KeychainReportTests(unittest.TestCase):
    def test_only_claude_service_names_and_times_are_reported(self):
        dump = '''keychain: "/Users/a/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    "acct"<blob>="a"
    "mdat"<timedate>=0x32303236  "20261015080000Z\\000"
    "svce"<blob>="Claude Code-credentials"
keychain: "/Users/a/Library/Keychains/login.keychain-db"
class: "genp"
attributes:
    "acct"<blob>="bank-user"
    "mdat"<timedate>=0x32303236  "20260101000000Z\\000"
    "svce"<blob>="Bank"
keychain: "/Users/a/Library/Keychains/login.keychain-db"
attributes:
    "svce"<blob>=<NULL>
'''
        with patch("claude_launcher.macos_check.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, dump, "")) as run:
            self.assertEqual(macos_check.keychain_claude_items(),
                             ["Claude Code-credentials | modified 20261015080000Z"])
            self.assertEqual(run.call_args.args[0], ["/usr/bin/security", "dump-keychain"])
        with patch("claude_launcher.macos_check.subprocess.run", side_effect=OSError):
            self.assertIsNone(macos_check.keychain_claude_items())


class MacCliTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("203.0.113.1", "fixture", 22, Path("unused"), Path("unused"),
                             "/unused")
        patcher = patch.object(cli, "MACOS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_linux_only_commands_are_reported_unsupported(self):
        for command in (["status"], ["sessions", "list"], ["reset"], ["shell"],
                        ["network-test"], ["auth", "reset"]):
            output = io.StringIO()
            with self.subTest(command=command), \
                    patch.object(sys, "argv", ["vps-route", "--config", "/unused", *command]), \
                    patch.object(cli.Config, "load", return_value=self.config), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(), 69)
            self.assertIn("command_unsupported_on_macos", output.getvalue())

    def test_claude_sandbox_is_disabled_only_on_macos(self):
        def settings(arguments):
            return json.loads(arguments[arguments.index("--settings") + 1])

        with patch("claude_launcher.customizations.sys.platform", "darwin"):
            self.assertEqual(settings(skills_arguments([]))["sandbox"], {"enabled": False})
        with patch("claude_launcher.customizations.sys.platform", "linux"):
            self.assertNotIn("sandbox", settings(skills_arguments([])))

    def test_launch_uses_seatbelt_installer_and_requires_profile_credentials_after_login(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            config = Config("203.0.113.1", "fixture", 22, home / "key", home / "hosts",
                            "/unused", texlive_root=None)
            sessions = []

            @contextlib.contextmanager
            def session(socket_path, profile_home, workspace, command, **options):
                sessions.append((command, options))
                yield ["fixture"], {}

            with patch("claude_launcher.cli.Path.home", return_value=home), \
                    patch("claude_launcher.macos.seatbelt_present", return_value=True), \
                    patch("claude_launcher.macos.seatbelt_session", side_effect=session), \
                    patch("claude_launcher.cli.namespace_available") as namespace, \
                    patch("claude_launcher.cli.SSHTunnel"), \
                    patch("claude_launcher.cli.connectivity_check", return_value=None), \
                    patch("claude_launcher.cli.run_supervised",
                          return_value=(0, "child_exited")), \
                    patch("claude_launcher.cli.receipt"), \
                    patch("claude_launcher.install.installation_present", return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.launch(config, config.profile_home, [], install=True), 0)
                self.assertEqual(sessions[-1][0], macos.INSTALL_COMMAND)
                self.assertEqual(sessions[-1][1]["protected_files"],
                                 (config.identity_file, config.known_hosts))
                namespace.assert_not_called()

                executable = config.claude_executable
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch(mode=0o700)
                workspace = config.profile_home / "auth-workspace"
                workspace.mkdir()
                self.assertEqual(cli.launch(config, workspace, ["auth", "login"], login=True), 69)
                credentials = config.profile_home / ".claude/.credentials.json"
                credentials.parent.mkdir()
                credentials.write_text("{}")
                self.assertEqual(cli.launch(config, workspace, ["auth", "login"], login=True), 0)

    def test_texlive_is_refused_on_macos(self):
        config = Config("203.0.113.1", "fixture", 22, Path("k"), Path("h"), "/unused",
                        texlive_root=Path("/opt/tex"))
        with patch("claude_launcher.cli.SSHTunnel") as tunnel, \
                self.assertRaisesRegex(RouteError, "texlive_root_unsupported_on_macos"):
            cli._launch(config, Path("/unused"), ["--version"], MagicMock())
        tunnel.assert_not_called()

    def test_research_filesystem_is_refused_before_connecting(self):
        config = Config("203.0.113.1", "fixture", 22, Path("k"), Path("h"), "/unused",
                        research_filesystem=True)
        with patch("claude_launcher.cli.SSHTunnel") as tunnel, \
                self.assertRaisesRegex(RouteError, "research_filesystem_unsupported_on_macos"):
            cli._launch(config, Path("/unused"), ["--version"], MagicMock())
        tunnel.assert_not_called()

    def test_doctor_reports_unsupported_macos_configuration(self):
        config = Config("203.0.113.1", "fixture", 22, Path("k"), Path("h"), "/unused",
                        research_filesystem=True)
        output = io.StringIO()
        with patch("claude_launcher.macos.isolation_report", return_value=GOOD), \
                patch("claude_launcher.cli.shutil.which", return_value="/tool"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.doctor(config, False), 69)
        self.assertFalse(json.loads(output.getvalue())["macos_configuration_supported"])


if __name__ == "__main__":
    unittest.main()
