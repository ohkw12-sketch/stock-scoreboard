import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from issue_spread import KST, build, refresh, trading_day

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=KST)


def fixture():
    facts, rows = [], []
    q = dict(status='verified', source='KRX', url='https://example.com/quote',
             adjustmentChecked=True, asOf=NOW.isoformat(), changePct=3, return5dPct=5,
             turnoverRatio=2, distance20Pct=5)
    for n in range(4):
        ticker = f'{n+1:06d}'
        ids = []
        for kind in ('contract','customer','guidance','policy','earnings','consensus'):
            ident = ticker + kind
            ids.append(ident)
            facts.append(dict(id=ident, ticker=ticker, kind=kind, period='2026Q3',
                source='DART', url='https://dart.fss.or.kr/test', status='verified', reviewer='test',
                summary='테스트 전용 검증 사실', polarity='positive',
                publishedAt=(NOW-timedelta(days=1)).isoformat(), verifiedAt=NOW.isoformat()))
        rows.append(dict(ticker=ticker, name='검증 예시 '+ticker, sector='전력기기', evidenceIds=ids, quote=copy.deepcopy(q)))
    return dict(asOf=NOW.isoformat(), issues=[dict(id='fixture', name='테스트 전용 이슈', evidence=facts,
            candidates=rows, leaders=[dict(name='예시 주도주', market='KR', quote=q)])])


class IssueTests(unittest.TestCase):
    def test_final_and_core_do_not_change_source_board(self):
        board = {'p1': {'rows':[dict(ticker='000001',priceDate='2026-09-10',entryState='진입가능')]},
                 'p11': {'rows':[dict(ticker='000001',priceDate='2026-09-10',entryFit='진입적합')]}}
        before = copy.deepcopy(board)
        row = build(fixture(), board, NOW, '15:00')['issues'][0]['candidates'][0]
        self.assertEqual((row['score'],row['stage'],row['tags']), (100,'최종선정',['CORE']))
        self.assertEqual(board, before)

    def test_morning_cannot_promote(self):
        rows = build(fixture(), {}, NOW, '08:00')['issues'][0]['candidates']
        self.assertTrue(all(r['stage']=='탐색' for r in rows))

    def test_midday_stops_at_spread(self):
        self.assertEqual(build(fixture(), {}, NOW, '10:30')['issues'][0]['candidates'][0]['stage'],'확산')

    def test_hot_is_watch_even_with_full_fundamentals(self):
        p = fixture(); p['issues'][0]['candidates'][0]['quote']['changePct']=29
        row = next(r for r in build(p, {}, NOW, '15:00')['issues'][0]['candidates'] if r['ticker']=='000001')
        self.assertEqual(row['scores']['entry'],0)
        self.assertIn('추격주의',row['tags']); self.assertNotEqual(row['stage'],'최종선정')

    def test_youtube_never_scores(self):
        p=fixture()
        for f in p['issues'][0]['evidence']: f['source']='YouTube'
        r=build(p, {}, NOW, '15:00')['issues'][0]
        self.assertEqual(r['strength'],0)
        self.assertTrue(all(x['fundamental']==0 and x['stage']!='최종선정' for x in r['candidates']))

    def test_stale_quotes_cannot_confirm(self):
        p=fixture()
        for r in p['issues'][0]['candidates']: r['quote']['asOf']=(NOW-timedelta(days=1)).isoformat()
        self.assertTrue(all(r['stage']=='탐색' for r in build(p, {}, NOW,'15:00')['issues'][0]['candidates']))

    def test_sector_exclusions(self):
        p=fixture()
        for r in p['issues'][0]['candidates']: r['sector']='바이오'
        self.assertEqual(build(p, {}, NOW,'15:00')['issues'][0]['candidates'],[])

    def test_duplicate_tickers_not_breadth(self):
        p=fixture(); p['issues'][0]['candidates']=[p['issues'][0]['candidates'][0]]*4
        self.assertEqual(build(p, {}, NOW,'15:00')['issues'][0]['spreadType'],'개별반응·미확인')

    def test_future_period_outside_window_not_scored(self):
        p=fixture()
        for f in p['issues'][0]['evidence']: f['period']='2028Q1'
        self.assertTrue(all(r['fundamental']==0 for r in build(p, {}, NOW,'15:00')['issues'][0]['candidates']))

    def test_holiday_no_io(self):
        with tempfile.TemporaryDirectory() as folder, patch('issue_spread.trading_day',return_value=False), patch('issue_spread.read_json') as read:
            self.assertIsNone(refresh(now=NOW,root=Path(folder)))
            read.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()),[])

    def test_calendar_weekend_and_chuseok(self):
        self.assertFalse(trading_day(datetime(2026,9,26,tzinfo=KST)))
        self.assertFalse(trading_day(datetime(2026,9,25,tzinfo=KST)))

    def test_missing_input_is_failure(self):
        self.assertEqual(build({}, {}, NOW,'15:00')['status'],'수집실패')

    def test_max_three_issues(self):
        p=fixture(); p['issues']=[dict(p['issues'][0],id=str(i)) for i in range(5)]
        self.assertEqual(len(build(p, {}, NOW,'15:00')['issues']),3)
