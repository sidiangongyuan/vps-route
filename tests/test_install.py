"""Installer channel selection without downloading or executing an installer."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claude_launcher import install


class InstallTests(unittest.TestCase):
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
