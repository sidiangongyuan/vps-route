import getpass
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from claude_launcher import token_auth as auth
from claude_launcher.runner import clean_environment
from claude_launcher.wire import RouteError


class TokenAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name).resolve() / 'profile'
        self.profile.mkdir(mode=0o700)
        self.token = 'sk-ant-oat01-' + 'synthetic_' * 8

    def test_replace_permissions_remove(self):
        auth.save_token(self.profile, self.token)
        auth.save_token(self.profile, self.token + 'new')
        self.assertEqual(auth.load_token(self.profile), self.token + 'new')
        folder = self.profile / '.launcher-auth'
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual((folder / 'oauth-token').stat().st_mode & 0o777, 0o600)
        self.assertTrue(auth.remove_token(self.profile))
        self.assertIsNone(auth.load_token(self.profile))

    def test_invalid_input_preserves_existing(self):
        auth.save_token(self.profile, self.token)
        for value in ('bad', self.token + '\nsecond', self.token + 'x' * 4096):
            with self.assertRaises(RouteError):
                auth.save_token(self.profile, value)
        self.assertEqual(auth.load_token(self.profile), self.token)

    def test_reject_unsafe_files(self):
        auth.save_token(self.profile, self.token)
        file = self.profile / '.launcher-auth/oauth-token'
        file.chmod(0o644)
        with self.assertRaises(RouteError):
            auth.load_token(self.profile)
        file.unlink()
        outside = self.profile / 'outside'
        outside.write_text(self.token)
        outside.chmod(0o600)
        file.symlink_to(outside)
        with self.assertRaises(RouteError):
            auth.save_token(self.profile, self.token)
        with self.assertRaises(RouteError):
            auth.load_token(self.profile)
        file.unlink()
        os.link(outside, file)
        with self.assertRaises(RouteError):
            auth.load_token(self.profile)
        file.unlink()
        os.mkfifo(file, 0o600)
        with self.assertRaises(RouteError):
            auth.load_token(self.profile)

    def test_reject_parent_symlink(self):
        alias = self.profile.parent / 'alias'
        alias.symlink_to(self.profile, target_is_directory=True)
        with self.assertRaises(RouteError):
            auth.save_token(alias, self.token)

    def test_prompt_requires_hidden_terminal(self):
        with patch.object(auth.sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(RouteError):
                auth.import_token(self.profile)
        with patch.object(auth.sys.stdin, 'isatty', return_value=True), \
             patch.object(auth.sys.stderr, 'isatty', return_value=True), \
             patch.object(getpass, 'getpass', side_effect=getpass.GetPassWarning):
            with self.assertRaises(RouteError):
                auth.import_token(self.profile)
        self.assertIsNone(auth.load_token(self.profile))

    def test_injection_preserves_environment_and_login(self):
        auth.save_token(self.profile, self.token)
        with patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': 'host-secret'}):
            env = clean_environment()
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        env['HTTPS_PROXY'] = 'http://127.0.0.1:1234'
        auth.apply_token(env, self.profile, [])
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], self.token)
        self.assertEqual(env['HTTPS_PROXY'], 'http://127.0.0.1:1234')
        for arguments, flags in ((['auth','login'], {'login':True}),
                                  ([], {'install':True}), (['update'], {}),
                                  (['setup-token'], {}), (['--version'], {})):
            auth.apply_token(env, self.profile, arguments, **flags)
            self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        auth.apply_token(env, self.profile, ['auth','status'])
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], self.token)
