"""Read-only Bash/Zsh completion, using the same argparse tree as the CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable


_FILES = "__VPS_ROUTE_FILES__"
_DIRECTORIES = "__VPS_ROUTE_DIRECTORIES__"


def _values(action: argparse.Action, prefix: str) -> list[str]:
    if action.choices is not None:
        return [str(value) for value in action.choices if str(value).startswith(prefix)]
    if action.type is Path:
        return [_DIRECTORIES if action.dest == "workspace" else _FILES]
    return []


def candidates(parser: argparse.ArgumentParser, words: list[str]) -> list[str]:
    """Walk already-completed arguments without parsing, exiting, or running actions."""
    words = words or [""]
    positional_index = 0
    expected: argparse.Action | None = None
    for word in words[:-1]:
        if expected is not None:
            expected = None
            continue
        if word == "--":
            return []  # Claude's pass-through arguments belong to the official CLI.
        flag, equals, _value = word.partition("=")
        option = parser._option_string_actions.get(flag)
        if option is not None:
            if option.nargs != 0 and not equals:
                expected = option
            continue
        if word.startswith("-"):
            return []
        positionals = [action for action in parser._actions if not action.option_strings]
        if positional_index >= len(positionals):
            return []
        action = positionals[positional_index]
        if isinstance(action, argparse._SubParsersAction):
            if word not in action.choices:
                return []
            parser = action.choices[word]
            positional_index = 0
        elif action.nargs == argparse.REMAINDER:
            return []
        else:
            positional_index += 1
    current = words[-1]
    if expected is not None:
        return _values(expected, current)
    if current.startswith("-"):
        return sorted(flag for flag in parser._option_string_actions
                      if flag.startswith(current))
    positionals = [action for action in parser._actions if not action.option_strings]
    result = []
    if positional_index < len(positionals):
        result = _values(positionals[positional_index], current)
    return result


def shell_script(shell: str) -> str:
    if shell == "bash":
        return r'''# VPS Route completion; compatible with Bash 3.2 and later.
_vps_route_complete() {
    local candidate cur
    cur="${COMP_WORDS[COMP_CWORD]}"
    COMPREPLY=()
    while IFS= read -r candidate; do
        case "$candidate" in
            __VPS_ROUTE_FILES__)
                while IFS= read -r candidate; do COMPREPLY+=("$candidate"); done \
                    < <(compgen -f -- "$cur") ;;
            __VPS_ROUTE_DIRECTORIES__)
                while IFS= read -r candidate; do COMPREPLY+=("$candidate"); done \
                    < <(compgen -d -- "$cur") ;;
            *) COMPREPLY+=("$candidate") ;;
        esac
    done < <(_VPS_ROUTE_COMPLETE=1 command vps-route "${COMP_WORDS[@]:1:COMP_CWORD}")
}
complete -o filenames -F _vps_route_complete vps-route
'''
    if shell == "zsh":
        return r'''# VPS Route completion for Zsh on Linux and macOS.
if (( ! $+functions[compdef] )); then
    autoload -Uz compinit
    compinit
fi
_vps_route_complete() {
    local -a matches
    matches=("${(@f)$(_VPS_ROUTE_COMPLETE=1 command vps-route \
        "${words[@]:1:$((CURRENT - 1))}")}")
    case "${matches[1]}" in
        __VPS_ROUTE_FILES__) _files ;;
        __VPS_ROUTE_DIRECTORIES__) _files -/ ;;
        *) (( ${#matches} )) && compadd -- "${matches[@]}" ;;
    esac
    return 0
}
compdef _vps_route_complete vps-route
'''
    raise ValueError("unsupported completion shell")


def dispatch(argv: list[str], parser_factory: Callable[[], argparse.ArgumentParser]) -> int | None:
    """Completion runs before platform runtime imports, config, credentials, or SSH."""
    if os.environ.get("_VPS_ROUTE_COMPLETE") == "1":
        for candidate in candidates(parser_factory(), argv):
            print(candidate)
        return 0
    # The personal wrapper may prepend --config; never load that file here.
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config")
    preliminary.add_argument("action", nargs="?")
    known, _unknown = preliminary.parse_known_args(argv)
    if known.action != "completion":
        return None
    args = parser_factory().parse_args(argv)
    print(shell_script(args.shell), end="")
    return 0
