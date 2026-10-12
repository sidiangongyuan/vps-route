"""Manual official-URL login and isolated fake-CLI integration; no live account."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.entries.browser_hook import official_login_url
from claude_launcher.runner import ENTRY_DIR
from claude_launcher.runner import clean_environment, run_supervised, sandbox_command


ROOT = Path(__file__).resolve().parent.parent


class AuthTests(unittest.TestCase):
    def test_only_official_https_authorization_urls_are_accepted(self):
        good = "https://claude.ai/oauth/authorize?state=fixture"
        self.assertTrue(official_login_url(good))
        for candidate in (
            good.replace("claude.ai", "claude.ai.attacker.invalid"),
            good.replace("https:", "http:", 1),
            good.replace("/authorize", "/other"),
            good.replace("https://", "https://user:pass@"),
            good + "\x1b[31m",
        ):
            self.assertFalse(official_login_url(candidate), candidate)

    def test_browser_hook_is_silent(self):
        result = subprocess.run(
            ["/usr/bin/python3", str(ENTRY_DIR / "browser_hook.py"),
             "https://claude.com/cai/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A34961%2Fcallback"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_unrelated_credentials_and_runtime_injection_not_inherited(self):
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "fixture", "NODE_OPTIONS": "fixture",
                                     "SSH_CONNECTION": "fixture", "GITHUB_TOKEN": "fixture",
                                     "HICC_TEST": "fixture", "TZ": "UTC"}):
            result = clean_environment()
        for name in ("AWS_SECRET_ACCESS_KEY", "NODE_OPTIONS", "SSH_CONNECTION", "GITHUB_TOKEN",
                     "HICC_TEST", "TZ"):
            self.assertNotIn(name, result)

    def exercise_login(self, authorization_code: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="auth-fixture-") as temporary:
            root = Path(temporary)
            profile, workspace = root / "profile", root / "workspace"
            profile.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            upstream = root / "unused-socket"
            upstream.touch()
            command, environment = sandbox_command(
                upstream, profile, workspace,
                ["/usr/bin/python3", str(workspace / "fake_auth_cli.py")], login=True,
            )
            import shutil
            shutil.copyfile(ROOT / "tests/fake_auth_cli.py", workspace / "fake_auth_cli.py")
            self.assertEqual(environment["BROWSER"],
                             str(profile / ".launcher-runtime/browser-hook"))
            self.assertTrue(os.access(environment["BROWSER"], os.X_OK))
            self.assertNotIn("VPS_ROUTE_AUTH_DIR", environment)
            reader, writer = os.pipe()
            try:
                os.write(writer, authorization_code.encode() + b"\n")
                os.close(writer)
                writer = -1
                with tempfile.TemporaryFile() as output:
                    code, _ = run_supervised(
                        command, environment, lambda: None, lambda: None, lambda: True, 0.2,
                        input_fd=reader, output_fd=output.fileno(),
                    )
                    output.seek(0)
                    transcript = output.read().decode(errors="replace")
                self.assertIn("https://claude.ai/oauth/authorize?", transcript)
                self.assertEqual(transcript.count("https://claude.ai/oauth/authorize?"), 1)
                self.assertNotIn("localhost", transcript)
                self.assertNotIn("Official Claude login URL", transcript)
                self.assertIn("Paste code here if prompted", transcript)
                self.assertEqual((profile / "synthetic-auth-result").exists(), code == 0)
                return code, transcript
            finally:
                os.close(reader)
                if writer != -1:
                    os.close(writer)

    def test_manual_code_roundtrip(self):
        code, transcript = self.exercise_login("synthetic-code#synthetic-state-123")
        self.assertEqual(code, 0, transcript)
        self.assertIn("SYNTHETIC_AUTH_COMPLETED", transcript)

    def test_wrong_code_is_rejected_by_client(self):
        code, transcript = self.exercise_login("wrong-code#synthetic-state-123")
        self.assertEqual(code, 3, transcript)
        self.assertIn("SYNTHETIC_AUTH_REJECTED", transcript)


if __name__ == "__main__":
    unittest.main()
