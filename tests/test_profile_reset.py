"""Reset behavior with disposable state only; no live credentials or network."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claude_launcher.profile_reset import full_reset_inventory, reset_local_state
from claude_launcher.wire import RouteError


class ProfileResetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="full-reset-fixture-")
        self.home = Path(self.temporary.name).resolve()
        self.profile = self.home / ".local/share/claude-launcher/profiles/fixture"
        self.profile.mkdir(parents=True)
        (self.profile / ".claude").mkdir()
        (self.profile / ".claude/.credentials.json").write_text("fixture only")
        (self.profile / ".claude/history.jsonl").write_text("fixture only")
        (self.profile / ".claude.json").write_text("fixture only")
        (self.home / ".claude").mkdir()
        (self.home / ".claude.json").write_text("fixture only")
        # The real Orca integration is a child link, not a link to all account state.
        (self.home / ".claude/projects").symlink_to(self.profile / ".claude/projects")
        self.config = SimpleNamespace(profile_home=self.profile, profile="fixture")
        self.patches = [
            patch.object(Path, "home", return_value=self.home),
            patch("claude_launcher.profile_reset._profile_is_running", return_value=False),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_preview_does_not_move_any_state(self):
        result = full_reset_inventory(self.config)
        self.assertTrue(result["profile_present"])
        self.assertTrue(result["existing_claude_outside_tool_unchanged"])
        self.assertNotIn("legacy_present", result)
        self.assertTrue((self.profile / ".claude/.credentials.json").exists())
        self.assertFalse((self.profile.parent / ".archives").exists())

    def test_full_reset_archives_only_tool_profile_and_keeps_host_claude(self):
        preserved = self.home / "project.py"
        preserved.write_text("source remains")
        shared = self.home / ".agents/skills/example"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text("generic instructions")
        result = reset_local_state(self.config)
        archived = Path(result["profile_archive"])
        self.assertNotIn("legacy_archive", result)
        self.assertTrue((archived / ".claude/.credentials.json").is_file())
        # The user's own Claude installation outside the tool is never moved.
        self.assertTrue((self.home / ".claude/projects").is_symlink())
        self.assertEqual((self.home / ".claude.json").read_text(), "fixture only")
        self.assertFalse((self.home / ".local/share/claude-launcher/legacy-archives").exists())
        self.assertFalse((self.profile / ".claude/.credentials.json").exists())
        self.assertFalse((self.profile / ".claude.json").exists())
        settings = self.profile / ".claude/settings.json"
        self.assertEqual(json.loads(settings.read_text()), {"autoUpdatesChannel": "latest"})
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archived.stat().st_mode & 0o777, 0o700)
        self.assertEqual(preserved.read_text(), "source remains")
        self.assertTrue((shared / "SKILL.md").exists())

    def test_running_profile_refuses_before_any_move(self):
        with patch("claude_launcher.profile_reset._profile_is_running", return_value=True):
            with self.assertRaisesRegex(RouteError, "still_running"):
                reset_local_state(self.config)
        self.assertTrue((self.home / ".claude.json").exists())
        self.assertTrue((self.profile / ".claude/.credentials.json").exists())

    def test_symlinked_host_claude_state_does_not_block_or_get_moved(self):
        (self.home / ".claude.json").unlink()
        (self.home / ".claude.json").symlink_to(self.home / "unrelated")
        result = reset_local_state(self.config)
        self.assertTrue((self.home / ".claude.json").is_symlink())
        self.assertTrue((Path(result["profile_archive"]) / ".claude/.credentials.json").exists())

    def test_symlinked_profile_ancestor_is_rejected(self):
        alias = self.home / "alias"
        alias.symlink_to(self.profile.parent)
        self.config.profile_home = alias / "fixture"
        with self.assertRaisesRegex(RouteError, "unsafe_profile_path"):
            reset_local_state(self.config)

    def test_second_reset_archives_the_fresh_profile_again(self):
        reset_local_state(self.config)
        second = reset_local_state(self.config)
        self.assertIsNotNone(second["profile_archive"])
        self.assertEqual(os.listdir(self.profile), [".claude"])


if __name__ == "__main__":
    unittest.main()
