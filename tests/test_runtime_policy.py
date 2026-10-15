"""Runtime egress checks and terminal cleanup, without live accounts/network."""

from dataclasses import replace
import os
import json
import pty
import select
import signal
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.cli import launch
from claude_launcher.config import Config
from claude_launcher.runner import (
    RuntimeConnectivityMonitor, TERMINAL_RESET, TERMINAL_HARD_RESET,
    connectivity_check, run_supervised,
)
from claude_launcher.wire import RouteError


class RuntimePolicyTests(unittest.TestCase):
    def test_forced_exit_resets_emulator_even_when_relay_exposes_pipes(self):
        with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
            started = time.monotonic()
            script = "import os,time; os.write(1,b'\\x1b[?1003h\\x1b[?1006h'); time.sleep(30)"
            result = run_supervised(
                ["/usr/bin/python3", "-c", script], dict(os.environ), None, lambda: None,
                lambda: time.monotonic() - started < 0.25, 1,
                input_fd=empty.fileno(), output_fd=output.fileno(),
            )
            output.seek(0)
            data = output.read()
            self.assertEqual(result, (69, "ssh_tunnel_lost"))
            self.assertIn(b"\x1b[?1003h", data)
            self.assertTrue(data.endswith(TERMINAL_HARD_RESET))

    def test_plain_redirected_output_is_not_polluted_by_terminal_reset(self):
        with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
            result = run_supervised(
                ["/bin/echo", "plain"], dict(os.environ), None, lambda: None,
                lambda: True, 1, input_fd=empty.fileno(), output_fd=output.fileno(),
            )
            output.seek(0)
            self.assertEqual(result, (0, "child_exited"))
            self.assertNotIn(b"\x1b", output.read())

    def test_readonly_stdin_tty_with_redirected_stdout_can_be_reset(self):
        master, slave = pty.openpty()
        readonly = os.open(os.ttyname(slave), os.O_RDONLY | os.O_NOCTTY)
        try:
            with tempfile.TemporaryFile() as output:
                result = run_supervised(
                    ["/bin/echo", "plain"], dict(os.environ), None, lambda: None,
                    lambda: True, 1, input_fd=readonly, output_fd=output.fileno(),
                )
            self.assertEqual(result, (0, "child_exited"))
            self.assertTrue(select.select([master], [], [], 0.1)[0])
            self.assertIn(TERMINAL_RESET, os.read(master, 65536))
        finally:
            os.close(readonly)
            os.close(slave)
            os.close(master)

    def test_connectivity_targets_receive_full_budget_and_do_not_wait_for_slow_peer(self):
        config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"),
                        "/unused", timeout_seconds=0.4)
        rendezvous = threading.Barrier(2, timeout=0.2)
        release = threading.Event()
        details = []

        def probe(_path, url, budget):
            if budget != config.timeout_seconds:
                raise RouteError("probe_transport_failure")
            rendezvous.wait()
            if url == config.connectivity_urls[0]:
                release.wait(1)

        try:
            with patch("claude_launcher.runner.proxy_reachable", side_effect=probe):
                self.assertIsNone(connectivity_check(config, Path("fixture.sock"), details.extend))
                self.assertFalse(release.is_set())
            self.assertEqual([v["reason"] for v in details], ["pending", "reachable"])
        finally:
            release.set()

    def test_connectivity_records_each_failed_target(self):
        config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"), "/unused")
        details = []
        with patch("claude_launcher.runner.proxy_reachable",
                   side_effect=RouteError("probe_transport_failure")):
            self.assertEqual(connectivity_check(config, Path("fixture.sock"), details.extend),
                             "probe_transport_failure")
        self.assertEqual([v["reason"] for v in details], ["probe_transport_failure"] * 2)
        self.assertTrue(all(v["elapsed_seconds"] >= 0 for v in details))

    def test_disconnect_discards_mouse_reports_arriving_after_terminal_reset(self):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        sent = threading.Event()
        start = time.monotonic()

        def delayed_terminal_input():
            output = bytearray()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    output.extend(os.read(master, 65536))
                    if TERMINAL_RESET in output:
                        # A remote terminal may have already sent these reports
                        # before it receives the request to stop mouse tracking.
                        time.sleep(0.05)
                        os.write(master, b"\x1b[<35;142;34M\n")
                        sent.set()
                        return

        client = threading.Thread(target=delayed_terminal_input, daemon=True)
        client.start()
        try:
            script = "import os,time; os.write(1,b'\\x1b[?1003h\\x1b[?1006h'); time.sleep(30)"
            result = run_supervised(
                ["/usr/bin/python3", "-c", script], dict(os.environ), None, lambda: None,
                lambda: time.monotonic() - start < 0.2, 0.01,
                input_fd=slave, output_fd=slave,
            )
            client.join(timeout=3)
            self.assertEqual(result, (69, "ssh_tunnel_lost"))
            self.assertTrue(sent.is_set())
            self.assertEqual(termios.tcgetattr(slave), original)
            self.assertFalse(select.select([slave], [], [], 0.05)[0],
                             "late mouse reports escaped into the shell input queue")
        finally:
            client.join(timeout=3)
            os.close(slave)
            os.close(master)

    def test_redirected_stdout_still_resets_the_interactive_input_terminal(self):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        try:
            with tempfile.TemporaryFile() as output:
                result = run_supervised(
                    ["/usr/bin/python3", "-c", "print('synthetic output')"],
                    dict(os.environ), None, lambda: None, lambda: True, 0.01,
                    input_fd=slave, output_fd=output.fileno(),
                )
                output.seek(0)
                self.assertNotIn(TERMINAL_RESET, output.read())
            self.assertEqual(result, (0, "child_exited"))
            self.assertEqual(termios.tcgetattr(slave), original)
            terminal_output = bytearray()
            while select.select([master], [], [], 0.05)[0]:
                terminal_output.extend(os.read(master, 65536))
            self.assertIn(TERMINAL_RESET, terminal_output)
        finally:
            os.close(slave)
            os.close(master)

    def test_route_cleanup_exception_still_restores_terminal_and_reaps_child(self):
        import subprocess

        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        handlers = {number: signal.getsignal(number)
                    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH)}
        children = []
        popen = subprocess.Popen

        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        def broken_close():
            raise OSError("synthetic route close failure")

        start = time.monotonic()
        try:
            with patch("claude_launcher.runner.subprocess.Popen", side_effect=spawn):
                with self.assertRaisesRegex(OSError, "synthetic route close failure"):
                    run_supervised(
                        ["/bin/sleep", "30"], dict(os.environ), None, broken_close,
                        lambda: time.monotonic() - start < 0.15, 0.01,
                        input_fd=slave, output_fd=slave,
                    )
            self.assertEqual(termios.tcgetattr(slave), original)
            self.assertIsNotNone(children[0].poll(), "test child was left running")
            for number, handler in handlers.items():
                self.assertEqual(signal.getsignal(number), handler)
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            termios.tcsetattr(slave, termios.TCSANOW, original)
            for number, handler in handlers.items():
                signal.signal(number, handler)
            os.close(slave)
            os.close(master)

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
                            patch("claude_launcher.cli.connectivity_check", return_value=None) as probe, \
                            patch("claude_launcher.cli.sandbox_command",
                                  return_value=(["fixture"], {})), \
                            patch("claude_launcher.cli.run_supervised",
                                  return_value=(0, "child_exited")) as supervise, \
                            patch("claude_launcher.cli.receipt"):
                        self.assertEqual(launch(config, home, ["--version"],
                                                verify_after=verify_after), 0)
                        self.assertEqual(probe.call_count, 2 if verify_after else 1)
                        for call in supervise.call_args_list:
                            self.assertIsInstance(call.args[2], RuntimeConnectivityMonitor)
                            self.assertIs(call.args[2].config, config)
                            self.assertEqual(call.args[5], 30)
                            self.assertIsNone(call.kwargs.get("reconnect"))

                with patch("claude_launcher.cli.namespace_available", return_value=True), \
                        patch("claude_launcher.cli.SSHTunnel") as tunnel, \
                        patch("claude_launcher.cli.connectivity_check", return_value=None), \
                        patch("claude_launcher.cli.sandbox_command",
                              return_value=(["fixture"], {})) as sandbox, \
                        patch("claude_launcher.cli.run_supervised",
                              return_value=(0, "child_exited")) as supervise, \
                        patch("claude_launcher.cli.receipt"):
                    self.assertEqual(launch(config, home, []), 0)
                    self.assertTrue(sandbox.call_args.kwargs["reconnectable"])
                    self.assertIsNone(supervise.call_args.args[2])
                    self.assertIs(supervise.call_args.kwargs["reconnect"],
                                  tunnel.return_value.reconnect)

                with patch("claude_launcher.cli.namespace_available", return_value=True), \
                        patch("claude_launcher.cli.SSHTunnel"), \
                        patch("claude_launcher.cli.connectivity_check",
                              return_value="connectivity_check_timeout"), \
                        patch("claude_launcher.cli.run_supervised") as supervise, \
                        patch("claude_launcher.cli.receipt"):
                    with self.assertRaisesRegex(RouteError, "connectivity_check_timeout"):
                        launch(config, home, ["--version"])
                    supervise.assert_not_called()
                log = home / ".local/state/claude-launcher/logs/usage.jsonl"
                records = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(records[-1]["reason"], "connectivity_check_timeout")
                failed_id = records[-1]["run_id"]
                self.assertEqual([r["event"] for r in records if r["run_id"] == failed_id],
                                 ["start", "exit"])

    def test_runtime_transport_failures_are_consecutive_and_recovery_resets_count(self):
        config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"),
                        "/unused", "203.0.113.2", "US")
        monitor = RuntimeConnectivityMonitor(config, Path("fixture.sock"))
        with patch("claude_launcher.runner.connectivity_check", side_effect=[
            "connectivity_check_timeout", None, "probe_transport_failure", "probe_transport_failure",
        ]):
            self.assertIsNone(monitor())
            self.assertEqual(monitor.consecutive_failures, 1)
            self.assertIsNone(monitor())
            self.assertEqual(monitor.consecutive_failures, 0)
            self.assertIsNone(monitor())
            self.assertEqual(monitor(), "probe_transport_failure")

    def test_runtime_failure_limit_is_configurable(self):
        config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"),
                        "/unused", "203.0.113.2", "US", runtime_probe_failures=10)
        with patch("claude_launcher.runner.connectivity_check", return_value="probe_http_failure"):
            monitor = RuntimeConnectivityMonitor(replace(config, runtime_probe_failures=1),
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
            self.assertIsNone(config.expected_ip)
            self.assertIsNone(config.expected_country)
            unpinned = {k: v for k, v in values.items()
                        if k not in {"expected_ip", "expected_country"}}
            self.assertEqual(Config.from_values(unpinned), config)
            self.assertEqual(config.runtime_probe_interval_seconds, 30)
            self.assertEqual(config.runtime_probe_failures, 2)
            for field, value in [
                ("runtime_probe_interval_seconds", 0), ("runtime_probe_interval_seconds", 301),
                ("runtime_probe_interval_seconds", True),
                ("runtime_probe_interval_seconds", float("nan")),
                ("runtime_probe_failures", 0), ("runtime_probe_failures", 11),
                ("runtime_probe_failures", 1.5), ("runtime_probe_failures", True),
                ("connectivity_urls", []), ("connectivity_urls", "https://example.com"),
                ("connectivity_urls", ["http://example.com"]),
                ("connectivity_urls", [123]),
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
                    self.assertEqual(TERMINAL_HARD_RESET in output, lost)
                finally:
                    os.close(slave)
                    os.close(master)


if __name__ == "__main__":
    unittest.main()
