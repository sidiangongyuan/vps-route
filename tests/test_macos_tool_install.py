"""Failure atomicity for known installer conflicts and native build targeting."""

import importlib.util
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_tool("dev_installer", "tools/macos-dev/install.py")
builder = load_tool("gh_builder", "tools/macos-gh/build.py")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = Path(temporary.name).resolve()
        go = self.profile / '.local/libexec/vps-route-go/go1.27.3/bin/go'
        go.parent.mkdir(parents=True)
        go.touch()
        self.pip = self.profile / '.config/pip/pip.conf'
        self.pip.parent.mkdir(parents=True)

    def install(self, *options):
        with patch.object(sys, 'argv', ['install', '--profile', str(self.profile), *options]):
            installer.main()

    def test_late_conflict_leaves_all_entries_unchanged(self):
        self.pip.write_text('user configuration')
        self.pip.chmod(0o600)
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(self.pip.read_text(), 'user configuration')
        self.assertEqual(list((self.profile / '.local/bin').iterdir()), [])

    def test_late_unsafe_file_does_not_partially_update_entries(self):
        self.install()
        go = self.profile / '.local/bin/go'
        go.write_text('existing entry')
        self.pip.chmod(0o644)
        with self.assertRaises(RuntimeError):
            self.install('--replace-existing')
        self.assertEqual(go.read_text(), 'existing entry')

    def test_private_entries_can_be_updated_and_have_expected_modes(self):
        self.install()
        go = self.profile / '.local/bin/go'
        go.write_text('old entry')
        self.install('--replace-existing')
        self.assertEqual(go.read_bytes(), (ROOT / 'tools/macos-dev/go').read_bytes())
        self.assertEqual(go.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.pip.stat().st_mode & 0o777, 0o600)

    def test_symlink_and_hardlink_are_not_replaced(self):
        target = self.profile / 'owned'
        target.write_text('keep')
        target.chmod(0o600)
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    self.pip.symlink_to(target)
                else:
                    os.link(target, self.pip)
                with self.assertRaises(RuntimeError):
                    self.install('--replace-existing')
                self.assertEqual(target.read_text(), 'keep')
                self.assertEqual(list((self.profile / '.local/bin').iterdir()), [])
                self.pip.unlink()


@unittest.skipUnless(sys.platform == 'darwin', 'macOS native tool')
class NativeBuildTests(unittest.TestCase):
    def test_host_cross_compile_settings_cannot_change_binary_target(self):
        compiler = shutil.which('go')
        if not compiler:
            self.skipTest('Go compiler is not installed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'main.go').write_text('package main\nfunc main() {}\n')
            hostile = dict(GOOS='linux', GOARCH='386', GOROOT='/nonexistent',
                           GOWORK='/nonexistent', GOEXPERIMENT='invalid', GOFLAGS='-invalid')
            with patch.dict(os.environ, hostile):
                env = builder.build_environment()
            env.update(GOCACHE=str(root / 'cache'), GOPATH=str(root / 'gopath'))
            binary = root / 'probe'
            result = subprocess.run([compiler, 'build', '-o', str(binary), str(root / 'main.go')],
                                    env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            builder.verify_target(compiler, binary, env)
            subprocess.run([str(binary)], check=True, timeout=5)
            wrong = dict(env, GOARCH='amd64' if platform.machine() == 'arm64' else 'arm64')
            with self.assertRaises(RuntimeError):
                builder.verify_target(compiler, binary, wrong)


if __name__ == '__main__':
    unittest.main()
