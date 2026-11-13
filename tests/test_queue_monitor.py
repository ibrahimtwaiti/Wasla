import unittest
from datetime import datetime, timezone
from scripts.check_queue import evaluate


class QueueMonitorTests(unittest.TestCase):
    def test_empty(self):
        self.assertTrue(evaluate(0, 0, None)['healthy'])

    def test_dead_event_fails_without_pending(self):
        self.assertFalse(evaluate(0, 1, None)['healthy'])

    def test_stale_and_recent(self):
        now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(evaluate(1, 0, '2026-11-12T11:54:59+00:00', now)['healthy'])
        self.assertTrue(evaluate(1, 0, '2026-11-12T11:59:00+00:00', now)['healthy'])

    def test_missing_and_invalid_timestamp_fail_closed(self):
        for stamp in (None, 'bad', '2026-11-12T11:59:00'):
            with self.assertRaises(ValueError):
                evaluate(1, 0, stamp)
