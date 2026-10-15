"""Layered, sanitized diagnostics using only the explicitly configured route."""

from __future__ import annotations

import ipaddress
import queue
import socket
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config
from .runner import SSHTunnel, connectivity_check
from .wire import RouteError, proxy_json, server_health


HINTS = {
    "network_isolation_unavailable": (
        "bubblewrap cannot create a private network namespace. Install bubblewrap; on Ubuntu "
        "23.10+ AppArmor may block unprivileged user namespaces (see docs FAQ). Only Ubuntu "
        "22.04 is verified."
    ),
    "bootstrap_proxy_unavailable": "Start the configured local VPN/proxy and check its port.",
    "openbsd_netcat_required_for_ssh_http_proxy": "Install OpenBSD netcat for the SSH bootstrap.",
    "ssh_key_or_verified_known_hosts_missing": "Check the SSH key and verified known_hosts paths.",
    "ssh_host_key_verification_failed": "Verify the VPS host fingerprint independently; do not bypass it.",
    "ssh_authentication_failed": "Check SSH user/key authorization on the VPS.",
    "ssh_tunnel_failed": "Check VPS reachability, SSH port and the configured bootstrap proxy.",
    "ssh_tunnel_start_timeout": "Check VPS/SSH connectivity; no direct fallback was attempted.",
    "proxy_socket_unavailable": "Check the private CONNECT service and remote socket path on the VPS.",
    "server_health_unavailable": "Check the VPS CONNECT service; the health response was incomplete.",
    "server_protocol_incompatible": "Inspect server-version; explicitly deploy a compatible VPS service.",
    "target_resolution_failed": "The VPS could not resolve the target; check its DNS/upstream.",
    "upstream_unavailable": "The VPS service could not connect upstream; check its exit/proxy.",
    "proxy_connect_rejected": "The VPS CONNECT request was rejected; check service/upstream policy.",
    "probe_tls_certificate_failed": "Check TLS certificates/clock/interception; do not disable verification.",
    "probe_tls_failed": "CONNECT succeeded but TLS failed; inspect the VPS upstream/proxy.",
    "probe_transport_failure": "Inspect VPS upstream/proxy and probe reachability; SSH alone is not enough.",
    "connectivity_check_timeout": "The connectivity check timed out; inspect the route and target reachability.",
    "probe_http_failure": "The probe website returned an error; inspect it before blaming the exit.",
    "probe_invalid_json": "The probe returned invalid JSON; use a compatible HTTPS IP/country endpoint.",
}


def failure_detail(reason: str) -> dict:
    if reason.startswith("ssh_") or reason.startswith("openbsd_"):
        stage = "ssh"
    elif reason.startswith("bootstrap_"):
        stage = "local_bootstrap"
    elif reason.startswith(("server_", "proxy_socket")):
        stage = "vps_service"
    elif reason in {"proxy_connect_rejected", "upstream_unavailable", "target_resolution_failed",
                    "connect_target_rejected", "nonpublic_target_rejected"}:
        stage = "vps_connect"
    elif reason.startswith("probe_tls"):
        stage = "target_tls"
    else:
        stage = "connectivity"
    return {"stage": stage, "result": reason,
            "hint": HINTS.get(reason, "Inspect the configured route; do not enable direct fallback.")}


def observe_exit(config: Config, socket_path: Path) -> dict:
    """Best-effort information for doctor only, never a launch/monitor condition."""
    outcome: queue.Queue = queue.Queue(maxsize=1)

    def inspect() -> None:
        try:
            value = proxy_json(str(socket_path), config.probe_url, config.timeout_seconds)
            address = str(ipaddress.ip_address(value["ip"]))
            country = value.get("country")
            country = country if isinstance(country, str) and len(country) == 2 else None
            outcome.put({"observed_exit_ip": address, "observed_exit_country": country,
                         "exit_info_result": "available"})
        except (RouteError, OSError, ValueError, TypeError, KeyError):
            outcome.put({"observed_exit_ip": None, "observed_exit_country": None,
                         "exit_info_result": "unavailable"})

    threading.Thread(target=inspect, daemon=True).start()
    try:
        return outcome.get(timeout=config.timeout_seconds)
    except queue.Empty:
        return {"observed_exit_ip": None, "observed_exit_country": None,
                "exit_info_result": "timeout"}


def connect_diagnostics(config: Config) -> dict:
    stages = []
    result = {"connectivity_ok": False, "route_result": "route_not_started",
              "stages": stages, "direct_fallback": False, "exit_ip_pinning_enabled": False,
              "observed_exit_ip": None, "observed_exit_country": None,
              "exit_info_result": "not_requested"}
    try:
        if config.ssh_http_proxy:
            address = urlsplit(config.ssh_http_proxy)
            try:
                with socket.create_connection(("127.0.0.1", address.port),
                                              timeout=config.timeout_seconds):
                    pass
            except OSError:
                raise RouteError("bootstrap_proxy_unavailable") from None
            stages.append({"stage": "local_bootstrap", "result": "tcp_reachable"})
        else:
            stages.append({"stage": "local_bootstrap", "result": "explicit_direct_ssh"})
        with tempfile.TemporaryDirectory(prefix="vps-route-doctor-") as temporary:
            tunnel = SSHTunnel(config, Path(temporary) / "proxy.sock")
            try:
                tunnel.start()
                stages.append({"stage": "ssh", "result": "host_verified_forward_created"})
                health = server_health(str(tunnel.socket_path), config.timeout_seconds)
                result["server"] = health
                stages.append({"stage": "vps_service", "result": health["status"]})
                if health["compatible"] is False:
                    raise RouteError("server_protocol_incompatible")
                reason = connectivity_check(config, tunnel.socket_path)
                if reason:
                    raise RouteError(reason)
                result.update(connectivity_ok=True, route_result="reachable")
                stages.append({"stage": "connectivity", "result": "https_reachable"})
                result.update(observe_exit(config, tunnel.socket_path))
                stages.append({"stage": "exit_info", "result": result["exit_info_result"]})
            finally:
                tunnel.dispose()
    except RouteError as exc:
        reason = str(exc)
        result["route_result"] = reason
        stages.append(failure_detail(reason))
    return result


def inspect_server(config: Config) -> dict:
    with tempfile.TemporaryDirectory(prefix="vps-route-server-check-") as temporary:
        tunnel = SSHTunnel(config, Path(temporary) / "proxy.sock")
        try:
            tunnel.start()
            return server_health(str(tunnel.socket_path), config.timeout_seconds)
        finally:
            tunnel.dispose()
