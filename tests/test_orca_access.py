"""Local synthetic Orca control without widening the child's network namespace."""

import json
import os
import shlex
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher import cli, runner
from claude_launcher.config import Config
from claude_launcher.orca_access import SANDBOX_ROOT, prepare_orca_access
from claude_launcher.wire import RouteError


class OrcaAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="orca-access-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.data = self.home / ".orca"
        self.data.mkdir(mode=0o700)
        self.socket = self.data / "runtime.sock"
        self.bootstrap = self.data / "orca-runtime.json"
        self.bootstrap.write_text(json.dumps({
            "pid": os.getpid(), "runtimeId": "synthetic-runtime", "authToken": "fixture-token",
            "transports": [{"kind": "unix", "endpoint": str(self.socket)},
                           {"kind": "websocket", "endpoint": "ws://127.0.0.1:9999"}],
            "unrelatedAccountData": "must not be copied",
        }))
        self.bootstrap.chmod(0o600)
        (self.data / "private-account").write_text("fixture account data")
        code = self.home / ".orca-remote/orcad-fixture/out/cli"
        code.mkdir(parents=True)
        script = code / "index.js"
        script.touch()
        node = self.home / ".orca-remote/node-fixture/bin/node"
        node.parent.mkdir(parents=True)
        node.write_text('''#!/usr/bin/python3
import json,os,socket
from pathlib import Path
b=json.loads((Path(os.environ['ORCA_USER_DATA_PATH'])/'orca-runtime.json').read_text())
with socket.socket(socket.AF_UNIX) as s:
 s.connect(b['transports'][0]['endpoint'])
 s.sendall((json.dumps({'authToken':b['authToken'],'method':'status'})+'\\n').encode())
 print(s.recv(4096).decode(),end='')
''')
        node.chmod(0o700)
        self.command = self.data / "cli/bin/orca"
        self.command.parent.mkdir(parents=True)
        self.command.write_text("#!/bin/sh\nexport ORCA_USER_DATA_PATH="
                                + shlex.quote(str(self.data)) + "\nexec "
                                + shlex.join([str(node), str(script)]) + ' "$@"\n')
        self.command.chmod(0o700)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                message = json.loads(self.rfile.readline())
                if message.get("authToken") == "fixture-token":
                    self.wfile.write(b'{"ok":true,"runtime":"synthetic-runtime"}\n')

        self.server = socketserver.ThreadingUnixStreamServer(str(self.socket), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        context = patch.dict(os.environ, {
            "ORCA_CLI_COMMAND": str(self.command), "ORCA_TERMINAL_HANDLE": "fixture-terminal",
            "ORCA_AGENT_HOOK_TOKEN": "must-not-inherit", "ANTHROPIC_API_KEY": "must-not-inherit",
        })
        context.start()
        self.addCleanup(context.stop)

    def test_bootstrap_is_ephemeral_private_and_only_exposes_selected_unix_transport(self):
        access = prepare_orca_access(self.home / "exposed")
        bootstrap = json.loads((access.files / "orca-runtime.json").read_text())
        self.assertEqual(bootstrap["transports"], [{
            "kind": "unix", "endpoint": SANDBOX_ROOT + "/runtime.sock",
        }])
        self.assertNotIn("unrelatedAccountData", bootstrap)
        self.assertFalse((access.files / "private-account").exists())
        self.assertEqual(access.files.stat().st_mode & 0o777, 0o700)
        self.assertEqual((access.files / "orca-runtime.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(access.context["ORCA_TERMINAL_HANDLE"], "fixture-terminal")
        self.assertNotIn("ORCA_AGENT_HOOK_TOKEN", access.context)
        self.assertEqual(len(access.resources), 2)

    def test_selected_missing_cli_never_falls_back_to_another_orca(self):
        with patch.dict(os.environ, {"ORCA_CLI_COMMAND": str(self.home / "missing")}):
            with self.assertRaisesRegex(RouteError, "orca_cli_not_found"):
                prepare_orca_access(self.home / "exposed")

    def test_unsupported_shell_layout_is_not_evaluated(self):
        marker = self.home / "unexpected-shell-evaluation"
        self.command.write_text(f'#!/bin/sh\nexec "$(touch {marker})" "$@"\n')
        with self.assertRaisesRegex(RouteError, "unsupported_orca_cli_layout"):
            prepare_orca_access(self.home / "exposed")
        self.assertFalse(marker.exists())

    def test_shared_or_symlinked_or_websocket_only_runtime_is_refused(self):
        self.data.chmod(0o755)
        with self.assertRaisesRegex(RouteError, "unsafe_orca_runtime"):
            prepare_orca_access(self.home / "shared")
        self.data.chmod(0o700)
        value = json.loads(self.bootstrap.read_text())
        value["transports"] = [{"kind": "websocket", "endpoint": "ws://127.0.0.1:9999"}]
        self.bootstrap.write_text(json.dumps(value))
        with self.assertRaisesRegex(RouteError, "orca_local_unix_runtime_required"):
            prepare_orca_access(self.home / "websocket")
        self.bootstrap.unlink()
        self.bootstrap.symlink_to(self.data / "other.json")
        with self.assertRaisesRegex(RouteError, "unsafe_orca_runtime"):
            prepare_orca_access(self.home / "symlink")

    def test_sandbox_can_control_runtime_but_keeps_accounts_hidden_and_direct_network_blocked(self):
        profile = self.home / ".local/share/claude-launcher/profiles/fixture"
        profile.mkdir(parents=True)
        workspace = self.home / "work"
        workspace.mkdir()
        upstream = self.home / "unused.sock"
        upstream.touch()
        code = r'''
import json,os,shutil,socket,subprocess
from pathlib import Path
root=Path(os.environ['FIXTURE_HOME'])
assert not (root/'.orca/private-account').exists()
assert not (root/'.orca/orca-runtime.json').exists()
assert 'ANTHROPIC_API_KEY' not in os.environ
assert 'ORCA_AGENT_HOOK_TOKEN' not in os.environ
assert os.environ['ORCA_TERMINAL_HANDLE']=='fixture-terminal'
assert len(Path('/proc/net/route').read_text().splitlines())==1
for family,address in [(socket.AF_INET,('203.0.113.99',443)),
                       (socket.AF_INET6,('2001:db8::1',443))]:
 with socket.socket(family,socket.SOCK_STREAM) as client:
  client.settimeout(0.2)
  try: client.connect(address)
  except OSError: continue
  raise AssertionError('direct network unexpectedly available')
for binary in ['orca-ide','orca']:
 assert shutil.which(binary).startswith('/run/claude-launcher/orca/bin/')
 result=subprocess.run([binary,'status','--json'],capture_output=True,text=True,timeout=3)
 assert result.returncode==0,result.stderr
 assert json.loads(result.stdout)=={'ok':True,'runtime':'synthetic-runtime'}
assert os.statvfs(os.environ['ORCA_USER_DATA_PATH']).f_flag & os.ST_RDONLY
print('LOCAL_ORCA_ONLY_NO_DIRECT_NETWORK')
'''
        access = prepare_orca_access(self.home / "exposed")
        with patch.object(runner.Path, "home", return_value=self.home):
            args, env = runner.sandbox_command(
                upstream, profile, workspace, ["/usr/bin/python3", "-c", code],
                research_filesystem=True, orca_access=access,
            )
        env["FIXTURE_HOME"] = str(self.home)
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("LOCAL_ORCA_ONLY_NO_DIRECT_NETWORK", result.stdout)

    def test_orca_control_is_explicit_opt_in_for_run_only(self):
        config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"), "/unused")
        for extra, enabled in [([], False), (["--orca"], True)]:
            with patch.object(sys, "argv", ["vps-route", "--config", "/unused", "run", *extra]), \
                    patch.object(cli.Config, "load", return_value=config), \
                    patch.object(Path, "mkdir"), \
                    patch.object(cli, "launch", return_value=0) as launch:
                self.assertEqual(cli.main(), 0)
                self.assertIs(launch.call_args.kwargs["orca"], enabled)

    def test_profile_opt_in_applies_to_orca_children_but_not_plain_ssh_terminals(self):
        config = Config("203.0.113.1", "fixture", 22, Path("key"), Path("hosts"), "/unused",
                        orca_control=True)
        for managed in (False, True):
            env = {"ORCA_CLI_COMMAND": str(self.command)} if managed else {}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(sys, "argv", ["vps-route", "--config", "/unused", "run"]), \
                    patch.object(cli.Config, "load", return_value=config), \
                    patch.object(Path, "mkdir"), \
                    patch.object(cli, "launch", return_value=0) as launch:
                self.assertEqual(cli.main(), 0)
                self.assertIs(launch.call_args.kwargs["orca"], managed)

    def test_profile_option_must_be_a_boolean(self):
        from claude_launcher.management import config_values

        config = Config("203.0.113.1", "fixture", 22, self.home / "key", self.home / "hosts",
                        "/unused")
        values = config_values(config, reveal=True)
        self.assertTrue(Config.from_values({**values, "orca_control": True}).orca_control)
        for value in ("true", 1, None):
            with self.assertRaises(RouteError):
                Config.from_values({**values, "orca_control": value})


if __name__ == "__main__":
    unittest.main()
