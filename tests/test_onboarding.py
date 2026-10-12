"""First-run repair uses synthetic state only, never authenticates."""

import json
import tempfile
import unittest
from pathlib import Path

from claude_launcher.onboarding import complete_authenticated_onboarding
from claude_launcher.wire import RouteError


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = Path(self.temporary.name)
        self.directory = self.profile / ".claude"
        self.directory.mkdir()
        self.path = self.directory / ".claude.json"
        self.credentials = self.directory / ".credentials.json"
        self.executable = self.profile / "2.1.274"
        self.original = {"oauthAccount": {"fixture": True}, "projects": {"fixture": {}},
                         "theme": "dark", "custom": [1, 2]}
        self.path.write_text(json.dumps(self.original))
        self.credentials.write_text('{"fixture":"unchanged"}')

    def repair(self):
        return complete_authenticated_onboarding(self.profile, self.executable)

    def test_repair_preserves_credentials_account_and_other_fields(self):
        credentials = self.credentials.read_bytes()
        original = self.path.read_bytes()
        self.assertTrue(self.repair())
        result = json.loads(self.path.read_text())
        self.assertTrue(result.pop("hasCompletedOnboarding"))
        self.assertEqual(result.pop("lastOnboardingVersion"), "2.1.274")
        self.assertEqual(result, self.original)
        self.assertEqual(self.credentials.read_bytes(), credentials)
        backup = self.directory / ".claude.json.before-onboarding-repair"
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.repair())

    def test_missing_login_is_not_marked_complete(self):
        self.credentials.unlink()
        self.assertFalse(self.repair())
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_missing_account_metadata_is_not_marked_complete(self):
        self.path.write_text('{}')
        self.assertFalse(self.repair())

    def test_invalid_or_symlinked_state_is_not_overwritten(self):
        self.path.write_text('invalid')
        with self.assertRaises(RouteError):
            self.repair()
        self.path.unlink()
        self.path.symlink_to(self.credentials)
        with self.assertRaises(RouteError):
            self.repair()


if __name__ == "__main__":
    unittest.main()
