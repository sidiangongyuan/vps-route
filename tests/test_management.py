"""Config operations with fake connection data and no live account reads."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.config import Config
from claude_launcher.management import config_values, private_json, set_config
from claude_launcher.wire import RouteError


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "config.json"
        self.values = {
            "ssh_host": "203.0.113.1", "ssh_user": "fixture", "ssh_port": 22,
            "identity_file": str(self.root / "key"), "known_hosts": str(self.root / "hosts"),
            "remote_socket": "/home/fixture/private/connect.sock", "expected_ip": "1.1.1.1",
            "expected_country": "US",
        }
        private_json(self.path, self.values)

    def tearDown(self):
        self.temporary.cleanup()

    def test_show_redacts_identifying_values_and_reveal_is_explicit(self):
        config = Config.load(self.path)
        shown = config_values(config)
        self.assertEqual(shown["ssh_host"], "xxx")
        self.assertEqual(shown["identity_file"], "xxx")
        self.assertEqual(shown["profile"], "xxx")
        self.assertEqual(config_values(config, reveal=True)["ssh_host"], "203.0.113.1")

    def test_change_previews_then_validates_and_writes_private_file(self):
        before = self.path.read_bytes()
        self.assertFalse(set_config(self.path, "timeout_seconds", "12")["applied"])
        self.assertEqual(self.path.read_bytes(), before)
        result = set_config(self.path, "timeout_seconds", "12", apply=True)
        self.assertTrue(result["existing_sessions_unchanged"])
        self.assertEqual(Config.load(self.path).timeout_seconds, 12)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        set_config(self.path, "timezone", "Asia/Shanghai", apply=True)
        self.assertEqual(Config.load(self.path).timezone, "Asia/Shanghai")
        set_config(self.path, "timezone", "null", apply=True)
        self.assertIsNone(Config.load(self.path).timezone)

    def test_invalid_change_does_not_replace_config(self):
        before = self.path.read_bytes()
        for key, text in [("ssh_port", "true"), ("timeout_seconds", "NaN"),
                          ("timeout_seconds", "null"), ("timezone", "Unknown/Zone"),
                          ("probe_url", "12"), ("ssh_host", "1"),
                          ("ssh_http_proxy", "12"),
                          ("unknown", "value"), ("ssh_http_proxy", "http://evil:8080")]:
            with self.subTest(key=key, text=text), self.assertRaises(RouteError):
                set_config(self.path, key, text, apply=True)
            self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_hardlink_shared_file_and_parent_are_refused(self):
        linked = self.root / "linked.json"
        linked.symlink_to(self.path)
        with self.assertRaisesRegex(RouteError, "unsafe_management"):
            private_json(linked, {})
        self.path.chmod(0o644)
        with self.assertRaisesRegex(RouteError, "unsafe_management"):
            private_json(self.path, {})
        self.path.chmod(0o600)
        parent = self.root / "linked-parent"
        parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RouteError, "symlinked"):
            private_json(parent / "report.json", {})

    def test_concurrent_replace_is_detected(self):
        original = Path.lstat
        calls = []

        def swapped(path):
            if path == self.path:
                calls.append(True)
                if len(calls) == 2:
                    self.path.write_text("concurrent user edit")
            return original(path)

        with patch.object(Path, "lstat", swapped):
            with self.assertRaisesRegex(RouteError, "management_file_changed"):
                private_json(self.path, {"new": 1})
        self.assertEqual(self.path.read_text(), "concurrent user edit")
