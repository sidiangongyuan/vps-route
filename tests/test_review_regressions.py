"""Regression evidence for the second WSL review; fixtures contain no accounts."""

import contextlib
import errno
import io
import json
import shutil
import socket
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from claude_launcher import cli, isolation, runner, seccomp, wsl
from claude_launcher.config import Config
from claude_launcher.wire import RouteError
from claude_launcher.entries import isolation_entry as probe


def config():
    return Config("203.0.113.8", "fixture", 22, Path("/unused/key"), Path("/unused/hosts"),
                  "/unused/service.sock")


class AliasRegressionTests(unittest.TestCase):
    def test_file_and_runtime_socket_aliases_are_covered_in_both_modes(self):
        for research in (False, True):
            with self.subTest(research=research), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "code/claude_launcher"
                shutil.copytree(runner.PACKAGE_DIR, package, ignore=shutil.ignore_patterns("__pycache__"))
                workspace, profile = root / "workspace", root / "profile"
                workspace.mkdir()
                profile.mkdir()
                file = package / "file-alias"
                file.write_text("synthetic-private-canary")
                alias = package / "socket-alias"
                inventory = [
                    wsl.Mount(Path("/"), Path("/"), "0:1", "ext4", False),
                    wsl.Mount(Path("/run/WSL"), Path("/run/WSL"), "0:2", "tmpfs", False),
                    wsl.Mount(alias, Path("/run/WSL/fixture.sock"), "0:2", "tmpfs", False),
                    wsl.Mount(file, Path("/Users/Fixture/AppData/canary"), "0:3", "9p", True),
                ]
                with socket.socket(socket.AF_UNIX) as upstream, socket.socket(socket.AF_UNIX) as server:
                    upstream.bind(str(root / "upstream"))
                    server.bind(str(alias))
                    server.listen()
                    with socket.socket(socket.AF_UNIX) as control:
                        control.connect(str(alias))
                        peer, _ = server.accept()
                        peer.close()
                    code = f"""
import errno,socket
from pathlib import Path
try: assert Path({str(file)!r}).read_bytes()==b''
except PermissionError: pass
assert Path({str(file)!r}).is_char_device()
assert Path({str(alias)!r}).is_char_device()
with socket.socket(socket.AF_UNIX) as client:
    try: client.connect({str(alias)!r})
    except OSError as e: assert e.errno in (errno.ECONNREFUSED,errno.ENOTSOCK,errno.EACCES)
    else: raise AssertionError('runtime alias connected')
"""
                    with patch.object(wsl, "version", return_value=2), \
                         patch.object(wsl, "mounts", return_value=inventory), \
                         patch.object(runner, "PACKAGE_DIR", package), \
                         patch.object(runner, "ENTRY_DIR", package / "entries"):
                        args, env = runner.sandbox_command(
                            root / "upstream", profile, workspace,
                            ["/usr/bin/python3", "-c", code], research_filesystem=research)
                    result = subprocess.run(args, env=env, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(file.read_text(), "synthetic-private-canary")

    def test_socket_scanner_only_receives_applied_masks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("profile", "project"):
                (root / name).mkdir()
            existing, missing = root / "covered-file", root / "missing-alias"
            existing.write_text("fixture")
            with patch.object(wsl, "version", return_value=2), \
                 patch.object(wsl, "mounts", return_value=[]), \
                 patch.object(wsl, "mask_paths", return_value=[existing, missing]), \
                 patch.object(runner, "host_socket_mounts", return_value=[]) as scan:
                runner.sandbox_command(root / "upstream", root / "profile", root / "project",
                                       ["/bin/true"], research_filesystem=True)
            self.assertEqual(scan.call_args.kwargs["masked"], (existing,))


class DoctorPolicyTests(unittest.TestCase):
    def test_selected_mode_only_and_explicit_override(self):
        with patch.object(wsl, "version", return_value=2), \
             patch.object(wsl, "mounts", return_value=[]), \
             patch.object(isolation, "_run", return_value={"passed": True}) as run:
            result = isolation.inspect(config())
            self.assertTrue(result["passed"])
            self.assertEqual(run.call_count, 1)
            self.assertFalse(run.call_args.args[1])
            self.assertNotIn("windows_opt_in", result)
            isolation.inspect(config(), windows=True)
            self.assertTrue(run.call_args.args[1])
            isolation.inspect(replace(config(), windows_interop=True))
            self.assertTrue(run.call_args.args[1])

    def test_unavailable_host_execution_and_ipv6_are_optional_in_strict_mode(self):
        spec = dict(wsl=2, windows=False, seccomp_supported=True,
                    parent_port6=0, host_fixture_ok=False)
        expected = isolation.expected_checks(spec, {"ipv6_routes": "unsupported"})
        self.assertNotIn("windows_execution", expected)
        self.assertNotIn("ipv6_routes", expected)
        spec["windows"] = True
        self.assertEqual(isolation.expected_checks(spec, {})["windows_execution"], "reachable")

    def test_connect_runs_when_isolation_fails(self):
        with patch.object(cli, "namespace_available", return_value=True), \
             patch.object(cli, "system_python_compatible", return_value=True), \
             patch.object(wsl, "summary", return_value={}), \
             patch.object(isolation, "inspect", return_value={"passed": False}), \
             patch("claude_launcher.diagnostics.connect_diagnostics",
                   return_value={"connectivity_ok": True}) as connect, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.doctor(config(), True)
        connect.assert_called_once()
        self.assertEqual(code, 69)
        self.assertTrue(json.loads(out.getvalue())["connectivity_ok"])
        self.assertFalse(json.loads(out.getvalue())["ready"])

    def test_native_unsupported_arch_never_executes_foreign_syscalls(self):
        with patch.object(probe.ctypes, "CDLL", side_effect=AssertionError("unsafe syscall")):
            checks, active = probe.filter_checks({"seccomp_supported": False}, {})
        self.assertFalse(active)
        self.assertTrue(all(value == "unsupported" for value in checks.values()))
        expected = isolation.expected_checks(
            dict(wsl=None, windows=False, seccomp_supported=False,
                 parent_port6=0, host_fixture_ok=False), {"ipv6_routes": "unsupported"})
        self.assertNotIn("seccomp_filter", expected)
        self.assertNotIn("io_uring", expected)
        self.assertEqual(expected["nested_bwrap"], "reachable")

    def test_native_unsupported_filter_keeps_production_builder_usable(self):
        with patch.object(wsl, "version", return_value=None), \
             patch.object(seccomp, "supported", return_value=False):
            result = isolation.inspect(None)
        self.assertTrue(result["passed"], result)
        checks = result["current"]["checks"]
        self.assertEqual(checks["seccomp_filter"], "unsupported")
        self.assertEqual(checks["nested_bwrap"], "reachable")
        with patch.object(wsl, "version", return_value=2), \
             patch.object(seccomp, "supported", return_value=False):
            with self.assertRaisesRegex(RouteError, "wsl_seccomp_architecture_unsupported"):
                runner.sandbox_command(Path("/unused"), Path("/unused/profile"),
                                       Path("/unused/project"), ["/bin/true"])

    def test_missing_ipv6_proc_and_socket_are_unsupported(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(probe.route_state(Path(directory) / "absent", -1), "unsupported")
        with patch.object(probe.socket, "socket",
                          side_effect=OSError(errno.EAFNOSUPPORT, "fixture")):
            self.assertEqual(probe.socket_result(socket.AF_INET6, ("::1", 1)), "unsupported")

    def test_host_ipv6_socket_creation_failure_keeps_probe_running(self):
        original = socket.socket
        def create(family=socket.AF_INET, *args, **kwargs):
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "fixture")
            return original(family, *args, **kwargs)
        with patch.object(isolation.socket, "socket", side_effect=create), \
             patch.object(wsl, "version", return_value=None):
            result = isolation.inspect(None)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["current"]["checks"]["parent_ipv6"], "unsupported")

    def test_disabled_interop_host_control_does_not_block_strict_ready(self):
        original = subprocess.run
        def run(args, **kwargs):
            if str(args[0]).endswith("cmd.exe"):
                self.assertEqual(args[1:], ["/d", "/c", "echo", "vps-route-probe"])
                return subprocess.CompletedProcess(args, 1, b"", b"")
            return original(args, **kwargs)
        if wsl.version() != 2:
            self.skipTest("WSL host fixture required")
        with patch.object(isolation.subprocess, "run", side_effect=run):
            result = isolation.inspect(None)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["current"]["checks"]["windows_execution"], "unsupported")

    def test_real_workspace_plan_uses_daily_mount_flags_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, profile = root / "project", root / "profile"
            workspace.mkdir()
            profile.mkdir()
            (workspace / "keep.txt").write_text("unchanged")
            with patch.object(Config, "profile_home", new_callable=lambda: property(lambda c: profile)), \
                 patch.object(isolation, "sandbox_command", wraps=runner.sandbox_command) as builder:
                result = isolation.workspace_plan(config(), workspace)
            self.assertTrue(result["passed"], result)
            self.assertTrue(builder.call_args.kwargs["project_instructions"])
            self.assertTrue(builder.call_args.kwargs["reconnectable"])
            self.assertEqual(list(workspace.iterdir()), [workspace / "keep.txt"])
            self.assertEqual((workspace / "keep.txt").read_text(), "unchanged")

    def test_fresh_maintenance_profile_is_private_without_changing_existing_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "new-parent/profile"
            with patch.object(Config, "profile_home", new_callable=lambda: property(lambda c: profile)):
                workspace = cli.private_workspace(config())
                self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
                self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
                profile.chmod(0o750)
                cli.private_workspace(config())
                self.assertEqual(profile.stat().st_mode & 0o777, 0o750)

    def test_workspace_refusal_is_separate_from_fixture(self):
        result = isolation.workspace_plan(config(), Path("/no-such-fixture-project"))
        self.assertFalse(result["passed"])
        self.assertFalse(result["executed"])


if __name__ == "__main__":
    unittest.main()
