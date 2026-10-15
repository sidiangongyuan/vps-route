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
  It hides common credential locations (`.ssh`, `.config`, `.aws`, shell rc files,
  `clash*`/`mihomo*` proxy directories and similar). Add your own HOME entries
  with `private_home_entries`, for example
  `vps-route config set private_home_entries '[".my-vpn", ".tool-*"]' --apply`.
- The VPS operator can observe destinations and connection timing. This tool does
  not conceal OS metadata or make the client appear to run on the VPS.
- Connectivity checks do not measure IP reputation or guarantee against account restrictions.

## Your data

The independent profile contains real credentials and conversation history after
login. Keep it private, along with SSH keys and connection config. Connection logs
record brief startup/check/exit metadata, including target indexes, elapsed time
and sanitized failure categories, plus SSH connection-loss/recovery events, not
prompts, login codes or account tokens.

`init` creates or reuses config and profile directories without clearing old data.
`reset` archives only the tool profile; it never moves `~/.claude`, `~/.claude.json`
or an existing `claude` command.
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

## Reporting problems

Report the command used and the error message. Remove keys, tokens, authorization
codes, account screenshots and conversation content before sharing. A diagnostic
report can be generated with:

```bash
vps-route doctor --connect --report ~/.config/vps-route/support.json
```

This report includes the observed public exit IP; redact it before sharing if needed.

The tool is under development and has not received an independent security audit.
