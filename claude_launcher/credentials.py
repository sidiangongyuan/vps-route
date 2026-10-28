"""Explicit, profile-local setup tokens; never inherit ambient account credentials."""

from __future__ import annotations

import fcntl
import getpass
import os
import re
import secrets
import stat
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .wire import RouteError


TOKEN_DIRECTORY = ".launcher-auth"
TOKEN_FILE = "setup-token"
MAX_TOKEN_SIZE = 8192
_TOKEN = re.compile(r"sk-ant-oat[0-9]+-[A-Za-z0-9_-]+", re.ASCII)


def validate_token(value: str) -> str:
    """Check local format only; validity and subscription remain server decisions."""
    if len(value) > MAX_TOKEN_SIZE + 1 or not _TOKEN.fullmatch(value.strip()):
        raise RouteError("invalid_setup_token_format")
    if len(value.strip()) > MAX_TOKEN_SIZE:
        raise RouteError("invalid_setup_token_format")
    return value.strip()


@contextmanager
def _token_directory(profile: Path, *, create: bool = False) -> Iterator[int | None]:
    home = Path.home().absolute()
    try:
        parts = profile.absolute().relative_to(home).parts
    except ValueError:
        raise RouteError("unsafe_token_directory") from None
    if not parts or any(part in {".", ".."} for part in parts):
        raise RouteError("unsafe_token_directory")
    descriptor = None
    try:
        descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in (*parts, TOKEN_DIRECTORY):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            except FileNotFoundError:
                yield None
                return
            info = os.fstat(child)
            # Existing owned ancestors can use shared permissions; the token
            # directory itself is private, and descriptor traversal rejects links.
            if (info.st_uid != os.getuid()
                    or (part == TOKEN_DIRECTORY and info.st_mode & 0o077)):
                os.close(child)
                raise RouteError("unsafe_token_directory")
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError:
        raise RouteError("unsafe_or_unreadable_token_storage") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_file(directory: int, name: str, flags: int) -> int:
    descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
                         dir_fd=directory)
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        os.close(descriptor)
        raise RouteError("unsafe_token_file")
    return descriptor


def read_token(profile: Path) -> str | None:
    """A missing token selects normal login; unsafe/invalid storage fails closed."""
    with _token_directory(profile) as directory:
        if directory is None:
            return None
        try:
            descriptor = _private_file(directory, TOKEN_FILE, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(descriptor, "rb") as stream:
                if os.fstat(stream.fileno()).st_size > MAX_TOKEN_SIZE + 1:
                    raise RouteError("invalid_setup_token_format")
                raw = stream.read(MAX_TOKEN_SIZE + 2)
            return validate_token(raw.decode("ascii"))
        except UnicodeError:
            raise RouteError("invalid_setup_token_format") from None


def has_token(profile: Path) -> bool:
    """Inspect metadata only, including when recovering a malformed token."""
    with _token_directory(profile) as directory:
        if directory is None:
            return False
        try:
            descriptor = _private_file(directory, TOKEN_FILE, os.O_RDONLY)
        except FileNotFoundError:
            return False
        os.close(descriptor)
        return True


def prompt_token() -> str:
    if not sys.stdin.isatty():
        raise RouteError("token_import_requires_interactive_terminal")
    # getpass can otherwise silently fall back to an echoed stdin prompt.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass("Paste the setup token (hidden): ")
        except getpass.GetPassWarning:
            raise RouteError("token_import_requires_hidden_input") from None
    return validate_token(value)


def store_token(profile: Path, token: str, *, replace: bool = False) -> None:
    token = validate_token(token)
    with _token_directory(profile, create=True) as directory:
        if directory is None:
            raise RouteError("unsafe_or_unreadable_token_storage")
        lock = _private_file(directory, ".lock", os.O_RDWR | os.O_CREAT)
        temporary = ".token-" + secrets.token_hex(8)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                existing = _private_file(directory, TOKEN_FILE, os.O_RDONLY)
            except FileNotFoundError:
                pass
            else:
                os.close(existing)
                if not replace:
                    raise RouteError("setup_token_exists; use_--replace")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write((token + "\n").encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, TOKEN_FILE, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(lock)


def remove_token(profile: Path) -> bool:
    """Remove this local copy only; never revoke upstream or change running sessions."""
    with _token_directory(profile) as directory:
        if directory is None:
            return False
        lock = _private_file(directory, ".lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                descriptor = _private_file(directory, TOKEN_FILE, os.O_RDONLY)
            except FileNotFoundError:
                return False
            os.close(descriptor)
            os.unlink(TOKEN_FILE, dir_fd=directory)
            return True
        finally:
            os.close(lock)


def token_status(profile: Path) -> dict[str, object] | None:
    if read_token(profile) is None:
        return None
    return {"authentication_source": "setup_token", "token_stored": True,
            "token_validity": "not_checked", "network_probe_executed": False}
