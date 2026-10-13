"""CLI policy routing without starting the real route or account client."""

import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher import cli
from claude_launcher.config import Config


class CliPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("203.0.113.1", "fixture", 22, Path("unused"), Path("unused"),
                             "/unused", "203.0.113.7", "US")
        barrier = patch.object(cli, "maintenance_barrier", return_value=contextlib.nullcontext())
        barrier.start()
        self.addCleanup(barrier.stop)

    def test_run_defaults_to_skills_with_optional_safe_mode_and_unmodified_update(self):
        for command, expected in [(["run", "--", "--version"], (False, False, True)),
                                  (["run", "--safe-mode"], (True, False, False)),
                                  (["run", "--", "--safe-mode"], (True, False, False)),
                                  (["update"], (False, True, False))]:
            with patch.object(sys, "argv", ["vps-route", "--config", "/unused", *command]), \
                 patch.object(cli.Config, "load", return_value=self.config), \
                 patch.object(cli, "launch", return_value=0) as launch, \
                 patch.object(Path, "mkdir"):
                self.assertEqual(cli.main(), 0)
            self.assertEqual((launch.call_args.kwargs.get("safe_mode"),
                              launch.call_args.kwargs.get("verify_after"),
                              launch.call_args.kwargs.get("skills_mode")), expected)

    def test_native_version_and_update_use_private_workspace(self):
        for command, arguments in [("native-version", ["--version"]),
                                   ("update", ["update"])]:
            with patch.object(sys, "argv", ["vps-route", "--config", "/unused", command]), \
                 patch.object(cli.Config, "load", return_value=self.config), \
                 patch.object(cli, "launch", return_value=0) as launch, \
                 patch.object(Path, "mkdir"):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(launch.call_args.args[1],
                             self.config.profile_home / "auth-workspace")
            self.assertEqual(launch.call_args.args[2], arguments)

    def test_run_from_home_uses_separate_workspace_but_project_is_preserved(self):
        home = Path.home()
        cases = [([], home, self.config.profile_home / "default-workspace"),
                 (["--workspace", str(home)], home, self.config.profile_home / "default-workspace"),
                 ([], home / "project", home / "project")]
        for arguments, cwd, expected in cases:
            with self.subTest(arguments=arguments, cwd=cwd), \
                 patch.object(sys, "argv", ["vps-route", "--config", "/unused", "run", *arguments]), \
                 patch.object(cli.Config, "load", return_value=self.config), \
                 patch.object(Path, "cwd", return_value=cwd), \
                 patch.object(Path, "mkdir"), \
                 patch.object(cli, "launch", return_value=0) as launch:
                self.assertEqual(cli.main(), 0)
                self.assertEqual(launch.call_args.args[1], expected)
                self.assertFalse(launch.call_args.kwargs["safe_mode"])
                self.assertTrue(launch.call_args.kwargs["skills_mode"])

    def test_run_rejects_auth_login_outside_dedicated_command(self):
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused",
                                            "run", "--", "auth", "login"]), \
             patch.object(cli.Config, "load", return_value=self.config), \
             patch.object(cli, "launch") as launch:
            self.assertEqual(cli.main(), 69)
            launch.assert_not_called()

    def test_auth_login_uses_manual_url_mode_in_separate_workspace(self):
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "auth", "login"]), \
             patch.object(cli.Config, "load", return_value=self.config), \
             patch.object(cli, "launch", return_value=0) as launch, \
             patch.object(Path, "mkdir"), patch.object(Path, "is_file", return_value=True):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(launch.call_args.args[2], ["auth", "login"])
        self.assertEqual(launch.call_args.kwargs, {"login": True})
        self.assertEqual(launch.call_args.args[1],
                         self.config.profile_home / "auth-workspace")

    def test_legacy_archive_apply_requires_explicit_retirement_flag(self):
        with patch.object(sys, "argv", ["vps-route", "legacy-archive", "--apply"]), \
             patch("claude_launcher.legacy_cutover.archive_legacy_state") as archive:
            self.assertEqual(cli.main(), 69)
            archive.assert_not_called()

    def test_full_reset_defaults_to_preview_without_install_or_mutation(self):
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "reset"]), \
             patch.object(cli.Config, "load", return_value=self.config), \
             patch.object(cli, "full_reset_inventory", return_value={}) as preview, \
             patch.object(cli, "reset_local_state") as reset, \
             patch.object(cli, "launch") as launch:
            self.assertEqual(cli.main(), 0)
            preview.assert_called_once_with(self.config)
            reset.assert_not_called()
            launch.assert_not_called()

    def test_full_reset_installs_through_existing_route_without_login(self):
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "reset", "--apply", "-y"]), \
             patch.object(cli.Config, "load", return_value=self.config), \
             patch.object(cli, "full_reset_inventory", return_value={}), \
             patch.object(cli, "reset_local_state", return_value={}) as reset, \
             patch.object(cli, "launch", return_value=0) as launch, \
             patch.object(Path, "mkdir"):
            self.assertEqual(cli.main(), 0)
            reset.assert_called_once_with(self.config)
            self.assertEqual(launch.call_args.args[2], [])
            self.assertEqual(launch.call_args.kwargs, {"install": True, "verify_after": True})

    def test_full_reset_install_failure_is_not_reported_as_success(self):
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "reset", "--apply", "-y"]), \
             patch.object(cli.Config, "load", return_value=self.config), \
             patch.object(cli, "full_reset_inventory", return_value={}), \
             patch.object(cli, "reset_local_state", return_value={}), \
             patch.object(cli, "launch", return_value=69), \
             patch.object(Path, "mkdir"):
            self.assertEqual(cli.main(), 69)

    def test_shell_reuses_verified_ssh_options_without_starting_claude(self):
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "shell"]), \
             patch.object(cli.Config, "load", return_value=self.config), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(cli.subprocess, "call", return_value=0) as call, \
             patch.object(cli, "launch") as launch:
            self.assertEqual(cli.main(), 0)
            command = call.call_args.args[0]
            self.assertIn("-tt", command)
            self.assertNotIn("-T", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("IdentitiesOnly=yes", command)
            self.assertEqual(command[-1], self.config.ssh_host)
            launch.assert_not_called()

    def test_shell_refuses_background_stdin(self):
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "shell"]), \
             patch.object(cli.Config, "load", return_value=self.config), \
             patch.object(sys.stdin, "isatty", return_value=False), \
             patch.object(cli.subprocess, "call") as call:
            self.assertEqual(cli.main(), 69)
            call.assert_not_called()

    def test_shell_password_mode_prompts_without_embedding_a_password(self):
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "shell", "--password"]), \
             patch.object(cli.Config, "load", return_value=self.config), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(cli.subprocess, "call", return_value=0) as call:
            self.assertEqual(cli.main(), 0)
            command = call.call_args.args[0]
            self.assertIn("BatchMode=no", command)
            self.assertNotIn("BatchMode=yes", command)
            self.assertIn("PubkeyAuthentication=no", command)
            self.assertIn("StrictHostKeyChecking=yes", command)

    def test_auth_reset_defaults_to_preview_and_apply_requires_confirmation(self):
        for arguments, applied in [([], False), (["--dry-run"], False),
                                   (["--apply", "-y"], True)]:
            with patch.object(sys, "argv", ["vps-route", "--config", "/unused",
                                            "auth", "reset", *arguments]), \
                    patch.object(cli.Config, "load", return_value=self.config), \
                    patch("claude_launcher.profile_reset.profile_reset_inventory",
                          return_value={"profile_process_running": False}), \
                    patch.object(cli, "reset_profile", return_value=None) as reset:
                self.assertEqual(cli.main(), 0)
                self.assertEqual(reset.called, applied)

    def test_auth_reset_decline_and_running_process_never_rotate(self):
        for running in (False, True):
            with patch.object(sys, "argv", ["vps-route", "--config", "/unused",
                                            "auth", "reset", "--apply"]), \
                    patch.object(cli.Config, "load", return_value=self.config), \
                    patch("claude_launcher.profile_reset.profile_reset_inventory",
                          return_value={"profile_process_running": running}), \
                    patch("claude_launcher.lifecycle.confirm_operation",
                          side_effect=cli.RouteError("operation_cancelled")), \
                    patch.object(cli, "reset_profile") as reset:
                self.assertEqual(cli.main(), 69)
                reset.assert_not_called()

    def test_process_status_and_self_update_do_not_load_account_or_route_config(self):
        with patch.object(sys, "argv", ["vps-route", "status"]), \
                patch.object(cli, "list_sessions", return_value=[]), \
                patch.object(cli.Config, "load") as load:
            self.assertEqual(cli.main(), 0)
            load.assert_not_called()
        with patch.object(sys, "argv", ["vps-route", "self-update", "--check"]), \
                patch("claude_launcher.updates.self_update", return_value={}) as update, \
                patch.object(cli.Config, "load") as load:
            self.assertEqual(cli.main(), 0)
            update.assert_called_once_with(version=None, apply=False, rollback=False)
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
