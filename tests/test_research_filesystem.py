"""Synthetic research file access without opening accounts or external networks."""

import json
import os
import socket
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from claude_launcher.runner import sandbox_command
from claude_launcher.filesystem import host_socket_mounts
from claude_launcher.wire import RouteError


class ResearchFilesystemTests(unittest.TestCase):
    def test_inaccessible_other_user_socket_does_not_abort_launch(self):
        entries = ('header\n'
                   '0: 0 0 0 0 0 0 /srv/private-user/daemon.sock\n'
                   '1: 0 0 0 0 0 0 /srv/accessible.sock\n')

        def socket_status(path):
            if path == Path('/srv/private-user/daemon.sock'):
                raise PermissionError(13, 'Permission denied')
            return True

        with patch('claude_launcher.filesystem.Path.read_text', return_value=entries), \
                patch('claude_launcher.filesystem.Path.is_socket', socket_status):
            mounts = host_socket_mounts(Path('/tmp/fixture'), Path('/tmp/fixture/profile'),
                                        Path('/tmp/upstream.sock'))
        self.assertEqual(mounts, ['--ro-bind', '/dev/null', '/srv/accessible.sock'])

    @unittest.skipUnless(sys.platform == "linux", "requires Linux network namespaces")
    def test_research_access_private_overlays_and_network_isolation(self):
        with tempfile.TemporaryDirectory(prefix='route-research-') as temporary:
            root = Path(temporary)
            home, data, repository = root / 'home', root / 'data', root / 'repository'
            home.mkdir()
            data.mkdir()
            repository.mkdir()
            profile = home / '.local/share/claude-launcher/profiles/fixture'
            profile.mkdir(parents=True)
            project = data / 'project'
            project.mkdir()
            (data / 'result.csv').write_text('fixture data')
            (home / 'notes.txt').write_text('ordinary home file')
            for directory in (home / '.ssh', home / '.claude', home / '.config',
                              home / '.local/share/claude-launcher/legacy-archives',
                              home / 'clash-for-linux', home / '.my-vpn-tool'):
                directory.mkdir(parents=True, exist_ok=True)
                (directory / 'secret').write_text('synthetic secret')
            (home / '.claude.json').write_text('synthetic account')
            cloud = data / 'cloud-private'
            cloud.mkdir()
            (cloud / 'secret').write_text('synthetic cloud secret')
            (home / '.aws').symlink_to(cloud, target_is_directory=True)
            (repository / 'config.vps.local.json').write_text('synthetic route config')
            for directory in (data / '.claude', project / '.claude'):
                (directory / 'rules').mkdir(parents=True)
                (directory / 'rules/test.md').write_text('instruction')
                (directory / 'settings.json').write_text('old settings')
            upstream = root / 'unused.sock'
            upstream.touch()
            host_socket = data / 'host-relay.sock'
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(host_socket))
                server.listen()
                code = f'''
import os, socket
from pathlib import Path
home, data, profile, repository = map(Path, {json.dumps([str(home), str(data), str(profile), str(repository)])})
assert (data/'result.csv').read_text() == 'fixture data'
assert (home/'notes.txt').read_text() == 'ordinary home file'
(data/'new-result').write_text('persisted')
(home/'new-note').write_text('persisted')
(profile/'new-state').write_text('isolated state')
assert not (home/'.ssh/secret').exists()
assert not (home/'.claude/secret').exists()
assert not (home/'.config/secret').exists()
assert not (home/'.local/share/claude-launcher/legacy-archives/secret').exists()
assert not (home/'clash-for-linux/secret').exists()
assert not (home/'.my-vpn-tool/secret').exists()
assert not (data/'cloud-private/secret').exists()
assert not (home/'.aws/secret').exists()
for secret in [home/'.claude.json', repository/'config.vps.local.json']:
    try: assert secret.read_text() == ''
    except PermissionError: pass
assert os.statvfs(home/'.ssh').f_flag & os.ST_RDONLY
for d in [data/'.claude', Path('.claude')]:
    assert (d/'rules/test.md').read_text() == 'instruction'
    assert not (d/'settings.json').exists()
assert all(r.split(':')[0].strip()=='lo' for r in Path('/proc/net/dev').read_text().splitlines()[2:])
assert len(Path('/proc/net/route').read_text().splitlines()) == 1
assert os.environ['HTTPS_PROXY'] == 'http://127.0.0.1:17878'
for family, address in [(socket.AF_UNIX, str(data/'host-relay.sock')),
                        (socket.AF_INET, ('203.0.113.99',443))]:
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(.2)
        try: client.connect(address)
        except OSError: pass
        else: raise AssertionError('unexpected external connection')
print('RESEARCH_FILES_PRIVATE_STATE_AND_NO_DIRECT_OK')
'''
                # Keep the real launcher code mounted; inject only the private-path inventory.
                from claude_launcher.filesystem import private_paths
                with patch('claude_launcher.runner.Path.home', return_value=home), \
                        patch('claude_launcher.filesystem.RESEARCH_ROOTS', (data, repository)), \
                        patch('claude_launcher.runner.private_paths',
                              side_effect=lambda h, r, extra: private_paths(h, repository, extra)):
                    args, env = sandbox_command(upstream, profile, project,
                                                ['/usr/bin/python3', '-c', code],
                                                research_filesystem=True,
                                                private_home_entries=('.my-vpn-*',),
                                                project_instructions=True)
                result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('RESEARCH_FILES_PRIVATE_STATE_AND_NO_DIRECT_OK', result.stdout)
            self.assertEqual((data / 'new-result').read_text(), 'persisted')
            self.assertEqual((home / 'new-note').read_text(), 'persisted')
            self.assertEqual((home / '.ssh/secret').read_text(), 'synthetic secret')
            self.assertEqual((profile / 'new-state').read_text(), 'isolated state')

    def test_cannot_select_a_masked_credential_directory_as_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            private = home / '.aws'
            private.mkdir()
            with patch('claude_launcher.runner.Path.home', return_value=home):
                with self.assertRaisesRegex(RouteError, 'account_or_key_directory'):
                    sandbox_command(home / 'socket', home / 'profile', private, ['/bin/true'],
                                    research_filesystem=True)


if __name__ == '__main__':
    unittest.main()
