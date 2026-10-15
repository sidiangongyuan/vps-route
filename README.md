# VPS Route

Run official Claude Code in a separate local environment, with its HTTPS traffic
routed through your own VPS over SSH. If the connection fails, there is no direct fallback.

Linux only. Requires your own VPS and Claude account. Claude is downloaded during
setup, not bundled with the tool. Not affiliated with Anthropic.

[中文使用说明](docs/USAGE.zh-CN.md) · [First-time setup example](docs/FIRST_RUN.zh-CN.md)
· [Security](docs/SECURITY.md)

## Requirements

- Your computer: Linux, Python 3.10+ (including `/usr/bin/python3`), bubblewrap,
  OpenSSH, curl and Bash. Unprivileged network namespaces must work.
- Your VPS: Linux, `/usr/bin/python3` 3.10+, systemd, outbound HTTPS/DNS,
  SSH key login and Unix-socket forwarding. Non-root users need a working systemd user service.
- An SSH private key and a verified VPS host fingerprint.

On Ubuntu/Debian, install the client dependencies:

```bash
sudo apt-get install git gh python3 python3-venv pipx bubblewrap openssh-client \
  netcat-openbsd curl ca-certificates
```

## Install the tool

Clone the repository with an account that has access, then install:

If needed, sign in to GitHub first with `gh auth login`.

```bash
gh repo clone sidiangongyuan/vps-route
cd vps-route
pipx install .
pipx ensurepath
```

Open a new terminal if `vps-route` is not found. Alternatively, install in a
virtual environment; see the [setup guide](docs/FIRST_RUN.zh-CN.md).
The package is not published to PyPI.

## First-time setup

```bash
vps-route init
vps-route setup-vps --apply
vps-route doctor --connect
vps-route install
vps-route auth login
vps-route auth status --text
```

`init` asks for your VPS address, SSH user/port, key and known-hosts paths,
and the private service path on the VPS. No fixed exit IP is required.
See the [complete example](docs/FIRST_RUN.zh-CN.md) for what each field means.
Skip `setup-vps` if your VPS already has a compatible service.

For login, open the official URL printed in the terminal using a browser routed
through the same intended exit. Paste the returned one-time code into that same
waiting terminal. The tool does not route your browser or provide a remote desktop.

Repeated `init` reuses your environment without clearing login or history.
Repeated `install` skips an existing executable installation entry; use `update`
to upgrade Claude. Conflicting configs or invalid entries are not overwritten.

## Daily use

```bash
cd /path/to/your/project
vps-route run
```

The same command works in a VS Code Remote SSH terminal. Installing this tool
does not replace an existing `claude` command or configure the VS Code extension.

| Need | Command |
| --- | --- |
| Check the connection and exit | `vps-route doctor --connect` |
| Check login | `vps-route auth status --text` |
| Update Claude | `vps-route update` |
| Show Claude's version | `vps-route native-version` |
| Resume a conversation | `vps-route run -- --resume SESSION_ID` |
| Disable customizations for troubleshooting | `vps-route run --safe-mode` |
| See recent connection logs | `vps-route logs --tail 20` |

## What stays separate

Claude's installation, login and history live in:

```text
~/.local/share/claude-launcher/profiles/default/
```

Your existing Claude data is not moved or deleted. Normal runs support skills,
project `CLAUDE.md`, rules and automatic memory. Hooks, MCP, account connectors,
remote control and cloud skill sync are disabled.

Before launch, the tool checks HTTPS connectivity through your VPS. During `run`,
Claude displays request errors and handles its own retries. The tool keeps your
session open and silently reconnects lost SSH connections through the same
configured VPS, without a direct fallback. There are no periodic HTTPS probes or
extra connection-status messages during daily use. SSH identity/authentication
failures still show a notice when automatic reconnect cannot continue.
Once connectivity returns, retry interrupted requests if needed. Login, install
and update commands still stop on route failure.
A changed exit IP does not stop Claude.
`doctor --connect` shows the observed exit IP for information only; a failed IP
lookup does not fail an otherwise successful connectivity check.
Browsers and unrelated applications are not covered. See [Security](docs/SECURITY.md).

## Update or uninstall the tool

Close active VPS Route sessions, then update from your checkout:

```bash
git pull --ff-only
pipx reinstall vps-route
```

To uninstall, review the deletion list first:

```bash
vps-route uninstall --dry-run
vps-route uninstall
```

Uninstall permanently removes local tool data, including login, history and
historical backups. To keep your Claude data, use `--keep-profiles --keep-backups`.
It does not remove anything on the VPS.

## License

See [LICENSE](LICENSE). Claude Code is installed separately under Anthropic's terms.
