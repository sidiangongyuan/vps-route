"""Run inside the private network namespace; expose the sole Unix route as HTTP."""

from __future__ import annotations

import os
import socket
import socketserver
import subprocess
import sys
import threading

from .wire import RouteError, relay, unix_connect

PROXY_PORT = 17878


class ShimHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            with unix_connect(self.server.upstream_path) as upstream:
                relay(self.request, upstream)
        except (OSError, RouteError):
            return


class ShimServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def handle_error(self, request: socket.socket, client_address: object) -> None:
        return


def main() -> int:
    socket_path, *command = sys.argv[1:]
    if not command:
        return 2
    # Each invocation has its own network namespace, so sessions can share a port.
    with ShimServer(("127.0.0.1", PROXY_PORT), ShimHandler) as server:
        server.upstream_path = socket_path
        threading.Thread(target=server.serve_forever, daemon=True).start()
        environment = dict(os.environ)
        for name in tuple(environment):
            if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
                environment.pop(name)
        proxy = f"http://127.0.0.1:{server.server_address[1]}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            environment[name] = proxy
        environment["NO_PROXY"] = environment["no_proxy"] = "localhost,127.0.0.1,::1"
        try:
            return subprocess.call(command, env=environment)
        finally:
            server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
