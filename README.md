# VPS Route

Run official Claude Code in a separate local environment, with its HTTPS traffic
routed through your own VPS over SSH. If the connection fails, there is no direct fallback.

Linux, verified on Ubuntu 22.04; other distributions are untested. macOS support
is experimental. Native isolation tests run on macOS 27.0.1; real-account and VPS
end-to-end validation is pending ([macOS guide](docs/MACOS.zh-CN.md)).
Requires your own VPS and Claude account. Claude is downloaded during setup, not bundled
with the tool. Not affiliated with Anthropic.

[中文使用说明](docs/USAGE.zh-CN.md) · [First-time setup example](docs/FIRST_RUN.zh-CN.md)
· [Security](docs/SECURITY.md)

## Requirements

- Your computer: Linux, Python 3.10+ (including `/usr/bin/python3`), bubblewrap,
  OpenSSH, curl and Bash. Unprivileged network namespaces must work.
- Your VPS: Linux, `/usr/bin/python3` 3.10+, systemd, outbound HTTPS/DNS,
  SSH key login and Unix-socket forwarding. Non-root users need a working systemd user service.
- An SSH private key and a verified VPS host fingerprint.
- macOS (experimental): Homebrew `pipx` and `gh`; isolation uses the built-in
  `sandbox-exec`. Run `vps-route macos-check` first. Some commands are Linux-only;
  see the [macOS guide](docs/MACOS.zh-CN.md).

On Ubuntu/Debian, install the client dependencies:

```bash
sudo apt-get install git gh python3 python3-venv pipx bubblewrap openssh-client \
  netcat-openbsd curl ca-certificates
```

## Install the tool

On macOS, install the `macos-support` branch with the
[macOS guide](docs/MACOS.zh-CN.md). The commands below install the default branch for Linux.

The repository is private. Sign in with a GitHub account that has access and let
git use that login, then install directly from GitHub (no clone needed):

```bash
gh auth login
gh auth setup-git
pipx install git+https://github.com/sidiangongyuan/vps-route.git
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
| Update Claude (not this tool) | `vps-route update` |
| Update this tool | `vps-route self-update --apply` |
| Show Claude's version | `vps-route native-version` |
| Resume a conversation | `vps-route run -- --resume SESSION_ID` |
| Disable customizations for troubleshooting | `vps-route run --safe-mode` |
| See recent connection logs | `vps-route logs --tail 20` |

## What stays separate

Claude's installation, login and history live in:

```text
~/.local/share/claude-launcher/profiles/default/
```

Your existing Claude data (`~/.claude`, `~/.claude.json`, an existing `claude`
command) is never moved or deleted, including by `reset` and `uninstall`.
Normal runs support skills, project `CLAUDE.md`, rules and automatic memory.
Hooks, MCP, account connectors, remote control and cloud skill sync are disabled.

## What Claude can reach

Inside a `vps-route run` session, only HTTPS on port 443 to public hosts works,
through your VPS. This covers the Claude API, `pip`/`npm` downloads and HTTPS
web access. These do **not** work inside the session:

- `git push`/`git clone` over SSH (`git@github.com:...`), or any other SSH connection
- plain `http://` URLs and ports other than 443
- private/LAN addresses and services on your computer (`localhost` of the host)

Run such commands from your normal terminal instead.

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

For the macOS branch, use the [macOS update steps](docs/MACOS.zh-CN.md#更新本工具).
`self-update` selects a published release; it does not track `macos-support`.

Close active VPS Route sessions first. `self-update` previews by default and
installs only a published GitHub release:

```bash
vps-route self-update            # show current and latest version
vps-route self-update --apply    # install the latest release
vps-route self-update --rollback --apply   # return to the previous version
```

It needs the same `gh` login used for installation. If `self-update` fails,
reinstall the latest code directly:

```bash
pipx install --force git+https://github.com/sidiangongyuan/vps-route.git
```

Updating the tool does not redeploy the VPS service. If `vps-route server-version`
reports `"compatible": false`, run `vps-route setup-vps --apply` again.

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
