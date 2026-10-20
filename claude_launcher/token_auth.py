"""Explicit profile-local OAuth credentials; never inherit host tokens."""
from __future__ import annotations

import contextlib
import getpass
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import warnings

from .wire import RouteError

FILENAME = 'oauth-token'


def validate_token(value: str) -> str:
    value = value.strip()
    if len(value) > 4096 or not re.fullmatch(r'sk-ant-oat01-[A-Za-z0-9_-]{20,}', value):
        raise RouteError('invalid_setup_token; paste_only_the_token')
    return value


def _private(info, directory=False):
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not correct_type or info.st_uid != os.getuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise RouteError('unsafe_token_storage')


@contextlib.contextmanager
def _directory(profile: Path, create=False):
    if not profile.is_absolute() or '..' in profile.parts or profile == Path('/'):
        raise RouteError('unsafe_token_storage')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in profile.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        _private(os.fstat(fd), directory=True)
        if create:
            try:
                os.mkdir('.launcher-auth', 0o700, dir_fd=fd)
            except FileExistsError:
                pass
        child = os.open('.launcher-auth', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = child
        _private(os.fstat(fd), directory=True)
        yield fd
    finally:
        os.close(fd)


def load_token(profile: Path) -> str | None:
    try:
        with _directory(profile) as directory:
            fd = os.open(FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                _private(os.fstat(stream.fileno()))
                return validate_token(stream.read(4097).decode('ascii'))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise RouteError('unsafe_token_storage') from None


def save_token(profile: Path, value: str):
    value = validate_token(value)
    try:
        with _directory(profile, create=True) as directory:
            try:
                _private(os.stat(FILENAME, dir_fd=directory, follow_symlinks=False))
            except FileNotFoundError:
                pass
            temporary = '.token-' + secrets.token_hex(16)
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(value.encode('ascii'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, FILENAME, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except OSError:
        raise RouteError('unsafe_token_storage') from None


def import_token(profile: Path):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise RouteError('token_import_requires_interactive_terminal')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            value = getpass.getpass('Paste the OAuth token (hidden): ')
        except (getpass.GetPassWarning, EOFError):
            raise RouteError('token_input_unavailable') from None
    save_token(profile, value)


def remove_token(profile: Path) -> bool:
    try:
        with _directory(profile) as directory:
            _private(os.stat(FILENAME, dir_fd=directory, follow_symlinks=False))
            os.unlink(FILENAME, dir_fd=directory)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise RouteError('unsafe_token_storage') from None


def apply_token(environment, profile, arguments, *, install=False, login=False):
    environment.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
    if install or login or arguments[:1] in (['setup-token'], ['update'], ['--version']):
        return
    token = load_token(profile)
    if token is not None:
        environment['CLAUDE_CODE_OAUTH_TOKEN'] = token
