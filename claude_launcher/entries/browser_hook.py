#!/usr/bin/python3
"""Suppress browser launch; the official CLI prints its manual-code URL."""

import sys
from urllib.parse import urlsplit

AUTH_HOSTS = {"claude.ai", "claude.com", "platform.claude.com", "console.anthropic.com"}


def official_login_url(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 16384 or any(ord(char) < 33 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in AUTH_HOSTS
                and parsed.port in (None, 443) and not parsed.username
                and not parsed.password and not parsed.fragment
                and parsed.path.endswith("/authorize"))
    except ValueError:
        return False


def main() -> int:
    try:
        if not official_login_url(sys.argv[-1]):
            raise ValueError
        # The CLI's manual-code URL is the only URL shown; no localhost callback.
        return 0
    except (IndexError, ValueError):
        print("vps-route: browser request rejected; use the CLI manual login link",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
