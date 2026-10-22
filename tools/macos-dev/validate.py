"""Exercise installed entries through an owned VPS tunnel and the current sandbox.

Run with PYTHONPATH=. from the repository. No Claude requests or token export.
Only test-owned temporary files and the test-owned SSH process are removed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

from claude_launcher import macos
from claude_launcher.config import Config
from claude_launcher.runner import SSHTunnel


class Validation:
    def __init__(self, config, root, socket):
        self.config, self.root, self.socket = config, root, socket
        self.results = []

    def run(self, name, command, *, failure=False, evidence=None, direct=False):
        cfg = self.config
        with macos.seatbelt_session(
            self.socket, cfg.profile_home, self.root, command,
            private_home_entries=cfg.private_home_entries,
            protected_files=(cfg.identity_file, cfg.known_hosts),
            timezone=cfg.timezone, allowed_roots=cfg.macos_allowed_roots,
        ) as (args, env):
            env.update(GOCACHE=str(self.root / 'go-cache'),
                       GOMODCACHE=str(self.root / 'go-modules'),
                       GOPATH=str(self.root / 'gopath'), GOENV='off',
                       PIP_CACHE_DIR=str(self.root / 'pip-cache'),
                       GIT_TERMINAL_PROMPT='0', GH_PROMPT_DISABLED='1',
                       GH_NO_UPDATE_NOTIFIER='1', GH_NO_EXTENSION_UPDATE_NOTIFIER='1')
            if direct:
                env['GIT_CURL_VERBOSE'] = '1'  # Only fixed-IP, credential-free test URLs.
                for key in list(env):
                    if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
                        del env[key]
            try:
                process = subprocess.run(args, env=env, capture_output=True, text=True,
                                         timeout=180, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                self.results.append(dict(name=name, command=command, passed=False, timeout=True))
                raise RuntimeError(f'{name}: timed out') from None
        output = process.stdout + process.stderr
        passed = (process.returncode != 0 if failure else process.returncode == 0)
        matched = evidence is None or evidence.lower() in output.lower()
        passed = passed and matched
        # Commands use public URLs or a credential-free private repository URL.
        # Do not retain process output: an installed credential helper may write secrets.
        self.results.append(dict(name=name, command=command, returncode=process.returncode,
                                 expected_failure=failure, evidence=evidence,
                                 evidence_matched=matched, passed=passed))
        print(f'{name}: {"PASS" if passed else "FAIL"}', flush=True)
        if not passed:
            raise RuntimeError(f'{name}: exit {process.returncode}; check command in report')

    def functional(self, private_remote):
        root = self.root
        (root / 'go.mod').write_text('module validation.example/tools\n\ngo 1.23\n')
        (root / 'hello.go').write_text('package main\nimport "fmt"\nfunc main(){fmt.Println("go-ok")}\n')
        self.run('go_compile', ['go', 'run', 'hello.go'], evidence='go-ok')
        self.run('go_download', ['go', 'get', 'github.com/google/uuid@v1.6.0'])
        (root / 'module.go').write_text('package main\nimport ("fmt"; "github.com/google/uuid")\n'
                                      'func main(){fmt.Println(uuid.Nil.String())}\n')
        self.run('go_dependency_compile', ['go', 'run', 'module.go'],
                 evidence='00000000-0000-0000-0000-000000000000')
        (root / 'cgo.go').write_text('package main\n/*\nint answer(){return 42;}\n*/\n'
                                   'import "C"\nimport "fmt"\nfunc main(){fmt.Println(C.answer())}\n')
        self.run('cgo_compile', ['go', 'run', 'cgo.go'], evidence='42')
        self.run('python_venv', ['python3', '-m', 'venv', str(root / 'venv')])
        python = str(root / 'venv/bin/python')
        self.run('pip_download', [python, '-m', 'pip', '--disable-pip-version-check', 'install',
                                  '--no-cache-dir', '--retries', '0', '--timeout', '20', 'idna==3.10'])
        self.run('python_import', [python, '-c', 'import idna; assert idna.__version__ == "3.10"'])
        (root / 'native.c').write_text('#include <Python.h>\n'
            'static PyObject *answer(PyObject *s, PyObject *a){return PyLong_FromLong(42);}\n'
            'static PyMethodDef methods[]={{"answer",answer,METH_NOARGS,""},{NULL,NULL,0,NULL}};\n'
            'static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"native",NULL,-1,methods};\n'
            'PyMODINIT_FUNC PyInit_native(void){return PyModule_Create(&module);}\n')
        self.run('python_native_extension', [python, '-c',
            'import subprocess,sysconfig; '
            'subprocess.run(["clang","-shared","-undefined","dynamic_lookup",'
            '"-I"+sysconfig.get_path("include"),"native.c","-o",'
            '"native"+sysconfig.get_config_var("EXT_SUFFIX")],check=True); '
            'import native; assert native.answer()==42'])
        (root / 'hello.cpp').write_text('#include <iostream>\nint main(){std::cout<<"cpp-ok";}\n')
        self.run('cpp_compile', ['clang++', 'hello.cpp', '-o', 'hello-cpp'])
        self.run('cpp_execute', [str(root / 'hello-cpp')], evidence='cpp-ok')
        self.run('git_clone', ['git', 'clone', '--depth=1', 'https://github.com/octocat/Hello-World.git', 'checkout'])
        self.run('git_fetch', ['git', '-C', 'checkout', 'fetch', '--depth=1', 'origin'])
        self.run('git_content', ['git', '-C', 'checkout', 'show', 'HEAD:README'], evidence='Hello World')
        if private_remote:
            self.run('git_private_remote', ['git', 'ls-remote', '--exit-code', private_remote, 'HEAD'])

    def closed_route(self):
        # Previously unrequested module: cannot pass from the successful download cache.
        self.run('go_route_down', ['go', 'mod', 'download', 'github.com/google/uuid@v1.5.0'], failure=True)
        self.run('pip_route_down', ['python3', '-m', 'pip', '--disable-pip-version-check', 'download',
                                  '--no-cache-dir', '--no-deps', '--retries', '0', '--timeout', '5',
                                  '-d', 'unavailable', 'idna==3.9'], failure=True)
        self.run('git_route_down', ['git', 'ls-remote', 'https://github.com/octocat/Hello-World.git'], failure=True)
        # Fixed IP bypasses name resolution; errno proves kernel refusal.
        (self.root / 'direct.go').write_text('package main\nimport ("net/http"; "os"; "fmt")\n'
            'func main(){_,e:=http.Get("https://1.1.1.1/"); if e!=nil {fmt.Println(e);os.Exit(1)}}\n')
        self.run('go_direct_denied', ['go', 'run', 'direct.go'], direct=True, failure=True,
                 evidence='operation not permitted')
        self.run('pip_direct_denied', ['python3', '-m', 'pip', '--disable-pip-version-check', '-vv',
            'download', '--no-cache-dir', '--no-deps', '--retries', '0', '--timeout', '5',
            '-d', 'unavailable', 'https://1.1.1.1/probe-1.0-py3-none-any.whl'], direct=True, failure=True,
            evidence='operation not permitted')
        self.run('git_direct_denied', ['git', '-c', 'http.proxy=', 'ls-remote', 'https://1.1.1.1/probe.git'],
                 direct=True, failure=True, evidence='operation not permitted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/vps-route/config.json')
    parser.add_argument('--report', type=Path, required=True, help='new JSON output path')
    parser.add_argument('--private-remote', help='optional GitHub HTTPS repository URL; no embedded credentials')
    args = parser.parse_args()
    if args.report.exists() or args.report.is_symlink():
        parser.error('report must be a new file')
    if args.private_remote:
        url = urlsplit(args.private_remote)
        if (url.scheme != 'https' or url.hostname != 'github.com' or url.username or url.password
                or url.query or url.fragment or url.port not in (None, 443)):
            parser.error('private remote must be a GitHub HTTPS URL without credentials or query')
    cfg = Config.load(args.config)
    with tempfile.TemporaryDirectory(prefix='vps-route-dev-validation-', dir='/private/tmp') as directory:
        root = Path(directory).resolve()
        route = root / 'route'
        route.mkdir(mode=0o700)
        workspace = root / 'workspace'
        workspace.mkdir(mode=0o700)
        tunnel = SSHTunnel(cfg, route / 'proxy.sock')
        validation = Validation(cfg, workspace, tunnel.socket_path)
        completed = False
        try:
            tunnel.start()
            validation.functional(args.private_remote)
            tunnel.close()
            validation.closed_route()
            completed = True
        finally:
            tunnel.close()
            report = dict(checks=validation.results,
                          source_sha256=hashlib.sha256(Path(macos.__file__).read_bytes()).hexdigest(),
                          passed=completed and all(r['passed'] for r in validation.results))
            with args.report.open('x') as stream:
                json.dump(report, stream, indent=2)
                stream.write('\n')


if __name__ == '__main__':
    main()
