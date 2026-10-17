import unittest
from datetime import datetime, timedelta
from cloud_runner import slot_for, validate_review, retain_holding_inputs
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


class ManualCloudTests(unittest.TestCase):
    def test_full_request_always_includes_whole_board(self):
        for hour in (0, 8):
            self.assertEqual(slot_for('', 'full', datetime(2026, 10, 19, hour, 0, tzinfo=KST)), '08:00')
        for hour in (9, 10, 11, 14, 16, 23):
            self.assertEqual(slot_for('', 'full', datetime(2026, 10, 19, hour, 30, tzinfo=KST)), '15:00')

    def test_manual_workflows_have_no_server_schedule(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        cloud = (root/'.github/workflows/cloud-refresh.yml').read_text('utf-8')
        mobile = (root/'.github/workflows/manual-refresh.yml').read_text('utf-8')
        self.assertNotIn('  schedule:', cloud)
        self.assertNotIn('CLOUD_AUTOMATION_ENABLED', cloud)
        self.assertIn('workflow_dispatch:', mobile)
        self.assertIn('slot: full', mobile)
        self.assertNotIn('    inputs:', mobile)

    def test_review_requires_real_current_official_closure_check(self):
        now = datetime(2026, 10, 19, 11, 0, tzinfo=KST)
        review = {'slot': '15:00', 'checkedAt': now.isoformat(), 'krxNotice': {
            'status': 'open', 'url': 'https://global.krx.co.kr/contents/GLB/06/0601/0601010000/GLB0601010000.jsp',
            'checkedAt': now.isoformat()}}
        validate_review(review, '15:00', now)
        import copy
        for field, bad in [('url', 'https://krx.co.kr.example.com/'), ('status', 'failed'),
                           ('checkedAt', (now-timedelta(hours=2)).isoformat())]:
            broken = copy.deepcopy(review)
            broken['krxNotice'][field] = bad
            with self.assertRaises(RuntimeError):
                validate_review(broken, '15:00', now)
        for checked in ((now+timedelta(minutes=1)).isoformat(), '2026-10-19T11:00:00'):
            broken = copy.deepcopy(review)
            broken['checkedAt'] = checked
            with self.assertRaises(RuntimeError):
                validate_review(broken, '15:00', now)


class HoldingInputPreservationTests(unittest.TestCase):
    def test_current_price_updates_keep_user_inputs_and_dated_assessments(self):
        old = {'p3': {'rows': [{'ticker': '000001', 'qty': 7, 'avg': 100,
            'judgment': 'user decision', 'holdingInput': {'source': 'user'}}],
            'assessments': [{'date': '2026-10-15', 'summary': 'original assessment'}]}}
        new = {'p3': {'rows': [{'ticker': '000001', 'qty': 0, 'avg': 0,
            'close': 120, 'judgment': 'generated decision'}], 'assessments': []}}
        result = retain_holding_inputs(old, new)
        self.assertEqual(result['p3']['rows'][0]['qty'], 7)
        self.assertEqual(result['p3']['rows'][0]['avg'], 100)
        self.assertEqual(result['p3']['rows'][0]['judgment'], 'user decision')
        self.assertEqual(result['p3']['rows'][0]['close'], 120)
        self.assertEqual(result['p3']['assessments'], old['p3']['assessments'])
        new['p3']['rows'][0]['holdingInput']['source'] = 'changed'
        self.assertEqual(old['p3']['rows'][0]['holdingInput']['source'], 'user')

    def test_added_or_removed_holdings_block_publication(self):
        with self.assertRaises(RuntimeError):
            retain_holding_inputs({'p3': {'rows': [{'ticker': '000001'}]}}, {'p3': {'rows': []}})
