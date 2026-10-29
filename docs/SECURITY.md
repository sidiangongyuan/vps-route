# Security

This page describes what VPS Route protects, where its protection ends, and how to report a
problem. It covers the Linux sandbox and the WSL2 work on `windows-wsl`. The macOS branch uses a different sandbox;
see its own guide.

## Reporting a vulnerability

Please report security issues privately through GitHub:
**Security → Report a vulnerability** on the repository page. Do not open a public issue for
anything that could put users' credentials, traffic or machines at risk.

Include the version (`vps-route --version`), what you ran, and what happened. Remove keys,
tokens, login codes, account screenshots, conversation content and your VPS address first.
There is no bug bounty. Reports are handled on a best-effort basis by the maintainers.

VPS Route is young and has not had an independent security audit.

## Network path

Claude runs on your Linux computer. Only its network traffic goes through the VPS.

- bubblewrap gives Claude and its child processes a private network namespace with no
  external IPv4 or IPv6 route.
- Inside that namespace, the only reachable endpoint is a local HTTP CONNECT proxy at
  `127.0.0.1:17878`. The proxy forwards to a Unix socket that SSH carries to your VPS.
- Destination DNS is resolved on the VPS. The proxy relays TLS end to end and never
  decrypts it.
- SSH uses key authentication with strict host-key checking. Confirm the VPS fingerprint
  through your provider's console before the first connection. Host verification is never
  relaxed, including during automatic reconnects.
- A local VPN or HTTP proxy configured as `ssh_http_proxy` may carry the SSH connection to
  the VPS. Claude never uses it as an exit.

### Before and during a session

- Before launch, HTTPS must work through the VPS. Two independent test sites are probed in
  parallel; one success is enough.
- During `run` there are no periodic probes. Claude reports request errors and retries
  them itself. VPS Route keeps Claude running through network outages.
- When the SSH process exits, VPS Route reconnects to the same VPS with a 5, 10, 20 and
  30 second backoff, then retries every 30 seconds. It replaces only its own socket in a
  private read-only directory. Claude is not restarted and never sees the host network.
- Host-key or authentication failures stop reconnecting and show a notice until you fix
  the cause and restart.
- A healthy SSH connection is left alone when upstream requests fail. VPS Route never
  replays a prompt; Claude may retry, or you may need to.
- `auth login`, `install` and `update` stop when SSH drops or two route checks in a row
  fail.
- No fallback transport exists at any point. While a connection is stalled or a check is
  pending, the namespace still has no direct route.

### Exit IP and checks

Exit IP and country are neither pinned nor compared. `doctor --connect` shows the
observed exit IP when the lookup works; a failed lookup is informational. Old
`expected_ip` and `expected_country` fields are ignored.

Checks confirm that CONNECT, destination TLS and an HTTP response work. They say nothing
about account or API health, and test sites can fail on their own. A recovered SSH
connection means only that the transport is back.

## Files Claude can reach

By default the sandbox contains:

- **read and write:** the project folder you start `run` in, the separate profile, and a
  private `/tmp`;
- **read only:** `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, the launcher's own
  code, and skills from `~/.claude/skills` and `~/.agents/skills`.

The rest of your home folder is absent. `run` refuses to use your whole home folder, or an
account or key directory, as the project folder.

Claude can read every file in the project folder, including secrets stored there. VPS Route
is no sandbox for hostile code.

### Optional research mode

`research_filesystem: true` mounts your home folder and `/mnt`, `/media`, `/opt` and `/srv`
read-write, then hides common credential locations: `.ssh`, `.config`, `.aws`, `.gnupg`,
`.docker`, `.kube`, shell startup files, `.env*`, `.netrc`, package-manager credentials,
`~/.claude*`, editor servers, `clash*` and `mihomo*` proxy directories, and the launcher's
own profiles and logs. Add more with `private_home_entries`, for example:

```bash
vps-route config set private_home_entries '[".my-vpn", ".tool-*"]' --apply
```

Unix sockets that exist under these folders at launch are hidden as well, so a host-side
relay there cannot become a way out. A socket created later inside the project folder is
not covered.

Hiding works by path: hidden folders appear empty and hidden files appear blank, but their
names stay visible. The list cannot cover every tool's credential location or every host
identifier. Use research mode only for trusted work.

## What is not covered

- Your browser, links opened outside the terminal, editor extensions and other
  applications use your normal network.
- Work done by host-side services on Claude's behalf, such as Orca terminals, happens
  outside the sandbox's network boundary.
- Your VPS provider can see destinations and connection timing.
- VPS Route does not mask OS details or make your computer appear to run on the VPS.
  Connectivity checks do not measure IP reputation and cannot prevent account restrictions.

## Optional Orca control

`run --orca` gives the session control of the selected local Orca runtime. Setting
`orca_control: true` enables this for daily runs started from an environment that provides
`ORCA_CLI_COMMAND`, including Orca-launched child agents. It is off by default and supports
Orca's Linux remote Node CLI wrapper.

Only the CLI files, a private temporary bootstrap and the runtime's Unix socket are mounted.
Orca's account and history databases and other relay sockets stay hidden. The bootstrap
holds an Orca control token and is deleted on exit. No remote or WebSocket transport is
exposed.

The Claude process keeps its private network and VPS proxy. Orca itself can open host
terminals, start other agents and run other host operations, all outside that boundary;
local IPC access grants that power in full. Make sure every new Claude uses a VPS Route
launcher. VPS Route does not enforce or change Orca's launch settings. Restart the session
after Orca restarts or its CLI changes.

## Your data

After login, the profile holds real credentials and conversation history. Keep it private,
together with your SSH keys and config.

Connection logs record startup, check and exit events, test-target indexes, elapsed time,
sanitized failure categories, and SSH loss and recovery. They never record prompts, login
codes or tokens.

- `init` creates or reuses config and profile directories and never clears data.
- `reset` archives only the tool's profile. It never moves `~/.claude`, `~/.claude.json`
  or an existing `claude` command.
- `install` skips an existing executable entry in the profile and refuses invalid or
  external entries. It does not touch account files.
- `setup-vps --apply` installs only this tool's service. It does not configure SSH
  authentication, VPNs, proxies or firewall rules. Do not point it at a socket path used
  by another application. A failed installation may leave the service's own files behind.
- `uninstall` permanently removes local tool data by default, including profiles and
  backups. Preview with `--dry-run` and keep data with the `--keep-*` options. It never
  touches the VPS. Deleting local files does not revoke server-side tokens or erase browser
  cookies, snapshots or other copies.

## Setup-token credentials

`auth import-token` reads a long-lived token from a hidden interactive prompt, checks its
local format, and stores it as an owned regular file with mode `0600` in a `0700`
directory inside the profile. Symlinks, hard links, foreign ownership and group- or
world-readable storage are refused. Replacing an existing token needs `--replace`.

The launcher cleans the child environment and then injects only this token; other account
variables from your shell stay excluded. SSH, installers, version and update commands and
browser login never receive it.

A stored token selects token authentication for new sessions, and invalid storage stops
launch. To use browser login again, remove the local token first. `auth status` reports the
local storage and source only and does not prove the server accepts the token. Removing it
neither revokes it upstream nor affects running processes. Archived profiles and kept
backups may still contain old tokens.

Claude, its child processes, the same OS user and administrators can read process
credentials. Network isolation does not make a token safe to share.

Shell completion reads only argument metadata. It never loads config or credentials,
starts a route, or records command lines.

## Sharing diagnostics

When asking for help, include the command and the error message. A sanitized report helps:

```bash
vps-route doctor --connect --report ~/vps-route-report.json
```

The report contains the observed public exit IP. Remove it before sharing if you prefer.


## WSL2

WSL2 uses the production Linux sandbox builder for every execution entry and for
doctor probes. WSL1 is refused. The supported seccomp architectures are x86_64
and aarch64; WSL fails closed on other architectures. The filter denies AF_VSOCK
socket/socketpair using the low 32 bits of the domain, denies all three io_uring
syscalls, and kills foreign ABIs including i386/x32. Descendants inherit the
filter. The filter also denies mount APIs after bubblewrap constructs its tree;
effective capabilities are dropped. Existing Linux namespace protections remain.

Strict mode hides Windows drives outside the chosen project, /run/WSL, /mnt/wsl,
/mnt/wslg and discovered runtime mount aliases. Research mode overlays whole
Windows mounts before rebinding the project and makes the overlays read-only.
This covers case-insensitive DrvFS lookup. Windows roots, user roots and sensitive
workspace paths are rejected case-insensitively, including resolved symlinks and
mount source aliases. Private profiles and launcher code require Linux storage.

The binfmt F flag can retain /init across mount isolation. Blocking the runtime
channels and AF_VSOCK therefore carries the execution boundary. Doctor copies a
system cmd.exe into a disposable project, verifies the same copy on the host,
and tests it inside the production sandbox. Missing or unsuccessful controls and
timeouts remain inconclusive. Required inconclusive/unsupported probes prevent
readiness. It also checks native/high-bit socket calls, filter inheritance,
io_uring, capabilities, mount creation, IPv4/IPv6 routes, parent loopback canaries,
and an approved fixture relay. The fixture uses no account or real host service.

Explicit windows_interop enables Windows execution, Windows drive writes and
WSLg display/audio endpoints during daily run. Windows processes use host
networking and user permissions, including access to user-readable Windows
credentials. They may start processes outside the Linux sandbox. Doctor reports
windows_interop_enabled, host_execution_allowed and vps_only_guarantee=false.
Linux processes retain their separate network namespace. Orca host control is
reported independently. Account and maintenance entries keep the strict policy.

Shared writable projects and broad Linux research directories require trust.
A host watcher may execute changed files later; a newly created pathname Unix
socket can grant host services or SCM_RIGHTS capabilities after startup. Socket
masking is a startup snapshot. Credentials deliberately copied or hard-linked
into a selected project remain inside that project's authorization. NTFS
junction canaries were verified locally: redirected private workspaces are rejected
and project links cannot read masked neighbors. Short-name path components are
conservatively refused. Additional relocated profile layouts require separate
canary validation for each deployment. The tool retains its documented scope
as a local workflow sandbox; these shared-file and host-service capabilities
limit containment of actively hostile code.

WSLg exposure grants display, clipboard and audio capabilities. Doctor records
endpoint availability and never reads clipboard contents or records audio.
An X11 fixture window was opened and closed locally. Wayland applications,
audio, clipboard behavior, untested NAT deployments and ARM64 runtime behavior
require additional platform validation. The verified host combination is
documented in the local validation report.
