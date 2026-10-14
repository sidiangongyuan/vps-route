"""Credential-free live test, invoked only inside the isolated network namespace."""

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    targets = sys.argv[1:]
    proxy = os.environ["HTTPS_PROXY"]
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    reachable = False
    for url in targets:
        try:
            with opener.open(url, timeout=8) as response:
                reachable = 200 <= response.status < 500
        except urllib.error.HTTPError as exc:
            reachable = 200 <= exc.code < 500
            exc.close()
        except (OSError, urllib.error.URLError):
            pass
        if reachable:
            break
    direct_blocked = False
    try:
        with socket.create_connection(("203.0.113.1", 443), timeout=1):
            pass
    except OSError as exc:
        # Only a missing route proves isolation; a timeout is not sufficient.
        import errno
        direct_blocked = exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH)
    interfaces = [
        row.split(":", 1)[0].strip()
        for row in Path("/proc/net/dev").read_text().splitlines()[2:]
    ]
    result = {
        "connectivity_ok": reachable,
        "exit_ip_pinning_enabled": False,
        "direct_route_blocked": direct_blocked,
        "only_loopback_interface": interfaces == ["lo"],
        "claude_started": False,
    }
    print(json.dumps(result, indent=2), flush=True)
    return 0 if reachable and direct_blocked and interfaces == ["lo"] else 69


if __name__ == "__main__":
    raise SystemExit(main())
