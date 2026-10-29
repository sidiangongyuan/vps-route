import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_launcher import seccomp

ROOT = Path(__file__).resolve().parents[1]
FILTER_ENTRY = ROOT / "claude_launcher/entries/filter_entry.py"
PROBE = r"""
import ctypes, errno, socket
libc = ctypes.CDLL(None, use_errno=True)
def outcome(result):
    return "ok" if result >= 0 else errno.errorcode[ctypes.get_errno()]
def family(value):
    try:
        socket.socket(value, socket.SOCK_STREAM).close()
        return "ok"
    except OSError as exc:
        return errno.errorcode[exc.errno]
print(family(socket.AF_VSOCK), outcome(libc.syscall(41, ctypes.c_long(40 | 1 << 32), 1, 0)),
      family(socket.AF_UNIX), family(socket.AF_INET),
      outcome(libc.syscall(425, 1, ctypes.create_string_buffer(120))))
"""


class FilterProgramTests(unittest.TestCase):
    def test_program_is_whole_instructions_for_supported_machines(self):
        for machine in ("x86_64", "aarch64"):
            for block in (True, False):
                program = seccomp.build(block_vsock=block, machine=machine)
                self.assertEqual(len(program) % 8, 0)
        self.assertFalse(seccomp.supported("riscv64"))
        with self.assertRaises(ValueError):
            seccomp.build(machine="riscv64")

    def test_entry_refuses_unknown_mode_or_command(self):
        for arguments in (["open", "bwrap", "--", "true"], ["default", "sh", "-c", "true"]):
            result = subprocess.run([sys.executable, str(FILTER_ENTRY), *arguments], check=False)
            self.assertEqual(result.returncode, 2)


@unittest.skipUnless(shutil.which("bwrap") and os.uname().machine == "x86_64",
                     "needs bubblewrap on x86_64")
class FilterRuntimeTests(unittest.TestCase):
    def run_probe(self, mode: str) -> list[str]:
        result = subprocess.run(
            ["/usr/bin/python3", str(FILTER_ENTRY), mode, "bwrap", "--unshare-user",
             "--unshare-net", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
             "--", "/usr/bin/python3", "-c", PROBE],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode:
            self.skipTest("nested bubblewrap unavailable: " + result.stderr.strip()[-200:])
        return result.stdout.split()

    def test_default_blocks_vsock_including_high_bits_and_io_uring(self):
        vsock, high_bits, unix, inet, io_uring = self.run_probe("default")
        self.assertEqual((vsock, high_bits, io_uring), ("EAFNOSUPPORT", "EAFNOSUPPORT", "ENOSYS"))
        self.assertEqual((unix, inet), ("ok", "ok"))

    def test_windows_mode_keeps_vsock_and_still_blocks_io_uring(self):
        vsock, _high_bits, unix, inet, io_uring = self.run_probe("windows")
        self.assertNotEqual(vsock, "EAFNOSUPPORT")
        self.assertEqual((unix, inet, io_uring), ("ok", "ok", "ENOSYS"))


if __name__ == "__main__":
    unittest.main()
