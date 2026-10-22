"""Install tested development entries into one private profile, without policy changes."""

import argparse
import os
from pathlib import Path
import secrets
import stat


def open_directory(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeError('destination must be owned by the current user and not group/world writable')
        return fd
    except BaseException:
        os.close(fd)
        raise


def check_target(fd, name, replace_existing):
    try:
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not replace_existing:
        raise RuntimeError(f'{name} already exists; preserve or review it before replacing')
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_nlink != 1 or before.st_mode & 0o077):
        raise RuntimeError(f'{name} is not a private regular file owned by the current user')
    return before


def install_file(directory, name, contents, *, replace_existing=False, mode=0o700):
    fd = open_directory(directory)
    temporary = '.dev-tool-' + secrets.token_hex(12)
    try:
        before = check_target(fd, name, replace_existing)
        item = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                       mode, dir_fd=fd)
        with os.fdopen(item, 'wb') as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        if before is None:
            os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=fd)
        else:
            now = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (now.st_dev,now.st_ino,now.st_mtime_ns,now.st_size) != (before.st_dev,before.st_ino,before.st_mtime_ns,before.st_size):
                raise RuntimeError(f'{name} changed during installation')
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, type=Path)
    parser.add_argument('--replace-existing', action='store_true')
    args = parser.parse_args()
    if not args.profile.is_absolute() or '..' in args.profile.parts:
        parser.error('profile must be an absolute path without parent traversal')
    if not (args.profile/'.local/libexec/vps-route-go/go1.27.3/bin/go').is_file():
        parser.error('install the verified Go 1.27.3 archive in this profile first; see README.md')
    source = Path(__file__).resolve().parent
    plan = [(args.profile/'.local/bin', name, (source/name).read_bytes(), 0o700)
            for name in ('go', 'python3', 'git', 'clang', 'clang++')]
    plan.append((args.profile/'.config/pip', 'pip.conf', (source/'pip.conf').read_bytes(), 0o600))
    # Reject known conflicts across the whole plan before changing any entry.
    # Each write checks again; this is not a transaction against OS failures.
    for directory, name, _, _ in plan:
        fd = open_directory(directory)
        try:
            check_target(fd, name, args.replace_existing)
        finally:
            os.close(fd)
    for directory, name, contents, mode in plan:
        install_file(directory, name, contents, replace_existing=args.replace_existing, mode=mode)
        print(f'Installed {name}')


if __name__ == '__main__':
    main()
