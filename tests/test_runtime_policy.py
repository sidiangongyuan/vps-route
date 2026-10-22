"""Runtime egress checks and terminal cleanup, without live accounts/network."""

from dataclasses import replace
import os
import json
import pty
import select
import signal
import subprocess
import sys
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
    def test_child_exit_stops_checks_while_final_output_is_blocked(self):
        script = r'''
import os, sys, threading
from unittest.mock import patch
from claude_launcher.runner import run_supervised
input_reader, input_writer = os.pipe()
output_reader, output_writer = os.pipe()
os.set_blocking(output_writer, False)
while True:
    try: os.write(output_writer, b'x' * 4096)
    except BlockingIOError: break
os.set_blocking(output_writer, True)
reaped = threading.Event()
checked = threading.Event()
waitpid = os.waitpid
def observe(*args):
    result = waitpid(*args)
    if result[0]: reaped.set()
    return result
def check():
    reaped.wait(timeout=3)
    checked.set()
    return 'connectivity_check_failed'
try:
    with patch('claude_launcher.runner.os.waitpid', side_effect=observe):
        result = run_supervised(
            [sys.executable, '-c', 'import time; time.sleep(0.2); print("done")'],
            dict(os.environ), check, lambda: None, lambda: True, 0.01,
            input_fd=input_reader, output_fd=output_writer,
        )
    assert checked.is_set() and reaped.is_set()
    assert result == (0, 'child_exited'), result
finally:
    for fd in (input_reader, input_writer, output_reader, output_writer): os.close(fd)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launcher_finishes_when_exit_notice_reader_stops(self):
        script = r'''
import contextlib, os, pty, sys, tempfile, termios
from pathlib import Path
from unittest.mock import Mock, patch
from claude_launcher import cli
from claude_launcher.config import Config
from claude_launcher.wire import RouteError
for pipe in (False, True):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    reader, writer = os.pipe() if pipe else (master, slave)
    os.set_blocking(writer, False)
    while True:
        try: os.write(writer, b'x' * 4096)
        except BlockingIOError: break
    os.set_blocking(writer, True)
    with tempfile.TemporaryDirectory() as directory, \
            os.fdopen(os.dup(slave), 'r') as input_stream, \
            os.fdopen(os.dup(writer), 'w', buffering=1) as output_stream:
        home = Path(directory).resolve()
        config = Config('203.0.113.1', 'fixture', 22, home/'key', home/'hosts',
                        '/unused', '203.0.113.2', 'US')
        with patch.object(cli.Path, 'home', return_value=home):
            config.profile_home.mkdir(parents=True, mode=0o700)
            config.claude_executable.parent.mkdir(parents=True)
            config.claude_executable.touch(mode=0o700)
            with patch.object(sys, 'stdin', input_stream), \
                    patch.object(sys, 'stdout', output_stream), \
                    patch.object(cli, 'MACOS', False), \
                    patch.object(cli, 'namespace_available', return_value=True), \
                    patch.object(cli, 'system_python_compatible', return_value=True), \
                    patch.object(cli, 'SSHTunnel'), \
                    patch.object(cli, 'connectivity_check', return_value=None), \
                    patch.object(cli, '_isolated', return_value=contextlib.nullcontext(
                        (['/bin/echo', 'final output'], dict(os.environ)))), \
                    patch.object(cli, 'receipt'):
                assert cli._launch(config, home, ['--version'], Mock(operation='version')) == 0
                with patch.object(cli.Config, 'load', side_effect=RouteError('fixture_failure')), \
                        patch.object(sys, 'argv', ['vps-route', 'run']):
                    assert cli.main() == 69
            assert termios.tcgetattr(slave) == original
            assert os.get_blocking(writer)
    if pipe: os.close(reader); os.close(writer)
    os.close(slave); os.close(master)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_large_redirected_output_is_complete(self):
        reader, writer = os.pipe()  # Keep stdin open without injecting PTY EOF echo.
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        with tempfile.TemporaryFile() as output:
            result = run_supervised(
                [sys.executable, '-c', 'import os; os.write(1, b"x" * 1048576)'],
                dict(os.environ), None, lambda: None, lambda: True, 1,
                input_fd=reader, output_fd=output.fileno(),
            )
            self.assertEqual(result, (0, 'child_exited'))
            output.seek(0)
            self.assertEqual(output.read(), b'x' * 1048576)

    def test_slow_output_reader_receives_complete_output_after_child_exit(self):
        script = r'''
import os, sys, threading, time
from claude_launcher.runner import run_supervised
input_reader, input_writer = os.pipe()
output_reader, output_writer = os.pipe()
received = bytearray()
def read_slowly():
    while True:
        data = os.read(output_reader, 4096)
        if not data: return
        received.extend(data)
        time.sleep(0.12)
reader = threading.Thread(target=read_slowly, daemon=True)
reader.start()
try:
    result = run_supervised(
        [sys.executable, '-c', 'import os; os.write(1, b"x" * 131072)'],
        dict(os.environ), None, lambda: None, lambda: True, 1,
        input_fd=input_reader, output_fd=output_writer,
    )
finally:
    os.close(output_writer)
    os.close(input_reader); os.close(input_writer)
reader.join(timeout=6)
os.close(output_reader)
assert not reader.is_alive()
assert result == (0, 'child_exited'), result
assert received == b'x' * 131072, len(received)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_full_child_input_still_handles_route_loss_and_launcher_interrupt(self):
        script = r'''
import os, signal, subprocess, sys, tempfile, threading, time
from claude_launcher.runner import run_supervised
for interrupted in (False, True):
    with tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as output:
        input_file.write(b'x' * 1048576); input_file.seek(0)
        started = time.monotonic()
        closed = []
        timer = threading.Timer(0.4, lambda: os.kill(os.getpid(), signal.SIGINT))
        if interrupted: timer.start()
        try:
            result = run_supervised(
                [sys.executable, '-c', 'import termios,tty,time; tty.setraw(0,termios.TCSANOW); time.sleep(30)'],
                dict(os.environ), None, lambda: closed.append(time.monotonic()),
                lambda: interrupted or time.monotonic()-started < 0.4, 1,
                input_fd=input_file.fileno(), output_fd=output.fileno(),
            )
            assert result == (69, 'launcher_interrupted' if interrupted else 'ssh_tunnel_lost'), result
            assert closed and closed[0]-started < 1.5
            assert time.monotonic()-started < 3
        finally:
            timer.cancel()
            if interrupted: timer.join()
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_full_terminal_still_handles_ctrl_c_and_route_loss(self):
        script = r'''
import os, pty, sys, termios, threading, time
from claude_launcher.runner import run_supervised
for lost in (False, True):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    os.set_blocking(slave, False)
    while True:
        try: os.write(slave, b'x' * 4096)
        except BlockingIOError: break
    os.set_blocking(slave, True)
    timer = threading.Timer(0.4, lambda: os.write(master, b'\x03'))
    started = time.monotonic()
    closed = []
    if not lost: timer.start()
    try:
        result = run_supervised(
            [sys.executable, '-c', 'import os,time; os.write(1,b"x"*1048576); time.sleep(30)'],
            dict(os.environ), None, lambda: closed.append(True),
            lambda: not lost or time.monotonic()-started < 0.4, 1,
            input_fd=slave, output_fd=slave,
        )
        assert result == ((69,'ssh_tunnel_lost') if lost else (130,'child_exited')), result
        assert closed and time.monotonic()-started < 4
        assert termios.tcgetattr(slave) == original
    finally:
        timer.cancel()
        if not lost: timer.join()
        os.close(slave); os.close(master)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cleanup_returns_when_terminal_or_pipe_reader_stops(self):
        # A separate process bounds the regression even if a kernel tty wait
        # blocks Python's signal handler. All descriptors belong to this test.
        script = r'''
import fcntl, os, pty, select, termios, time, tty
from claude_launcher.runner import _restore_terminal, TERMINAL_RESET, run_supervised
for full in (False, True):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    tty.setraw(slave, termios.TCSANOW)
    os.write(slave, b'queued output')
    if full:
        os.set_blocking(slave, False)
        while True:
            try: os.write(slave, b'x' * 4096)
            except BlockingIOError: break
        fcntl.fcntl(slave, fcntl.F_SETFL, flags)
    flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    started = time.monotonic()
    _restore_terminal(slave, slave, original)
    assert time.monotonic() - started < 2
    assert termios.tcgetattr(slave) == original
    assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags
    if not full:
        assert TERMINAL_RESET in os.read(master, 65536)
    os.close(slave); os.close(master)

reader, writer = os.pipe()
flags = fcntl.fcntl(writer, fcntl.F_GETFL)
os.set_blocking(writer, False)
while True:
    try: os.write(writer, b'x' * 4096)
    except BlockingIOError: break
fcntl.fcntl(writer, fcntl.F_SETFL, flags)
flags = fcntl.fcntl(writer, fcntl.F_GETFL)
started = time.monotonic()
_restore_terminal(reader, writer, None, rendered_tui=True)
assert time.monotonic() - started < 1
assert fcntl.fcntl(writer, fcntl.F_GETFL) == flags
os.close(reader); os.close(writer)

# Exercise startup and normal exit while the terminal reader is stopped.
master, slave = pty.openpty()
original = termios.tcgetattr(slave)
os.set_blocking(slave, False)
while True:
    try: os.write(slave, b'x' * 4096)
    except BlockingIOError: break
os.set_blocking(slave, True)
closed = []
result = run_supervised(['/bin/echo', 'final output'], dict(os.environ), None,
                       lambda: closed.append(True), lambda: True, 1,
                       input_fd=slave, output_fd=slave)
assert result == (0, 'child_exited') and closed
assert termios.tcgetattr(slave) == original
os.close(slave); os.close(master)

# The same backpressure can occur on redirected stdout without any termios.
reader, writer = os.pipe()
os.set_blocking(writer, False)
while True:
    try: os.write(writer, b'x' * 4096)
    except BlockingIOError: break
os.set_blocking(writer, True)
with open(os.devnull, 'rb') as empty:
    result = run_supervised(['/bin/echo', 'final output'], dict(os.environ), None,
                           lambda: None, lambda: True, 1,
                           input_fd=empty.fileno(), output_fd=writer)
    assert result == (0, 'child_exited')
assert os.get_blocking(writer)
os.close(reader); os.close(writer)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)

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

    @patch("claude_launcher.cli.MACOS", False)
    @patch("claude_launcher.cli.system_python_compatible", return_value=True)
    def test_launch_checks_each_new_route_and_enables_runtime_monitor(self, _python):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            config = Config("203.0.113.1", "fixture", 22, home / "key", home / "hosts",
                            "/unused", "203.0.113.2", "US")
            with patch("claude_launcher.cli.Path.home", return_value=home):
                config.profile_home.mkdir(parents=True, mode=0o700)
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
            path = Path(directory).resolve() / "config.json"
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

    @patch("claude_launcher.cli.MACOS", False)
    def test_namespace_failure_is_logged_before_any_ssh_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
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
