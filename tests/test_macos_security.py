"""Native Seatbelt regression tests. Only test-owned files, processes and sockets.

Run on macOS outside an existing sandbox; no account or VPS is required.
"""

import json
import http.server
import os
import shutil
import socket
import select
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import termios
import pty
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher import macos
from claude_launcher.remote import ConnectHandler, ConnectServer
from claude_launcher.runner import run_supervised


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

    def test_interruption_restores_terminal_after_isolated_child_exits(self):
        code = r'''
import os, socket, time
for family, address in [(socket.AF_INET, ('203.0.113.1',443)),
                        (socket.AF_INET6, ('::1', PROXY_PORT))]:
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        try: connection.connect(address)
        except PermissionError: pass
        else: raise AssertionError('unexpected network permission')
print('ISOLATION_OK', flush=True)
time.sleep(30)
'''.replace('PROXY_PORT', str(self.port))
        for target in ('child', 'launcher'):
            with self.subTest(target=target):
                master, slave = pty.openpty()
                original = termios.tcgetattr(slave)
                handlers = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH)}
                ready = threading.Event()
                closed = []

                def interrupt():
                    data = b''
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            data += os.read(master, 65536)
                            if b'ISOLATION_OK' in data:
                                ready.set()
                                if target == 'child':
                                    os.write(master, b'\x03')
                                else:
                                    os.kill(os.getpid(), signal.SIGINT)
                                return

                worker = threading.Thread(target=interrupt, daemon=True)
                worker.start()
                started = time.monotonic()
                try:
                    result = run_supervised(
                        [macos.SANDBOX_EXEC, '-p', self.policy, os.path.realpath(sys.executable),
                         '-I', '-S', '-c', code],
                        {'PATH': '/usr/bin:/bin', 'HOME': str(self.profile)},
                        None, lambda: closed.append(True), lambda: time.monotonic()-started < 6, 1,
                        input_fd=slave, output_fd=slave,
                    )
                    worker.join(timeout=1)
                    self.assertTrue(ready.is_set())
                    self.assertEqual(result, (130, 'child_exited') if target == 'child'
                                     else (69, 'launcher_interrupted'))
                    self.assertTrue(closed)
                    self.assertEqual(termios.tcgetattr(slave), original)
                    self.assertEqual({n: signal.getsignal(n) for n in handlers}, handlers)
                    self.assertLess(time.monotonic()-started, 5)
                finally:
                    worker.join(timeout=1)
                    os.close(slave)
                    os.close(master)

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

    def test_claude_temp_files_stay_inside_session_directory(self):
        code = r'''
import json, os
from pathlib import Path
base = Path(os.environ['CLAUDE_CODE_TMPDIR'])
target = base / ('claude-' + str(os.getuid()))
target.mkdir()
(target / 'settings-probe').write_text('fixture')
result = {'private_write': (target / 'settings-probe').read_text() == 'fixture'}
try:
    Path(os.environ['SHARED_PROBE']).write_text('unexpected')
    result['shared_write'] = 'allowed'
except PermissionError:
    result['shared_write'] = 'blocked'
print(json.dumps(result))
'''
        with tempfile.TemporaryDirectory(prefix="route-shared-probe-", dir="/private/tmp") as shared:
            with macos.seatbelt_session(self.root / "unused.sock", self.profile, self.profile,
                                       [os.path.realpath(sys.executable), "-I", "-S", "-c", code]) as (args, env):
                env['SHARED_PROBE'] = str(Path(shared) / 'probe')
                result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 {'private_write': True, 'shared_write': 'blocked'})
                temporary = Path(env['CLAUDE_CODE_TMPDIR'])
            self.assertFalse(temporary.exists())

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

    def test_extra_roots_keep_file_and_network_boundaries_when_route_is_down(self):
        work, misc = self.root / "work", self.root / "misc"
        work.mkdir()
        misc.mkdir()
        (work / "escape").symlink_to(self.outside)
        protected = misc / "protected"
        protected.write_text("test credential")
        code = r'''
import json, os, socket, sys
from pathlib import Path
from urllib.parse import urlsplit
work, misc, outside, protected = map(Path, sys.argv[1:])
r = {}
for root in (work, misc):
    file = root / 'ordinary'
    file.write_text('allowed')
    r[root.name] = file.read_text()
for name, path in [('outside', outside), ('symlink', work / 'escape'), ('protected', protected)]:
    try: path.read_text(); r[name] = 'allowed'
    except PermissionError: r[name] = 'blocked'
proxy = urlsplit(os.environ['HTTPS_PROXY'])
with socket.create_connection((proxy.hostname, proxy.port), timeout=2) as connection:
    try:
        connection.sendall(b'CONNECT example.com:443 HTTP/1.1\r\n\r\n')
        r['route_down'] = not connection.recv(1)
    except OSError:
        r['route_down'] = True
# Try bypassing the unavailable proxy, without using any proxy variables.
for name, address in [('direct', ('203.0.113.1',443)), ('host_service', ('127.0.0.1',22))]:
    try: socket.create_connection(address, timeout=1); r[name] = 'allowed'
    except PermissionError: r[name] = 'blocked'
print(json.dumps(r))
'''
        with macos.seatbelt_session(
            self.root / "absent-upstream.sock", self.profile, self.profile,
            [os.path.realpath(sys.executable), "-I", "-S", "-c", code,
             str(work), str(misc), str(self.outside), str(protected)],
            allowed_roots=(str(work), str(misc)), protected_files=(protected,),
        ) as (args, env):
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                'work': 'allowed', 'misc': 'allowed', 'outside': 'blocked',
                'symlink': 'blocked', 'protected': 'blocked', 'route_down': True,
                'direct': 'blocked', 'host_service': 'blocked',
            })

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
    'direct_proxy_port': attempt(socket.SOCK_STREAM, 'connect', ('203.0.113.1', port)),
}))
''', self.port)
        self.assertEqual(result, {"proxy": "allowed", "udp_proxy": "EPERM",
                                  "bind": "EPERM", "direct": "EPERM", "direct_proxy_port": "EPERM"})

    def test_ipv6_host_service_on_the_proxy_port_is_denied(self):
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as other:
            other.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            other.bind(("::1", self.port))
            other.listen(1)
            result = self.run_code('''
import errno, json, socket, sys
try:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        connection.connect(('::1', int(sys.argv[1])))
    result = 'connected'
except OSError as error:
    result = errno.errorcode.get(error.errno, str(error.errno))
print(json.dumps(result))
''', self.port)
            self.assertEqual(result, "EPERM")

    def test_self_check_rejects_a_rule_that_allows_the_ipv6_proxy_port(self):
        broad = self.policy.replace('(remote tcp4 ', '(remote tcp ')
        observed = macos.run_self_check(broad, self.port, Path.home())
        self.assertIn("loopback_proxy_ipv6", macos.check_failures(observed))

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
