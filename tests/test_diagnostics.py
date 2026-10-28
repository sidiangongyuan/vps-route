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
from claude_launcher.diagnostics import (
    _local_https_probe, connect_diagnostics, connection_diagnosis, local_network_diagnostics,
)
from claude_launcher.remote import ConnectHandler, ConnectServer
from claude_launcher.wire import PROTOCOL_VERSION, RouteError, server_health


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("203.0.113.1", "fixture", 22, Path("unused"), Path("unused"),
                             "/unused.sock", "1.1.1.1", "US")
        controls = patch("claude_launcher.diagnostics.local_network_diagnostics", return_value={
            "stage": "local_network", "result": "https_reachable", "probes": [],
        })
        self.controls = controls.start()
        self.addCleanup(controls.stop)

    def test_https_control_uses_only_configured_proxy_and_verifies_tls(self):
        from dataclasses import replace
        import ssl

        config = replace(self.config, ssh_http_proxy="http://127.0.0.1:7890")
        with patch("claude_launcher.diagnostics.http.client.HTTPSConnection") as client:
            client.return_value.getresponse.return_value.status = 204
            self.assertEqual(_local_https_probe(config, "https://example.org/test"),
                             "https_reachable")
            self.assertEqual(client.call_args.args, ("127.0.0.1", 7890))
            context = client.call_args.kwargs["context"]
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            client.return_value.set_tunnel.assert_called_once_with("example.org", 443)
            client.return_value.request.side_effect = OSError("private connection details")
            self.assertEqual(_local_https_probe(config, "https://example.org/test"),
                             "probe_transport_failure")
            self.assertEqual(client.call_count, 2)
            self.assertEqual(client.call_args.args, ("127.0.0.1", 7890))
            self.assertEqual(client.return_value.close.call_count, 2)

    def test_explicit_system_network_control_ignores_environment_proxy(self):
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://unrelated:99"}), \
                patch("claude_launcher.diagnostics.http.client.HTTPSConnection") as client:
            client.return_value.getresponse.return_value.status = 403
            self.assertEqual(_local_https_probe(self.config, "https://example.org/test"),
                             "https_reachable")
            self.assertEqual(client.call_args.args, ("example.org", 443))
            client.return_value.set_tunnel.assert_not_called()

    def test_control_targets_are_independent_and_one_success_proves_connectivity(self):
        def probe(config, url):
            return "https_reachable" if url == config.connectivity_urls[0] else "probe_timeout"

        with patch("claude_launcher.diagnostics._local_https_probe", side_effect=probe):
            result = local_network_diagnostics(self.config)
        self.assertEqual(result["result"], "https_reachable")
        self.assertEqual(len(result["probes"]), len(self.config.connectivity_urls))
        self.assertEqual(result["probes"][1]["result"], "probe_timeout")

    def test_failed_controls_do_not_block_a_working_vps_route(self):
        self.controls.return_value = {"stage": "local_network", "result": "unconfirmed"}
        with patch("claude_launcher.diagnostics.SSHTunnel") as tunnel, \
                patch("claude_launcher.diagnostics.server_health", return_value={
                    "status": "available", "compatible": True,
                }), patch("claude_launcher.diagnostics.connectivity_check", return_value=None), \
                patch("claude_launcher.diagnostics.observe_exit", return_value={
                    "exit_info_result": "unavailable",
                }):
            result = connect_diagnostics(self.config)
        tunnel.return_value.start.assert_called_once()
        self.assertTrue(result["connectivity_ok"])
        self.assertEqual(result["diagnosis"]["fault_domain"], "none")
        self.assertEqual(result["stages"][1]["result"], "unconfirmed")

    def test_failed_controls_and_ssh_leave_vps_state_uncertain(self):
        self.controls.return_value = {"stage": "local_network", "result": "unconfirmed"}
        with patch("claude_launcher.diagnostics.SSHTunnel") as tunnel:
            tunnel.return_value.start.side_effect = RouteError("ssh_tunnel_start_timeout")
            result = connect_diagnostics(self.config)
        self.assertEqual(result["diagnosis"]["fault_domain"], "local_network_or_vps_path")
        self.assertFalse(result["diagnosis"]["cause_confirmed"])
        self.assertIn("优先检查本机", result["diagnosis"]["summary_zh"])

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
            self.assertFalse(result["connectivity_ok"])
            self.assertEqual(result["stages"][-1]["stage"], "ssh")
            self.assertIn("authorization", result["stages"][-1]["hint"])
            self.assertEqual(result["diagnosis"]["fault_domain"], "ssh_authentication")
            self.assertIn("connectivity", result["diagnosis"]["not_checked"])
            tunnel.return_value.dispose.assert_called_once()

    def test_ssh_timeout_does_not_blame_the_vps_or_optional_upstream(self):
        with patch("claude_launcher.diagnostics.SSHTunnel") as tunnel, \
                patch("claude_launcher.diagnostics.server_health") as health:
            tunnel.return_value.start.side_effect = RouteError("ssh_tunnel_start_timeout")
            result = connect_diagnostics(self.config)
        self.assertEqual(result["route_result"], "ssh_tunnel_start_timeout")
        diagnosis = result["diagnosis"]
        self.assertEqual(diagnosis["fault_domain"], "vps_or_network_path")
        self.assertIn("本机经配置路径访问测试网站成功", diagnosis["summary_zh"])
        self.assertFalse(diagnosis["cause_confirmed"])
        self.assertEqual(diagnosis["not_checked"], ["vps_service", "connectivity"])
        health.assert_not_called()

    def test_local_proxy_failure_stops_before_ssh(self):
        from dataclasses import replace

        config = replace(self.config, ssh_http_proxy="http://127.0.0.1:7890")
        with patch("claude_launcher.diagnostics.socket.create_connection",
                   side_effect=ConnectionRefusedError), \
                patch("claude_launcher.diagnostics.SSHTunnel") as tunnel:
            result = connect_diagnostics(config)
        self.assertEqual(result["diagnosis"]["fault_domain"], "local_environment")
        self.assertTrue(result["diagnosis"]["cause_confirmed"])
        tunnel.assert_not_called()
        self.controls.assert_not_called()

    def test_diagnosis_is_generic_for_vps_service_and_forwarded_https_failures(self):
        ssh = [{"stage": "ssh", "result": "host_verified_forward_created"}]
        service = [{"stage": "vps_service", "result": "available"}]
        result = connection_diagnosis("server_health_unavailable", ssh)
        self.assertEqual(result["fault_domain"], "vps_proxy_service")
        self.assertEqual(result["not_checked"], ["connectivity"])
        result = connection_diagnosis("server_protocol_incompatible", ssh + service)
        self.assertEqual(result["not_checked"], ["connectivity"])
        for reason in ("upstream_unavailable", "probe_tls_failed", "target_resolution_failed"):
            with self.subTest(reason=reason):
                result = connection_diagnosis(reason, ssh + service)
                self.assertEqual(result["fault_domain"], "vps_to_destination")
                self.assertFalse(result["cause_confirmed"])

    def test_missing_local_dependencies_have_a_diagnosis_without_network_access(self):
        with patch.object(cli, "namespace_available", return_value=False), \
                patch.object(cli, "system_python_compatible", return_value=True), \
                patch("claude_launcher.diagnostics.connect_diagnostics") as connect, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.doctor(self.config, connect=True), 69)
        result = json.loads(output.getvalue())
        self.assertFalse(result["network_probe_executed"])
        self.assertEqual(result["diagnosis"]["fault_domain"], "local_environment")
        connect.assert_not_called()

    def test_protocol_mismatch_blocks_exit_probe_and_does_not_deploy(self):
        with patch("claude_launcher.diagnostics.SSHTunnel"), patch(
            "claude_launcher.diagnostics.server_health",
            return_value={"status": "available", "compatible": False, "protocol": 999},
        ), patch("claude_launcher.diagnostics.connectivity_check") as probe:
            result = connect_diagnostics(self.config)
            self.assertEqual(result["route_result"], "server_protocol_incompatible")
            probe.assert_not_called()

    def test_upstream_failure_and_success_keep_sanitized_stage_information(self):
        for reason in ("upstream_unavailable", "probe_tls_failed", None):
            with self.subTest(reason=reason), patch("claude_launcher.diagnostics.SSHTunnel"), \
                    patch("claude_launcher.diagnostics.server_health", return_value={
                        "status": "available", "compatible": True, "protocol": 1,
                    }), patch("claude_launcher.diagnostics.connectivity_check", return_value=reason), \
                    patch("claude_launcher.diagnostics.proxy_json",
                          return_value={"ip": "8.8.8.8", "country": "JP"}):
                result = connect_diagnostics(self.config)
                self.assertEqual(result["connectivity_ok"], reason is None)
                self.assertNotIn("203.0.113", json.dumps(result))
                self.assertFalse(result["direct_fallback"])
                if reason is None:
                    self.assertEqual(result["observed_exit_ip"], "8.8.8.8")
                    self.assertEqual(result["observed_exit_country"], "JP")
                    self.assertFalse(result["exit_ip_pinning_enabled"])
                    self.assertEqual(result["diagnosis"]["fault_domain"], "none")
                    self.assertEqual(result["diagnosis"]["not_checked"], [])

    def test_exit_lookup_failure_is_informational_and_doctor_still_succeeds(self):
        with patch("claude_launcher.diagnostics.SSHTunnel"), \
                patch("claude_launcher.diagnostics.server_health", return_value={
                    "status": "available", "compatible": True, "protocol": 1,
                }), patch("claude_launcher.diagnostics.connectivity_check", return_value=None), \
                patch("claude_launcher.diagnostics.proxy_json",
                      side_effect=RouteError("probe_transport_failure")), \
                patch.object(cli, "namespace_available", return_value=True), \
                patch.object(cli, "system_python_compatible", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.doctor(self.config, connect=True), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["connectivity_ok"])
            self.assertIsNone(result["observed_exit_ip"])
            self.assertEqual(result["exit_info_result"], "unavailable")
            self.assertIn("VPS SSH 已连接", output.getvalue())

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
