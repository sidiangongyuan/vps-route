"""Hand bwrap a seccomp program through an inherited memfd, then exec it."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from claude_launcher.seccomp import build


def main() -> int:
    mode, *command = sys.argv[1:]
    if mode not in {"default", "windows"} or command[:1] != ["bwrap"]:
        return 2
    descriptor = os.memfd_create("vps-route-seccomp", 0)
    os.write(descriptor, build(block_vsock=mode == "default"))
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.set_inheritable(descriptor, True)
    os.execvp("bwrap", ["bwrap", "--seccomp", str(descriptor), *command[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
