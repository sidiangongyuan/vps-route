# Security

## Connection protection

Claude runs on your Linux computer, not on the VPS. The tool gives the Claude
process and its children a separate network environment with no direct external
IPv4/IPv6 route. HTTPS goes through a private proxy connection carried over SSH
to your VPS. Destination DNS resolution happens on the VPS; destination TLS is
not terminated by the tool's proxy.

SSH key authentication and strict host-key verification are required. Verify
the VPS fingerprint through its console or administrator before configuring it.
A configured local VPN/proxy may carry SSH, but it is not a fallback Claude exit.

Before launch, HTTPS must be reachable through the configured VPS route. Preflight
probes independent websites in parallel; one reachable target is enough. During
normal `run`, there are no periodic HTTPS probes or launcher connection-status
messages. Claude handles request errors and its own retries; the launcher does
not terminate Claude for a network outage. SSH process loss triggers reconnects
to the same configured VPS with 5/10/20/30-second backoff, capped at 30 seconds
between attempts.
Host-key or authentication failure stops retries until the user fixes the cause
and restarts; this blocked state shows an actionable notice. Host verification is
never relaxed. A healthy SSH process is not restarted for upstream request errors.
No failed prompt is replayed by the launcher. Claude may retry a request, or the
user may need to do so.

The namespace has no direct route even while Claude is kept alive. Reconnects
replace only the owned SSH socket in a private, read-only-mounted directory;
they do not restart Claude or expose the host network. No fallback transport or
VPN is selected. Login, install and update commands retain fail-fast supervision:
SSH loss or two consecutive failed checks stop those operations by default.

Exit IP and country are not pinned or compared. `doctor --connect` displays the
observed exit IP when available; IP lookup failure is informational. Changing a
VPS upstream exit does not itself stop Claude. Old `expected_ip`/`expected_country`
config fields are ignored. Host-key verification remains mandatory.

Preflight/diagnostic checks and maintenance-command probes verify CONNECT,
destination TLS and an HTTP response, not account/API health. Test websites can
fail independently of Claude. SSH recovery events describe only SSH transport;
they do not establish upstream or API recovery.
While a connection is stalled or a check is pending, the isolated network still
has no direct fallback.

## What is not covered

- Your browser, links opened outside the terminal, editor extensions and unrelated
  applications do not automatically use this route.
- A host-side service asked to perform work is outside the child's network boundary.
- Project files, including secrets in the selected project, can be read by Claude.
  The tool is not a sandbox for running hostile code.
- Optional `research_filesystem` access exposes more host files. Enable it only
  for trusted work; it does not hide every credential location or host identifier.
- The VPS operator can observe destinations and connection timing. This tool does
  not conceal OS metadata or make the client appear to run on the VPS.
- Connectivity checks do not measure IP reputation or guarantee against account restrictions.

## Optional Orca control

`run --orca` grants the selected local Orca runtime's control capability. Setting
`orca_control: true` also enables it for daily runs whose launching environment
provides `ORCA_CLI_COMMAND`, including Orca-launched children. It defaults to false.
The integration currently supports Orca's Linux remote Node CLI wrapper. Only the
CLI executable resources, a private temporary Unix-only bootstrap and its runtime socket are
mounted; host Orca account/history databases and other relay sockets stay hidden.
The temporary bootstrap contains an Orca control token and is removed on exit.
No remote/WebSocket transport is exposed by this integration.

The child keeps its private network namespace and VPS proxy. However, Orca can
create host-side terminals, start other agents and perform other host operations.
Those operations are outside the child's network boundary: local IPC access is
not a restriction to read-only orchestration. Every new Claude must use a VPS
Route launcher, not a separate binary, managed account or structured Chat UI that
bypasses it. The tool does not enforce or modify Orca launch settings.
Only enable this option when you intend to grant that host-control capability.
Restart the Claude session after Orca restarts or its CLI/runtime changes.

## Your data

The independent profile contains real credentials and conversation history after
login. Keep it private, along with SSH keys and connection config. Connection logs
record brief startup/check/exit metadata, including target indexes, elapsed time
and sanitized failure categories, plus SSH connection-loss/recovery events, not
prompts, login codes or account tokens.

`init` creates or reuses config and profile directories without clearing old data.
`install` skips an existing executable entry inside the profile and refuses
invalid or external entries. It does not reset account files.

`setup-vps --apply` installs this tool's private service. It does not configure
SSH authentication, a VPN, residential proxies or firewall rules. Do not select
a service path used by another application. Service installation may leave its
own files behind on failure; there is no direct fallback.

`uninstall` permanently removes local tool data by default, including profiles
and historical backups. Preview first and use `--keep-*` options to retain data.
It never connects to or removes anything on the VPS. Local file deletion does
not revoke server-side tokens or erase browser cookies, snapshots or other copies.

## Setup-token credentials

`auth import-token` accepts a long-lived token through a hidden interactive
prompt. It validates local format and stores an owned regular `0600` file in a
`0700` directory in the selected profile. Symlinks, hard-linked credential files,
foreign ownership and group/world-readable token storage are refused.
Existing tokens require explicit `--replace`. Only the selected token is injected
after the launcher sanitizes the child environment; ambient account variables
remain excluded. SSH, installers, version/update commands and browser login do
not receive the imported token.

A stored token selects token authentication for new daily sessions. Invalid
storage stops launch. To choose browser login, explicitly remove the local token.
Status reports local storage and source only; it does not certify a valid server
session. Removal does not revoke the upstream credential or change running
processes. Reset archives and retained backups may still contain old tokens.
Claude, its child processes, the same OS user and administrators can access
process credentials. Network isolation does not make a token safe to publish.

Shell completion is a metadata-only path before config and runtime imports.
It never reads credential contents, starts a route, or records command lines.

## Reporting problems

Report the command used and the error message. Remove keys, tokens, authorization
codes, account screenshots and conversation content before sharing. A diagnostic
report can be generated with:

```bash
vps-route doctor --connect --report ~/.config/vps-route/support.json
```

This report includes the observed public exit IP; redact it before sharing if needed.

The tool is under development and has not received an independent security audit.
