"""Acquire the PTY in a fresh session before exec; no threaded-parent preexec."""

import fcntl
import os
import sys
import termios

# Some Python builds omit the constant; values from Linux and macOS headers.
TIOCSCTTY = getattr(termios, "TIOCSCTTY", 0x20007461 if sys.platform == "darwin" else 0x540E)
fcntl.ioctl(0, TIOCSCTTY, 0)
os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
