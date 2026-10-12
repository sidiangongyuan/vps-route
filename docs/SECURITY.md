# Security boundaries

## Supported claim

The Linux launcher creates a private network namespace for the child process
tree. Its only intended external route is a private Unix socket forwarded over
verified SSH, presented as a loopback HTTP CONNECT proxy. In that namespace,
removing proxy variables does not create a direct IPv4/IPv6 route. The server
resolves target names on the VPS and accepts only public HTTPS destinations on
TCP 443. It does not terminate the destination TLS connection.

Changing the host VPN can change SSH reachability; it does not add an interface
to the child's network namespace. An explicit local bootstrap HTTP proxy is
used only for the SSH transport. No automatic direct SSH fallback is selected
if that proxy is unavailable.

SSH host keys must be independently verified and stored in the configured
known-hosts file. The launcher uses strict checking and key authentication.
The VPS operator can observe connection destinations/timing and manage the
server. This tool does not hide local OS metadata from Claude or promise that
the client appears to execute on the VPS.

## Failure handling

- Before every launch, the configured public exit IP and country must match.
- A failed preflight prevents the Claude child from starting.
- SSH process exit closes the owned route and terminates the owned child tree.
- During a transport stall, connections may hang until SSH declares failure.
  The child has no direct route in the meantime.
- Runtime checks are not periodic public exit-IP checks. Changing the VPS's
  upstream route after startup is not necessarily detected.
- No IP reputation, country, proxy, or timezone test guarantees account safety.

## Scope exclusions

The host browser, VS Code extension, Orca, other processes, and a Claude process
started outside this tool are not covered. Children launched in the tool's
namespace are covered; asking a host-side service to act on their behalf is a
different boundary.

Default project-only mode hides the host HOME. It still exposes the selected
project, system runtime files including /etc, and the independent profile.
Project secrets such as .env files in that selected project can be read.

Opt-in research_filesystem mode intentionally exposes broader host trees and
masks known private paths and currently bound filesystem Unix sockets.
This is not a hostile-code sandbox or a generic data-loss-prevention mechanism:
new host IPC relays, alternate credential locations, hard links, other host
users' accessible files, and races are not comprehensively prohibited. Do not
enable it for untrusted projects. It is not a guarantee of hiding every host
identifier or file.

The profile contains real credentials after login. Reset archives may contain
them too. Archive is not deletion or revocation. Do not publish profile/config
directories, logs, history, keys, or archives.

## Setup side effects

init writes one new private connection config and refuses to overwrite it.
setup-vps --apply uploads only server modules and installs/enables/restarts the
owned systemd CONNECT service. It does not provision SSH keys, modify SSH
authentication, rewrite firewalls, or install VPN/residential-proxy services.
For an existing compatible service, skip this command. Service failures can
leave tool-owned files installed; there is no automatic full rollback.

Do not point setup-vps at an existing socket owned by another service.
The private Unix socket is never a public TCP proxy.

## Reporting

This is a private alpha, not a security-reviewed public release. Report issues
privately without pasting keys, tokens, prompts, or account screenshots.
