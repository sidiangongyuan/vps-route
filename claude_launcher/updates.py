"""Manual official-release updates. Never update Claude data or the VPS here."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .management import private_json
from .sessions import maintenance_barrier, state_directory
from .wire import RouteError


REPOSITORY = "sidiangongyuan/vps-route"
PACKAGE = Path(__file__).resolve().parent
VERSION = re.compile(r"\d+\.\d+\.\d+")


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 60) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RouteError("self_update_command_failed; check_git_gh_or_package_manager") from None
    if result.returncode:
        # Package/SSH/git output can contain credentials or private paths.
        raise RouteError("self_update_command_failed; check_access_and_installation")
    return result.stdout.strip()


def official_release(version: str | None = None) -> dict:
    if version is not None and not VERSION.fullmatch(version):
        raise RouteError("self_update_requires_a_formal_X_Y_Z_release")
    if not shutil.which("gh"):
        raise RouteError("github_cli_required; authenticate_gh_for_the_private_repository")
    endpoint = f"repos/{REPOSITORY}/releases/" + (f"tags/v{version}" if version else "latest")
    try:
        try:
            response = _run(["gh", "api", endpoint])
        except RouteError:
            raise RouteError(
                "formal_release_unavailable; check_published_releases_and_gh_access"
            ) from None
        value = json.loads(response)
        tag = value["tag_name"]
        if (value.get("draft") is not False or value.get("prerelease") is not False
                or not isinstance(tag, str) or not tag.startswith("v")
                or not VERSION.fullmatch(tag[1:]) or (version and tag != f"v{version}")):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise RouteError("self_update_no_valid_formal_release; no_code_changed") from None
    return {"version": tag[1:], "tag": tag, "repository": REPOSITORY}


def installation() -> dict:
    root = PACKAGE.parent
    if (root / ".git").is_dir():
        origin = _run(["git", "remote", "get-url", "origin"], cwd=root)
        if origin not in {f"https://github.com/{REPOSITORY}",
                          f"https://github.com/{REPOSITORY}.git",
                          f"git@github.com:{REPOSITORY}.git"}:
            raise RouteError("self_update_refuses_unrecognized_repository")
        return {"kind": "checkout", "root": str(root)}
    if sys.prefix == sys.base_prefix:
        raise RouteError("self_update_refuses_system_python; use_pipx_or_a_private_venv")
    try:
        distribution = importlib.metadata.distribution("vps-route")
        metadata_paths = [Path(distribution.locate_file(item)) for item in distribution.files or []
                          if str(item).endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise ValueError
        metadata = metadata_paths[0].parent
        prefix = Path(sys.prefix).resolve()
        if not PACKAGE.is_relative_to(prefix) or not metadata.is_relative_to(prefix):
            raise ValueError
    except (importlib.metadata.PackageNotFoundError, ValueError):
        raise RouteError("cannot_identify_owned_package_installation") from None
    kind = "venv"
    marker = Path(sys.prefix) / "pipx_metadata.json"
    if marker.is_file():
        try:
            if json.loads(marker.read_text())["main_package"]["package"] != "vps-route":
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise RouteError("cannot_identify_package_manager") from None
        if not shutil.which("pipx"):
            raise RouteError("pipx_required_for_self_update")
        kind = "pipx"
    return {"kind": kind, "root": str(PACKAGE.parent), "metadata": str(metadata),
            "prefix": sys.prefix, "python": sys.executable}


def _idle() -> None:
    from .profile_reset import _profile_is_running

    # Include sessions launched by older clients without process leases.
    profiles = Path.home() / ".local/share/claude-launcher/profiles"
    if _profile_is_running(profiles):
        raise RouteError("claude_sessions_running; close_them_before_self_update")


def _clean(root: Path) -> None:
    if _run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root):
        raise RouteError("self_update_refuses_dirty_checkout; preserve_your_changes_first")


def _validate_code(source: Path, version: str) -> None:
    # Do not import downloaded code merely to read its version.
    text = (source / "claude_launcher/__init__.py").read_text()
    if not re.search(rf'^__version__ = "{re.escape(version)}"$', text, re.MULTILINE):
        raise RouteError("release_package_version_mismatch")


def _package_snapshot(plan: dict, directory: Path) -> None:
    for name, path in (("package", PACKAGE), ("metadata", Path(plan["metadata"]))):
        if path.is_symlink() or any(item.is_symlink() for item in path.rglob("*")):
            raise RouteError("self_update_refuses_linked_package")
        shutil.copytree(path, directory / name, ignore=shutil.ignore_patterns("__pycache__"))


def _restore_package(plan: dict, directory: Path) -> None:
    from .lifecycle import _validate_tree

    root = Path(plan["root"])
    for base in (directory, directory / "package", directory / "metadata"):
        if base.is_symlink() or not base.is_dir() or base.stat().st_uid != os.getuid():
            raise RouteError("unsafe_update_backup")
    for item in directory.rglob("*"):
        if item.is_symlink() or item.stat().st_uid != os.getuid():
            raise RouteError("unsafe_update_backup")
    # Validate every target before touching one, and stage all copies first.
    targets = [root / "claude_launcher", *root.glob("vps_route-*.dist-info")]
    for path in targets:
        if (path.is_symlink() or path.parent != root
                or (path.exists() and path.stat().st_uid != os.getuid())):
            raise RouteError("unsafe_update_rollback_path")
        _validate_tree(path)
    with tempfile.TemporaryDirectory(prefix=".route-rollback-", dir=root) as temporary:
        stage = Path(temporary)
        shutil.copytree(directory / "package", stage / "package")
        shutil.copytree(directory / "metadata", stage / "metadata")
        moved = []
        published = []
        try:
            for index, path in enumerate(targets):
                if path.exists():
                    old = stage / f"old-{index}"
                    os.replace(path, old)
                    moved.append((path, old))
            for staged, path in ((stage / "package", root / "claude_launcher"),
                                 (stage / "metadata", Path(plan["metadata"]))):
                os.replace(staged, path)
                published.append(path)
        except BaseException:
            for path in published:
                shutil.rmtree(path)
            for path, old in moved:
                os.replace(old, path)
            raise


def _verify(plan: dict, version: str) -> None:
    python = plan.get("python", sys.executable)
    command = [python, "-I", "-c",
               "import sys;sys.path.insert(0,sys.argv[1]);"
               "import claude_launcher.cli;"
               "from claude_launcher import __version__;print(__version__)", plan["root"]]
    if _run(command) != version:
        raise RouteError("updated_launcher_version_check_failed")


def self_update(*, version: str | None = None, apply: bool = False,
                rollback: bool = False) -> dict:
    plan = installation()
    if rollback and version:
        raise RouteError("rollback_and_version_are_mutually_exclusive")
    if rollback:
        record = state_directory(create=False) / "updates/last.json"
        try:
            for parent in (record.parent, *record.parent.parents):
                if parent.is_symlink():
                    raise ValueError
            fd = os.open(record, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or info.st_mode & 0o077):
                    raise ValueError
                value = json.loads(stream.read(16385))
            if (not isinstance(value, dict) or not isinstance(value["previous_version"], str)
                    or not re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", value["previous_version"])):
                raise ValueError
            if plan["kind"] == "checkout":
                if not all(re.fullmatch(r"[a-f0-9]{40,64}", value[key])
                           for key in ("previous_commit", "installed_commit")):
                    raise ValueError
            elif not re.fullmatch(r"backup-[A-Za-z0-9_-]+", value["backup"]):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError):
            raise RouteError("no_self_update_backup") from None
        original = value.get("installation")
        if not isinstance(original, dict):
            raise RouteError("no_self_update_backup")
        # A wheel upgrade necessarily changes the dist-info directory name.
        # Compare the immutable installation identity, not that versioned path.
        if ({key: item for key, item in original.items() if key != "metadata"}
                != {key: item for key, item in plan.items() if key != "metadata"}):
            raise RouteError("rollback_installation_changed; no_code_changed")
        if plan["kind"] != "checkout":
            old_metadata = Path(original.get("metadata", ""))
            if (old_metadata.parent != Path(plan["root"])
                    or old_metadata.name != f"vps_route-{value['previous_version']}.dist-info"):
                raise RouteError("unsafe_update_rollback_path")
        preview = {"current": __version__, "target": value["previous_version"],
                   "action": "rollback", "applied": apply, "accounts_and_vps_unchanged": True}
    else:
        release = official_release(version)
        current = re.match(r"(\d+)\.(\d+)\.(\d+)", __version__)
        target_numbers = tuple(map(int, release["version"].split(".")))
        current_numbers = tuple(map(int, current.groups())) if current else None
        newer = (current_numbers is None or target_numbers > current_numbers
                 or (target_numbers == current_numbers and not VERSION.fullmatch(__version__)))
        preview = {"current": __version__, "target": release["version"],
                   "action": "update", "applied": apply, "installation": plan["kind"],
                   "newer_release_available": newer,
                   "accounts_and_vps_unchanged": True}
    if not apply or (not rollback and (release["version"] == __version__
                                      or (version is None and not newer))):
        preview["applied"] = False
        return preview
    with maintenance_barrier(exclusive=True):
        _idle()
        root = Path(plan["root"])
        if plan["kind"] == "checkout":
            _clean(root)
        if rollback:
            if plan["kind"] == "checkout":
                if _run(["git", "rev-parse", "HEAD"], cwd=root) != value["installed_commit"]:
                    raise RouteError("rollback_checkout_changed; no_code_changed")
                _run(["git", "switch", "--detach", value["previous_commit"]], cwd=root)
            else:
                if __version__ != value["installed_version"]:
                    raise RouteError("rollback_package_changed; no_code_changed")
                backup = state_directory() / "updates" / value["backup"]
                if backup.parent != state_directory() / "updates" or backup.is_symlink():
                    raise RouteError("unsafe_update_backup")
                _restore_package(original, backup)
            _verify(plan, value["previous_version"])
            record.unlink()
            return preview
        update_root = state_directory() / "updates"
        if update_root.is_symlink():
            raise RouteError("unsafe_update_backup")
        update_root.mkdir(mode=0o700, exist_ok=True)
        if update_root.stat().st_uid != os.getuid() or update_root.stat().st_mode & 0o077:
            raise RouteError("unsafe_update_backup")
        with tempfile.TemporaryDirectory(prefix="release-", dir=update_root) as temporary:
            source = Path(temporary) / "source"
            _run(["gh", "repo", "clone", REPOSITORY, str(source), "--", "--depth", "1",
                  "--branch", release["tag"]], timeout=120)
            _validate_code(source, release["version"])
            record_value = {"installation": plan, "previous_version": __version__,
                            "installed_version": release["version"]}
            if plan["kind"] == "checkout":
                previous = _run(["git", "rev-parse", "HEAD"], cwd=root)
                target = _run(["git", "rev-parse", "HEAD"], cwd=source)
                _run(["git", "fetch", "--no-tags", str(source), target], cwd=root)
                _clean(root)
                record_value.update(previous_commit=previous, installed_commit=target)
                private_json(update_root / "last.json", record_value)
                try:
                    _run(["git", "switch", "--detach", target], cwd=root)
                    _verify(plan, release["version"])
                except (RouteError, OSError, KeyboardInterrupt):
                    _run(["git", "switch", "--detach", previous], cwd=root)
                    raise
            else:
                wheels = Path(temporary) / "wheels"
                # Build isolation may download build dependencies through host networking.
                _run([sys.executable, "-m", "pip", "wheel", "--no-deps", str(source),
                      "--wheel-dir", str(wheels)], timeout=180)
                matches = list(wheels.glob(f"vps_route-{release['version']}-*.whl"))
                if len(matches) != 1:
                    raise RouteError("release_wheel_missing")
                backup = Path(tempfile.mkdtemp(prefix="backup-", dir=update_root))
                _package_snapshot(plan, backup)
                record_value["backup"] = backup.name
                private_json(update_root / "last.json", record_value)
                prefix = (["pipx", "runpip", "vps-route"] if plan["kind"] == "pipx"
                          else [sys.executable, "-m", "pip"])
                try:
                    _run([*prefix, "install", "--no-deps", "--no-index", "--force-reinstall",
                          str(matches[0])], timeout=120)
                    _verify(plan, release["version"])
                except (RouteError, OSError, KeyboardInterrupt):
                    _restore_package(plan, backup)
                    raise
    return preview
