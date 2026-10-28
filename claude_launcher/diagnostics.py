"""Layered, sanitized diagnostics using only the explicitly configured route."""

from __future__ import annotations

import http.client
import ipaddress
import queue
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config
from .runner import SSHTunnel, connectivity_check
from .wire import RouteError, proxy_json, server_health


HINTS = {
    "invalid_setup_token_format": "Paste the full long-lived token printed by `claude setup-token`.",
    "setup_token_active; remove_local_token_before_browser_login": (
        "An imported token is active. Preview `vps-route auth remove-token`, run it with "
        "--apply, then log in with the browser."
    ),
    "token_import_requires_interactive_terminal": (
        "Run `vps-route auth import-token` in an interactive terminal; the input stays hidden."
    ),
    "unsafe_or_unreadable_token_storage": (
        "Check the ownership and permissions of this profile's token directory; symlinks are refused."
    ),
    "orca_cli_not_found": "Start from an Orca terminal where ORCA_CLI_COMMAND is set.",
    "unsupported_orca_cli_layout": "Orca integration needs Orca's Linux remote Node CLI wrapper.",
    "orca_runtime_unavailable": "Check that the selected local Orca runtime is running.",
    "unsafe_orca_runtime": "Check that the Orca runtime is owned by you and not shared with others.",
    "orca_local_unix_runtime_required": "Orca integration needs a local Unix socket, not a remote URL.",
    "network_isolation_unavailable": (
        "bubblewrap could not create a private network namespace. Install bubblewrap. On Ubuntu "
        "23.10 and later, AppArmor may block unprivileged user namespaces (see Troubleshooting "
        "in the README). Only Ubuntu 22.04 is verified."
    ),
    "bootstrap_proxy_unavailable": "Start the local proxy configured for SSH and check its port.",
    "openbsd_netcat_required_for_ssh_http_proxy": (
        "Install OpenBSD netcat; SSH needs it to use the configured local proxy."
    ),
    "ssh_key_or_verified_known_hosts_missing": "Check the SSH key and known_hosts paths in the config.",
    "ssh_host_key_verification_failed": (
        "Confirm the VPS host fingerprint through your provider's console; never skip this check."
    ),
    "ssh_authentication_failed": "Check the SSH user and key authorization on the VPS (authorized_keys).",
    "ssh_tunnel_failed": "Check that the VPS is reachable on its SSH port, and the local proxy if set.",
    "ssh_tunnel_start_timeout": "SSH did not connect in time. Nothing was sent directly.",
    "proxy_socket_unavailable": "Check the connection service and its socket path on the VPS.",
    "server_health_unavailable": "The VPS service gave an incomplete health response; check the service.",
    "server_protocol_incompatible": (
        "Run `vps-route server-version`, then redeploy with `vps-route setup-vps --apply`."
    ),
    "target_resolution_failed": "The VPS could not resolve the site; check the VPS DNS.",
    "upstream_unavailable": "The VPS could not reach the site; check its outbound network or proxy.",
    "proxy_connect_rejected": "The VPS service refused the request; check its upstream policy.",
    "probe_tls_certificate_failed": (
        "TLS certificate check failed. Check the clock and any HTTPS interception; keep verification on."
    ),
    "probe_tls_failed": "The VPS connected but TLS failed; check the VPS outbound proxy.",
    "probe_transport_failure": "SSH works, but the test site was unreachable; check the VPS outbound path.",
    "connectivity_check_timeout": "The connectivity check timed out; check the VPS and the test site.",
    "probe_http_failure": "The test site returned an error; check that site before changing the VPS.",
    "probe_invalid_json": "The IP lookup returned invalid JSON; use an HTTPS endpoint that returns IP and country.",
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
            "hint": HINTS.get(reason, "Check the configured route; vps-route never falls back to a direct connection.")}


def _local_https_probe(config: Config, url: str) -> str:
    """Probe the configured bootstrap path, with verified TLS and no fallback."""
    target = urlsplit(url)
    proxy = urlsplit(config.ssh_http_proxy) if config.ssh_http_proxy else None
    connection = http.client.HTTPSConnection(
        proxy.hostname if proxy else target.hostname,
        proxy.port if proxy else 443,
        timeout=config.timeout_seconds,
        context=ssl.create_default_context(),
    )
    if proxy:
        connection.set_tunnel(target.hostname, 443)
    try:
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        connection.request("GET", path)
        response = connection.getresponse()
        return "https_reachable" if 200 <= response.status < 500 else "probe_http_failure"
    except ssl.SSLCertVerificationError:
        return "probe_tls_certificate_failed"
    except ssl.SSLError:
        return "probe_tls_failed"
    except (TimeoutError, socket.timeout):
        return "probe_timeout"
    except (OSError, http.client.HTTPException):
        return "probe_transport_failure"
    finally:
        connection.close()


def local_network_diagnostics(config: Config) -> dict:
    """Bound concurrent control probes; failures do not prevent the VPS check."""
    outcome: queue.Queue = queue.Queue()
    started = time.monotonic()

    def inspect(index: int, url: str) -> None:
        began = time.monotonic()
        reason = _local_https_probe(config, url)
        outcome.put({"target_index": index, "result": reason,
                     "elapsed_seconds": round(time.monotonic() - began, 3)})

    for index, url in enumerate(config.connectivity_urls):
        threading.Thread(target=inspect, args=(index, url), daemon=True).start()
    results = {}
    while len(results) < len(config.connectivity_urls):
        try:
            item = outcome.get(timeout=max(0, config.timeout_seconds -
                                           (time.monotonic() - started)))
        except queue.Empty:
            break
        results[item["target_index"]] = item
    probes = [results.get(index, {"target_index": index, "result": "probe_timeout",
                                "elapsed_seconds": round(time.monotonic() - started, 3)})
              for index in range(len(config.connectivity_urls))]
    reachable = any(item["result"] == "https_reachable" for item in probes)
    return {
        "stage": "local_network", "result": "https_reachable" if reachable else "unconfirmed",
        "path": "configured_http_proxy" if config.ssh_http_proxy else "system_network",
        "probes": probes,
        "summary_zh": "本机联网路径已完成 HTTPS 实测。" if reachable else
                      "本机 HTTPS 测试未成功；本机网络、代理或测试网站可能异常，继续检查 VPS。",
    }


def connection_diagnosis(reason: str, stages: list[dict]) -> dict[str, object]:
    """Describe observed boundaries without guessing a provider or root cause."""
    completed = {item["stage"] for item in stages if "hint" not in item}
    attempted = {item["stage"] for item in stages}
    # Checks run in order; a failed check may not have recorded a success row.
    if "ssh" in completed:
        attempted.add("vps_service")
    if "vps_service" in completed and not reason.startswith("server_"):
        attempted.add("connectivity")
    if reason == "reachable":
        domain, confirmed = "none", True
        summary = "The verified VPS SSH route and forwarded HTTPS requests are reachable."
        chinese = "VPS SSH 已连接，经 VPS 转发的 HTTPS 请求正常。"
        next_step = "No connection repair is required for this check."
    elif reason in {"bootstrap_proxy_unavailable", "local_dependencies_unavailable",
                    "openbsd_netcat_required_for_ssh_http_proxy",
                    "ssh_key_or_verified_known_hosts_missing", "ssh_forward_socket_changed"}:
        domain, confirmed = "local_environment", True
        summary = "A local proxy, dependency, key file or forwarding path is unavailable."
        chinese = "本机前置代理、依赖、SSH 文件或转发路径检查失败；尚未验证 VPS 链路。"
        next_step = HINTS.get(reason, "Check local dependencies and network isolation support.")
    elif reason == "ssh_authentication_failed":
        domain, confirmed = "ssh_authentication", True
        summary = "The SSH endpoint responded, but SSH authentication was rejected."
        chinese = "SSH 服务已响应，但身份验证被拒绝；请核对本机密钥和 VPS 用户授权。"
        next_step = HINTS[reason]
    elif reason == "ssh_host_key_verification_failed":
        domain, confirmed = "ssh_host_identity", True
        summary = "The SSH endpoint responded, but its host identity could not be verified."
        chinese = "SSH 服务已响应，但服务器指纹未通过验证；请独立核实 VPS 身份。"
        next_step = HINTS[reason]
    elif "ssh" not in completed:
        domain, confirmed = "vps_or_network_path", False
        summary = "No verified SSH connection to the VPS was established. The VPS, VPN " \
                  "or intermediate network path may be unavailable."
        chinese = "未建立到 VPS 的可信 SSH 连接；VPS、SSH 服务、VPN 或中间线路均可能异常。"
        next_step = "Check the VPS console, SSH port and firewall; compare another network " \
                    "path. A listening local proxy port alone does not prove VPN connectivity."
        local = next((item for item in stages if item["stage"] == "local_network"), None)
        if local and local["result"] == "https_reachable":
            chinese = "本机经配置路径访问测试网站成功，但到 VPS 的 SSH 未建立；" \
                      "请检查 VPS、SSH 端口及到该 VPS 的专用路径。"
            summary = "Local HTTPS control probes succeeded, but verified VPS SSH failed. " \
                      "The VPS, SSH service or target-specific network path needs checking."
        elif local:
            domain = "local_network_or_vps_path"
            chinese = "本机 HTTPS 测试和 VPS SSH 均未成功；优先检查本机网络/VPN，" \
                      "VPS 状态仍待确认。"
            summary = "Local HTTPS controls and VPS SSH both failed. Check local networking " \
                      "and the configured VPN first; VPS availability remains unconfirmed."
    elif "vps_service" not in completed or reason.startswith("server_"):
        domain, confirmed = "vps_proxy_service", False
        summary = "VPS SSH connected, but the private proxy service check did not succeed."
        chinese = "VPS SSH 已连接，但 VPS 私有代理服务检查失败；请检查服务及转发配置。"
        next_step = HINTS.get(reason, "Check the connection service and its socket path on the VPS.")
    else:
        domain, confirmed = "vps_to_destination", False
        summary = "VPS SSH and the proxy endpoint responded, but forwarded HTTPS failed. " \
                  "The VPS exit, DNS, optional upstream proxy or destination may be affected."
        chinese = "VPS SSH 和代理端点已响应，转发 HTTPS 失败；请检查 VPS 出网、DNS、" \
                  "可选上游代理及目标网站。"
        next_step = HINTS.get(reason, "Check that the VPS can reach the destination.")
    return {
        "fault_domain": domain, "cause_confirmed": confirmed,
        "summary": summary, "summary_zh": chinese, "next_step": next_step,
        "not_checked": [stage for stage in ("ssh", "vps_service", "connectivity")
                        if stage not in attempted],
    }


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
        stages.append(local_network_diagnostics(config))
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
    result["diagnosis"] = connection_diagnosis(result["route_result"], stages)
    return result


def inspect_server(config: Config) -> dict:
    with tempfile.TemporaryDirectory(prefix="vps-route-server-check-") as temporary:
        tunnel = SSHTunnel(config, Path(temporary) / "proxy.sock")
        try:
            tunnel.start()
            return server_health(str(tunnel.socket_path), config.timeout_seconds)
        finally:
            tunnel.dispose()
