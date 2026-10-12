# Linux alpha validation

Date: 2026-10-12. Current version: 0.3.1a1.

## Local lifecycle update: 0.3.1a1

- Final affected source tests: 14 setup tests and 16 local lifecycle tests returned OK.
  They cover default initialization archival, opt-out, confirmation/preview,
  active-process refusal, permanent backup deletion, selective preservation,
  nested custom config, SSH material protection, mount/link refusal, changed
  cleanup scope, corrupt standard config and unrelated custom-file protection.
- Built the new wheel with the declared isolated build requirements and installed
  it into a disposable Python virtual environment outside the source repository.
- Installed-package setup/lifecycle tests and package removal are checked in that
  disposable environment. No real SSH, VPS service, credentials, login, or model
  request is used. Process checks in the removal smoke are simulated.
- The initial whole-suite run returned three setup fixture failures because
  process checks observed live host clients. Tests now mock those process checks;
  affected tests were rerun rather than repeating the unrelated full suite.
- Host setuptools was too old for the project's SPDX license declaration when
  building without isolation. Normal PEP 517 isolation uses setuptools 77+ as
  declared in pyproject.toml and built successfully; no host package was upgraded.

The update does not run init/uninstall on the current user profile or change the
live launcher. File removal is not a claim of disk sanitization or global trace
removal. Source and package-manager environments are retained unless explicitly
handled by their original installer; uninstall does not connect to the VPS.

## Previously completed: 0.3.0a1

- Source unit/integration suite: 82 tests, OK, one optional TeX Live test skipped.
  After scoped setup/CLI/filesystem corrections, 24 affected tests also returned OK.
- Built the wheel and installed it into a new temporary Python 3.12 virtual
  environment. Sandbox children use the Linux system Python 3.10.
- Imported the installed package while outside the source repository.
- Installed-package route integration: 15 tests returned OK. Test-owned Unix
  CONNECT sockets and a local TLS service exercised successful proxied HTTPS,
  missing direct network routes, missing proxy failure, startup exit checks,
  owned-descendant termination, and native-launcher update fixtures.
- Installed-package login regression: five tests returned OK after the fix below.
  They use a fake CLI and synthetic codes, not Anthropic accounts.
- Installed-package runtime regression: two tests returned OK. A relocated package
  under a mock pipx directory survives private HOME overlays, while adjacent
  packages and old synthetic credentials remain hidden.
- Installed CLI smoke: version, config import, offline doctor, deployment preview,
  and refused config overwrite returned their expected statuses in a fresh HOME
  and unrelated working directory. Legacy synthetic state remained unchanged.
- Wheel inventory: 32 members, including six packaged child-entry modules.
  No test suite, local connection config, live account files, or cache directories
  are included in the wheel.
- Source publication audit and targeted personal-address/credential-pattern review
  are performed before pushing the private repository.

## Packaging defect found and fixed

The first installed-wheel login fixture failed because package installation did
not preserve the execute bit of the browser helper. The launcher now generates a
private executable helper in the isolated profile instead of executing a package
data file directly. The helper suppresses browser opening and prints no second
OAuth URL. It refuses to overwrite an unrelated or outdated helper; removing only
the generated .launcher-runtime/browser-hook while the CLI is stopped lets the
next login regenerate it. It does not remove credentials or sessions.

## Not tested / not claimed

- No real Claude login, token exchange, model request, or subscription was tested.
- The new setup-vps deployment was tested with synthetic files and mocked
  SSH/systemd, not executed against the current live VPS or a fresh external VPS.
- No actual SSH key was generated or SSH server reconfigured.
- No cross-distribution or second-machine installation was tested.
- Optional real TeX Live compilation was skipped.
- No macOS backend is implemented or tested.
- No ongoing public exit-IP monitoring, IP reputation guarantee, full hostile-code
  containment, or account-restriction prevention is claimed.

The original running launcher, connection configuration, Claude profile, and VPS
services were left in place. This release is a separate staging copy, not a live
cutover. A new user's real SSH deployment and official login still need validation
before describing the alpha as broadly production-ready.
