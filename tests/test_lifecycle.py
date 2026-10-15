"""Permanent cleanup tested only in disposable homes, with package/VPS calls mocked."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.cli import main
from claude_launcher.lifecycle import (
    _package_command as detect_package_command, uninstall_inventory, uninstall_local,
)
from claude_launcher.wire import RouteError


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lifecycle-fixture-")
        self.home = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))
        self.stack.enter_context(patch(
            "claude_launcher.lifecycle._profile_is_running", return_value=False))
        self.stack.enter_context(patch("claude_launcher.lifecycle._package_command", return_value=None))
        self.profile = self.home / ".local/share/claude-launcher/profiles/default"
        self.archive = self.profile.parent / ".archives/fixture"
        self.legacy = self.home / ".local/share/claude-launcher/legacy-archives/fixture"
        self.logs = self.home / ".local/state/claude-launcher/logs"
        self.config = self.home / ".config/vps-route/config.json"
        for path in (self.profile, self.archive, self.legacy, self.logs, self.config.parent):
            path.mkdir(parents=True)
            (path / "fixture").write_text("synthetic state")
        self.config.write_text("deliberately broken config; cleanup must still work")
        self.outside = self.home / "project"
        self.outside.mkdir()
        (self.outside / "source.py").write_text("preserve source")
        (self.profile / "project-link").symlink_to(self.outside, target_is_directory=True)
        self.key = self.home / ".ssh/key"
        self.key.parent.mkdir()
        self.key.write_text("synthetic key")
        self.shared = self.home / ".agents/skills/example/SKILL.md"
        self.shared.parent.mkdir(parents=True)
        self.shared.write_text("preserve shared instructions")
        self.server = self.home / ".local/share/claude-launcher/server/remote.py"
        self.server.parent.mkdir()
        self.server.write_text("do not change the server")
        self.host_claude = self.home / ".local/bin/claude"
        self.host_claude.parent.mkdir(parents=True)
        self.host_claude.write_text("#!/bin/sh\necho user's own Claude CLI\n")

    def test_preview_lists_profiles_and_backups_without_reading_config_or_deleting(self):
        result = uninstall_inventory(self.config)
        self.assertIn(str(self.profile), result["delete_paths"])
        self.assertIn(str(self.archive.parent), result["delete_paths"])
        self.assertTrue(result["local_only"])
        self.assertTrue(self.config.exists())

    def test_default_permanently_removes_backups_and_preserves_unrelated_files(self):
        with patch("claude_launcher.lifecycle.subprocess.run") as run:
            result = uninstall_local(self.config, yes=True)
        run.assert_not_called()
        for path in (self.profile, self.archive, self.legacy, self.logs, self.config):
            self.assertFalse(path.exists(), path)
        for path in (self.outside / "source.py", self.key, self.shared, self.server,
                     self.host_claude):
            self.assertTrue(path.exists(), path)
        self.assertFalse(result["vps_changed"])

    def test_keep_flags_preserve_selected_data(self):
        uninstall_local(self.config, yes=True, keep_backups=True, keep_logs=True,
                        keep_config=True, keep_package=True)
        self.assertFalse(self.profile.exists())
        for path in (self.archive, self.legacy, self.logs, self.config):
            self.assertTrue(path.exists())

    def test_keep_profiles_does_not_implicitly_keep_backups(self):
        uninstall_local(self.config, yes=True, keep_profiles=True)
        self.assertTrue(self.profile.exists())
        self.assertFalse(self.archive.exists())

    def test_declining_and_running_processes_refuse_before_deletion(self):
        with self.assertRaisesRegex(RouteError, "operation_cancelled"):
            uninstall_local(self.config, prompt=lambda _: "n")
        with patch("claude_launcher.lifecycle._profile_is_running", return_value=True):
            with self.assertRaisesRegex(RouteError, "still_running"):
                uninstall_local(self.config, yes=True)
        self.assertTrue(self.profile.exists())
        self.assertTrue(self.config.exists())

    def test_symlinked_config_root_refuses_without_following_or_partial_cleanup(self):
        for path in self.config.parent.iterdir():
            path.unlink()
        self.config.parent.rmdir()
        self.config.parent.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(RouteError, "unsafe_tool_path"):
            uninstall_local(self.config, yes=True)
        self.assertTrue(self.profile.exists())
        self.assertTrue((self.outside / "source.py").exists())

    def test_host_claude_binary_and_processes_do_not_matter(self):
        self.host_claude.unlink()
        self.host_claude.symlink_to(self.outside / "source.py")
        # Only processes started from tool profiles block cleanup.
        with patch("claude_launcher.sessions.list_sessions", return_value=[]):
            self.assertFalse(uninstall_inventory(self.config)["process_running"])
        uninstall_local(self.config, yes=True)
        self.assertTrue(self.host_claude.is_symlink())
        self.assertTrue((self.outside / "source.py").exists())

    def test_configured_key_in_data_root_requires_explicit_preservation(self):
        protected = self.profile / "key"
        protected.write_text("synthetic key")
        self.config.write_text(json.dumps({
            "ssh_host": "203.0.113.1", "ssh_user": "fixture", "ssh_port": 22,
            "identity_file": str(protected), "known_hosts": str(self.key),
            "remote_socket": "/fixture/connect.sock", "expected_ip": "1.1.1.1",
            "expected_country": "US",
        }))
        with self.assertRaisesRegex(RouteError, "ssh_material_inside_cleanup_target"):
            uninstall_local(self.config, yes=True)
        self.assertTrue(self.config.exists())
        self.assertTrue(protected.exists())
        uninstall_local(self.config, yes=True, keep_profiles=True)
        self.assertTrue(protected.exists())

    def test_custom_config_inside_home_removed_but_outside_home_rejected(self):
        custom = self.home / "custom.json"
        custom.write_text(json.dumps({
            "ssh_host": "203.0.113.1", "ssh_user": "fixture", "ssh_port": 22,
            "identity_file": str(self.key), "known_hosts": str(self.key),
            "remote_socket": "/fixture/connect.sock", "expected_ip": "1.1.1.1",
            "expected_country": "US",
        }))
        uninstall_local(custom, yes=True)
        self.assertFalse(custom.exists())
        with self.assertRaisesRegex(RouteError, "outside_home"):
            uninstall_inventory(self.home.parent / "unrelated.json")

    def test_custom_config_flag_cannot_delete_an_unrelated_key_or_document(self):
        with self.assertRaisesRegex(RouteError, "custom_config_not_identifiable"):
            uninstall_local(self.key, yes=True)
        self.assertTrue(self.key.exists())
        self.assertTrue(self.profile.exists())

    def test_nested_custom_config_is_not_deleted_twice(self):
        custom = self.profile / "custom-config.json"
        custom.write_text("fixture")
        inventory = uninstall_inventory(custom)
        self.assertNotIn(str(custom), inventory["delete_paths"])
        uninstall_local(custom, yes=True)
        self.assertFalse(self.profile.exists())

    def test_mount_refusal_happens_before_any_deletion(self):
        original = __import__("os").path.ismount
        with patch("claude_launcher.lifecycle.os.path.ismount",
                   side_effect=lambda path: Path(path) == self.logs or original(path)):
            with self.assertRaisesRegex(RouteError, "mount"):
                uninstall_local(self.config, yes=True)
        self.assertTrue(self.profile.exists())

    def test_files_changing_during_confirmation_require_another_review(self):
        def answer(_: str) -> str:
            (self.profile.parent / "second-profile").mkdir()
            return "y"
        with self.assertRaisesRegex(RouteError, "state_changed"):
            uninstall_local(self.config, prompt=answer)
        self.assertTrue(self.profile.exists())

    def test_package_manager_called_only_after_confirmation_and_data_removal(self):
        with patch("claude_launcher.lifecycle._package_command", return_value=["fixture-manager"]), \
                patch("claude_launcher.lifecycle.subprocess.run") as run:
            run.return_value.returncode = 0
            uninstall_local(self.config, yes=True)
        run.assert_called_once_with(["fixture-manager"], check=False)
        self.assertFalse(self.profile.exists())

    def test_cli_dry_run_accepts_broken_config_and_never_contacts_vps(self):
        with patch("sys.argv", ["vps-route", "uninstall", "--dry-run"]), \
                patch("claude_launcher.lifecycle.subprocess.run") as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertTrue(json.loads(output.getvalue())["local_only"])
        self.assertTrue(self.profile.exists())
        run.assert_not_called()

    def test_package_manager_uses_pipx_or_venv_and_refuses_system_python(self):
        prefix = self.home / "environment"
        prefix.mkdir()
        with patch("claude_launcher.lifecycle.importlib.metadata.distribution"), \
                patch("claude_launcher.lifecycle.sys.prefix", str(prefix)), \
                patch("claude_launcher.lifecycle.sys.base_prefix", "/fixture-system"):
            command = detect_package_command()
            self.assertEqual(command[-5:], ["-m", "pip", "uninstall", "-y", "vps-route"])
            metadata = prefix / "pipx_metadata.json"
            metadata.write_text(json.dumps({"main_package": {"package": "vps-route"}}))
            with patch("claude_launcher.lifecycle.shutil.which", return_value="/fixture/pipx"):
                self.assertEqual(detect_package_command(), ["/fixture/pipx", "uninstall", "vps-route"])
        with patch("claude_launcher.lifecycle.importlib.metadata.distribution"), \
                patch("claude_launcher.lifecycle.sys.prefix", "/fixture-system"), \
                patch("claude_launcher.lifecycle.sys.base_prefix", "/fixture-system"):
            with self.assertRaisesRegex(RouteError, "system_python_uninstall"):
                detect_package_command()


if __name__ == "__main__":
    unittest.main()
