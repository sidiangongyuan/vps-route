"""Reject unsupported platforms before importing platform-specific runtime modules."""

import sys


def main() -> int:
    if sys.platform not in {"linux", "darwin"}:
        print("vps-route: Linux and macOS only", file=sys.stderr)
        return 69
    from .cli import main as cli_main

    return cli_main()


def server_main() -> int:
    if sys.platform != "linux":
        print("vps-route-server: Linux only", file=sys.stderr)
        return 69
    from .remote import main as remote_main

    return remote_main()
