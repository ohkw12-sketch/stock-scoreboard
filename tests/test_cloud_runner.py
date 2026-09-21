import unittest
from datetime import datetime
from cloud_runner import slot_for
from growth_discovery import KST


class CloudScheduleTests(unittest.TestCase):
    def test_delayed_jobs_keep_original_korean_slot(self):
        delayed = datetime(2026, 9, 21, 16, 20, tzinfo=KST)
        self.assertEqual(slot_for('0 23 * * 0-4', 'auto', delayed), '08:00')
        self.assertEqual(slot_for('30 1 * * 1-5', 'auto', delayed), '10:30')
        self.assertEqual(slot_for('0 6 * * 1-5', 'auto', delayed), '15:00')

    def test_manual_selection_is_explicit_and_unknown_schedule_fails(self):
        now = datetime(2026, 9, 21, 8, 0, tzinfo=KST)
        self.assertEqual(slot_for('', '15:00', now), '15:00')
        self.assertEqual(slot_for('', 'auto', now), '08:00')
        with self.assertRaises(ValueError):
            slot_for('unexpected', 'auto', now)
