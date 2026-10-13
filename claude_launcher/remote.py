"""VPS-side HTTPS CONNECT service, reachable only through a private Unix socket."""

from __future__ import annotations

import argparse
import errno
import fcntl
import ipaddress
import json
import os
import signal
import socket
import socketserver
import stat
from pathlib import Path

from . import __version__
from .wire import PROTOCOL_VERSION, RouteError, read_header, relay


def public_target(authority: str) -> list[tuple[int, int, int, tuple]]:
    """Resolve on the VPS and pin the public address used for this connection."""
    host, separator, port = authority.rpartition(":")
    if not separator or port != "443" or not host or any(c in host for c in "/@?#\\"):
        raise RouteError("connect_target_rejected")
    host = host.removeprefix("[").removesuffix("]")
    if any(ord(c) <= 32 for c in host):
        raise RouteError("connect_target_rejected")
    try:
        resolved = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise RouteError("target_resolution_failed") from None
    addresses = []
    for family, kind, protocol, _, address in resolved:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global:
            raise RouteError("nonpublic_target_rejected")
        item = (family, kind, protocol, address)
        if item not in addresses:
            addresses.append(item)
    if not addresses:
        raise RouteError("target_resolution_failed")
    return addresses


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = None
        established = False
        try:
            self.request.settimeout(10)
            line = read_header(self.request).split(b"\r\n", 1)[0].decode("ascii")
            parts = line.split()
            if parts == ["GET", "/vps-route-health", "HTTP/1.1"]:
                body = json.dumps({"version": __version__, "protocol": PROTOCOL_VERSION}).encode()
                self.request.sendall(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n".encode() + body
                )
                return
            if (
                len(parts) != 3 or parts[0] != "CONNECT"
                or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}
            ):
                raise RouteError("connect_only")
            for family, kind, protocol, address in public_target(parts[1]):
                candidate = socket.socket(family, kind, protocol)
                candidate.settimeout(8)
                try:
                    candidate.connect(address)
                    upstream = candidate
                    break
                except OSError:
                    candidate.close()
            if upstream is None:
                raise RouteError("upstream_unavailable")
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            established = True
            relay(self.request, upstream)
        except (OSError, ValueError, RouteError) as exc:
            if not established:
                # Fixed reason codes only: never expose hosts, exception text, or traffic.
                reason = str(exc) if isinstance(exc, RouteError) else "upstream_unavailable"
                if reason not in {"target_resolution_failed", "upstream_unavailable",
                                  "connect_target_rejected", "nonpublic_target_rejected"}:
                    reason = "connect_target_rejected"
                try:
                    self.request.sendall(
                        ("HTTP/1.1 502 Tunnel Unavailable\r\nContent-Length: 0\r\n"
                         f"X-VPS-Route-Error: {reason}\r\n\r\n").encode()
                    )
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()


class ConnectServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request: socket.socket, client_address: object) -> None:
        # Never log CONNECT hosts or traffic contents on unexpected failures.
        return


def serve(path: Path) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise RouteError("socket_directory_must_be_private_and_owned")
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    previous_umask = os.umask(0o077)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise RouteError("socket_path_already_exists")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as existing:
                existing.settimeout(1)
                try:
                    existing.connect(str(path))
                except OSError as exc:
                    if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                        raise RouteError("existing_socket_state_unknown") from None
                else:
                    raise RouteError("connect_service_already_running")
            path.unlink(missing_ok=True)
        with ConnectServer(str(path), ConnectHandler) as server:
            os.chmod(path, 0o600)
            print("connect_service_ready; unix_socket_only; https_port_443_only", flush=True)
            try:
                server.serve_forever(poll_interval=0.25)
            finally:
                path.unlink(missing_ok=True)
    finally:
        os.umask(previous_umask)
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()

    def stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        serve(args.socket)
    except KeyboardInterrupt:
        return 0
    except (OSError, RouteError):
        print("connect_service_failed; inspect_socket_ownership_and_path")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
