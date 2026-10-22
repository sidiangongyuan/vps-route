"""Package mounts and system-Python child imports after relocation."""

import json
import shutil
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from claude_launcher import runner


class PackageRuntimeTests(unittest.TestCase):
    def test_generated_browser_entry_is_executable_and_refuses_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary).resolve()
            hook = runner.prepare_browser_hook(profile)
            result = subprocess.run(
                [str(hook), "https://claude.com/cai/oauth/authorize?state=fixture"],
                capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout + result.stderr, b"")
            self.assertEqual(hook.stat().st_mode & 0o777, 0o700)
            self.assertEqual(runner.prepare_browser_hook(profile), hook)
            hook.write_text("unrelated user file")
            from claude_launcher.wire import RouteError
            with self.assertRaisesRegex(RouteError, "unsafe_or_outdated"):
                runner.prepare_browser_hook(profile)
            self.assertEqual(hook.read_text(), "unrelated user file")

    @unittest.skipUnless(sys.platform == "linux", "requires Linux network namespaces")
    def test_only_package_is_exposed_and_venv_path_survives_private_home_masks(self):
        with tempfile.TemporaryDirectory(prefix="vps-route-package-") as temporary:
            home = Path(temporary).resolve()
            package = home / ".local/pipx/venvs/route/lib/site-packages/claude_launcher"
            package.parent.mkdir(parents=True)
            shutil.copytree(runner.PACKAGE_DIR, package, ignore=shutil.ignore_patterns("__pycache__"))
            profile = home / ".local/share/claude-launcher/profiles/fixture"
            profile.mkdir(parents=True)
            workspace = home / "workspace"
            workspace.mkdir()
            socket = home / "unused.sock"
            socket.touch()
            old = home / ".claude"
            old.mkdir()
            (old / ".credentials.json").write_text("synthetic old token")
            (package.parent / "unrelated-package").mkdir()
            (package.parent / "unrelated-package/secret").write_text("unrelated venv data")
            script = (
                "import os,socket,sys; from pathlib import Path; "
                f"sys.path.insert(0,{str(package.parent)!r}); "
                "import claude_launcher.wire; "
                f"assert not Path({str(old / '.credentials.json')!r}).exists(); "
                f"assert not Path({str(package.parent / 'unrelated-package/secret')!r}).exists(); "
                f"assert os.statvfs({str(package)!r}).f_flag & os.ST_RDONLY; "
                "assert os.environ['HTTPS_PROXY']=='http://127.0.0.1:17878'; "
                "assert len(Path('/proc/net/route').read_text().splitlines())==1; "
                "print('PACKAGED_RUNTIME_OK')"
            )
            with patch.object(runner.Path, "home", return_value=home), \
                 patch.object(runner, "PACKAGE_DIR", package), \
                 patch.object(runner, "ENTRY_DIR", package / "entries"):
                args, env = runner.sandbox_command(
                    socket, profile, workspace, ["/usr/bin/python3", "-c", script],
                    research_filesystem=True,
                )
            self.assertNotIn(str(package.parent), args)
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("PACKAGED_RUNTIME_OK", result.stdout)
            self.assertEqual((old / ".credentials.json").read_text(), "synthetic old token")


if __name__ == "__main__":
    unittest.main()
