"""Manual upgrade policy and real temporary Git checkout update/rollback."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher import updates
from claude_launcher.sessions import maintenance_barrier
from claude_launcher.wire import RouteError


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.temporary.cleanup()

    def test_release_validation_rejects_drafts_prereleases_and_non_version_tags(self):
        for values in [
            {"tag_name": "v1.0.0", "draft": True, "prerelease": False},
            {"tag_name": "v1.0.0", "draft": False, "prerelease": True},
            {"tag_name": "main", "draft": False, "prerelease": False},
            {"tag_name": "v1.0.0a1", "draft": False, "prerelease": False},
        ]:
            with patch.object(updates, "_fetch_release", return_value=json.dumps(values)), \
                    self.assertRaisesRegex(RouteError, "no_valid_formal_release"):
                updates.official_release()
        with self.assertRaisesRegex(RouteError, "formal_X_Y_Z"):
            updates.official_release("main")

    def test_release_lookup_uses_public_api_without_github_cli(self):
        value = {"tag_name": "v1.2.3", "draft": False, "prerelease": False}
        with patch.object(updates, "_fetch_release", return_value=json.dumps(value)) as fetch, \
                patch("claude_launcher.updates.shutil.which", return_value=None):
            self.assertEqual(updates.official_release()["tag"], "v1.2.3")
        fetch.assert_called_once_with("repos/sidiangongyuan/vps-route/releases/latest")
        with patch("claude_launcher.updates.urllib.request.urlopen", side_effect=OSError), \
                self.assertRaisesRegex(RouteError, "formal_release_unavailable"):
            updates.official_release("1.2.3")

    def test_preview_never_updates_or_reads_profile_state(self):
        release = {"version": "1.0.0", "tag": "v1.0.0"}
        with patch.object(updates, "installation", return_value={"kind": "checkout"}), \
                patch.object(updates, "official_release", return_value=release), \
                patch.object(updates, "_idle") as idle, patch.object(updates, "_run") as run:
            self.assertFalse(updates.self_update()["applied"])
            idle.assert_not_called()
            run.assert_not_called()

    def test_updates_refuse_active_barrier_and_old_profile_process(self):
        release = {"version": "1.0.0", "tag": "v1.0.0"}
        with patch.object(updates, "installation", return_value={"kind": "checkout"}), \
                patch.object(updates, "official_release", return_value=release):
            with maintenance_barrier(), self.assertRaisesRegex(RouteError, "active_sessions"):
                updates.self_update(apply=True)
            with patch("claude_launcher.profile_reset._profile_is_running", return_value=True), \
                    self.assertRaisesRegex(RouteError, "sessions_running"):
                updates.self_update(apply=True)

    def git(self, root, *arguments):
        result = subprocess.run(["git", "-C", str(root), *arguments], text=True,
                                capture_output=True, check=True)
        return result.stdout.strip()

    def repo(self, name, version):
        root = self.home / name
        package = root / "claude_launcher"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f'__version__ = "{version}"\n')
        (package / "cli.py").write_text("# fixture CLI\n")
        (root / ".gitignore").write_text("__pycache__/\n")
        self.git(root, "init", "-q")
        self.git(root, "add", ".")
        self.git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "fixture")
        self.git(root, "remote", "add", "origin",
                 "https://github.com/sidiangongyuan/vps-route.git")
        return root

    def test_checkout_upgrade_and_code_only_rollback_with_no_external_network(self):
        root = self.repo("checkout", "0.3.1a2")
        source = self.repo("released-source", "0.4.0")
        before = self.git(root, "rev-parse", "HEAD")
        plan = {"kind": "checkout", "root": str(root)}
        real_run = updates._run

        def fake_download(command, **options):
            if command[:2] == ["git", "clone"]:
                shutil.copytree(source, Path(command[-1]))
                return ""
            return real_run(command, **options)

        account = self.home / "account-marker"
        account.write_text("synthetic account unchanged")
        with patch.object(updates, "PACKAGE", root / "claude_launcher"), \
                patch.object(updates, "__version__", "0.3.1a2"), \
                patch.object(updates, "official_release", return_value={
                    "version": "0.4.0", "tag": "v0.4.0",
                }), patch.object(updates, "_run", side_effect=fake_download), \
                patch.object(updates, "_idle"):
            self.assertTrue(updates.self_update(apply=True)["applied"])
            self.assertIn('"0.4.0"', (root / "claude_launcher/__init__.py").read_text())
            with patch.object(updates, "__version__", "0.4.0"):
                self.assertTrue(updates.self_update(rollback=True, apply=True)["applied"])
        self.assertEqual(self.git(root, "rev-parse", "HEAD"), before)
        self.assertEqual(account.read_text(), "synthetic account unchanged")
        self.assertFalse((self.home / ".local/state/claude-launcher/updates/last.json").exists())

    def test_dirty_checkout_is_refused_before_download(self):
        root = self.repo("dirty", "0.3.1a2")
        (root / "user-change").write_text("keep")
        with patch.object(updates, "PACKAGE", root / "claude_launcher"), \
                patch.object(updates, "__version__", "0.3.1a2"), \
                patch.object(updates, "official_release", return_value={
                    "version": "0.4.0", "tag": "v0.4.0",
                }), patch.object(updates, "_idle"), \
                self.assertRaisesRegex(RouteError, "dirty_checkout"):
            updates.self_update(apply=True)
        self.assertEqual((root / "user-change").read_text(), "keep")

    def test_unrecognized_checkout_and_system_package_are_refused(self):
        root = self.repo("foreign", "0.3.1a2")
        self.git(root, "remote", "set-url", "origin", "https://github.com/example/other.git")
        with patch.object(updates, "PACKAGE", root / "claude_launcher"), \
                self.assertRaisesRegex(RouteError, "unrecognized_repository"):
            updates.installation()
        package = self.home / "site-packages/claude_launcher"
        package.mkdir(parents=True)
        with patch.object(updates, "PACKAGE", package), \
                patch("claude_launcher.updates.sys.prefix", "/usr"), \
                patch("claude_launcher.updates.sys.base_prefix", "/usr"), \
                self.assertRaisesRegex(RouteError, "system_python"):
            updates.installation()

    def test_package_backup_restore_preserves_other_packages(self):
        root = self.home / "site-packages"
        package = root / "claude_launcher"
        metadata = root / "vps_route-0.3.1a2.dist-info"
        package.mkdir(parents=True)
        metadata.mkdir()
        (package / "__init__.py").write_text("old code")
        (metadata / "METADATA").write_text("old metadata")
        unrelated = root / "unrelated-package"
        unrelated.mkdir()
        (unrelated / "keep").write_text("keep")
        backup = self.home / "backup"
        backup.mkdir()
        plan = {"root": str(root), "metadata": str(metadata)}
        with patch.object(updates, "PACKAGE", package):
            updates._package_snapshot(plan, backup)
        (package / "__init__.py").write_text("new code")
        shutil.rmtree(metadata)
        (root / "vps_route-0.4.0.dist-info").mkdir()
        updates._restore_package(plan, backup)
        self.assertEqual((package / "__init__.py").read_text(), "old code")
        self.assertEqual((metadata / "METADATA").read_text(), "old metadata")
        self.assertEqual((unrelated / "keep").read_text(), "keep")

    def test_package_update_and_rollback_accept_versioned_metadata_change(self):
        root = self.home / "venv/site-packages"
        package = root / "claude_launcher"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('__version__ = "0.3.1a2"\n')
        (package / "cli.py").write_text("# fixture\n")
        old_metadata = root / "vps_route-0.3.1a2.dist-info"
        new_metadata = root / "vps_route-0.4.0.dist-info"
        old_metadata.mkdir()
        (old_metadata / "METADATA").write_text("old metadata")
        source = self.repo("release", "0.4.0")
        old_plan = {"kind": "venv", "root": str(root), "metadata": str(old_metadata),
                    "prefix": str(self.home / "venv"), "python": updates.sys.executable}
        new_plan = {**old_plan, "metadata": str(new_metadata)}
        real_run = updates._run

        def fake_packages(command, **options):
            if command[:2] == ["git", "clone"]:
                shutil.copytree(source, command[-1])
            elif command[1:4] == ["-m", "pip", "wheel"]:
                wheels = Path(command[-1])
                wheels.mkdir()
                (wheels / "vps_route-0.4.0-py3-none-any.whl").write_text("synthetic wheel")
            elif command[1:4] == ["-m", "pip", "install"]:
                (package / "__init__.py").write_text('__version__ = "0.4.0"\n')
                shutil.rmtree(old_metadata)
                new_metadata.mkdir()
                (new_metadata / "METADATA").write_text("new metadata")
            else:
                return real_run(command, **options)
            return ""

        with patch.object(updates, "PACKAGE", package), \
                patch.object(updates, "installation", side_effect=[old_plan, new_plan]), \
                patch.object(updates, "official_release", return_value={
                    "version": "0.4.0", "tag": "v0.4.0",
                }), patch.object(updates, "_run", side_effect=fake_packages), \
                patch.object(updates, "_idle"), \
                patch.object(updates, "__version__", "0.3.1a2"):
            self.assertTrue(updates.self_update(apply=True)["applied"])
            with patch.object(updates, "__version__", "0.4.0"):
                self.assertTrue(updates.self_update(rollback=True, apply=True)["applied"])
        self.assertIn("0.3.1a2", (package / "__init__.py").read_text())
        self.assertTrue(old_metadata.exists())
        self.assertFalse(new_metadata.exists())
