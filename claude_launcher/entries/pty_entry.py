"""Acquire the PTY in a fresh session before exec; no threaded-parent preexec."""

import fcntl
import os
import sys
import termios

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
