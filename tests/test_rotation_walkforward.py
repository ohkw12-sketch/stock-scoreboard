import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rotation_screener as engine
import rotation_rules as rules
from rotation_walkforward import published_before, forward_outcome, historical_financials, summarize


class WalkForwardTest(unittest.TestCase):
    def test_unknown_time_release_only_available_next_day(self):
        self.assertFalse(published_before('20260813000496', '2026-08-13'))
        self.assertTrue(published_before('20260813000496', '2026-08-14'))
        self.assertFalse(published_before('20260913000496', '2026-08-14'))
        self.assertFalse(published_before('20269999000496', '2026-08-14'))
        self.assertFalse(published_before(None, '2026-08-14'))

    def test_next_open_execution_pending_and_missing_market_sessions(self):
        dates = pd.bdate_range('2026-08-03', periods=7)
        frame = pd.DataFrame(dict(date=dates, ticker='a', open=100., close=110., volume=100))
        frame.loc[0, 'close'] = 50  # Signal close must not be the entry price.
        result = forward_outcome(frame, 'a', dates[0], 5)
        self.assertEqual(result['netPct'], 9.7)
        self.assertEqual(result['entryDate'], '2026-08-04')
        self.assertEqual(forward_outcome(frame, 'a', dates[0], 10)['status'], 'pending')
        another = frame.assign(ticker='b')
        missing = pd.concat([frame.drop(index=1), another])
        self.assertEqual(forward_outcome(missing, 'a', dates[0], 5)['status'], 'missing_session')

    def test_normalization_never_receives_future_corrected_version(self):
        frame = pd.DataFrame([
            dict(ticker='a',receipt='20260514000001',as_of='2026-03-31'),
            dict(ticker='b',receipt='20260814000001',as_of='2026-03-31')])
        universe = pd.DataFrame(dict(ticker=['a','b']))
        with patch('dart_fundamentals._attach_normalized_ttm', side_effect=lambda rows,*args: (rows,[])):
            fund, _ = historical_financials({(2026,'11013'):frame},universe,'2026-07-31')
        self.assertEqual(list(fund.ticker), ['a'])

    def test_cached_causal_features_equal_prefix_engine_output(self):
        prices = engine.generate_sample_market()
        config = engine.load_config(None, 'sample')
        date = sorted(prices.date.unique())[-8]
        prefix = prices[prices.date.le(date)].copy()
        fund = engine.generate_sample_fundamentals(prefix)
        regular = engine.run_engine(prefix.copy(), config, 'test', fund)
        overrides = engine.read_overrides(config['sector_overrides_file'])
        prices['sector'] = prices.ticker.map(overrides).fillna(prices.sector)
        stocks, sectors = engine.build_daily_features(prices)
        rotation = rules.features(prices)
        expected_stocks, expected_sectors = engine.build_daily_features(prices[prices.date.le(date)])
        pd.testing.assert_frame_equal(stocks[stocks.date.le(date)],expected_stocks)
        pd.testing.assert_frame_equal(sectors[sectors.date.le(date)],expected_sectors)
        pd.testing.assert_frame_equal(rotation[rotation.date.le(date)].reset_index(drop=True),
                                      rules.features(prices[prices.date.le(date)]).reset_index(drop=True))
        with patch.object(engine,'read_overrides',return_value={}), \
             patch.object(engine,'build_daily_features',return_value=(stocks[stocks.date.le(date)],sectors[sectors.date.le(date)])), \
             patch.object(rules,'features',return_value=rotation[rotation.date.eq(date)]):
            cached = engine.run_engine(prefix.copy(),config,'test',fund)
        self.assertEqual(cached['rows'],regular['rows'])
        self.assertEqual(cached['_allSectors'],regular['_allSectors'])
        self.assertEqual(cached['_eligibility'],regular['_eligibility'])
        for row in regular['_eligibility']:
            if row['financialExclusionReasons']:
                self.assertEqual(row['selectionStage'],'실적 선조건 탈락')
                self.assertFalse(row['displayed'])
                self.assertFalse(row['eligible'])

    def test_missing_returns_not_zero_and_cash_slots_explicit(self):
        measured = dict(forward5=dict(status='measured',netPct=10))
        pending = dict(forward5=dict(status='pending',netPct=None))
        summary = summarize([dict(rows=[measured]),dict(rows=[pending]),
            dict(rows=[],horizonMatured={'5':True})], 'rows', 5)
        self.assertEqual(summary['measuredSignals'],1)
        self.assertEqual(summary['completeSignalDays'],2)
        self.assertEqual(summary['meanFiveSlotCohortNetPct'],1)


if __name__ == '__main__':
    unittest.main()
