"""Parser-driven completion and real Bash/Zsh functions, with no account reads."""

import builtins
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.arguments import build_parser
from claude_launcher.completions import candidates, dispatch, shell_script
from claude_launcher.entrypoint import main


ROOT = Path(__file__).resolve().parent.parent


class CompletionTests(unittest.TestCase):
    def test_commands_subcommands_options_and_prefixes(self):
        parser = build_parser()
        self.assertIn("auth", candidates(parser, [""]))
        self.assertEqual(candidates(parser, ["r"]), ["reset", "run"])
        self.assertEqual(candidates(parser, ["auth", "imp"]), ["import-token"])
        self.assertIn("remove-token", candidates(parser, ["auth", ""]))
        self.assertEqual(candidates(parser, ["config", "sh"]), ["show"])
        self.assertEqual(candidates(parser, ["doctor", "--con"]), ["--connect"])
        self.assertEqual(candidates(parser, ["auth", "import-token", "--rep"]), ["--replace"])
        self.assertEqual(candidates(parser, ["completion", "z"]), ["zsh"])
        self.assertEqual(candidates(parser, ["run", "--", "auth", ""]), [])
        self.assertEqual(candidates(parser, ["unknown", ""]), [])

    def test_global_config_values_and_path_options_are_not_commands(self):
        parser = build_parser()
        self.assertEqual(candidates(parser, ["--config", "auth", "auth", "imp"]), ["import-token"])
        self.assertEqual(candidates(parser, ["--config=/missing", "auth", "imp"]), ["import-token"])
        self.assertEqual(candidates(parser, ["--config", ""]), ["__VPS_ROUTE_FILES__"])
        self.assertEqual(candidates(parser, ["run", "--workspace", ""]),
                         ["__VPS_ROUTE_DIRECTORIES__"])

    def test_completion_does_not_load_config_credentials_or_platform_runtime(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.rsplit(".", 1)[-1] in {"cli", "runner", "config", "credentials"}:
                raise AssertionError("completion imported runtime: " + name)
            return original_import(name, *args, **kwargs)

        for shell in ("bash", "zsh"):
            with patch("sys.argv", ["vps-route", "--config", "/missing", "completion", shell]), \
                    patch("claude_launcher.entrypoint.sys.platform", "darwin"), \
                    patch("builtins.__import__", side_effect=guarded_import), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 0)
                self.assertEqual(output.getvalue(), shell_script(shell))
        with patch.dict(os.environ, {"_VPS_ROUTE_COMPLETE": "1"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(dispatch(["--config", "/missing", "auth", "imp"], build_parser), 0)
        self.assertEqual(output.getvalue().strip(), "import-token")

    def run_shell(self, shell, words, *, expected):
        executable = shutil.which(shell)
        if executable is None:
            self.skipTest(shell + " unavailable")
        with tempfile.TemporaryDirectory(prefix="completion-fixture-") as temporary:
            home = Path(temporary)
            binary = home / "bin/vps-route"
            binary.parent.mkdir()
            binary.write_text("#!/usr/bin/env python3\n"
                              "from claude_launcher.entrypoint import main\nraise SystemExit(main())\n")
            binary.chmod(0o700)
            environment = {"PATH": str(binary.parent) + ":/usr/bin:/bin",
                           "PYTHONPATH": str(ROOT), "HOME": str(home), "LANG": "C.UTF-8"}
            if shell == "bash":
                call = (f"COMP_WORDS=({words}); COMP_CWORD=$((${{#COMP_WORDS[@]}}-1)); "
                        '_vps_route_complete; printf "%s\\n" "${COMPREPLY[@]}"')
                script = shell_script(shell) + call
            else:
                script = ('compdef() { :; }; compadd() { shift; printf "%s\\n" "$@"; };\n'
                          + shell_script(shell)
                          + f"words=({words}); CURRENT=${{#words[@]}}; _vps_route_complete")
            result = subprocess.run([executable, "-f", "-c", script] if shell == "zsh"
                                    else [executable, "--noprofile", "--norc", "-c", script],
                                    env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip().splitlines(), expected)
            self.assertEqual(sorted(item.name for item in home.iterdir()), ["bin"])

    def test_actual_bash_function(self):
        self.run_shell("bash", "vps-route auth imp", expected=["import-token"])

    def test_actual_zsh_function_used_on_macos_and_linux(self):
        self.run_shell("zsh", "vps-route auth imp", expected=["import-token"])


if __name__ == "__main__":
    unittest.main()
