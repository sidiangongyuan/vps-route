#!/usr/bin/python3
"""Synthetic manual-code client. No provider request or account state is used."""

import os
import subprocess
from pathlib import Path
from urllib.parse import urlencode


url = "https://claude.ai/oauth/authorize?" + urlencode({
    "state": "synthetic-state-123",
    "redirect_uri": "https://platform.claude.com/oauth/code/callback",
    "code_challenge": "fixture",
    "client_id": "fixture",
})
browser_url = url.replace("https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback",
                          "http%3A%2F%2Flocalhost%3A34961%2Fcallback")
print("If the browser didn't open, visit: " + url, flush=True)
subprocess.run([os.environ["BROWSER"], browser_url], check=True,
               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL)
print("Paste code here if prompted: ", end="", flush=True)
if input().strip() != "synthetic-code#synthetic-state-123":
    print("SYNTHETIC_AUTH_REJECTED")
    raise SystemExit(3)
(Path.home() / "synthetic-auth-result").write_text("fixture only")
print("SYNTHETIC_AUTH_COMPLETED")
