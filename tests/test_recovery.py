"""Keep synthetic sessions alive through route loss; never touch real accounts."""

import os
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from claude_launcher.config import Config
from claude_launcher.runner import SSHTunnel, run_supervised, sandbox_command
from claude_launcher.wire import RouteError


class RecoveryTests(unittest.TestCase):
    def supervise(self, check, alive, reconnect, duration=0.65):
        events = []
        with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
            result = run_supervised(
                ["/usr/bin/python3", "-c",
                 f"import time,os; print(os.getpid(),flush=True); time.sleep({duration}); "
                 "print('SESSION_STILL_ALIVE',flush=True)"],
                dict(os.environ), check, lambda: None, alive, 0.01,
                input_fd=empty.fileno(), output_fd=output.fileno(), reconnect=reconnect,
                connection_event=lambda *event: events.append(event),
            )
            output.seek(0)
            text = output.read().decode()
        self.assertEqual(result, (0, "child_exited"))
        self.assertIn("SESSION_STILL_ALIVE", text)
        return text, events

    def test_probe_outage_warns_once_without_closing_session_or_healthy_ssh(self):
        calls = 0

        def check():
            nonlocal calls
            calls += 1
            return "connectivity_check_timeout" if calls <= 2 else None

        reconnect = Mock()
        text, events = self.supervise(check, lambda: True, reconnect)
        reconnect.assert_not_called()
        self.assertEqual([e[0] for e in events], ["connection_lost", "connection_restored"])
        self.assertEqual(text.count("Connection unavailable"), 1)
        self.assertEqual(text.count("VPS route reachable again"), 1)

    def test_host_verification_failure_keeps_session_but_stops_retries(self):
        reconnect = Mock(side_effect=RouteError("ssh_host_key_verification_failed"))
        text, events = self.supervise(lambda: None, lambda: False, reconnect)
        self.assertEqual(reconnect.call_count, 1)
        self.assertIn("automatic reconnect stopped", text)
        self.assertEqual(events[-1], ("reconnect_blocked", "ssh_host_key_verification_failed"))
        self.assertNotIn("connection_restored", [e[0] for e in events])

    def test_normal_exit_cancels_an_in_progress_reconnect(self):
        cancelled = threading.Event()

        def reconnect(stop):
            if stop.wait(2):
                cancelled.set()

        self.supervise(lambda: None, lambda: False, reconnect, duration=0.3)
        self.assertTrue(cancelled.is_set())

    def test_transient_reconnect_failure_is_not_retried_in_a_busy_loop(self):
        reconnect = Mock(side_effect=RouteError("ssh_tunnel_failed"))
        _, events = self.supervise(lambda: None, lambda: False, reconnect)
        self.assertEqual(reconnect.call_count, 1)  # initial retry delay is five seconds
        self.assertEqual([e[0] for e in events], ["connection_lost"])

    def test_reconnect_replaces_only_owned_socket_and_honors_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.sock"
            cfg = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"), "/unused")
            tunnel = SSHTunnel(cfg, path)
            stop = threading.Event()
            try:
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.bind(str(path))
                    info = path.stat()
                    tunnel._socket_identity = (info.st_dev, info.st_ino)
                with patch.object(tunnel, "_start") as start:
                    tunnel.reconnect(stop)
                    self.assertFalse(path.exists())
                    start.assert_called_once_with(stop)
                path.write_text("unrelated file")
                with patch.object(tunnel, "_start") as start:
                    with self.assertRaisesRegex(RouteError, "ssh_forward_socket_changed"):
                        tunnel.reconnect(stop)
                    start.assert_not_called()
                    stop.set()
                    tunnel.reconnect(stop)
                    start.assert_not_called()
                self.assertEqual(path.read_text(), "unrelated file")
            finally:
                tunnel.dispose()

    def test_restarted_ssh_keeps_verified_host_key_and_original_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key, hosts = root / "key", root / "hosts"
            key.touch()
            hosts.touch()
            cfg = Config("203.0.113.1", "fixture", 2222, key, hosts, "/remote.sock",
                         ssh_http_proxy="http://127.0.0.1:7890")
            tunnel = SSHTunnel(cfg, root / "proxy.sock")
            listeners = []

            def spawn(*_args, **_kwargs):
                listener = socket.socket(socket.AF_UNIX)
                listener.bind(str(tunnel.socket_path))
                listeners.append(listener)
                return Mock(poll=lambda: None)

            def close():
                if listeners:
                    listeners[-1].close()

            try:
                with patch("claude_launcher.runner.subprocess.Popen", side_effect=spawn) as popen, \
                        patch.object(tunnel, "close", side_effect=close), \
                        patch("claude_launcher.runner.bootstrap_proxy_options",
                              return_value=["-o", "ProxyCommand=fixture-only"]):
                    tunnel.start()
                    tunnel.reconnect(threading.Event())
                    first, second = [call.args[0] for call in popen.call_args_list]
                    self.assertEqual(first, second)
                    self.assertIn("StrictHostKeyChecking=yes", second)
                    self.assertIn("ProxyCommand=fixture-only", second)
                    self.assertEqual(second[-1], cfg.ssh_host)
                    self.assertIn(f"{tunnel.socket_path}:{cfg.remote_socket}", second)
            finally:
                close()
                tunnel._errors.close()

    def test_existing_namespace_sees_replaced_socket_without_direct_route(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                if self.request.recv(4) == b"ping":
                    self.request.sendall(b"pong")

        with tempfile.TemporaryDirectory(prefix="route-recovery-") as directory:
            root = Path(directory)
            route, profile, workspace = [root / name for name in ("route", "profile", "work")]
            for path in (route, profile, workspace):
                path.mkdir(mode=0o700)
            path = route / "proxy.sock"
            servers = []
            alive = threading.Event()

            def start():
                server = socketserver.ThreadingUnixStreamServer(str(path), Echo)
                server.daemon_threads = True
                servers.append(server)
                threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02),
                                 daemon=True).start()
                alive.set()

            def close():
                alive.clear()
                if servers:
                    servers[-1].shutdown()
                    servers[-1].server_close()

            def check():
                if (workspace / "ready").exists() and not (workspace / "down").exists():
                    close()
                    (workspace / "down").touch()
                    return "probe_transport_failure"
                return None

            def reconnect(stop):
                deadline = time.monotonic() + 5
                while not (workspace / "outage_verified").exists():
                    if stop.wait(0.02) or time.monotonic() > deadline:
                        raise RouteError("ssh_tunnel_failed")
                path.unlink()
                start()

            script = r'''
import os, socket, time
from pathlib import Path
pid = os.getpid()
def request():
    with socket.create_connection(('127.0.0.1',17878),timeout=0.3) as client:
        client.sendall(b'ping')
        assert client.recv(4) == b'pong'
def direct_blocked():
    for family, address in [(socket.AF_INET,('203.0.113.99',443)),
                            (socket.AF_INET6,('2001:db8::1',443))]:
        with socket.socket(family,socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            try:
                client.connect(address)
            except OSError:
                continue
            raise AssertionError('direct connection unexpectedly available')
    assert len(Path('/proc/net/route').read_text().splitlines()) == 1
request()
direct_blocked()
Path('ready').touch()
deadline = time.monotonic()+6
while not Path('down').exists():
    assert time.monotonic()<deadline
    time.sleep(0.02)
try:
    request()
except (OSError,AssertionError):
    pass
else:
    raise AssertionError('request unexpectedly worked while route was down')
direct_blocked()
try:
    Path('/run/claude-launcher/route/forbidden').touch()
except OSError:
    pass
else:
    raise AssertionError('route directory writable')
Path('outage_verified').touch()
while True:
    try:
        request()
        break
    except (OSError,AssertionError):
        assert time.monotonic()<deadline
        time.sleep(0.02)
direct_blocked()
assert os.getpid()==pid
print('SAME_PROCESS_RECOVERED_NO_DIRECT_ROUTE',flush=True)
time.sleep(0.3)
'''
            start()
            try:
                args, env = sandbox_command(path, profile, workspace,
                                            ["/usr/bin/python3", "-c", script],
                                            reconnectable=True)
                with open(os.devnull, "rb") as empty, tempfile.TemporaryFile() as output:
                    events = []
                    result = run_supervised(
                        args, env, check, close, alive.is_set, 0.02,
                        input_fd=empty.fileno(), output_fd=output.fileno(),
                        reconnect=reconnect, connection_event=lambda *event: events.append(event),
                    )
                    output.seek(0)
                    text = output.read().decode()
                self.assertEqual(result, (0, "child_exited"), text)
                self.assertIn("SAME_PROCESS_RECOVERED_NO_DIRECT_ROUTE", text)
                self.assertEqual([e[0] for e in events],
                                 ["connection_lost", "connection_restored"])
            finally:
                close()


if __name__ == "__main__":
    unittest.main()
