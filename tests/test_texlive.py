"""Opt-in read-only TeX tools; no account or external network is used."""

import json
import os
import platform
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path

from claude_launcher.config import Config
from claude_launcher.runner import sandbox_command
from claude_launcher.wire import RouteError


class TexLiveTests(unittest.TestCase):
    def test_config_parses_optional_installation_path(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory).resolve() / "config.json"
            values = dict(ssh_host="203.0.113.1", ssh_user="fixture", ssh_port=22,
                          identity_file="/unused", known_hosts="/unused",
                          remote_socket="/unused", expected_ip="8.8.8.8",
                          expected_country="US", texlive_root="~/texlive/2025")
            config.write_text(json.dumps(values))
            self.assertEqual(Config.load(config).texlive_root, Path.home() / "texlive/2025")

    def test_mount_is_opt_in_and_does_not_expose_whole_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile, workspace = root / "profile", root / "project"
            profile.mkdir()
            workspace.mkdir()
            args, env = sandbox_command(root / "socket", profile, workspace, ["/bin/true"])
            self.assertNotIn("TEXMFVAR", env)
            with self.assertRaisesRegex(RouteError, "invalid_texlive"):
                sandbox_command(root / "socket", profile, workspace, ["/bin/true"],
                                texlive_root=Path.home())
            tex = root / "texlive"
            binary = tex / "bin" / f"{platform.machine()}-linux"
            binary.mkdir(parents=True)
            for name in ("latexmk", "pdflatex"):
                (binary / name).touch()
            cnf = tex / "texmf-dist/web2c/texmf.cnf"
            cnf.parent.mkdir(parents=True)
            cnf.touch()
            args, env = sandbox_command(root / "socket", profile, workspace, ["/bin/true"],
                                        texlive_root=tex)
            i = args.index(str(tex))
            self.assertEqual(args[i - 1:i + 2], ["--ro-bind", str(tex), str(tex)])
            self.assertEqual(env["PATH"].split(":")[:2],
                             [str(profile / ".local/bin"), str(binary)])
            for name in ("TEXMFVAR", "TEXMFCONFIG", "TEXMFHOME", "TEXMFCACHE"):
                self.assertTrue(Path(env[name]).is_relative_to(profile))
            self.assertIn("--unshare-net", args)

    @unittest.skipUnless(os.environ.get("VPS_ROUTE_TEST_TEXLIVE_ROOT"),
                         "set VPS_ROUTE_TEST_TEXLIVE_ROOT for a local compilation smoke")
    @unittest.skipUnless(sys.platform == "linux", "requires Linux network namespaces")
    def test_real_tex_compiles_in_isolation_and_project_outputs_persist(self):
        tex = Path(os.environ["VPS_ROUTE_TEST_TEXLIVE_ROOT"]).resolve()
        with tempfile.TemporaryDirectory(prefix="vps-route-tex-") as directory:
            root = Path(directory).resolve()
            profile, project = root / "profile", root / "project"
            profile.mkdir()
            project.mkdir()
            socket = root / "unused-socket"
            socket.touch()  # No network requests; the shim never opens this fixture.
            (project / "smoke.tex").write_text(
                "\\documentclass{article}\n\\usepackage{booktabs,tikz}\n"
                "\\begin{document}Sandbox TeX smoke.\n"
                "\\begin{tabular}{lr}\\toprule Item & Value\\\\\\midrule A & 1\\\\"
                "\\bottomrule\\end{tabular}\n"
                "\\begin{tikzpicture}\\draw (0,0) -- (1,1);\\end{tikzpicture}\n"
                "\\end{document}\n"
            )
            script = (
                "import os,pathlib,subprocess; "
                f"assert os.statvfs({str(tex)!r}).f_flag & os.ST_RDONLY; "
                "assert all(x.split(':')[0].strip()=='lo' for x in "
                "pathlib.Path('/proc/net/dev').read_text().splitlines()[2:]); "
                f"assert not pathlib.Path({str(Path.home() / '.ssh')!r}).exists(); "
                "pathlib.Path('.work/pdf').mkdir(parents=True,exist_ok=True); "
                "pathlib.Path('.work/xe').mkdir(parents=True,exist_ok=True); "
                "subprocess.run(['latexmk','-pdf','-interaction=nonstopmode',"
                "'-halt-on-error','-outdir=.work/pdf','smoke.tex'],check=True); "
                "subprocess.run(['xelatex','-interaction=nonstopmode','-halt-on-error',"
                "'-output-directory=.work/xe','smoke.tex'],check=True); "
                "p=pathlib.Path(os.environ['TEXMFVAR']);p.mkdir(parents=True,exist_ok=True); "
                "(p/'persistent-fixture').touch()"
            )
            args, env = sandbox_command(socket, profile, project,
                                        ["/usr/bin/python3", "-c", script], texlive_root=tex)
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout[-2500:] + result.stderr[-2500:])
            # A fresh namespace sees project outputs and private cache, not old /tmp.
            verify = (
                "import os,pathlib; "
                "assert pathlib.Path('.work/pdf/smoke.pdf').stat().st_size>0; "
                "assert pathlib.Path('.work/xe/smoke.pdf').stat().st_size>0; "
                "assert (pathlib.Path(os.environ['TEXMFVAR'])/'persistent-fixture').exists()"
            )
            args, env = sandbox_command(socket, profile, project,
                                        ["/usr/bin/python3", "-c", verify], texlive_root=tex)
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
