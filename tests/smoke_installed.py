"""Exercise an installed wheel from another working directory with a fresh HOME.

Usage: python tests/smoke_installed.py /absolute/path/to/venv/bin/vps-route
No real keys, remote connections, account logins, or model requests.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    executable = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="vps-route-installed-") as temporary:
        root = Path(temporary)
        home, project = root / "fresh-home", root / "project"
        home.mkdir(mode=0o700)
        project.mkdir()
        key, hosts = home / "key", home / "hosts"
        key.write_text("synthetic key, not usable")
        hosts.write_text("synthetic host pin, not usable")
        old = home / ".claude"
        old.mkdir()
        (old / "keep").write_text("synthetic legacy data")
        source = root / "route.json"
        source.write_text(json.dumps({
            "ssh_host": "203.0.113.1", "ssh_user": "fixture", "ssh_port": 22,
            "identity_file": str(key), "known_hosts": str(hosts),
            "remote_socket": "/srv/fixture/connect.sock",
            "expected_ip": "1.1.1.1", "expected_country": "US",
        }))
        env = {**os.environ, "HOME": str(home), "PYTHONPATH": ""}
        runs = []
        for arguments, expected in [
            (["--version"], 0),
            (["init", "--from-file", str(source)], 0),
            (["doctor"], 0),
            (["setup-vps"], 0),
            (["init", "--from-file", str(source)], 69),
        ]:
            result = subprocess.run([str(executable), *arguments], env=env, cwd=project,
                                    capture_output=True, text=True, timeout=15)
            assert result.returncode == expected, result.stdout + result.stderr
            runs.append({"command": arguments[0], "exit_code": result.returncode})
        config = home / ".config/vps-route/config.json"
        assert config.stat().st_mode & 0o777 == 0o600
        assert (old / "keep").read_text() == "synthetic legacy data"
        assert not (home / ".local/share/claude-launcher/profiles").exists()
        print(json.dumps({"installed_cli_smoke": "ok", "fresh_home": True,
                          "legacy_state_preserved": True, "account_login_performed": False,
                          "remote_deployment_performed": False, "runs": runs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
