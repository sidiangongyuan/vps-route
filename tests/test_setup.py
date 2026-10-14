"""Initialization and deployment with fake keys, homes, SSH, and systemd."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.config import Config
from claude_launcher.entrypoint import main
from claude_launcher.provision import (
    MANAGED_HEADER, SERVER_FILES, SERVICE_NAME, apply_server, deploy_server,
)
from claude_launcher.setup import default_config_path, initialize_config
from claude_launcher.wire import RouteError


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vps-route-setup-")
        self.home = Path(self.temporary.name)
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.process_patch = patch(
            "claude_launcher.legacy_cutover._same_user_legacy_process_running", return_value=False,
        )
        self.profile_patch = patch(
            "claude_launcher.profile_reset._profile_is_running", return_value=False,
        )
        self.process_patch.start()
        self.profile_patch.start()
        self.key = self.home / "key"
        self.hosts = self.home / "known-hosts"
        self.key.write_text("synthetic key, not usable")
        self.hosts.write_text("synthetic host pin, not usable")
        self.source = self.home / "source.json"
        self.source.write_text(json.dumps({
            "ssh_host": "203.0.113.1", "ssh_user": "fixture", "ssh_port": 22,
            "identity_file": str(self.key), "known_hosts": str(self.hosts),
            "remote_socket": str(self.home / ".local/state/connect/connect.sock"),
            "expected_ip": "1.1.1.1", "expected_country": "US",
        }))

    def tearDown(self):
        self.process_patch.stop()
        self.profile_patch.stop()
        self.home_patch.stop()
        self.temporary.cleanup()

    def test_import_is_private_no_overwrite_no_accounts(self):
        old = self.home / ".claude"
        old.mkdir()
        (old / "keep").write_text("synthetic old state")
        config = initialize_config(default_config_path(), self.source, keep_existing=True)
        self.assertEqual(config.profile_home,
                         self.home / ".local/share/claude-launcher/profiles/default")
        self.assertEqual(default_config_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(default_config_path().parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((old / "keep").read_text(), "synthetic old state")
        before = default_config_path().read_bytes()
        self.assertEqual(initialize_config(default_config_path(), self.source), config)
        self.assertEqual(default_config_path().read_bytes(), before)
        self.assertEqual(list(config.profile_home.iterdir()), [])

    def test_missing_keys_and_symlink_parent_fail_before_config_write(self):
        self.key.unlink()
        with self.assertRaisesRegex(RouteError, "known_hosts_missing"):
            initialize_config(default_config_path(), self.source)
        self.assertFalse(default_config_path().exists())
        self.key.touch()
        outside = self.home / "outside"
        outside.mkdir(mode=0o700)
        (self.home / ".config").symlink_to(outside)
        with self.assertRaisesRegex(RouteError, "symlinked"):
            initialize_config(default_config_path(), self.source)
        self.assertFalse((outside / "vps-route/config.json").exists())

    def test_interactive_init_reuses_validator(self):
        answers = iter(["fixture", "203.0.113.1", "", str(self.key), str(self.hosts),
                        "", "", "", ""])
        config = initialize_config(default_config_path(), prompt=lambda _: next(answers))
        self.assertEqual(config.ssh_port, 22)
        self.assertIsNone(config.ssh_http_proxy)
        self.assertIsNone(config.timezone)
        self.assertFalse(config.research_filesystem)

    def test_default_init_preserves_legacy_state_and_creates_private_profile(self):
        old = self.home / ".claude"
        old.mkdir()
        (old / "keep").write_text("synthetic old state")
        hicc = self.home / ".hicc"
        hicc.mkdir()
        (hicc / "state").write_text("synthetic unrelated state")
        with patch("claude_launcher.legacy_cutover._same_user_legacy_process_running",
                   return_value=False), patch(
                       "claude_launcher.profile_reset._profile_is_running", return_value=False):
            config = initialize_config(default_config_path(), self.source, yes=True)
        self.assertEqual((old / "keep").read_text(), "synthetic old state")
        self.assertFalse((self.home / ".local/share/claude-launcher/legacy-archives").exists())
        self.assertEqual(list(config.profile_home.iterdir()), [])
        self.assertEqual(config.profile_home.stat().st_mode & 0o777, 0o700)
        self.assertEqual((hicc / "state").read_text(), "synthetic unrelated state")

    def test_init_preview_does_not_create_config_or_profile(self):
        old = self.home / ".claude"
        old.mkdir()
        with patch("claude_launcher.legacy_cutover._same_user_legacy_process_running",
                   return_value=False), patch(
                       "claude_launcher.profile_reset._profile_is_running", return_value=False):
            initialize_config(default_config_path(), self.source, dry_run=True)
        self.assertFalse(default_config_path().exists())
        self.assertFalse((self.home / ".config").exists())
        self.assertTrue(old.exists())

    def test_init_can_reuse_environment_while_old_claude_is_running(self):
        with patch("claude_launcher.legacy_cutover._same_user_legacy_process_running",
                   return_value=True):
            config = initialize_config(default_config_path(), self.source, yes=True)
            self.assertEqual(initialize_config(default_config_path()), config)
        self.assertTrue(default_config_path().exists())

    def test_second_config_reuses_profile_and_keeps_credentials_and_sessions(self):
        config = initialize_config(default_config_path(), self.source)
        credentials = config.profile_home / ".claude/.credentials.json"
        credentials.parent.mkdir()
        credentials.write_text("synthetic account state")
        session = credentials.parent / "projects/fixture/session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text("synthetic session")
        other = default_config_path().parent / "other.json"
        initialize_config(other, self.source)
        initialize_config(default_config_path())
        self.assertEqual(credentials.read_text(), "synthetic account state")
        self.assertEqual(session.read_text(), "synthetic session")

    def test_init_preserves_ssh_material_in_custom_account_directories(self):
        old = self.home / ".claude"
        old.mkdir()
        protected = old / "private-key"
        protected.write_text("synthetic key")
        values = json.loads(self.source.read_text())
        values["identity_file"] = str(protected)
        self.source.write_text(json.dumps(values))
        initialize_config(default_config_path(), self.source, yes=True)
        self.assertTrue(protected.exists())
        self.assertTrue(default_config_path().exists())

    def test_repeat_init_rejects_changed_or_invalid_config_without_overwrite(self):
        initialize_config(default_config_path(), self.source)
        original = default_config_path().read_bytes()
        values = json.loads(self.source.read_text())
        values["ssh_port"] = 2222
        self.source.write_text(json.dumps(values))
        with self.assertRaisesRegex(RouteError, "exists_and_differs"):
            initialize_config(default_config_path(), self.source)
        self.assertEqual(default_config_path().read_bytes(), original)
        default_config_path().write_text("invalid fixture")
        with self.assertRaisesRegex(RouteError, "invalid_or_unreadable_config"):
            initialize_config(default_config_path())
        self.assertEqual(default_config_path().read_text(), "invalid fixture")

    def test_repeat_init_recovers_missing_profile_without_reasking(self):
        config = initialize_config(default_config_path(), self.source)
        config.profile_home.rmdir()
        initialize_config(default_config_path(), prompt=lambda _: self.fail("unexpected prompt"))
        self.assertTrue(config.profile_home.is_dir())

    def test_config_is_not_written_inside_account_profile(self):
        profile = Config.load(self.source).profile_home
        with self.assertRaisesRegex(RouteError, "outside_claude_profile"):
            initialize_config(profile / ".config/route.json", self.source)
        self.assertFalse(profile.exists())

    def test_profile_creation_failure_is_recoverable_without_config_replacement(self):
        config = Config.load(self.source)
        original_mkdir = Path.mkdir

        def fail_profile(path, *args, **kwargs):
            if path == config.profile_home:
                raise OSError("synthetic interruption")
            return original_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", fail_profile):
            with self.assertRaisesRegex(OSError, "synthetic interruption"):
                initialize_config(default_config_path(), self.source)
        before = default_config_path().read_bytes()
        initialize_config(default_config_path())
        self.assertEqual(default_config_path().read_bytes(), before)
        self.assertTrue(config.profile_home.is_dir())

    def test_nonprivate_directory_is_not_chmodded(self):
        folder = self.home / "shared"
        folder.mkdir(mode=0o755)
        with self.assertRaisesRegex(RouteError, "must_be_private"):
            initialize_config(folder / "config.json", self.source)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o755)

    def payload(self):
        return {"remote_socket": str(self.home / ".local/state/connect/connect.sock"),
                "files": {name: "# synthetic server fixture\n" for name in SERVER_FILES}}

    def test_service_deploy_only_managed_files_and_service(self):
        units = self.home / "units"
        for system in (False, True):
            with self.subTest(system=system), patch(
                "claude_launcher.provision.subprocess.run"
            ) as run:
                result = apply_server(self.payload(), unit_directory=units, system=system)
                self.assertEqual(result["scope"], "system" if system else "user")
                unit = (units / SERVICE_NAME).read_text()
                self.assertTrue(unit.startswith(MANAGED_HEADER))
                self.assertNotIn("sshd", unit)
                self.assertNotIn("iptables", unit)
                self.assertEqual(run.call_count, 4)
                self.assertEqual(run.call_args.args[0][-3:],
                                 ["is-active", "--quiet", SERVICE_NAME])
                self.assertEqual(self.home.joinpath(".local/state/connect").stat().st_mode
                                 & 0o777, 0o700)

    def test_unmanaged_service_and_outside_socket_are_rejected(self):
        units = self.home / "units"
        units.mkdir()
        (units / SERVICE_NAME).write_text("unrelated service")
        with self.assertRaisesRegex(ValueError, "unmanaged_service"):
            apply_server(self.payload(), unit_directory=units, system=False)
        self.assertFalse((self.home / ".local").exists())
        payload = self.payload()
        payload["remote_socket"] = "/tmp/unowned.sock"
        with self.assertRaisesRegex(ValueError, "private_directory"):
            apply_server(payload, unit_directory=units, system=False)

    def test_existing_socket_without_managed_service_is_not_taken_over(self):
        socket = Path(self.payload()["remote_socket"])
        socket.parent.mkdir(parents=True)
        socket.write_text("unrelated socket fixture")
        with patch("claude_launcher.provision.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "existing_socket"):
                apply_server(self.payload(), unit_directory=self.home / "units", system=False)
            run.assert_not_called()
        self.assertEqual(socket.read_text(), "unrelated socket fixture")

    def test_ssh_deployment_is_host_verified_and_has_no_password(self):
        config = Config.load(self.source)
        with patch("claude_launcher.provision.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertEqual(deploy_server(config), 0)
            arguments = run.call_args.args[0]
            self.assertIn("StrictHostKeyChecking=yes", arguments)
            self.assertIn("BatchMode=yes", arguments)
            payload = json.loads(run.call_args.kwargs["input"])
            self.assertEqual(set(payload["files"]), SERVER_FILES)
            self.assertNotIn("identity_file", payload)
            self.assertNotIn("private key", run.call_args.kwargs["input"])
        with patch("claude_launcher.provision.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("fixture", 1)):
            self.assertEqual(deploy_server(config), 69)

    def test_unsupported_platform_fails_without_importing_runtime(self):
        with patch("claude_launcher.entrypoint.sys.platform", "darwin"):
            self.assertEqual(main(), 69)


if __name__ == "__main__":
    unittest.main()
