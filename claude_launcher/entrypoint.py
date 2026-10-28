"""Reject unsupported platforms before importing Linux-only runtime modules."""

import sys


def main() -> int:
    from .arguments import build_parser
    from .completions import dispatch

    completed = dispatch(sys.argv[1:], build_parser)
    if completed is not None:
        return completed
    if sys.platform != "linux":
        print("vps-route: this branch runs Claude on Linux; "
              "macOS runtime is on macos-support", file=sys.stderr)
        return 69
    from .cli import main as cli_main

    return cli_main()


def server_main() -> int:
    if sys.platform != "linux":
        print("vps-route-server: Linux only", file=sys.stderr)
        return 69
    from .remote import main as remote_main

    return remote_main()
