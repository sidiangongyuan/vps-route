"""Explicit official native install inside the launcher's separate home."""

import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    cache = Path.home() / ".cache/claude-launcher"
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="installer-", dir=cache) as temporary:
        script = Path(temporary) / "install.sh"
        downloaded = subprocess.run([
            "curl", "--fail", "--silent", "--show-error", "--location",
            "--proto", "=https", "--proto-redir", "=https",
            "https://claude.ai/install.sh", "--output", str(script),
        ], check=False)
        if downloaded.returncode:
            return downloaded.returncode
        return subprocess.call(["bash", str(script), "latest"])
