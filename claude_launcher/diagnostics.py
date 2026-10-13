"""Layered, sanitized diagnostics using only the explicitly configured route."""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config
from .runner import SSHTunnel, egress_check
from .wire import RouteError, server_health


HINTS = {
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
    "egress_check_timeout": "The exit probe timed out; this alone does not identify the failed provider.",
    "probe_http_failure": "The probe website returned an error; inspect it before blaming the exit.",
    "probe_invalid_json": "The probe returned invalid JSON; use a compatible HTTPS IP/country endpoint.",
    "egress_ip_changed": "Observed exit differs from configured IP; inspect routing before changing the pin.",
    "egress_country_changed": "Observed country differs from configured country; inspect routing/probe data.",
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
        stage = "exit_probe"
    return {"stage": stage, "result": reason,
            "hint": HINTS.get(reason, "Inspect the configured route; do not enable direct fallback.")}


def connect_diagnostics(config: Config) -> dict:
    stages = []
    result = {"configured_egress_matches": False, "route_result": "route_not_started",
              "stages": stages, "direct_fallback": False}
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
                reason = egress_check(config, tunnel.socket_path)
                if reason:
                    raise RouteError(reason)
                result.update(configured_egress_matches=True,
                              route_result="configured_ip_and_country_match")
                stages.append({"stage": "exit_probe", "result": "ip_and_country_match"})
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
