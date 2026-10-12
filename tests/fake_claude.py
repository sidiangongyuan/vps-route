#!/usr/bin/python3
"""Synthetic native-install layout used solely by the local update test."""

import os
import shutil
import sys
from pathlib import Path


if sys.argv[1:] == ["update"]:
    version = Path.home() / ".local/share/claude/versions/fixture-v2"
    version.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve(), version)
    version.chmod(0o700)
    launcher = Path.home() / ".local/bin/claude"
    replacement = launcher.with_name("claude.next")
    replacement.symlink_to(version)
    os.replace(replacement, launcher)
    print("SYNTHETIC_UPDATE_OK")
elif sys.argv[1:] == ["--version"]:
    print(Path(__file__).resolve().name)
else:
    raise SystemExit(2)
