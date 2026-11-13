import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import ops_alert


class OpsAlertTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)

    def test_only_main_failed_monitors_alert(self):
        event = {
            "workflow_run": {
                "name": "PostgreSQL database backup to R2",
                "conclusion": "failure",
                "head_branch": "main",
                "id": 123,
                "html_url": "https://evil.example/private",
            }
        }
        alert = ops_alert.alert_from_event(event, "workflow_run")
        self.assertEqual(alert[0], "workflow-postgresql-database-backup-to-r2")
        self.assertIn("/actions/runs/123", alert[2])
        self.assertNotIn("evil.example", alert[2])
        self.assertIn("@ibrahimtwaiti", alert[2])

    def test_success_preview_or_unwatched_workflow_does_not_alert(self):
        base = {
            "name": "Production health",
            "conclusion": "failure",
            "head_branch": "main",
            "id": 345,
        }
        for changes in (
            {"conclusion": "success"},
            {"head_branch": "feature"},
            {"name": "Untrusted PR workflow"},
            {"id": "not-an-id"},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(ops_alert.alert_from_event(
                    {"workflow_run": {**base, **changes}}, "workflow_run"))

    def test_manual_test_issue_is_distinct(self):
        alert = ops_alert.alert_from_event({}, "workflow_dispatch")
        self.assertEqual(alert[0], "test")
        self.assertIn("تجربة", alert[1])

    def test_backup_age_fresh(self):
        started = self.now - timedelta(hours=25)
        run = {"created_at": started.isoformat(), "id": 111}
        self.assertIsNone(ops_alert.backup_age_alert(run, now=self.now))

    def test_backup_age_old_or_missing(self):
        started = self.now - timedelta(hours=27)
        run = {"created_at": started.isoformat(), "id": 111}
        overdue = ops_alert.backup_age_alert(run, now=self.now)
        self.assertEqual(overdue[0], "backup-overdue")
        self.assertIn("/actions/runs/111", overdue[2])
        self.assertEqual(ops_alert.backup_age_alert(None, now=self.now)[0], "backup-overdue")

    def test_deduplicate_open_alert(self):
        alert = ("workflow-production-health", "Alert", "Content")
        existing = {"body": "<!-- wasla-ops-alert:workflow-production-health -->"}
        with patch.object(ops_alert, "open_issues", return_value=[existing]), \
             patch.object(ops_alert, "api") as api:
            self.assertFalse(ops_alert.create_once(alert))
            api.assert_not_called()

    def test_new_alert_assigned_to_sole_owner_without_email_in_public_issue(self):
        alert = ("backup-overdue", "Missed backup", "Details")
        with patch.object(ops_alert, "open_issues", return_value=[]), \
             patch.object(ops_alert, "api") as api:
            self.assertTrue(ops_alert.create_once(alert))
            method, path, payload = api.call_args.args
            self.assertEqual((method, path), ("POST", "issues"))
            self.assertEqual(payload["assignees"], ["ibrahimtwaiti"])
            self.assertIn("<!-- wasla-ops-alert:backup-overdue -->", payload["body"])
            self.assertNotIn("@gmail.com", payload["body"])

    def test_initial_deployment_push_creates_test_alert(self):
        alert = ops_alert.alert_from_event({}, "push")
        self.assertEqual(alert[0], "test")
        self.assertIn("[Wasla TEST]", alert[1])


if __name__ == "__main__":
    unittest.main()
