"""Passive, redacted socket metadata for already-running same-user HICC processes."""

from __future__ import annotations

import collections
import ipaddress
import os
from pathlib import Path


def endpoint(value: str) -> tuple[bool, int]:
    address, port = value.split(":")
    raw = bytes.fromhex(address)
    if len(raw) == 4:
        raw = raw[::-1]
    else:
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    return ipaddress.ip_address(raw).is_loopback, int(port, 16)


def observe_hicc() -> dict:
    inodes: set[str] = set()
    process_count = 0
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            if (process / "comm").read_text().strip() != "hicc":
                continue
            process_count += 1
            for descriptor in (process / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                except OSError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    inodes.add(target[8:-1])
        except OSError:
            continue
    rows = []
    for name in ("tcp", "tcp6"):
        try:
            lines = (Path("/proc/self/net") / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) >= 10 and fields[9] in inodes:
                rows.append(fields)
    listeners = {endpoint(row[1])[1] for row in rows if row[3] == "0A"}
    categories = collections.Counter()
    for row in rows:
        if row[3] != "01":
            continue
        _, local_port = endpoint(row[1])
        loopback, remote_port = endpoint(row[2])
        if local_port in listeners:
            category = "accepted_on_local_listener"
        elif loopback:
            category = f"connection_to_loopback_port_{remote_port}"
        else:
            category = f"connection_to_nonloopback_port_{remote_port}"
        categories[category] += 1
    return {
        "same_user_hicc_processes": process_count,
        "owned_tcp_listen_ports": sorted(listeners),
        "established_tcp_socket_counts": dict(sorted(categories.items())),
        "limits": "one passive snapshot; control and data connections are not distinguished",
        "account_files_read": False,
        "hicc_commands_executed": False,
    }
