"""Only disposable-home tests for a future HICC retirement; no live account."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.legacy_cutover import (
    activate_wrapper, archive_legacy_state, legacy_inventory,
)
from claude_launcher.wire import RouteError


class LegacyCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="legacy-cutover-fixture-")
        self.home = Path(self.temporary.name)
        (self.home / ".claude").mkdir(mode=0o700)
        (self.home / ".claude/old-session").write_text("fixture only")
        (self.home / ".claude.json").write_text("fixture only")
        (self.home / ".hicc").mkdir(mode=0o700)
        (self.home / ".hicc/old-state").write_text("fixture only")
        (self.home / ".cache/claude-cli-nodejs").mkdir(parents=True)
        (self.home / ".cache/claude-cli-nodejs/old-cache").write_text("fixture only")
        bin_dir = self.home / ".local/bin"
        bin_dir.mkdir(parents=True)
        self.wrapper = bin_dir / "claude"
        self.wrapper.write_text("#!/bin/sh\necho OLD_WRAPPER\n")
        self.wrapper.chmod(0o700)
        route = bin_dir / "vps-route"
        route.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        route.chmod(0o700)
        self.running = patch(
            "claude_launcher.legacy_cutover._same_user_legacy_process_running",
            return_value=False,
        )
        self.running.start()

    def tearDown(self):
        self.running.stop()
        self.temporary.cleanup()

    def test_archive_moves_only_named_legacy_paths_and_keeps_private_manifest(self):
        before = legacy_inventory(self.home)
        self.assertEqual(set(before["present"]),
                         {"claude-home", "claude-global", "hicc-home", "claude-cli-cache"})
        self.assertFalse(before["legacy_process_running"])
        unrelated = self.home / "project.py"
        unrelated.write_text("source remains")

        archive = archive_legacy_state(self.home)
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".claude.json").exists())
        self.assertFalse((self.home / ".hicc").exists())
        self.assertFalse((self.home / ".cache/claude-cli-nodejs").exists())
        self.assertEqual((archive / "claude-home/old-session").read_text(), "fixture only")
        self.assertEqual((archive / "hicc-home/old-state").read_text(), "fixture only")
        self.assertEqual((archive / "claude-cli-cache/old-cache").read_text(), "fixture only")
        self.assertEqual(unrelated.read_text(), "source remains")
        self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        self.assertEqual((archive / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads((archive / "manifest.json").read_text())["items"]),
                         set(before["present"]))
        with self.assertRaisesRegex(RouteError, "no_legacy_state_present"):
            archive_legacy_state(self.home)

    def test_active_hicc_refuses_archive_without_moving_anything(self):
        with patch("claude_launcher.legacy_cutover._same_user_legacy_process_running",
                   return_value=True):
            with self.assertRaisesRegex(RouteError, "still_running"):
                archive_legacy_state(self.home)
        self.assertTrue((self.home / ".claude.json").exists())

    def test_symlinked_legacy_path_is_rejected(self):
        outside = self.home / "outside"
        outside.mkdir()
        (self.home / ".claude.json").unlink()
        (self.home / ".claude.json").symlink_to(outside)
        with self.assertRaisesRegex(RouteError, "unsafe_legacy_path"):
            legacy_inventory(self.home)

    def test_wrapper_dispatches_normal_login_and_update_after_explicit_archive(self):
        archive = archive_legacy_state(self.home)
        backup = activate_wrapper(archive, self.home)
        self.assertIn("OLD_WRAPPER", backup.read_text())
        self.assertNotIn("OLD_WRAPPER", self.wrapper.read_text())
        project = self.home / "project"
        project.mkdir()
        cases = {
            ("auth", "login"): ["auth", "login"],
            ("auth", "status", "--text"): ["auth", "status", "--text"],
            ("update",): ["update"],
            ("install",): ["install"],
            ("--version",): ["native-version"],
        }
        for arguments, expected in cases.items():
            result = subprocess.run([str(self.wrapper), *arguments], cwd=project,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), expected)

    def test_wrapper_activation_refuses_live_hicc(self):
        archive = archive_legacy_state(self.home)
        with patch("claude_launcher.legacy_cutover._same_user_legacy_process_running",
                   return_value=True):
            with self.assertRaisesRegex(RouteError, "still_running"):
                activate_wrapper(archive, self.home)
        self.assertIn("OLD_WRAPPER", self.wrapper.read_text())


if __name__ == "__main__":
    unittest.main()
