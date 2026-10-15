"""Local skills, instructions and memory without external integrations."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .wire import RouteError


SKILL_SETTINGS = {
    "disableAllHooks": True,
    "disableClaudeAiConnectors": True,
    "syncClaudeAiSkills": False,
    "autoMemoryEnabled": True,
    "disableRemoteControl": True,
}

# Do not let pass-through arguments silently undo the selected loading boundary.
LOADING_FLAGS = {"--settings", "--setting-sources", "--mcp-config", "--plugin-dir",
                 "--plugin-url", "--agents", "--remote-control", "--rc"}


def skills_arguments(arguments: list[str]) -> list[str]:
    if arguments[:1] in (["remote-control"], ["rc"]):
        raise RouteError("remote_control_disabled")
    for argument in arguments:
        if argument.split("=", 1)[0] in LOADING_FLAGS:
            raise RouteError("customization_override_requires_review")
    settings = dict(SKILL_SETTINGS)
    if sys.platform == "darwin":
        # The launcher's Seatbelt profile already applies; nesting it fails.
        settings["sandbox"] = {"enabled": False}
    return ["--setting-sources", "user,project,local", "--settings", json.dumps(settings),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', *arguments]


def shared_skill_mounts(profile_home: Path, live_home: Path) -> list[str]:
    """Merge installed user skills into the child only, without sharing account stores.

    Profile-local skills win name collisions. Global skills are read-only and
    rediscovered on every launch, so installer additions need no copying/sync job.
    Only links into the declared skill roots are accepted.
    """
    destination = profile_home / ".claude/skills"
    roots = [destination, live_home / ".agents/skills", live_home / ".claude/skills"]
    allowed = [root for root in roots if root.is_dir() and not root.is_symlink()]
    selected: dict[str, tuple[Path, bool]] = {}
    for root in allowed:
        for entry in sorted(root.iterdir()):
            name = entry.name
            if (name in selected or name.startswith(".")
                    or name.lower() == "synced"
                    or name.lower().startswith("anthropic-skills")):
                continue
            source = entry.resolve()
            if not any(source.is_relative_to(base) for base in allowed):
                continue
            if (not source.is_dir() or not (source / "SKILL.md").is_file()
                    or (source / ".claude-plugin").exists()):
                continue
            selected[name] = (source, root == destination)
    if not selected:
        return []
    mounts = ["--tmpfs", str(destination)]
    for name, (source, private) in selected.items():
        mounts.extend(["--bind" if private else "--ro-bind", str(source),
                       str(destination / name)])
    return mounts


def instruction_directory_mounts(directory: Path) -> list[str]:
    if directory.is_symlink():
        raise RouteError("project_claude_directory_must_not_be_symlinked")
    if not directory.is_dir():
        return []
    mounts = ["--tmpfs", str(directory)]
    for name in ("CLAUDE.md", "CLAUDE.local.md", "rules"):
        source = directory / name
        if source.is_symlink():
            raise RouteError("project_instructions_must_not_be_symlinked")
        if source.exists():
            mounts.extend(["--bind", str(source), str(source)])
    return mounts


def project_instruction_mounts(workspace: Path, *, include_ancestors: bool = False) -> list[str]:
    """Expose project instructions, not old settings or project customizations.

    Each existing .claude gets an ephemeral view; the host files are untouched.
    Instruction directories are rebound so edits to existing rules persist.
    """
    mounts: list[str] = []
    if include_ancestors:
        for parent in reversed(workspace.parents):
            if parent != Path.home() and (parent / '.claude').exists():
                mounts.extend(instruction_directory_mounts(parent / '.claude'))
    skipped = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    for base, directories, _files in os.walk(workspace, followlinks=False):
        if ".claude" in directories:
            directory = Path(base) / ".claude"
            mounts.extend(instruction_directory_mounts(directory))
        directories[:] = [name for name in directories
                          if name not in skipped and name != ".claude"]
    return mounts
