"""Real installed Go/pip/Git must reject a local untrusted TLS endpoint.

Set VPS_ROUTE_TEST_DEV_PROFILE to the configured profile. Uses test-owned local
servers and temporary projects; no VPS or real credentials are accessed.
"""

import http.server
import os
from pathlib import Path
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


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('VPS_ROUTE_TEST_DEV_PROFILE'),
                     'requires macOS and VPS_ROUTE_TEST_DEV_PROFILE')
class DevelopmentTlsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='route-dev-tls-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = Path(os.environ['VPS_ROUTE_TEST_DEV_PROFILE']).resolve()
        self.cert, self.key = self.root / 'cert.pem', self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(self.key), '-out', str(self.cert), '-days', '1',
                        '-subj', '/CN=probe.test', '-addext', 'subjectAltName=DNS:probe.test'],
                       check=True, capture_output=True, timeout=30)

        class Web(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.server.requests += 1
                handler.send_response(200)
                handler.send_header('Content-Length', '0')
                handler.end_headers()

            def log_message(self, *_):
                pass

        self.web = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Web)
        self.web.requests = 0
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.cert, self.key)
        self.web.socket = tls.wrap_socket(self.web.socket, server_side=True)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.addCleanup(self.web.server_close)
        self.addCleanup(self.web.shutdown)
        self.socket = self.root / 'proxy.sock'
        self.upstream = ConnectServer(str(self.socket), ConnectHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        target = [(socket.AF_INET, socket.SOCK_STREAM, 0, self.web.server_address)]
        resolver = patch('claude_launcher.remote.public_target', return_value=target)
        resolver.start()
        self.addCleanup(resolver.stop)

    def run_tool(self, command):
        with macos.seatbelt_session(self.socket, self.profile, self.root, command) as (args, env):
            env.update(GOCACHE=str(self.root / 'cache'), GOMODCACHE=str(self.root / 'modules'),
                       GOPATH=str(self.root / 'gopath'), GOENV='off', GIT_TERMINAL_PROMPT='0',
                       PIP_CACHE_DIR=str(self.root / 'pip-cache'))
            return subprocess.run(args, env=env, capture_output=True, text=True, timeout=90)

    def assert_rejected(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex((result.stdout + result.stderr).lower(),
                         'unknown authority|certificate_verify_failed|certificate problem')
        self.assertEqual(self.web.requests, 0)

    def test_go_certificate_validation(self):
        (self.root / 'probe.go').write_text('package main\nimport ("net/http"; "os"; "fmt")\n'
            'func main(){_,e:=http.Get("https://probe.test/");if e!=nil {fmt.Println(e);os.Exit(1)}}\n')
        self.assert_rejected(self.run_tool(['go', 'run', 'probe.go']))

    def test_pip_certificate_validation(self):
        self.assert_rejected(self.run_tool(['python3', '-m', 'pip', '--disable-pip-version-check',
            '-vv', 'download', '--no-cache-dir', '--no-deps', '--retries', '0', '--timeout', '5',
            '-d', 'download', 'https://probe.test/probe-1.0-py3-none-any.whl']))

    def test_git_certificate_validation(self):
        self.assert_rejected(self.run_tool(['git', 'ls-remote', 'https://probe.test/repository.git']))


if __name__ == '__main__':
    unittest.main()
