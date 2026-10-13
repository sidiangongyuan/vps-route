"""Small stream helpers shared by the local shim and the VPS CONNECT service."""

from __future__ import annotations

import http.client
import json
import select
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1


class RouteError(RuntimeError):
    """A route operation failed; messages deliberately omit endpoint values."""


def unix_connect(path: str, timeout: float = 8.0) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(path)
    except OSError:
        connection.close()
        raise RouteError("proxy_socket_unavailable") from None
    return connection


def read_header(connection: socket.socket, limit: int = 16384,
                *, deadline: float | None = None) -> bytes:
    """Read exactly through the header, leaving tunneled bytes on the socket."""
    data = bytearray()
    while len(data) < limit:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.settimeout(remaining)
        chunk = connection.recv(1)
        if not chunk:
            raise RouteError("incomplete_proxy_header")
        data.extend(chunk)
        if data.endswith(b"\r\n\r\n"):
            return bytes(data)
    raise RouteError("proxy_header_too_large")


def relay(left: socket.socket, right: socket.socket) -> None:
    """Copy opaque bytes, retaining the other direction after a half-close."""
    left.settimeout(None)
    right.settimeout(None)
    peers = {left: right, right: left}
    readers = [left, right]
    try:
        while readers:
            ready, _, _ = select.select(readers, [], [], 1.0)
            for source in ready:
                data = source.recv(65536)
                if data:
                    peers[source].sendall(data)
                else:
                    readers.remove(source)
                    try:
                        peers[source].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
    except OSError:
        return


def proxy_json(
    socket_path: str,
    url: str,
    timeout: float = 8.0,
    context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    """Fetch a bounded HTTPS JSON response through exactly one Unix proxy.

    There is no environment-proxy lookup, direct fallback, or redirect follow.
    TLS is end-to-end to the probe host; the proxy only sees CONNECT metadata.
    """
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise RouteError("invalid_probe_url")
    host = parsed.hostname.encode("idna").decode("ascii")
    if any(ch in host for ch in "\r\n[]:"):
        raise RouteError("unsupported_probe_hostname")
    raw = unix_connect(socket_path, timeout)
    connection = None
    try:
        authority = f"{host}:443"
        raw.sendall(
            f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii")
        )
        header = read_header(raw)
        status = header.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[1] != b"200":
            for reason in ("target_resolution_failed", "upstream_unavailable",
                           "connect_target_rejected", "nonpublic_target_rejected"):
                if f"X-VPS-Route-Error: {reason}\r\n".encode() in header:
                    raise RouteError(reason)
            raise RouteError("proxy_connect_rejected")
        try:
            tls = (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=host)
        except ssl.SSLCertVerificationError:
            raise RouteError("probe_tls_certificate_failed") from None
        except ssl.SSLError:
            raise RouteError("probe_tls_failed") from None
        connection = http.client.HTTPConnection(host, timeout=timeout)
        connection.sock = tls
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise RouteError("probe_http_failure")
        payload = response.read(65537)
        if len(payload) > 65536:
            raise RouteError("probe_response_too_large")
        try:
            value = json.loads(payload)
        except ValueError:
            raise RouteError("probe_invalid_json") from None
        if not isinstance(value, dict):
            raise RouteError("probe_invalid_json")
        return value
    except RouteError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise RouteError("probe_transport_failure") from None
    finally:
        if connection is not None:
            connection.close()
        raw.close()


def server_health(socket_path: str, timeout: float = 8.0) -> dict:
    """Read local service metadata, never contacts websites or returns account state."""
    with unix_connect(socket_path, timeout) as connection:
        try:
            deadline = time.monotonic() + timeout
            connection.sendall(b"GET /vps-route-health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            header = read_header(connection, deadline=deadline)
            if header.split(b"\r\n", 1)[0].split()[1:2] != [b"200"]:
                return {"status": "legacy_or_health_unsupported", "protocol": None,
                        "compatible": None}
            lengths = [line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")
                       if line.lower().startswith(b"content-length:")]
            if len(lengths) != 1 or not 1 <= int(lengths[0]) <= 4096:
                raise ValueError
            remaining = int(lengths[0])
            body = bytearray()
            while remaining:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise TimeoutError
                connection.settimeout(wait)
                chunk = connection.recv(remaining)
                if not chunk:
                    raise ValueError
                body.extend(chunk)
                remaining -= len(chunk)
            value = json.loads(body)
            if (not isinstance(value, dict) or type(value.get("protocol")) is not int
                    or not isinstance(value.get("version"), str)):
                raise ValueError
            import re
            if not re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", value["version"]):
                raise ValueError
            return {"status": "available", "protocol": value["protocol"],
                    "version": value["version"],
                    "compatible": value["protocol"] == PROTOCOL_VERSION}
        except (OSError, ValueError, IndexError):
            raise RouteError("server_health_unavailable") from None
