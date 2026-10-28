"""Synthetic setup-token storage and dispatch; no live account or model request."""

import contextlib
import io
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from claude_launcher import cli, credentials
from claude_launcher.config import Config
from claude_launcher.profile_reset import reset_profile
from claude_launcher.runner import clean_environment, sandbox_command
from claude_launcher.wire import RouteError


TOKEN = "sk-ant-oat01-synthetic_fixture_not_a_real_token"
OTHER = "sk-ant-oat01-another_synthetic_fixture"


class CredentialTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="route-token-test-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"),
                             "/unused", profile="fixture")
        self.profile = self.config.profile_home
        self.path = self.profile / credentials.TOKEN_DIRECTORY / credentials.TOKEN_FILE

    def invoke(self, arguments):
        output = io.StringIO()
        with patch("sys.argv", ["vps-route", "--config", "/unused", *arguments]), \
                patch.object(cli.Config, "load", return_value=self.config), \
                patch.object(cli, "maintenance_barrier", return_value=contextlib.nullcontext()), \
                patch.object(cli, "launch") as launch, contextlib.redirect_stdout(output):
            code = cli.main()
        return code, output.getvalue(), launch

    def test_missing_token_is_read_only(self):
        self.assertIsNone(credentials.read_token(self.profile))
        self.assertFalse(credentials.has_token(self.profile))
        self.assertFalse((self.home / ".local").exists())

    def test_disappearing_directory_never_falls_back_to_working_directory(self):
        @contextlib.contextmanager
        def disappeared(*_args, **_kwargs):
            yield None

        with patch.object(credentials, "_token_directory", side_effect=disappeared), \
                self.assertRaisesRegex(RouteError, "unsafe_or_unreadable_token_storage"):
            credentials.store_token(self.profile, TOKEN)

    def test_private_store_replace_and_local_removal(self):
        credentials.store_token(self.profile, TOKEN)
        self.assertEqual(credentials.read_token(self.profile), TOKEN)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(RouteError, "setup_token_exists"):
            credentials.store_token(self.profile, OTHER)
        self.assertEqual(credentials.read_token(self.profile), TOKEN)
        credentials.store_token(self.profile, OTHER, replace=True)
        self.assertEqual(credentials.read_token(self.profile), OTHER)
        self.assertNotIn(OTHER, json.dumps(credentials.token_status(self.profile)))
        self.assertTrue(credentials.remove_token(self.profile))
        self.assertFalse(credentials.remove_token(self.profile))
        self.assertIsNone(credentials.read_token(self.profile))

    def test_malformed_oversized_or_unrelated_credentials_are_rejected(self):
        for value in ("", "one-time-code#state", "sk-ant-api03-synthetic", TOKEN + "\nextra",
                      TOKEN + "\x1b", TOKEN + "x" * credentials.MAX_TOKEN_SIZE):
            with self.subTest(value=value[:20]), self.assertRaises(RouteError) as error:
                credentials.store_token(self.profile, value)
            self.assertNotIn(TOKEN, str(error.exception))
        self.assertFalse(self.profile.exists())

    def test_unsafe_files_fail_closed_without_web_login_fallback(self):
        credentials.store_token(self.profile, TOKEN)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(RouteError, "unsafe_token_file"):
            credentials.read_token(self.profile)
        self.path.chmod(0o600)
        linked = self.home / "linked-token"
        os.link(self.path, linked)
        with self.assertRaisesRegex(RouteError, "unsafe_token_file"):
            credentials.read_token(self.profile)
        linked.unlink()
        self.path.unlink()
        self.path.symlink_to(self.home / "unrelated")
        with self.assertRaises(RouteError):
            credentials.store_token(self.profile, OTHER, replace=True)
        self.assertFalse((self.home / "unrelated").exists())

    def test_symlinked_directory_is_rejected(self):
        self.profile.mkdir(parents=True, mode=0o700)
        target = self.home / "other"
        target.mkdir(mode=0o700)
        self.path.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(RouteError):
            credentials.store_token(self.profile, TOKEN)
        self.assertEqual(list(target.iterdir()), [])

    def test_owned_shared_ancestors_preserve_existing_web_login_compatibility(self):
        outer = self.home / ".local"
        outer.mkdir()
        outer.chmod(0o775)
        self.assertIsNone(credentials.read_token(self.profile))
        credentials.store_token(self.profile, TOKEN)
        self.assertEqual(credentials.read_token(self.profile), TOKEN)
        self.path.parent.chmod(0o755)
        with self.assertRaisesRegex(RouteError, "unsafe_token_directory"):
            credentials.read_token(self.profile)

    def test_prompt_refuses_noninteractive_and_echoed_fallback(self):
        with patch("sys.stdin.isatty", return_value=False), \
                patch.object(credentials.getpass, "getpass") as prompt:
            with self.assertRaises(RouteError):
                credentials.prompt_token()
            prompt.assert_not_called()
        with patch("sys.stdin.isatty", return_value=True), \
                patch.object(credentials.getpass, "getpass", return_value=TOKEN):
            self.assertEqual(credentials.prompt_token(), TOKEN)
        with patch("sys.stdin.isatty", return_value=True), \
                patch.object(credentials.getpass, "getpass",
                             side_effect=credentials.getpass.GetPassWarning):
            with self.assertRaisesRegex(RouteError, "hidden_input"):
                credentials.prompt_token()

    def test_import_and_status_do_not_authenticate_or_print_the_token(self):
        with patch.object(cli, "prompt_token", return_value=TOKEN):
            code, output, launch = self.invoke(["auth", "import-token"])
        self.assertEqual(code, 0, output)
        launch.assert_not_called()
        self.assertNotIn(TOKEN, output)
        for option in ([], ["--text"]):
            code, output, launch = self.invoke(["auth", "status", *option])
            self.assertEqual(code, 0, output)
            launch.assert_not_called()
            self.assertNotIn(TOKEN, output)
            self.assertIn("not_checked" if not option else "not checked", output)
        with patch.object(cli, "prompt_token") as prompt:
            self.assertEqual(self.invoke(["auth", "import-token"])[0], 69)
            prompt.assert_not_called()
        with patch.object(cli, "prompt_token", return_value=OTHER):
            self.assertEqual(self.invoke(["auth", "import-token", "--replace"])[0], 0)
        self.assertEqual(credentials.read_token(self.profile), OTHER)

    def test_browser_login_requires_explicit_local_token_removal(self):
        credentials.store_token(self.profile, TOKEN)
        code, output, launch = self.invoke(["auth", "login"])
        self.assertEqual(code, 69, output)
        launch.assert_not_called()
        self.assertEqual(credentials.read_token(self.profile), TOKEN)
        self.assertEqual(self.invoke(["auth", "remove-token"])[0], 0)
        self.assertTrue(credentials.has_token(self.profile))
        self.assertEqual(self.invoke(["auth", "remove-token", "--apply", "-y"])[0], 0)
        self.assertFalse(credentials.has_token(self.profile))

    def test_invalid_stored_token_can_be_removed_and_is_never_accepted(self):
        credentials.store_token(self.profile, TOKEN)
        self.path.write_text("invalid")
        self.assertEqual(self.invoke(["auth", "status"])[0], 69)
        self.assertEqual(self.invoke(["auth", "remove-token", "--apply", "-y"])[0], 0)

    def test_accidental_token_argument_is_redacted_from_parser_errors(self):
        from claude_launcher.arguments import build_parser

        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            build_parser().parse_args(["auth", "import-token", TOKEN])
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertIn("[redacted token]", output.getvalue())

    @unittest.skipUnless(sys.platform in {"linux", "darwin"}, "Unix PTY input")
    def test_interactive_import_uses_hidden_input_in_a_real_pty(self):
        config = self.home / "route.json"
        config.write_text(json.dumps({
            "ssh_host": "203.0.113.1", "ssh_user": "fixture", "ssh_port": 22,
            "identity_file": str(self.home / "key"), "known_hosts": str(self.home / "hosts"),
            "remote_socket": "/unused", "profile": "fixture",
        }))
        root = Path(__file__).resolve().parent.parent
        environment = {"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                       "PYTHONPATH": str(root), "LANG": "C.UTF-8"}
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-c",
             "from claude_launcher.entrypoint import main; raise SystemExit(main())",
             "--config", str(config), "auth", "import-token"],
            env=environment, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        output = bytearray()
        sent = False
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    output.extend(os.read(master, 65536))
                    if b"(hidden):" in output and not sent:
                        os.write(master, TOKEN.encode() + b"\n")
                        sent = True
                if process.poll() is not None:
                    while select.select([master], [], [], 0.05)[0]:
                        output.extend(os.read(master, 65536))
                    break
            self.assertEqual(process.wait(timeout=1), 0, output.decode(errors="replace"))
            self.assertTrue(sent)
            self.assertNotIn(TOKEN.encode(), output)
            self.assertEqual(credentials.read_token(self.profile), TOKEN)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)
            os.close(slave)

    def test_profile_reset_keeps_only_an_inactive_private_archive(self):
        credentials.store_token(self.profile, TOKEN)
        with patch("claude_launcher.profile_reset._profile_is_running", return_value=False):
            archive = reset_profile(self.config)
        self.assertIsNone(credentials.read_token(self.profile))
        self.assertEqual((archive / credentials.TOKEN_DIRECTORY / credentials.TOKEN_FILE)
                         .read_text().strip(), TOKEN)

    def exercise_launch(self, arguments=None, *, macos=False, stored=True):
        if stored:
            credentials.store_token(self.profile, TOKEN, replace=True)
        executable = self.config.claude_executable
        executable.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        environment = {}
        with contextlib.ExitStack() as stack:
            for name, value in (("namespace_available", True), ("system_python_compatible", True),
                                ("connectivity_check", None), ("receipt", None),
                                ("complete_authenticated_onboarding", False)):
                stack.enter_context(patch.object(cli, name, return_value=value))
            stack.enter_context(patch.object(cli, "SSHTunnel"))
            if hasattr(cli, "MACOS"):
                stack.enter_context(patch.object(cli, "MACOS", macos))
            if macos:
                stack.enter_context(patch("claude_launcher.macos.seatbelt_present", return_value=True))
                stack.enter_context(patch.object(cli, "_isolated", return_value=
                                                contextlib.nullcontext((["fixture"], environment))))
            else:
                stack.enter_context(patch.object(cli, "sandbox_command",
                                                return_value=(["fixture"], environment)))
            supervise = stack.enter_context(patch.object(cli, "run_supervised",
                                                         return_value=(0, "child_exited")))
            audit = Mock(operation="run")
            self.assertEqual(cli._launch(self.config, self.home, arguments or [], audit,
                                         skills_mode=True), 0)
            self.assertIsNone(supervise.call_args.args[2])
        return environment

    def test_only_imported_token_reaches_daily_child_and_not_ssh_or_ambient_env(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": OTHER,
                                     "ANTHROPIC_API_KEY": "synthetic-api-key"}):
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", clean_environment())
            self.assertNotIn("ANTHROPIC_API_KEY", clean_environment())
            self.assertEqual(self.exercise_launch()["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
            self.assertEqual(os.environ["CLAUDE_CODE_OAUTH_TOKEN"], OTHER)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.exercise_launch(["setup-token"]))

    def test_macos_child_injection_uses_the_same_explicit_token_boundary(self):
        if not hasattr(cli, "_isolated"):
            self.skipTest("macOS backend lives on macos-support")
        self.assertEqual(self.exercise_launch(macos=True)["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)

    @unittest.skipUnless(sys.platform == "linux", "Linux namespace test")
    def test_token_reaches_isolated_child_with_proxy_and_no_direct_route(self):
        credentials.store_token(self.profile, TOKEN)
        workspace = self.home / "workspace"
        workspace.mkdir()
        upstream = self.home / "unused.sock"
        upstream.touch()
        script = (
            "import os,socket; from pathlib import Path; "
            f"assert os.environ['CLAUDE_CODE_OAUTH_TOKEN']=={TOKEN!r}; "
            "assert os.environ['HTTPS_PROXY']=='http://127.0.0.1:17878'; "
            "assert len(Path('/proc/net/route').read_text().splitlines())==1; "
            "print('SYNTHETIC_TOKEN_NAMESPACE_OK')"
        )
        command, environment = sandbox_command(
            upstream, self.profile, workspace, ["/usr/bin/python3", "-c", script],
        )
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = credentials.read_token(self.profile)
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "SYNTHETIC_TOKEN_NAMESPACE_OK")


if __name__ == "__main__":
    unittest.main()
