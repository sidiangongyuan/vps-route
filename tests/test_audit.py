"""Private usage-log behavior with temporary files and no real client."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.audit import AuditLog, log_directory, log_tail
from claude_launcher.wire import RouteError


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="audit-fixture-")
        self.home = Path(self.temporary.name)
        self.patch = patch.object(Path, "home", return_value=self.home)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temporary.cleanup()

    def test_start_preflight_exit_are_appended_with_private_permissions(self):
        audit = AuditLog("run", "Asia/Singapore")
        audit.write("start")
        audit.write("preflight_passed")
        audit.exit_code, audit.reason = 0, "child_exited"
        audit.write("exit")
        records = [json.loads(line) for line in log_tail().splitlines()]
        self.assertEqual([record["event"] for record in records],
                         ["start", "preflight_passed", "exit"])
        self.assertEqual(len({record["run_id"] for record in records}), 1)
        self.assertTrue(records[1]["connectivity_ok"])
        self.assertFalse(records[1]["exit_ip_pinning_enabled"])
        self.assertEqual(records[2]["reason"], "child_exited")
        self.assertTrue(records[0]["local_time"].endswith("+08:00"))
        self.assertNotIn("ip", records[1])
        self.assertEqual(log_directory().stat().st_mode & 0o777, 0o700)
        self.assertEqual((log_directory() / "usage.jsonl").stat().st_mode & 0o777, 0o600)

    def test_raw_error_text_is_never_logged(self):
        audit = AuditLog("auth_login", None)
        audit.reason = "private token and prompt must not be saved"
        audit.write("exit")
        self.assertNotIn("private token", log_tail())
        self.assertEqual(json.loads(log_tail())["reason"], "operation_failed")

    def test_probe_diagnostics_keep_timing_but_omit_raw_errors_and_urls(self):
        audit = AuditLog("run", None)
        audit.connection_check([{
            "target_index": 0, "elapsed_seconds": 12.1234,
            "reason": "private proxy password", "url": "https://private.example/token",
        }])
        record = json.loads(log_tail())
        self.assertEqual(record["targets"], [{"target_index": 0, "elapsed_seconds": 12.123,
                                              "reason": "connectivity_check_failed"}])
        self.assertNotIn("private", log_tail())

    def test_runtime_upstream_failure_reason_is_retained(self):
        audit = AuditLog("run", None)
        audit.exit_code, audit.reason = 69, "probe_transport_failure"
        audit.write("exit")
        self.assertEqual(json.loads(log_tail())["reason"], "probe_transport_failure")

    def test_connection_state_records_preservation_and_redacts_unknown_errors(self):
        audit = AuditLog("run", None)
        audit.connection_state("connection_lost", "private password in error")
        audit.connection_state("connection_restored", None)
        records = [json.loads(line) for line in log_tail().splitlines()]
        self.assertEqual(records[0]["reason"], "operation_failed")
        self.assertNotIn("private password", log_tail())
        self.assertTrue(all(record["session_preserved"] for record in records))
        self.assertTrue(all(not record["direct_fallback"] for record in records))
        self.assertNotIn("reason", records[1])

    def test_logs_rotate_without_dropping_current_record(self):
        with patch("claude_launcher.audit.LOG_LIMIT", 1):
            for _ in range(7):
                AuditLog("version", None).write("start")
        self.assertTrue((log_directory() / "usage.jsonl.4").is_file())
        self.assertFalse((log_directory() / "usage.jsonl.5").exists())
        self.assertEqual(json.loads(log_tail())["operation"], "version")

    def test_log_symlink_is_not_followed(self):
        root = log_directory()
        root.mkdir(parents=True)
        outside = self.home / "keep"
        outside.write_text("keep unchanged")
        (root / "usage.jsonl").symlink_to(outside)
        with self.assertRaisesRegex(RouteError, "unsafe_audit_file"):
            AuditLog("run", None).write("start")
        self.assertEqual(outside.read_text(), "keep unchanged")

    def test_tail_count_is_bounded(self):
        with self.assertRaisesRegex(RouteError, "log_tail_must"):
            log_tail(201)


if __name__ == "__main__":
    unittest.main()
