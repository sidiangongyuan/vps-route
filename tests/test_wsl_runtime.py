"""Real production sandbox fixtures, without credentials or external services."""

import ctypes
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher import wsl
from claude_launcher.runner import sandbox_command
from claude_launcher.wire import RouteError


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap required")
class RuntimeTests(unittest.TestCase):
    def sandbox(self, code, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile, project = root / "profile", root / "project"
            profile.mkdir()
            project.mkdir()
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(root / "upstream"))
                args, env = sandbox_command(root / "upstream", profile, project,
                                            ["/usr/bin/python3", "-c", code], **kwargs)
                return subprocess.run(args, env=env, capture_output=True, timeout=15)

    @unittest.skipUnless(platform.machine() == "x86_64", "x86 ABI machine-code fixtures")
    def test_foreign_abis_are_killed_after_exec(self):
        # Each fixture asks only for getpid; a foreign ABI must receive SIGSYS.
        for machine_code in ("b814000000cd80c3", "b8270000400f05c3"):
            code = (
                "import ctypes,mmap,resource; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); "
                "m=mmap.mmap(-1,4096,prot=7); m.write(bytes.fromhex(" + repr(machine_code) + ")); "
                "ctypes.CFUNCTYPE(ctypes.c_long)(ctypes.addressof(ctypes.c_char.from_buffer(m)))()"
            )
            wrapper = ("import subprocess,signal; r=subprocess.run(['/usr/bin/python3','-c',"
                       + repr(code) + "]); assert r.returncode == -signal.SIGSYS")
            result = self.sandbox(wrapper)
            self.assertEqual(result.returncode, 0, result.stderr)


    @unittest.skipUnless(wsl.version() == 2, "WSL runtime alias policy")
    def test_runtime_alias_hides_sockets_created_before_and_after_start(self):
        with tempfile.TemporaryDirectory(prefix="route-runtime-fixture-", dir=Path.home()) as directory:
            root = Path(directory)
            runtime, project, profile = (root / name for name in ("runtime", "project", "profile"))
            for path in (runtime, project, profile):
                path.mkdir()
            stop = threading.Event()
            errors = []
            original_paths = wsl.runtime_paths

            def late_socket():
                try:
                    for _ in range(200):
                        if (project / "go").exists():
                            break
                        if stop.wait(.02):
                            return
                    with socket.socket(socket.AF_UNIX) as server:
                        server.bind(str(runtime / "late.sock"))
                        server.listen()
                        (project / "ready").touch()
                        stop.wait(5)
                except Exception as exc:
                    errors.append(type(exc).__name__)

            with socket.socket(socket.AF_UNIX) as early, socket.socket(socket.AF_UNIX) as upstream:
                early.bind(str(runtime / "early.sock"))
                early.listen()
                upstream.bind(str(root / "upstream"))
                # A positive host control establishes that this fixture is connectable.
                with socket.socket(socket.AF_UNIX) as control:
                    control.connect(str(runtime / "early.sock"))
                    peer, _ = early.accept()
                    peer.close()
                thread = threading.Thread(target=late_socket)
                thread.start()
                code = f"""
import errno,socket,time
from pathlib import Path
Path('go').touch()
for _ in range(200):
    if Path('ready').exists(): break
    time.sleep(.02)
assert Path('ready').exists()
for name in ('early.sock','late.sock'):
    with socket.socket(socket.AF_UNIX) as client:
        try: client.connect(str(Path({str(runtime)!r})/name))
        except OSError as e: assert e.errno in (errno.ENOENT,errno.EACCES,errno.EPERM)
        else: raise AssertionError('shared runtime was reachable')
"""
                try:
                    with patch.object(wsl, "runtime_paths",
                                      side_effect=lambda mounts: [*original_paths(mounts), runtime]):
                        args, env = sandbox_command(root / "upstream", profile, project,
                                                    ["/usr/bin/python3", "-c", code],
                                                    research_filesystem=True)
                    result = subprocess.run(args, env=env, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                finally:
                    stop.set()
                    thread.join(timeout=6)
                self.assertEqual(errors, [])

    def test_mount_api_denied_after_namespace_setup(self):
        code = """
import ctypes,errno
c=ctypes.CDLL(None,use_errno=True)
for nr in (428,429,430,431,432,433,442):
    assert c.syscall(nr,0,0,0,0,0)==-1 and ctypes.get_errno()==errno.EPERM
print('mount_api_blocked')
"""
        result = self.sandbox(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"mount_api_blocked", result.stdout)

    @unittest.skipUnless(os.environ.get("VPS_ROUTE_WINDOWS_TEST_ROOT"),
                         "set an owned disposable Windows test root")
    def test_windows_project_writable_neighbors_hidden_both_modes(self):
        parent = Path(os.environ["VPS_ROUTE_WINDOWS_TEST_ROOT"])
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="route-fixture-", dir=parent) as directory, \
                tempfile.TemporaryDirectory() as private:
            root, profile = Path(directory), Path(private)
            project = root / "project"
            project.mkdir()
            (root / "AppData").mkdir()
            (root / "AppData/canary").write_text("synthetic-only")
            (project / "source.txt").write_text("fixture-source")
            source = next(m.target / "Windows/System32/cmd.exe"
                          for m in wsl.windows_mounts(wsl.mounts())
                          if (m.target / "Windows/System32/cmd.exe").is_file())
            exe = project / "cmd.exe"
            shutil.copyfile(source, exe)
            exe.chmod(0o700)
            control = subprocess.run([str(exe), "/c", "echo", "vps-route-probe"],
                                     capture_output=True, timeout=5)
            self.assertEqual(control.returncode, 0)
            def windows_path(path):
                return subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
            for link in (root / "project-link", project / "outside"):
                junction = subprocess.run(
                    [str(source), "/c", "mklink", "/J", windows_path(link),
                     windows_path(root / "AppData")], capture_output=True, timeout=5)
                self.assertEqual(junction.returncode, 0)
            self.assertEqual((root / "project-link").resolve(), root / "AppData")
            with self.assertRaises(RouteError):
                wsl.validate_workspace(root / "project-link", wsl.mounts())
            for research in (False, True):
                with socket.socket(socket.AF_UNIX) as sock:
                    address = profile / ("research.sock" if research else "default.sock")
                    sock.bind(str(address))
                    code = f"""
import os,subprocess
from pathlib import Path
from unittest.mock import patch
root=Path({str(root)!r})
assert Path('source.txt').read_text() == 'fixture-source'
Path('result.txt').write_text('fixture-output')
assert not (root/'AppData/canary').exists()
assert not (root/'APPDATA/canary').exists()
assert not Path('outside/canary').exists()
r=subprocess.run(['./cmd.exe','/c','echo','vps-route-probe'],capture_output=True,timeout=5)
assert r.returncode != 0 and b'vps-route-probe' not in r.stdout
"""
                    args, env = sandbox_command(address, profile, project,
                                                ["/usr/bin/python3", "-c", code],
                                                research_filesystem=research)
                    result = subprocess.run(args, env=env, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((project / "result.txt").read_text(), "fixture-output")


if __name__ == "__main__":
    unittest.main()
