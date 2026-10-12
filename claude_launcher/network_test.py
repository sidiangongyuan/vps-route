"""Credential-free live test, invoked only inside the isolated network namespace."""

import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    expected_ip, expected_country = sys.argv[1:]
    proxy = os.environ["HTTPS_PROXY"]
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    with opener.open("https://ipinfo.io/json", timeout=15) as response:
        reading = json.loads(response.read(65536))
    matches = (
        ipaddress.ip_address(reading["ip"]) == ipaddress.ip_address(expected_ip)
        and reading.get("country") == expected_country
    )
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
    try:
        with opener.open("https://generativelanguage.googleapis.com/v1beta/models",
                         timeout=15) as response:
            gemini_status = response.status
    except urllib.error.HTTPError as exc:
        gemini_status = exc.code
        exc.close()
    result = {
        "egress_matches": matches,
        "egress_country": reading.get("country"),
        "direct_route_blocked": direct_blocked,
        "only_loopback_interface": interfaces == ["lo"],
        "gemini_https_status": gemini_status,
        "gemini_credentials_sent": False,
        "gemini_inference_tested": False,
        "claude_started": False,
    }
    print(json.dumps(result, indent=2), flush=True)
    return 0 if matches and direct_blocked and interfaces == ["lo"] else 69


if __name__ == "__main__":
    raise SystemExit(main())
