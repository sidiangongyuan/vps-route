# VPS Route

A Linux CLI that starts an independently installed, official Claude Code in a
separate profile and routes its HTTPS connections through your own VPS over SSH.

**Status: private alpha, Linux only.** macOS is a planned native backend, not
implemented support. This repository contains no Claude binary, credentials,
conversation history, VPN subscription, or vendor-specific authorization code.
It is not affiliated with Anthropic.

[中文使用说明](docs/USAGE.zh-CN.md) · [Security boundaries](docs/SECURITY.md)

## What it does

- Verifies the SSH host against your existing known-hosts file.
- Checks the configured final public exit IP and country before starting Claude.
- Uses a separate Linux network namespace: only the local proxy is reachable,
  not the host network. Host proxy environment variables are not inherited.
- Exposes a fixed proxy address inside each isolated invocation:
  http://127.0.0.1:17878. It is not a host-wide listener.
- Terminates its own Claude process tree when its SSH tunnel exits.
- Keeps Claude's installation, account, sessions, and memory in an independent
  user-owned profile. Your existing installation is not replaced.
- Supports the official latest installer and official update command through
  the same route, manual OAuth-code login, skills, and project instructions.
- Records compact private startup/exit metadata, not prompts, terminal output,
  login URLs, authorization codes, or account tokens.

Runtime supervision checks **SSH process liveness**, not periodic exit-IP
reputation or IP changes. A silent upstream failure can make requests fail
without terminating the CLI. The namespace still supplies no direct IP route.

## Requirements

Client:

- Linux with working unprivileged user/network namespaces and bubblewrap.
- Python 3.10+ for the package **and /usr/bin/python3** for sandbox children.
- OpenSSH client, Bash, curl, and CA certificates.
- OpenBSD netcat if you select a local HTTP proxy to reach the VPS.

VPS:

- Linux, /usr/bin/python3 3.10+, systemd, and outbound HTTPS/DNS access.
- Existing SSH key authentication, a verified host key, and Unix-socket forwarding.
- An SSH user with a private home directory. Root uses a system service; ordinary
  users use a systemd user service and need a functioning user manager.

On Ubuntu/Debian, the typical client dependencies are:

~~~bash
sudo apt-get install python3 python3-venv bubblewrap openssh-client \
  netcat-openbsd curl ca-certificates
~~~

The tool does not change kernel namespace restrictions. If isolation is disabled
by your distribution or administrator, startup fails rather than using an
unprotected fallback.

## Install

The repository is private; clone with your authenticated GitHub account:

~~~bash
gh repo clone sidiangongyuan/vps-route
cd vps-route
pipx install .
~~~

Or use a dedicated virtual environment:

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/vps-route --help
~~~

Use that environment's vps-route executable in the examples below.
The package is **not published to PyPI**: installing by the bare package name is
not currently a supported installation instruction.

## First setup

Prepare an SSH key accepted by your VPS and verify its host fingerprint through
your VPS console or administrator. Do not blindly trust unverified scan output.
The tool does not create keys, copy passwords, enable password login, or modify
sshd configuration.

~~~bash
vps-route init
vps-route doctor
~~~

The wizard asks for the VPS numeric IP, SSH user/port, existing private key,
known-hosts file, private remote socket path, expected final exit IP/country,
profile name, and optional timezone/local SSH bootstrap proxy.

Config defaults to ~/.config/vps-route/config.json, with private permissions.
The wizard refuses to overwrite an existing file. You can also edit a copy of
[config.example.json](config.example.json) and import it:

~~~bash
vps-route init --from-file ./my-route.json
~~~

For a VPS that does not already run the private CONNECT service:

~~~bash
vps-route setup-vps                 # preview only
vps-route setup-vps --apply         # deploy through verified SSH
vps-route doctor --connect
vps-route network-test
~~~

Deployment adds only this tool's server modules, private Unix socket, and
vps-route-connect.service. It does not edit SSH, firewall, SS/Hy2, VPN,
residential-proxy, or Claude-account configuration. An unmanaged service with
the same name is not overwritten. Existing compatible CONNECT services can be
used without deployment; specify their socket in your config.

For a non-root VPS user, the administrator may need to enable its systemd user
manager and persistence, for example using loginctl enable-linger USER.
The tool does **not** make that administrator change automatically.
If setup fails midway, installed tool files may remain; it reports failure and
does not substitute a direct route.

## Install Claude, sign in, and work

~~~bash
vps-route install
vps-route auth login
vps-route auth status --text
cd /path/to/your/project
vps-route run
~~~

The install command downloads and runs Anthropic's official **latest** installer
in the separate profile. Signing in prints the official manual-code URL. Open it
in a browser using the intended final exit, authorize, and paste the one-time code
back into the same waiting terminal. Do not change the URL parameters.
The browser and any remote GUI are not provided or routed by this package.

If your host proxy/VPN is needed to reach SSH, set ssh_http_proxy explicitly,
for example http://127.0.0.1:7890. It is an SSH transport hop, not Claude's
final proxy. Only this configured hop is used; inherited global HTTP proxy
variables do not select it. A TUN-mode VPN may carry SSH through OS routing.

Useful commands:

~~~bash
vps-route run --safe-mode
vps-route run -- --resume SESSION_ID
vps-route run --workspace /path/to/project
vps-route native-version
vps-route update
vps-route logs --tail 20
vps-route shell
~~~

For another config, put the global flag **before** the command:

~~~bash
vps-route --config /private/path/other-route.json doctor --connect
~~~

Run the same commands in a VS Code Remote SSH integrated terminal. The VS Code
extension itself is not automatically redirected to this profile or route.
Installation does not replace the system or user's ordinary claude command.

## Files and customizations

Default profile:

~~~text
~/.local/share/claude-launcher/profiles/default/
  .local/bin/claude       official installation entry
  .claude/               credentials, settings, skills, projects, memory
  auth-workspace/        empty login/maintenance directory
  default-workspace/     used when launched from the host HOME
~~~

Normal runs load independent user skills and project CLAUDE.md/rules, allow
automatic memory, and disable hooks, MCP, account connectors, remote control,
and cloud skill sync. Profile-local skills take priority over read-only skills
from ~/.agents/skills and ~/.claude/skills. No skills ship with this package.
Use --safe-mode to disable all customizations.

By default, only the selected project and private profile are writable. /tmp
is temporary; extra host runtimes under HOME are not automatically exposed.
The texlive_root setting enables a validated read-only TeX Live installation.
The research_filesystem setting opts into broader host HOME and standard research
mount access with private-directory/socket overlays. This is for trusted
projects, **not** a hostile-code security boundary; see the security document.

Timezone is optional and process-scoped. Leaving it null uses the host default,
not a hardcoded city, and does not change either machine's system timezone.

## Reset and migration

Normal setup does not clean up your old Claude/HICC files. Isolation prevents
automatic reuse without destroying the original data.

~~~bash
vps-route reset                    # preview full inventory
vps-route reset --apply            # archive profile AND detected legacy state, reinstall
vps-route reset --apply --no-install
~~~

**Read the preview first.** Full reset can move the user's conventional
~/.claude, ~/.claude.json, ~/.hicc, and Claude cache into a private archive,
not just this tool's profile. It refuses while relevant processes run.
For profile-only rotation use vps-route auth reset; reinstall afterward.

Archive is preservation, not physical erasure, token revocation, a new machine
identity, or a guarantee about account restrictions.
Legacy wrapper cutover remains an explicit advanced migration command, not an
installation default. Do not use it just to get started.

## Development and release status

~~~bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m pip wheel --no-deps . -w dist
~~~

Tests use synthetic accounts, temporary files, test-owned local sockets, and
mocked SSH/systemd. No real Claude authorization or model request is required.
See [VALIDATION.md](docs/VALIDATION.md) for what was actually checked.

Uses Python's standard library, OpenSSH, bubblewrap, and the externally installed
official Claude Code. No third-party implementation or HICC/QB Gate code is
bundled. The SSH/VPS transport can be configured for different users and hosts.
Optional residential exit routing must be configured separately on the VPS;
this package does not deploy a whole-machine transparent proxy.

Private staging; no open-source license selected yet. See [LICENSE](LICENSE).
