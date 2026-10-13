"""Layer diagnostics and version negotiation without a real VPS/account."""

import contextlib
import io
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher import cli
from claude_launcher.config import Config
from claude_launcher.diagnostics import connect_diagnostics
from claude_launcher.remote import ConnectHandler, ConnectServer
from claude_launcher.wire import PROTOCOL_VERSION, RouteError, server_health


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("203.0.113.1", "fixture", 22, Path("unused"), Path("unused"),
                             "/unused.sock", "1.1.1.1", "US")

    def test_health_from_real_test_unix_service_never_resolves_a_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "service.sock")
            with ConnectServer(path, ConnectHandler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with patch("claude_launcher.remote.public_target") as resolve:
                        result = server_health(path)
                        self.assertTrue(result["compatible"])
                        self.assertEqual(result["protocol"], PROTOCOL_VERSION)
                        resolve.assert_not_called()
                finally:
                    server.shutdown()
                    thread.join()

    def test_old_service_is_reported_unknown_not_declared_compatible(self):
        left, right = socket.socketpair()

        def old_service():
            right.recv(4096)
            right.sendall(b"HTTP/1.1 502 Tunnel Unavailable\r\nContent-Length: 0\r\n\r\n")
            right.close()

        worker = threading.Thread(target=old_service)
        worker.start()
        with patch("claude_launcher.wire.unix_connect", return_value=left):
            self.assertIsNone(server_health("unused")["compatible"])
        worker.join()

    def test_ssh_failure_is_retained_in_json_not_lost_in_exception(self):
        with patch("claude_launcher.diagnostics.SSHTunnel") as tunnel:
            tunnel.return_value.start.side_effect = RouteError("ssh_authentication_failed")
            result = connect_diagnostics(self.config)
            self.assertFalse(result["configured_egress_matches"])
            self.assertEqual(result["stages"][-1]["stage"], "ssh")
            self.assertIn("authorization", result["stages"][-1]["hint"])
            tunnel.return_value.dispose.assert_called_once()

    def test_protocol_mismatch_blocks_exit_probe_and_does_not_deploy(self):
        with patch("claude_launcher.diagnostics.SSHTunnel"), patch(
            "claude_launcher.diagnostics.server_health",
            return_value={"status": "available", "compatible": False, "protocol": 999},
        ), patch("claude_launcher.diagnostics.egress_check") as probe:
            result = connect_diagnostics(self.config)
            self.assertEqual(result["route_result"], "server_protocol_incompatible")
            probe.assert_not_called()

    def test_upstream_failure_and_success_keep_sanitized_stage_information(self):
        for reason in ("upstream_unavailable", "probe_tls_failed", "egress_ip_changed", None):
            with self.subTest(reason=reason), patch("claude_launcher.diagnostics.SSHTunnel"), \
                    patch("claude_launcher.diagnostics.server_health", return_value={
                        "status": "available", "compatible": True, "protocol": 1,
                    }), patch("claude_launcher.diagnostics.egress_check", return_value=reason):
                result = connect_diagnostics(self.config)
                self.assertEqual(result["configured_egress_matches"], reason is None)
                self.assertNotIn("203.0.113", json.dumps(result))
                self.assertFalse(result["direct_fallback"])

    def test_doctor_report_is_private_and_does_not_read_account_settings(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            cli, "namespace_available", return_value=True,
        ), patch.object(cli, "system_python_compatible", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            path = Path(temporary) / "report.json"
            self.assertEqual(cli.doctor(None, False, report=path), 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            result = json.loads(path.read_text())
            self.assertFalse(result["live_account_files_read"])
            self.assertFalse(result["network_probe_executed"])
