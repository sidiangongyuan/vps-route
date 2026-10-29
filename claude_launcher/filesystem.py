"""Opt-in research files with explicit account/credential overlays."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .wire import RouteError


RESEARCH_ROOTS = (Path('/mnt'), Path('/media'), Path('/opt'), Path('/srv'))
PRIVATE_NAMES = {
    '.claude', '.claude.json', '.codex', '.ssh', '.local', '.config', '.cache',
    '.aws', '.azure', '.kube', '.docker', '.gnupg', '.aliyun', '.terraform.d',
    '.netrc', '.npmrc', '.pypirc', '.git-credentials', '.gitconfig', '.s3cfg', '.boto',
    '.gemini', '.copilot', '.cursor', '.cursor-server', '.vscode', '.vscode-server',
    '.codegeex', '.cc-switch', '.agents', '.Xauthority', '.orca', '.orca-remote',
    '.profile', '.pam_environment', '.viminfo', '.wget-hsts',
}
# Proxy clients installed in HOME commonly keep subscription URLs next to their binaries.
PRIVATE_PATTERNS = ('.claude*', '.bash*', '.zsh*', '.shell*', '.env*', '.condarc*',
                    'clash*', 'mihomo*')


def research_mounts(home: Path) -> list[str]:
    """Bind ordinary files read/write; host DAC permissions still apply."""
    mounts: list[str] = []
    for root in (*RESEARCH_ROOTS, home):
        if root.is_dir():
            mounts.extend(['--bind', str(root), str(root)])
    return mounts


def private_paths(home: Path, repository: Path, extra: tuple[str, ...] = ()) -> list[Path]:
    """Mask resolved targets too, so a home symlink cannot reveal credentials.

    ``extra`` holds the user's own HOME entry names or patterns from config.
    """
    candidates = [home / name for name in PRIVATE_NAMES]
    patterns = (*PRIVATE_PATTERNS, *extra)
    candidates.extend(p for p in home.iterdir()
                      if any(fnmatch.fnmatch(p.name, pattern) for pattern in patterns))
    candidates.extend(home / name for name in ('.cargo/credentials', '.cargo/credentials.toml'))
    candidates.extend(repository.glob('config*.local.json'))
    candidates.append(repository / 'config.json')
    protected: set[Path] = set()
    for path in candidates:
        if not path.exists():
            continue
        target = path.resolve()
        if target == home or target in home.parents or target in RESEARCH_ROOTS:
            raise RouteError('private_path_resolves_to_broad_filesystem_root')
        protected.add(target)
    # Avoid overlays below an already masked parent, notably .local's archives.
    return [p for p in sorted(protected, key=lambda p: (len(p.parts), str(p)))
            if not any(parent in protected for parent in p.parents)]


def private_mounts(paths: list[Path]) -> list[str]:
    mounts: list[str] = []
    for path in paths:
        if path.is_dir():
            mounts.extend(['--tmpfs', str(path)])
        else:
            mounts.extend(['--ro-bind', '/dev/null', str(path)])
    return mounts


def host_socket_mounts(home: Path, profile: Path, upstream: Path,
                       *, masked: tuple[Path, ...] = ()) -> list[str]:
    """Hide currently bound host sockets under newly visible file trees.

    This is not a generic hostile-code sandbox: do not place a host-side network
    relay in the writable project after startup. /run and /tmp stay private.
    """
    roots = (*RESEARCH_ROOTS, home)
    mounts: list[str] = []
    for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
        fields = line.split(maxsplit=7)
        if len(fields) < 8 or not fields[7].startswith('/'):
            continue
        path = Path(fields[7])
        if any(path.is_relative_to(root) for root in masked):
            continue
        if path == upstream or path.is_relative_to(profile):
            continue
        if not any(path.is_relative_to(root) for root in roots):
            continue
        try:
            is_socket = path.is_socket()
        except PermissionError:
            # /proc/net/unix also lists other users' private paths. Host DAC
            # already denies this user access; do not abort unrelated launches.
            continue
        if is_socket:
            mounts.extend(['--ro-bind', '/dev/null', str(path)])
    return mounts
