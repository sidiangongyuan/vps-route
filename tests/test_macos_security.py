"""Native Seatbelt regression tests. Only test-owned files, processes and sockets.

Run on macOS outside an existing sandbox; no account or VPS is required.
"""

import json
import http.server
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher import macos
from claude_launcher.remote import ConnectHandler, ConnectServer


@unittest.skipUnless(sys.platform == "darwin", "requires native Seatbelt")
class NativeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="route-boundary-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.outside = self.root / "host-file"
        self.outside.write_text("test-owned data")
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]
        self.policy = macos.seatbelt_profile(self.port, self.profile, [])

    def run_code(self, code, *arguments):
        # The shell creates a child; no proxy variables are set.
        result = subprocess.run(
            [macos.SANDBOX_EXEC, "-p", self.policy, "/bin/sh", "-c",
             '"$@"; rc=$?; exit "$rc"', "probe", os.path.realpath(sys.executable),
             "-I", "-S", "-c", code, *map(str, arguments)],
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.profile)}, cwd="/",
            capture_output=True, text=True, timeout=15, close_fds=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_host_mach_services_are_denied(self):
        outcomes = self.run_code('''
import ctypes, json
lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
bootstrap = ctypes.c_uint.in_dll(lib, 'bootstrap_port')
result = {}
for name in ('com.apple.coreservices.launchservicesd', 'com.apple.securityd.xpc',
             'com.apple.trustd.agent'):
    port = ctypes.c_uint()
    result[name] = lib.bootstrap_look_up(bootstrap, name.encode(), ctypes.byref(port))
    if port.value:
        lib.mach_port_deallocate(lib.mach_task_self(), port)
print(json.dumps(result))
''')
        self.assertEqual(set(outcomes.values()), {1100})  # BOOTSTRAP_NOT_PRIVILEGED

    def test_signal_to_host_parent_is_denied(self):
        result = self.run_code('''
import json, os, sys
try:
    os.kill(int(sys.argv[1]), 0)
    result = 'allowed'
except PermissionError:
    result = 'blocked'
print(json.dumps(result))
''', os.getpid())
        self.assertEqual(result, "blocked")

    def test_host_files_are_closed_and_profile_is_writable(self):
        result = self.run_code('''
import json, sys
from pathlib import Path
outside, profile = map(Path, sys.argv[1:])
result = {}
for name, action in [('read', outside.read_text),
                     ('write', lambda: outside.write_text('changed'))]:
    try:
        action(); result[name] = 'allowed'
    except PermissionError:
        result[name] = 'blocked'
own = profile / 'output'
own.write_text('own data')
result['own'] = own.read_text()
print(json.dumps(result))
''', self.outside, self.profile)
        self.assertEqual(result, {"read": "blocked", "write": "blocked", "own": "own data"})

    def test_only_proxy_tcp_is_allowed_and_child_cannot_bind(self):
        result = self.run_code('''
import errno, json, socket, sys
port = int(sys.argv[1])
def attempt(kind, action, address):
    try:
        with socket.socket(socket.AF_INET, kind) as s:
            s.settimeout(1)
            if action == 'udp': s.sendto(b'test', address)
            else: getattr(s, action)(address)
        return 'allowed'
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
print(json.dumps({
    'proxy': attempt(socket.SOCK_STREAM, 'connect', ('127.0.0.1', port)),
    'udp_proxy': attempt(socket.SOCK_DGRAM, 'udp', ('127.0.0.1', port)),
    'bind': attempt(socket.SOCK_STREAM, 'bind', ('127.0.0.1', 0)),
    'direct': attempt(socket.SOCK_STREAM, 'connect', ('203.0.113.1', 443)),
}))
''', self.port)
        self.assertEqual(result, {"proxy": "allowed", "udp_proxy": "EPERM",
                                  "bind": "EPERM", "direct": "EPERM"})

    def test_host_file_cannot_be_reached_by_symlink_or_hardlink(self):
        result = self.run_code('''
import json, os, sys
from pathlib import Path
outside, profile = map(Path, sys.argv[1:])
alias = profile / 'alias'
alias.symlink_to(outside)
result = {}
try:
    alias.read_text(); result['symlink'] = 'allowed'
except PermissionError:
    result['symlink'] = 'blocked'
try:
    os.link(outside, profile / 'hardlink'); result['hardlink'] = 'allowed'
except PermissionError:
    result['hardlink'] = 'blocked'
print(json.dumps(result))
''', self.outside, self.profile)
        self.assertEqual(result, {"symlink": "blocked", "hardlink": "blocked"})

    def test_data_volume_alias_does_not_expose_host_file(self):
        alias = Path("/System/Volumes/Data") / self.outside.relative_to("/")
        if not alias.exists():
            self.skipTest("Data volume alias is not present")
        result = self.run_code('''
import json, sys
from pathlib import Path
try:
    Path(sys.argv[1]).read_text(); result = 'allowed'
except PermissionError:
    result = 'blocked'
print(json.dumps(result))
''', alias)
        self.assertEqual(result, "blocked")

    def test_protected_subtree_cannot_be_moved_out_of_policy(self):
        vault = self.profile / "vault"
        vault.mkdir()
        secret = vault / "credential"
        secret.write_text("test-owned data")
        self.policy = macos.seatbelt_profile(self.port, self.profile, [secret])
        result = self.run_code('''
import json, sys
from pathlib import Path
vault = Path(sys.argv[1])
result = {}
for name, action in [('read', lambda: (vault / 'credential').read_text()),
                     ('rename', lambda: vault.rename(vault.with_name('moved')))]:
    try:
        action(); result[name] = 'allowed'
    except PermissionError:
        result[name] = 'blocked'
ordinary = vault.parent / 'ordinary'
ordinary.write_text('ok')
ordinary.rename(vault.parent / 'renamed')
result['ordinary_rename'] = 'allowed'
print(json.dumps(result))
''', vault)
        self.assertEqual(result, {"read": "blocked", "rename": "blocked",
                                  "ordinary_rename": "allowed"})

    def test_real_session_tls_and_private_temporary_directory(self):
        # The TLS key stays outside the sandbox. Only its public certificate is shared.
        cert, key = self.profile / "ca.pem", self.root / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=probe.test", "-addext", "subjectAltName=DNS:probe.test",
        ], check=True, capture_output=True, timeout=30)

        class Web(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"TLS_THROUGH_ROUTE")

            def log_message(self, *_):
                pass

        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Web)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        web.socket = context.wrap_socket(web.socket, server_side=True)
        upstream = self.root / "upstream.sock"
        proxy = ConnectServer(str(upstream), ConnectHandler)
        for server in (web, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            target = [(socket.AF_INET, socket.SOCK_STREAM, 0, web.server_address)]
            with patch("claude_launcher.remote.public_target", return_value=target), \
                    macos.seatbelt_session(upstream, self.profile, self.profile, [
                        "/usr/bin/curl", "--silent", "--show-error", "--fail", "--max-time", "5",
                        "--cacert", str(cert), "https://probe.test/",
                    ]) as (args, env):
                result = subprocess.run(args, env=env, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"TLS_THROUGH_ROUTE")
                temporary = Path(env["TMPDIR"])
                self.assertTrue(temporary.is_dir())
                self.assertEqual(temporary.stat().st_mode & 0o777, 0o700)
            self.assertFalse(temporary.exists())
        finally:
            for server in (proxy, web):
                server.shutdown()
                server.server_close()

    def test_node_runtime_and_controlling_terminal(self):
        from claude_launcher.macos_check import _pty_check

        self.assertEqual(_pty_check(self.policy, self.profile, self.port), "ok")
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed")
        # Node does not need access to host Mach services to spawn a sandboxed child.
        result = subprocess.run(
            [macos.SANDBOX_EXEC, "-p", self.policy, node, "-e",
             "console.log(require('child_process').execFileSync('/bin/echo',['ok']).toString().trim())"],
            env={"HOME": str(self.profile), "PATH": "/usr/bin:/bin"}, cwd="/",
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr)

    def test_bun_runtime_from_private_profile(self):
        bun = shutil.which("bun")
        if bun is None:
            self.skipTest("Bun is not installed")
        executable = self.profile / "bun"
        shutil.copyfile(bun, executable)
        executable.chmod(0o700)
        result = subprocess.run(
            [macos.SANDBOX_EXEC, "-p", self.policy, str(executable), "-e",
             "console.log(Bun.spawnSync(['/bin/echo','ok']).stdout.toString().trim())"],
            env={"HOME": str(self.profile), "PATH": "/usr/bin:/bin"}, cwd="/",
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr)


if __name__ == "__main__":
    unittest.main()
