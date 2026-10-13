"""Runtime egress checks and terminal cleanup, without live accounts/network."""

from dataclasses import replace
import os
import json
import pty
import select
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.cli import launch
from claude_launcher.config import Config
from claude_launcher.runner import RuntimeEgressMonitor, TERMINAL_RESET, run_supervised
from claude_launcher.wire import RouteError


class RuntimePolicyTests(unittest.TestCase):
    def test_launch_checks_each_new_route_and_enables_runtime_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = Config("203.0.113.1", "fixture", 22, home / "key", home / "hosts",
                            "/unused", "203.0.113.2", "US")
            with patch("claude_launcher.cli.Path.home", return_value=home):
                executable = config.claude_executable
                executable.parent.mkdir(parents=True)
                executable.touch(mode=0o700)
                for verify_after in (False, True):
                    with self.subTest(verify_after=verify_after), \
                            patch("claude_launcher.cli.namespace_available", return_value=True), \
                            patch("claude_launcher.cli.SSHTunnel"), \
                            patch("claude_launcher.cli.egress_check", return_value=None) as probe, \
                            patch("claude_launcher.cli.sandbox_command",
                                  return_value=(["fixture"], {})), \
                            patch("claude_launcher.cli.run_supervised",
                                  return_value=(0, "child_exited")) as supervise, \
                            patch("claude_launcher.cli.receipt"):
                        self.assertEqual(launch(config, home, ["--version"],
                                                verify_after=verify_after), 0)
                        self.assertEqual(probe.call_count, 2 if verify_after else 1)
                        for call in supervise.call_args_list:
                            self.assertIsInstance(call.args[2], RuntimeEgressMonitor)
                            self.assertIs(call.args[2].config, config)
                            self.assertEqual(call.args[5], 30)

                with patch("claude_launcher.cli.namespace_available", return_value=True), \
                        patch("claude_launcher.cli.SSHTunnel"), \
                        patch("claude_launcher.cli.egress_check",
                              return_value="egress_check_timeout"), \
                        patch("claude_launcher.cli.run_supervised") as supervise, \
                        patch("claude_launcher.cli.receipt"):
                    with self.assertRaisesRegex(RouteError, "egress_check_timeout"):
                        launch(config, home, ["--version"])
                    supervise.assert_not_called()
                log = home / ".local/state/claude-launcher/logs/usage.jsonl"
                records = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(records[-1]["reason"], "egress_check_timeout")
                failed_id = records[-1]["run_id"]
                self.assertEqual([r["event"] for r in records if r["run_id"] == failed_id],
                                 ["start", "exit"])

    def test_runtime_transport_failures_are_consecutive_and_recovery_resets_count(self):
        config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"),
                        "/unused", "203.0.113.2", "US")
        monitor = RuntimeEgressMonitor(config, Path("fixture.sock"))
        with patch("claude_launcher.runner.egress_check", side_effect=[
            "egress_check_timeout", None, "probe_transport_failure", "probe_transport_failure",
        ]):
            self.assertIsNone(monitor())
            self.assertEqual(monitor.consecutive_failures, 1)
            self.assertIsNone(monitor())
            self.assertEqual(monitor.consecutive_failures, 0)
            self.assertIsNone(monitor())
            self.assertEqual(monitor(), "probe_transport_failure")

    def test_runtime_changed_exit_is_immediate_and_failure_limit_is_configurable(self):
        config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"),
                        "/unused", "203.0.113.2", "US", runtime_probe_failures=10)
        for reason in ("egress_ip_changed", "egress_country_changed"):
            with self.subTest(reason=reason), \
                    patch("claude_launcher.runner.egress_check", return_value=reason):
                self.assertEqual(RuntimeEgressMonitor(config, Path("fixture.sock"))(), reason)
        with patch("claude_launcher.runner.egress_check", return_value="probe_http_failure"):
            monitor = RuntimeEgressMonitor(replace(config, runtime_probe_failures=1),
                                           Path("fixture.sock"))
            self.assertEqual(monitor(), "probe_http_failure")

    def test_old_config_defaults_and_runtime_policy_validation(self):
        values = {
            "ssh_host": "203.0.113.1", "ssh_user": "fixture", "ssh_port": 22,
            "identity_file": "/fixture/key", "known_hosts": "/fixture/hosts",
            "remote_socket": "/unused", "expected_ip": "8.8.8.8", "expected_country": "US",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(values))
            config = Config.load(path)
            self.assertEqual(config.runtime_probe_interval_seconds, 30)
            self.assertEqual(config.runtime_probe_failures, 2)
            for field, value in [
                ("runtime_probe_interval_seconds", 0), ("runtime_probe_interval_seconds", 301),
                ("runtime_probe_interval_seconds", True),
                ("runtime_probe_interval_seconds", float("nan")),
                ("runtime_probe_failures", 0), ("runtime_probe_failures", 11),
                ("runtime_probe_failures", 1.5), ("runtime_probe_failures", True),
            ]:
                with self.subTest(field=field, value=value):
                    path.write_text(json.dumps({**values, field: value}))
                    with self.assertRaisesRegex(RouteError, "invalid_or_unreadable_config"):
                        Config.load(path)

    def test_namespace_failure_is_logged_before_any_ssh_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = Config("203.0.113.1", "fixture", 22, home / "key", home / "hosts",
                            "/unused", "203.0.113.2", "US")
            with patch.object(Path, "home", return_value=home), \
                    patch("claude_launcher.cli.namespace_available", return_value=False), \
                    patch("claude_launcher.cli.SSHTunnel") as tunnel:
                with self.assertRaisesRegex(RouteError, "network_isolation_unavailable"):
                    launch(config, home, [])
                tunnel.assert_not_called()
            records = [json.loads(line) for line in
                       (home / ".local/state/claude-launcher/logs/usage.jsonl").read_text().splitlines()]
            self.assertEqual(records[-1]["reason"], "network_isolation_unavailable")

    def test_tty_cleanup_after_normal_exit_and_transport_loss(self):
        for lost in (False, True):
            with self.subTest(transport_lost=lost):
                master, slave = pty.openpty()
                original = termios.tcgetattr(slave)
                start = time.monotonic()
                closed = []
                script = (
                    "import os,time; "
                    "os.write(1, b'\\x1b[?1003h\\x1b[?1006h\\x1b[?2004h'); "
                    f"time.sleep({30 if lost else 0.1})"
                )
                try:
                    # No background probe thread should exist in the normal runtime path.
                    with patch("claude_launcher.runner.threading.Thread",
                               side_effect=AssertionError("unexpected periodic probe")):
                        result = run_supervised(
                            ["/usr/bin/python3", "-c", script], dict(os.environ),
                            None, lambda: closed.append(True),
                            lambda: not lost or time.monotonic() - start < 0.25, 0.01,
                            input_fd=slave, output_fd=slave,
                        )
                    self.assertEqual(result, (69, "ssh_tunnel_lost") if lost
                                     else (0, "child_exited"))
                    self.assertTrue(closed)
                    self.assertEqual(termios.tcgetattr(slave), original)
                    output = bytearray()
                    while select.select([master], [], [], 0.1)[0]:
                        output.extend(os.read(master, 65536))
                    self.assertIn(b"\x1b[?1003h", output)
                    self.assertTrue(output.endswith(TERMINAL_RESET))
                finally:
                    os.close(slave)
                    os.close(master)


if __name__ == "__main__":
    unittest.main()
