"""Explicit SSH proxy settings; no real SSH connections in these tests."""

import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.config import Config
from claude_launcher.runner import bootstrap_proxy_options
from claude_launcher.wire import RouteError


class BootstrapTests(unittest.TestCase):
    def config(self, proxy):
        return Config("203.0.113.1", "fixture", 22, Path("unused"), Path("unused"),
                      "/unused", "203.0.113.7", "US", ssh_http_proxy=proxy)

    def test_proxy_is_explicit_and_not_an_environment_fallback(self):
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:9999"}):
            self.assertEqual(bootstrap_proxy_options(self.config(None)), [])
        with patch("claude_launcher.runner.shutil.which", return_value="/usr/bin/nc"):
            options = bootstrap_proxy_options(self.config("http://127.0.0.1:7890"))
        self.assertEqual(options[0], "-o")
        command = shlex.split(options[1].removeprefix("ProxyCommand="))
        self.assertEqual(command, ["/usr/bin/nc", "-n", "-X", "connect", "-x",
                                   "127.0.0.1:7890", "%h", "%p"])

    def test_missing_proxy_helper_does_not_fall_back(self):
        with patch("claude_launcher.runner.shutil.which", return_value=None):
            with self.assertRaisesRegex(RouteError, "netcat_required"):
                bootstrap_proxy_options(self.config("http://127.0.0.1:7890"))

    def test_proxy_rejects_credentials_remote_hosts_and_shell_injection(self):
        values = {
            "ssh_host": "203.0.113.1", "ssh_user": "fixture", "ssh_port": 22,
            "identity_file": "/unused", "known_hosts": "/unused", "remote_socket": "/unused",
            "expected_ip": "1.1.1.1", "expected_country": "US",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            for proxy in ["http://user:pass@127.0.0.1:7890", "http://example.com:7890",
                          "http://127.0.0.1:7890/;touch BAD", "socks5://127.0.0.1:7890"]:
                path.write_text(json.dumps({**values, "ssh_http_proxy": proxy}))
                with self.assertRaises(RouteError):
                    Config.load(path)
            path.write_text(json.dumps({**values, "ssh_http_proxy": "http://127.0.0.1:7890"}))
            self.assertEqual(Config.load(path).ssh_http_proxy, "http://127.0.0.1:7890")
