"""WSL policy tests use synthetic proc data and disposable fixtures."""

import contextlib
import io
import json
import socket
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from claude_launcher import cli, isolation, wsl
from claude_launcher.config import Config
from claude_launcher.runner import bootstrap_proxy_options, sandbox_command
from claude_launcher.wire import RouteError


def config():
    return Config("203.0.113.8", "fixture", 22, Path("/unused/key"), Path("/unused/hosts"),
                  "/unused/service.sock")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        (self.proc / "sys/kernel").mkdir(parents=True)
        (self.proc / "self").mkdir()
        (self.proc / "net").mkdir()

    def release(self, value):
        (self.proc / "sys/kernel/osrelease").write_text(value)

    def test_versions(self):
        for release, expected in (("6.8.0-generic", None), ("4.4.0-Microsoft", 1),
                                  ("6.18.0-microsoft-standard-WSL2", 2),
                                  ("5.15.0-microsoft-standard", 2)):
            self.release(release)
            self.assertEqual(wsl.version(self.proc), expected)

    def test_binfmt_both_registrations(self):
        root = self.proc / "sys/fs/binfmt_misc"
        root.mkdir(parents=True)
        (root / "WSLInterop").write_text("enabled\ninterpreter /init\nflags: PF\n")
        (root / "WSLInterop-late").write_text("disabled\ninterpreter /init\nflags: P\n")
        values = wsl.registrations(self.proc)
        self.assertTrue(values[0]["persistent_interpreter"])
        self.assertFalse(values[1]["enabled"])
        self.assertFalse(values[1]["persistent_interpreter"])

    def test_mount_types_escaping_and_bind_aliases(self):
        (self.proc / "self/mountinfo").write_text(
            "1 0 0:1 / / rw - ext4 root rw\n"
            "2 1 0:2 / /drives/c rw - 9p C: rw,aname=drvfs;path=C:\n"
            "3 1 0:3 / /custom/d rw - drvfs D: rw\n"
            "4 1 0:4 / /with\\040space/e rw - virtiofs E: rw\n"
            "5 1 0:2 /Users/Test /alias rw - 9p C: rw,aname=drvfs\n"
            "6 1 0:5 / /mnt/wsl rw - tmpfs none rw\n"
            "7 1 0:5 / /shared-alias rw - tmpfs none rw\n")
        inventory = wsl.mounts(self.proc)
        self.assertEqual(len(wsl.windows_mounts(inventory)), 4)
        self.assertEqual(inventory[3].target, Path("/with space/e"))
        self.assertIn(Path("/shared-alias"), wsl.runtime_paths(inventory))
        with self.assertRaises(RouteError):
            wsl.validate_workspace(Path("/alias/AppData/project"), inventory)

    def test_gateway_uses_route_metric_and_network_mode(self):
        self.release("6.18-microsoft-standard-WSL2")
        (self.proc / "net/route").write_text(
            "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
            "eth0 00000000 010214AC 0003 0 0 100 00000000\n"
            "eth1 00000000 010114AC 0003 0 0 50 00000000\n")
        self.assertEqual(wsl.default_gateway(self.proc), "172.20.1.1")
        with patch.object(wsl, "network_mode", return_value="nat"):
            self.assertEqual(wsl.proxy_host("windows-host", self.proc), "172.20.1.1")
        with patch.object(wsl, "network_mode", return_value="mirrored"):
            with self.assertRaisesRegex(RouteError, "requires_nat"):
                wsl.proxy_host("windows-host", self.proc)
        self.release("6.8-generic")
        with self.assertRaisesRegex(RouteError, "requires_wsl2"):
            wsl.proxy_host("windows-host", self.proc)

    def test_missing_gateway_and_malformed_mount_fail_closed(self):
        (self.proc / "net/route").write_text("header\n")
        with self.assertRaises(RouteError):
            wsl.default_gateway(self.proc)
        (self.proc / "self/mountinfo").write_text("broken")
        with self.assertRaises(RouteError):
            wsl.mounts(self.proc)

    def test_network_mode_bounded_and_normalized(self):
        def run(args, **kwargs):
            self.assertEqual(args, ["wslinfo", "--networking-mode"])
            self.assertLessEqual(kwargs["timeout"], 3)
            return subprocess.CompletedProcess(args, 0, "Mirrored\n", "")
        self.assertEqual(wsl.network_mode(run), "mirrored")


class PathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.inventory = [wsl.Mount(Path("/drives/c"), Path("/"), "0:2", "9p", True)]

    def test_case_insensitive_broad_and_private_paths(self):
        for relative in ("", "Users", "uSeRs/Alice", "Users/Alice/APPDATA/project",
                         "Users/Alice/.sSh", "Users/Alice/.AWS/sub",
                         "Users/Alice/.Claude-backup", "Users/Alice/.CODEX-extra/sub",
                         "Users/Alice/NTUSER.DAT.backup/sub", "Users/Alice/.config/sub"):
            with self.subTest(relative=relative), self.assertRaises(RouteError):
                wsl.validate_workspace(Path("/drives/c") / relative, self.inventory)
        wsl.validate_workspace(Path("/drives/c/Users/Alice/projects/demo"), self.inventory)

    def test_alias_root_and_runtime_are_refused(self):
        alias = wsl.Mount(Path("/alias"), Path("/Users/Alice"), "0:2", "9p", True)
        for path in ("/alias", "/alias/AppData/sub", "/mnt/wsl/project", "/run/WSL/project"):
            with self.assertRaises(RouteError):
                wsl.validate_workspace(Path(path), [*self.inventory, alias])

    def test_linux_symlink_into_private_windows_source(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "project"
            link.symlink_to("/drives/c/Users/Alice/.ssh")
            with self.assertRaises(RouteError):
                wsl.validate_workspace(link, self.inventory)


    def test_relocated_profile_markers_and_short_names(self):
        with tempfile.TemporaryDirectory() as directory:
            drive = Path(directory)
            home = drive / "relocated" / "someone"
            (home / "AppData").mkdir(parents=True)
            inventory = [wsl.Mount(drive, Path("/"), "0:2", "drvfs", True)]
            with self.assertRaises(RouteError):
                wsl.validate_workspace(home, inventory)
        with self.assertRaises(RouteError):
            wsl.validate_workspace(Path("/drives/c/Users/Alice/APPDAT~1/project"), self.inventory)

    def test_ssh_proxy_config_and_runtime_resolution(self):
        values = json.loads(Path("config.example.json").read_text())
        values.update(ssh_host="203.0.113.8", ssh_user="fixture",
                      windows_interop=True, ssh_http_proxy="http://windows-host:7897")
        c = Config.from_values(values)
        with patch("claude_launcher.runner.shutil.which", return_value="/usr/bin/nc"), \
             patch.object(wsl, "proxy_host", return_value="172.20.1.1"):
            self.assertIn("172.20.1.1:7897", bootstrap_proxy_options(c)[1])
        for invalid in ("https://windows-host:7897", "http://example.org:7897",
                        "http://user:password@windows-host:7897", "http://windows-host:7897/path"):
            with self.assertRaises(RouteError):
                Config.from_values(dict(values, ssh_http_proxy=invalid))
        with self.assertRaises(RouteError):
            Config.from_values(dict(values, windows_interop="true"))


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("profile", "project", "windows"):
            (self.root / name).mkdir()
        (self.root / "upstream").touch()
        self.inventory = [wsl.Mount(self.root / "windows", Path("/"), "0:2", "drvfs", True)]

    def build(self, **kwargs):
        return sandbox_command(self.root / "upstream", self.root / "profile",
                               self.root / "project", ["/bin/true"], **kwargs)

    def test_all_commands_receive_filter(self):
        with patch.object(wsl, "version", return_value=2), \
             patch.object(wsl, "mounts", return_value=self.inventory):
            for kwargs in ({}, {"login": True}, {"research_filesystem": True},
                           {"windows_interop": True}, {"project_instructions": True}):
                args, env = self.build(**kwargs)
                self.assertEqual(args[0], "/usr/bin/python3")
                self.assertTrue(args[1].endswith("/filter_entry.py"))
                self.assertEqual(args[2], "windows" if kwargs.get("windows_interop") else "default")
                self.assertIn("--unshare-net", args)
                self.assertIn("--cap-drop", args)
                if not kwargs.get("windows_interop"):
                    self.assertNotIn("WSL_INTEROP", env)
                    self.assertNotIn("WSLENV", env)

    def test_research_masks_precede_workspace_and_finish_readonly(self):
        with patch.object(wsl, "version", return_value=2), \
             patch.object(wsl, "mounts", return_value=self.inventory):
            args, _ = self.build(research_filesystem=True)
        mask = args.index(str(self.root / "windows"))
        workspace = args.index(str(self.root / "project"))
        self.assertLess(mask, workspace)
        self.assertEqual(args[mask - 1], "--tmpfs")
        self.assertIn(["--remount-ro", str(self.root / "windows")],
                      [args[i:i+2] for i in range(len(args)-1)])

    def test_wsl1_nonwsl_and_unsupported_architecture_fail_closed(self):
        with patch.object(wsl, "version", return_value=1):
            with self.assertRaisesRegex(RouteError, "wsl1_unsupported"):
                self.build()
        with patch.object(wsl, "version", return_value=None):
            with self.assertRaisesRegex(RouteError, "requires_wsl2"):
                self.build(windows_interop=True)
        with patch.object(wsl, "version", return_value=2), \
             patch("claude_launcher.runner.seccomp.supported", return_value=False):
            with self.assertRaisesRegex(RouteError, "architecture_unsupported"):
                self.build()
        with patch.object(wsl, "version", return_value=None), \
             patch("claude_launcher.runner.seccomp.supported", return_value=False):
            self.assertEqual(self.build()[0][0], "bwrap")

    def test_doctor_timeout_and_bad_output_never_pass(self):
        with patch.object(wsl, "version", return_value=None), \
             patch("claude_launcher.isolation.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("probe", 25)):
            result = isolation.inspect(None)
        self.assertFalse(result["passed"])
        self.assertEqual(result["current"]["result"], "inconclusive")

    def test_doctor_failure_prevents_ready(self):
        with patch.object(cli, "namespace_available", return_value=True), \
             patch.object(cli, "system_python_compatible", return_value=True), \
             patch.object(isolation, "inspect", return_value={"passed": False}), \
             patch.object(wsl, "summary", return_value={}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.doctor(None, False)
        self.assertEqual(code, 69)
        self.assertFalse(json.loads(output.getvalue())["ready"])


if __name__ == "__main__":
    unittest.main()
