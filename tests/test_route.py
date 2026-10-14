"""Local integration checks for isolation and owned-process termination.

All network traffic terminates at test-owned local sockets. No HICC command,
Claude executable, account file, real VPS, or external IP service is involved.
"""

import http.server
import os
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.config import Config
from claude_launcher.remote import ConnectHandler, ConnectServer, public_target
from claude_launcher.runner import (
    RuntimeConnectivityMonitor, SSH_KEEPALIVE_OPTIONS, connectivity_check, run_supervised,
    sandbox_command, ssh_arguments,
)
from claude_launcher.wire import RouteError, proxy_json


ROOT = Path(__file__).resolve().parent.parent


class JsonHandler(http.server.BaseHTTPRequestHandler):
    body = b'{"ip":"203.0.113.7","country":"US"}'

    def do_GET(self):
        body = self.body
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class RouteIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="claude-launcher-fixture-")
        cls.root = Path(cls.temporary.name)
        cls.cert = cls.root / "cert.pem"
        key = cls.root / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cls.cert), "-days", "1",
            "-subj", "/CN=probe.test", "-addext", "subjectAltName=DNS:probe.test",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), JsonHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.web.socket = context.wrap_socket(cls.web.socket, server_side=True)
        cls.web_thread = threading.Thread(target=cls.web.serve_forever, daemon=True)
        cls.web_thread.start()
        cls.socket_path = cls.root / "up.sock"
        cls.proxy = ConnectServer(str(cls.socket_path), ConnectHandler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        cls.context = ssl.create_default_context(cafile=str(cls.cert))

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.web.shutdown()
        cls.web.server_close()
        cls.temporary.cleanup()

    def local_target(self, authority):
        self.assertEqual(authority, "probe.test:443")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, self.web.server_address)]

    def test_ssh_keepalive_tolerates_transient_misses(self):
        cfg = Config("vps.example", "fixture", 22, Path("key"), Path("known-hosts"),
                     "/unused", "203.0.113.7", "US")
        args = ssh_arguments(cfg)
        self.assertIn("ServerAliveInterval=5", args)
        self.assertIn("ServerAliveCountMax=3", args)
        self.assertEqual(SSH_KEEPALIVE_OPTIONS,
                         ("-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"))

    def run_fixture(self, mode, check, extra=()):
        profile = self.root / ("profile-" + mode)
        profile.mkdir(exist_ok=True)
        fixture = self.root / "fixture_client.py"
        shutil.copyfile(ROOT / "tests/fixture_client.py", fixture)
        command = ["/usr/bin/python3", str(fixture), mode, *extra]
        args, environment = sandbox_command(self.socket_path, profile, self.root, command)
        with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
            code, reason = run_supervised(
                args, environment, check, lambda: None, lambda: True, 0.2,
                input_fd=empty.fileno(), output_fd=output.fileno(),
            )
            output.seek(0)
            text = output.read().decode(errors="replace")
        return code, reason, text

    def test_01_real_tls_probe_over_unix_connect(self):
        with patch("claude_launcher.remote.public_target", self.local_target):
            reading = proxy_json(str(self.socket_path), "https://probe.test/json", context=self.context)
        self.assertEqual(reading, {"ip": "203.0.113.7", "country": "US"})

    def test_02_namespace_proxy_works_and_direct_route_does_not(self):
        with patch("claude_launcher.remote.public_target", self.local_target), patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "synthetic", "CLAUDE_CODE_OAUTH_TOKEN": "synthetic",
             "ALL_PROXY": "http://127.0.0.1:1"},
        ):
            code, reason, text = self.run_fixture(
                "network", lambda: None, [str(self.cert), str(Path.home())],
            )
        self.assertEqual((code, reason), (0, "child_exited"), text)
        self.assertIn("ISOLATED_PROXY_OK", text)
        self.assertTrue((self.root / "profile-network/synthetic-state").is_file())

    def test_03_failed_check_terminates_owned_descendants_only(self):
        marker = self.root / "surviving-child"
        ready = self.root / "owned-child-ready"
        unrelated = subprocess.Popen(["/bin/sleep", "10"])
        try:
            code, reason, text = self.run_fixture(
                "sleep", lambda: "connectivity_check_failed" if ready.exists() else None,
                [str(marker), str(ready)],
            )
            self.assertIn("SYNTHETIC_SESSION_READY", text)
            self.assertEqual((code, reason), (69, "connectivity_check_failed"))
            self.assertIsNone(unrelated.poll())
            time.sleep(1.6)
            self.assertFalse(marker.exists(), "managed descendant survived shutdown")
        finally:
            unrelated.terminate()
            unrelated.wait()

    def test_04_proxy_rejects_nonpublic_destinations(self):
        with self.assertRaises(RouteError):
            public_target("127.0.0.1:443")
        with self.assertRaises(RouteError):
            public_target("127.0.0.1:22")

    def test_repeated_upstream_failure_stops_owned_tree_even_while_ssh_is_alive(self):
        marker = self.root / "upstream-surviving-child"
        ready = self.root / "upstream-owned-child-ready"
        config = Config("203.0.113.1", "fixture", 22, Path("unused"), Path("unused"),
                        "/unused", "203.0.113.7", "US")
        monitor = RuntimeConnectivityMonitor(config, self.socket_path)
        unrelated = subprocess.Popen(["/bin/sleep", "10"])
        try:
            with patch("claude_launcher.runner.connectivity_check", side_effect=lambda *_:
                       "probe_transport_failure" if ready.exists() else None):
                # run_fixture deliberately keeps route_alive=True for this test.
                code, reason, text = self.run_fixture(
                    "sleep", monitor, [str(marker), str(ready)],
                )
            self.assertIn("SYNTHETIC_SESSION_READY", text)
            self.assertEqual((code, reason), (69, "probe_transport_failure"))
            self.assertEqual(monitor.consecutive_failures, 2)
            self.assertIsNone(unrelated.poll())
            time.sleep(1.6)
            self.assertFalse(marker.exists(), "managed descendant survived upstream failure")
        finally:
            unrelated.terminate()
            unrelated.wait()

    def test_05_missing_route_has_no_direct_fallback(self):
        with self.assertRaises(RouteError):
            proxy_json(str(self.root / "missing.sock"), "https://probe.test/json")

    def test_06_connectivity_never_requires_ip_country_or_json(self):
        cfg = Config("203.0.113.1", "fixture", 22, Path("unused"), Path("unused"),
                     "/unused", "8.8.8.8", "JP",
                     connectivity_urls=("https://probe.test/text",))
        for body in (b'{"ip":"203.0.113.7","country":"US"}', b'not JSON'):
            with self.subTest(body=body), patch.object(JsonHandler, "body", body), \
                    patch("claude_launcher.remote.public_target", self.local_target), \
                    patch("claude_launcher.wire.ssl.create_default_context",
                          return_value=self.context):
                self.assertIsNone(connectivity_check(cfg, self.socket_path))

    def test_connectivity_uses_alternate_host_before_declaring_failure(self):
        cfg = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"), "/unused")
        with patch("claude_launcher.runner.proxy_reachable", side_effect=[
            RouteError("probe_transport_failure"), None,
        ]) as probe:
            self.assertIsNone(connectivity_check(cfg, self.socket_path))
            self.assertEqual([c.args[1] for c in probe.call_args_list],
                             list(cfg.connectivity_urls))
        with patch("claude_launcher.runner.proxy_reachable",
                   side_effect=RouteError("proxy_socket_unavailable")):
            self.assertEqual(connectivity_check(cfg, self.socket_path), "proxy_socket_unavailable")

    def test_07_native_symlink_update_inside_separate_profile(self):
        profile = self.root / "profile-update"
        version = profile / ".local/share/claude/versions/fixture-v1"
        version.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "tests/fake_claude.py", version)
        version.chmod(0o700)
        launcher = profile / ".local/bin/claude"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(version)
        for arguments, expected in [(["update"], "SYNTHETIC_UPDATE_OK"),
                                    (["--version"], "fixture-v2")]:
            args, environment = sandbox_command(
                self.socket_path, profile, self.root, [str(launcher), *arguments],
            )
            with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
                result = run_supervised(
                    args, environment, lambda: None, lambda: None, lambda: True, 0.2,
                    input_fd=empty.fileno(), output_fd=output.fileno(),
                )
                output.seek(0)
                text = output.read().decode()
            self.assertEqual(result, (0, "child_exited"), text)
            self.assertIn(expected, text)

    def test_08_lost_transport_stops_child_without_waiting_for_probe(self):
        profile = self.root / "profile-transport-loss"
        profile.mkdir()
        args, environment = sandbox_command(
            self.socket_path, profile, self.root, ["/bin/sleep", "30"],
        )
        lost = threading.Event()
        closed = threading.Event()
        timer = threading.Timer(0.2, lost.set)
        timer.start()
        try:
            with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
                result = run_supervised(
                    args, environment, None, closed.set,
                    lambda: not lost.is_set(), 60,
                    input_fd=empty.fileno(), output_fd=output.fileno(),
                )
            self.assertEqual(result, (69, "ssh_tunnel_lost"))
            self.assertTrue(closed.is_set())
        finally:
            timer.cancel()

    def test_09_whole_probe_has_a_deadline(self):
        release = threading.Event()
        done = threading.Event()
        cfg = Config("203.0.113.1", "fixture", 22, Path("unused"), Path("unused"),
                     "/unused", "203.0.113.7", "US", timeout_seconds=0.1)

        def blocked_probe(*_args):
            release.wait(2)
            done.set()
            return {"ip": "203.0.113.7", "country": "US"}

        try:
            with patch("claude_launcher.runner.proxy_reachable", blocked_probe):
                self.assertEqual(connectivity_check(cfg, self.socket_path), "connectivity_check_timeout")
                self.assertFalse(done.is_set())
        finally:
            release.set()
            self.assertTrue(done.wait(2))

    def test_10_remote_service_recovers_stale_socket_and_preserves_live_owner(self):
        private = self.root / "remote-service"
        private.mkdir(mode=0o700)
        path = private / "connect.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(path))
        command = ["/usr/bin/python3", "-m", "claude_launcher.remote", "--socket", str(path)]
        service = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(select.select([service.stdout], [], [], 3)[0])
            self.assertIn(b"connect_service_ready", service.stdout.readline())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            duplicate = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=3)
            self.assertEqual(duplicate.returncode, 1)
            self.assertIsNone(service.poll())
            self.assertTrue(path.exists())
        finally:
            service.terminate()
            try:
                service.wait(timeout=3)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait()
            service.stdout.close()
        self.assertEqual(service.returncode, 0)
        self.assertFalse(path.exists())

    def test_11_child_uses_host_timezone_not_inherited_tz(self):
        profile = self.root / "profile-timezone"
        profile.mkdir(mode=0o700)
        command = ["/bin/sh", "-c",
                   "printenv TZ || printf 'unset\\n'; cat /etc/timezone; "
                   "readlink /etc/localtime; date +%Z"]
        with patch.dict(os.environ, {"TZ": "Asia/Singapore"}):
            args, environment = sandbox_command(self.socket_path, profile, self.root, command)
        self.assertNotIn("TZ", environment)
        with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
            result = run_supervised(
                args, environment, lambda: None, lambda: None, lambda: True, 0.2,
                input_fd=empty.fileno(), output_fd=output.fileno(),
            )
            output.seek(0)
            lines = output.read().decode().splitlines()
        self.assertEqual(result, (0, "child_exited"))
        self.assertEqual(lines[:3], ["unset", Path("/etc/timezone").read_text().strip(),
                                     os.readlink("/etc/localtime")])
        host_environment = {key: value for key, value in os.environ.items() if key != "TZ"}
        host_zone = subprocess.check_output(["date", "+%Z"], env=host_environment,
                                            text=True).strip()
        self.assertEqual(lines[3], host_zone)

    def test_12_account_archive_cannot_be_used_as_workspace(self):
        profile = self.root / "profile-protected-workspace"
        profile.mkdir(mode=0o700)
        workspace = Path.home() / ".local/share/claude-launcher/legacy-archives"
        with self.assertRaisesRegex(RouteError, "account_or_key_directory"):
            sandbox_command(self.socket_path, profile, workspace, ["/bin/true"])

    def test_explicit_process_timezone_does_not_change_system_timezone_files(self):
        profile = self.root / "profile-explicit-timezone"
        profile.mkdir(mode=0o700)
        command = ["/bin/sh", "-c", "printenv TZ; cat /etc/timezone; date +%z"]
        args, environment = sandbox_command(self.socket_path, profile, self.root, command,
                                             timezone="Asia/Singapore")
        self.assertEqual(environment["TZ"], "Asia/Singapore")
        with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
            result = run_supervised(args, environment, None, lambda: None, lambda: True, 0.2,
                                    input_fd=empty.fileno(), output_fd=output.fileno())
            output.seek(0)
            lines = output.read().decode().splitlines()
        self.assertEqual(result, (0, "child_exited"))
        self.assertEqual(lines, ["Asia/Singapore", Path("/etc/timezone").read_text().strip(),
                                "+0800"])

    def test_13_only_tool_owned_workspaces_are_allowed_under_local(self):
        tool_profiles = self.root / ".local/share/claude-launcher/profiles"
        profile = tool_profiles / "fixture"
        with patch("claude_launcher.runner.Path.home", return_value=self.root):
            for name in ("auth-workspace", "default-workspace"):
                workspace = profile / name
                workspace.mkdir(parents=True, mode=0o700)
                args, environment = sandbox_command(
                    self.socket_path, profile, workspace, ["/bin/true"]
                )
                self.assertIn(str(workspace), args)
                self.assertEqual(environment["HOME"], str(profile))
            with self.assertRaisesRegex(RouteError, "account_or_key_directory"):
                sandbox_command(self.socket_path, profile, profile / "other", ["/bin/true"])
            workspace.rmdir()
            workspace.symlink_to(self.root, target_is_directory=True)
            with self.assertRaisesRegex(RouteError, "choose_a_project_directory"):
                sandbox_command(self.socket_path, profile, workspace, ["/bin/true"])


if __name__ == "__main__":
    unittest.main()
