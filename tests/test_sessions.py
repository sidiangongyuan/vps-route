"""Lease lifecycle and safe process stop using only test-owned subprocesses."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.sessions import (
    SessionLease, list_sessions, maintenance_barrier, maintenance_in_use,
    state_directory, stop_session,
)
from claude_launcher.wire import RouteError


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.patch = patch.object(Path, "home", return_value=self.home)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temporary.cleanup()

    def test_live_lease_is_discoverable_and_removed_on_exit(self):
        self.assertFalse(maintenance_in_use())
        self.assertFalse((self.home / ".local/state/claude-launcher").exists())
        with maintenance_barrier(), SessionLease("a" * 16, "fixture", "run"):
            self.assertTrue(maintenance_in_use())
            result = list_sessions()
            self.assertEqual(result[0]["state"], "running")
            self.assertEqual(result[0]["pid"], os.getpid())
            self.assertNotIn("arguments", result[0])
            with self.assertRaisesRegex(RouteError, "active_sessions_or_maintenance"):
                with maintenance_barrier(exclusive=True):
                    pass
        self.assertEqual(list_sessions(), [])
        self.assertFalse(maintenance_in_use())
        with maintenance_barrier(exclusive=True):
            self.assertTrue(maintenance_in_use())
            with self.assertRaises(RouteError):
                with maintenance_barrier():
                    pass

    def test_stale_record_and_reused_pid_never_receive_signal(self):
        with SessionLease("b" * 16, "fixture", "run") as lease:
            lease.path.write_text(json.dumps({**lease.payload, "start_ticks": "old"}))
            self.assertEqual(list_sessions()[0]["state"], "stale")
            with patch("signal.pidfd_send_signal", create=True) as send:
                with self.assertRaisesRegex(RouteError, "stale"):
                    stop_session("b" * 16)
                send.assert_not_called()
        with self.assertRaisesRegex(RouteError, "invalid_session_id"):
            stop_session("../../secret")

    def test_old_unlocked_record_is_stale_even_with_current_pid(self):
        lease = SessionLease("c" * 16, "fixture", "run")
        lease.path.write_text(json.dumps(lease.payload))
        lease.path.chmod(0o600)
        self.assertEqual(list_sessions()[0]["state"], "stale")
        with self.assertRaisesRegex(RouteError, "stale"):
            stop_session("c" * 16)

    @unittest.skipUnless(sys.platform == "linux", "requires Linux pidfd process signalling")
    def test_stop_signals_only_registered_test_launcher(self):
        script = (
            "from pathlib import Path; from unittest.mock import patch; import time,sys; "
            "from claude_launcher.sessions import SessionLease; "
            "p=patch.object(Path,'home',return_value=Path(sys.argv[1]));p.start(); "
            "lease=SessionLease('dddddddddddddddd','fixture','run');lease.__enter__(); "
            "print('ready',flush=True);time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(self.home)],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertTrue(stop_session("d" * 16)["stop_requested"])
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
            self.assertEqual(list_sessions()[0]["state"], "stale")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()

    def test_symlink_registry_is_refused(self):
        (state_directory() / "sessions").symlink_to(self.home)
        with self.assertRaisesRegex(RouteError, "unsafe_session_directory"):
            list_sessions()
