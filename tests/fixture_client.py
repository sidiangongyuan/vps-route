"""Synthetic workload; never imports or launches Claude/HICC."""

import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


mode = sys.argv[1]
if mode == "network":
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    assert "ALL_PROXY" not in os.environ
    assert "all_proxy" not in os.environ
    baseline_home = Path(sys.argv[3])
    for name in (".claude", ".claude.json", ".hicc", ".ssh", ".codex"):
        assert not (baseline_home / name).exists(), "baseline state visible in isolated client"
    assert len(Path("/proc/net/route").read_text().splitlines()) == 1
    for row in Path("/proc/net/dev").read_text().splitlines()[2:]:
        assert row.split(":")[0].strip() == "lo"
    direct = socket.socket()
    direct.settimeout(0.3)
    try:
        direct.connect(("203.0.113.99", 443))
        raise AssertionError("unexpected direct route")
    except OSError:
        pass
    finally:
        direct.close()
    proxy = urlsplit(os.environ["HTTPS_PROXY"])
    assert proxy.hostname == "127.0.0.1" and proxy.port == 17878
    for name in ("HTTP_PROXY", "http_proxy", "https_proxy"):
        assert os.environ[name] == os.environ["HTTPS_PROXY"]
    context = ssl.create_default_context(cafile=sys.argv[2])
    client = http.client.HTTPSConnection(proxy.hostname, proxy.port, context=context, timeout=3)
    client.set_tunnel("probe.test", 443)
    client.request("GET", "/json")
    response = client.getresponse()
    assert response.status == 200
    value = json.loads(response.read())
    assert value == {"ip": "203.0.113.7", "country": "US"}
    client.close()
    Path.home().joinpath("synthetic-state").write_text("fixture only")
    print("ISOLATED_PROXY_OK", flush=True)
elif mode == "sleep":
    marker = sys.argv[2]
    subprocess.Popen([
        "/usr/bin/python3", "-c",
        "import time; from pathlib import Path; time.sleep(1.5); "
        f"Path({marker!r}).write_text('unexpected surviving descendant')",
    ])
    print("SYNTHETIC_SESSION_READY", flush=True)
    Path(sys.argv[3]).write_text("ready")
    time.sleep(30)
else:
    raise SystemExit(2)
