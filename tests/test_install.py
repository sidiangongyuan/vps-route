"""Installer channel selection without downloading or executing an installer."""

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from claude_launcher import install
from claude_launcher import cli
from claude_launcher.config import Config
from claude_launcher.wire import RouteError


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="install-state-fixture-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.config = Config("203.0.113.1", "fixture", 22, self.home / "key",
                             self.home / "hosts", "/fixture.sock", "1.1.1.1", "US")

    def native_entry(self):
        profile = self.config.profile_home
        profile.mkdir(parents=True, mode=0o700)
        target = profile / ".local/share/claude/versions/fixture"
        target.parent.mkdir(parents=True)
        target.write_text("synthetic binary, never executed")
        target.chmod(0o700)
        entry = self.config.claude_executable
        entry.parent.mkdir(parents=True)
        entry.symlink_to(target)
        return target

    def test_existing_native_install_skips_network_and_preserves_account(self):
        target = self.native_entry()
        account = self.config.profile_home / ".claude/.credentials.json"
        account.parent.mkdir()
        account.write_text("synthetic login")
        with patch.object(cli, "_launch") as launch, \
                patch.object(cli, "SSHTunnel") as tunnel:
            self.assertEqual(cli.launch(self.config, self.home, [], install=True), 0)
            self.assertEqual(cli.launch(self.config, self.home, [], install=True), 0)
            launch.assert_not_called()
            tunnel.assert_not_called()
        self.assertEqual(account.read_text(), "synthetic login")
        self.assertEqual(target.read_text(), "synthetic binary, never executed")

    def test_missing_entry_is_detected_without_resetting_partial_account_state(self):
        profile = self.config.profile_home
        profile.mkdir(parents=True, mode=0o700)
        account = profile / "synthetic-account"
        account.write_text("keep")
        self.assertFalse(install.installation_present(self.config))
        self.assertEqual(account.read_text(), "keep")

    def test_missing_install_uses_existing_supervised_route_under_exclusive_lock(self):
        with patch.object(cli, "AuditLog", return_value=Mock()), \
                patch.object(cli, "maintenance_barrier",
                             return_value=contextlib.nullcontext()) as barrier, \
                patch.object(cli, "SessionLease", return_value=contextlib.nullcontext()), \
                patch.object(cli, "_launch", return_value=0) as launch:
            self.assertEqual(cli.launch(self.config, self.home, [], install=True), 0)
            barrier.assert_called_once_with(exclusive=True)
            self.assertTrue(launch.call_args.kwargs["install"])

    def test_invalid_or_external_native_entry_is_not_overwritten(self):
        target = self.native_entry()
        target.chmod(0o600)
        with self.assertRaisesRegex(RouteError, "installation_invalid"):
            cli.launch(self.config, self.home, [], install=True)
        target.chmod(0o700)
        target.unlink()
        with self.assertRaisesRegex(RouteError, "installation_invalid"):
            cli.launch(self.config, self.home, [], install=True)
        self.config.claude_executable.unlink()
        self.config.claude_executable.symlink_to("/usr/bin/true")
        with self.assertRaisesRegex(RouteError, "must_stay_inside"):
            cli.launch(self.config, self.home, [], install=True)
        self.assertTrue(self.config.claude_executable.is_symlink())

    def test_missing_entry_with_external_install_directory_is_refused(self):
        profile = self.config.profile_home
        profile.mkdir(parents=True, mode=0o700)
        (profile / ".local").symlink_to(self.home / "external")
        with self.assertRaisesRegex(RouteError, "install_directory_must_not_be_symlinked"):
            install.installation_present(self.config)

    def test_install_rechecks_after_acquiring_maintenance_lock(self):
        with patch.object(cli, "namespace_available") as namespace, \
                patch.object(install, "installation_present", return_value=True):
            audit = SimpleNamespace()
            self.assertEqual(cli._launch(self.config, self.home, [], audit, install=True), 0)
            namespace.assert_not_called()
            self.assertEqual(audit.reason, "already_installed")

    def test_cli_repeat_install_never_creates_workspace_or_launches_child(self):
        self.native_entry()
        with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "install"]), \
                patch.object(cli.Config, "load", return_value=self.config), \
                patch.object(cli, "private_workspace") as workspace, \
                patch.object(cli, "_launch") as child:
            self.assertEqual(cli.main(), 0)
            workspace.assert_not_called()
            child.assert_not_called()

    def test_official_installer_uses_latest_channel(self):
        with tempfile.TemporaryDirectory(prefix="install-fixture-") as directory, \
             patch.object(Path, "home", return_value=Path(directory)), \
             patch.object(install.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             patch.object(install.subprocess, "call", return_value=0) as call:
            self.assertEqual(install.main(), 0)
            self.assertEqual(call.call_args.args[0][-1], "latest")

    def test_failed_download_never_executes_installer(self):
        with tempfile.TemporaryDirectory(prefix="install-fixture-") as directory, \
             patch.object(Path, "home", return_value=Path(directory)), \
             patch.object(install.subprocess, "run", return_value=SimpleNamespace(returncode=22)), \
             patch.object(install.subprocess, "call") as call:
            self.assertEqual(install.main(), 22)
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
