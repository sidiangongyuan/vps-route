"""Exercise a real gh binary under Seatbelt with local TLS and route fixtures.

VPS_ROUTE_TEST_GH_BINARY selects the candidate. No real credentials or network
services are used. Run outside an enclosing sandbox on macOS.
"""

import contextlib
import http.server
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from claude_launcher import macos
from claude_launcher.remote import ConnectHandler, ConnectServer


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("VPS_ROUTE_TEST_GH_BINARY"),
                     "requires macOS and VPS_ROUTE_TEST_GH_BINARY")
class GhBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="route-gh-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = self.root / "profile"
        self.profile.mkdir(mode=0o700)
        self.binary = self.profile / "gh-candidate"
        shutil.copyfile(os.environ["VPS_ROUTE_TEST_GH_BINARY"], self.binary)
        self.binary.chmod(0o700)
        self.cert = self.profile / "test-ca.pem"
        self.key = self.root / "test-key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(self.key), "-out", str(self.cert), "-days", "1",
            "-subj", "/CN=probe.test", "-addext", "subjectAltName=DNS:probe.test",
        ], check=True, capture_output=True, timeout=30)

        class Web(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.server.requests += 1
                body = b'{"ok":true}'
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(self, *_):
                pass

        self.web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Web)
        self.web.requests = 0
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(self.cert, self.key)
        self.web.socket = self.tls.wrap_socket(self.web.socket, server_side=True)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.addCleanup(self.web.server_close)
        self.addCleanup(self.web.shutdown)
        self.socket = self.root / "proxy.sock"
        self.upstream = ConnectServer(str(self.socket), ConnectHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        target = [(socket.AF_INET, socket.SOCK_STREAM, 0, self.web.server_address)]
        resolver = patch("claude_launcher.remote.public_target", return_value=target)
        resolver.start()
        self.addCleanup(resolver.stop)

    @contextlib.contextmanager
    def session(self, url="https://probe.test/check", *, executable=None):
        with macos.seatbelt_session(
            self.socket, self.profile, self.profile,
            [str(executable or self.binary), "api", url, "--jq", ".ok"],
        ) as (command, environment):
            environment.update(
                GH_TOKEN="fixture-token-not-a-credential",
                GH_ENTERPRISE_TOKEN="fixture-token-not-a-credential",
                GH_NO_UPDATE_NOTIFIER="1", GH_NO_EXTENSION_UPDATE_NOTIFIER="1",
                GH_PROMPT_DISABLED="1", GH_PAGER="cat",
                SSL_CERT_FILE=str(self.cert), GODEBUG="x509sslcertoverrideplatform=1",
            )
            yield command, environment

    def run_gh(self, command, environment):
        return subprocess.run(command, env=environment, capture_output=True, text=True,
                              timeout=15, stdin=subprocess.DEVNULL)

    def test_trusted_certificate_works_without_host_trust_service(self):
        with self.session() as (command, environment):
            result = self.run_gh(command, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true")
        self.assertEqual(self.web.requests, 1)

    def test_untrusted_certificate_is_rejected(self):
        with self.session() as (command, environment):
            environment["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"
            result = self.run_gh(command, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("certificate signed by unknown authority", result.stderr)
        self.assertEqual(self.web.requests, 0)

    def test_wrong_hostname_is_rejected(self):
        with self.session("https://wrong.test/check") as (command, environment):
            result = self.run_gh(command, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("certificate is valid for probe.test, not wrong.test", result.stderr)
        self.assertEqual(self.web.requests, 0)

    def test_expired_certificate_is_rejected(self):
        expired = self.root / "expired.pem"
        request = self.root / "expired.csr"
        (self.root / "index").touch()
        (self.root / "serial").write_text("01\n")
        (self.root / "issued").mkdir()
        configuration = self.root / "ca.cnf"
        configuration.write_text(f"""[ca]
default_ca = issuer
[issuer]
database = {self.root}/index
serial = {self.root}/serial
new_certs_dir = {self.root}/issued
default_md = sha256
policy = subject
[subject]
commonName = supplied
[leaf]
subjectAltName = DNS:probe.test
basicConstraints = CA:FALSE
extendedKeyUsage = serverAuth
""")
        subprocess.run([
            "openssl", "req", "-new", "-key", str(self.key),
            "-out", str(request), "-subj", "/CN=probe.test",
        ], check=True, capture_output=True, timeout=15)
        subprocess.run([
            "openssl", "ca", "-batch", "-notext", "-config", str(configuration),
            "-in", str(request), "-out", str(expired), "-keyfile", str(self.key),
            "-cert", str(self.cert), "-startdate", "20000101000000Z",
            "-enddate", "20000102000000Z", "-extensions", "leaf",
        ], check=True, capture_output=True, timeout=15)
        self.tls.load_cert_chain(expired, self.key)
        with self.session() as (command, environment):
            result = self.run_gh(command, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("certificate has expired", result.stderr)
        self.assertEqual(self.web.requests, 0)

    def test_missing_ca_bundle_does_not_enable_insecure_tls(self):
        with self.session() as (command, environment):
            environment["SSL_CERT_FILE"] = str(self.profile / "absent-ca.pem")
            result = self.run_gh(command, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("certificate", result.stderr)
        self.assertEqual(self.web.requests, 0)

    def test_route_loss_fails_without_direct_fallback(self):
        with self.session() as (command, environment):
            before = self.run_gh(command, environment)
            self.assertEqual(before.returncode, 0, before.stderr)
            self.upstream.shutdown()
            self.upstream.server_close()
            after = self.run_gh(command, environment)
        self.assertNotEqual(after.returncode, 0)
        self.assertEqual(self.web.requests, 1)

    def test_removed_proxy_cannot_reach_another_host_listener(self):
        url = f"https://127.0.0.1:{self.web.server_address[1]}/check"
        with self.session(url) as (command, environment):
            for name in list(environment):
                if name.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
                    environment.pop(name)
            result = self.run_gh(command, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("operation not permitted", result.stderr)
        self.assertEqual(self.web.requests, 0)

    def test_wrapper_pins_public_ca_and_preserves_cli_arguments(self):
        destination = self.profile / ".local/libexec/vps-route-gh/gh"
        destination.parent.mkdir(parents=True)
        shutil.copyfile(self.binary, destination)
        destination.chmod(0o700)
        wrapper = self.profile / ".local/bin/gh"
        wrapper.parent.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "tools/macos-gh/gh"
        shutil.copyfile(source, wrapper)
        wrapper.chmod(0o700)
        with self.session(executable=wrapper) as (command, environment):
            # A project cannot cause this wrapper to trust its custom CA bundle.
            environment["SSL_CERT_DIR"] = str(self.profile)
            environment["GODEBUG"] = "x509sslcertoverrideplatform=0"
            result = self.run_gh(command, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("certificate signed by unknown authority", result.stderr)
        self.assertEqual(self.web.requests, 0)


if __name__ == "__main__":
    unittest.main()
