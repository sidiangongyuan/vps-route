"""Run a credential-free network diagnostic through the isolated route."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from claude_launcher.network_test import main

if __name__ == "__main__":
    raise SystemExit(main())
