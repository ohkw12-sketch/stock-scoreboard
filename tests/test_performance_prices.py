import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from performance_prices import collect_performance_prices
from rotation_screener import load_config


class PerformancePriceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = load_config(None, 'sample')
        self.config['cache_dir'] = Path(self.directory.name)
        self.base = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp('2026-09-07'),
            'close': 110., 'open': 100., 'high': 111., 'low': 99., 'volume': 1000,
            'price_date_verified': True, 'adjusted_basis': 'unknown'} for t in ('000001', '000002')])
        self.ledger = {'cohorts': [{'observedPublishedAt': '2026-09-04T18:00:00+09:00',
                                   'records': [{'ticker': '000001'}]}]}
        self.fresh = self.base[self.base.ticker.eq('000001')].copy()
        self.fresh['adjusted_basis'] = 'provider_adjusted_close'
        self.fresh['adjusted_close'] = 55.

    def test_no_published_records_calls_no_provider(self):
        with patch('performance_prices.MarketDataLoader') as loader:
            result, status = collect_performance_prices({}, self.base, self.config)
            loader.assert_not_called()
        self.assertEqual(status['networkRequests'], 0)
        pd.testing.assert_frame_equal(result, self.base)

    def test_reuse_is_offline_and_does_not_mutate_base(self):
        before = copy.deepcopy(self.base)
        with patch('performance_prices.MarketDataLoader') as loader:
            _, status = collect_performance_prices(self.ledger, self.base, self.config, reuse=True)
            loader.assert_not_called()
        self.assertEqual(status['networkRequests'], 0)
        pd.testing.assert_frame_equal(before, self.base)

    def test_only_published_tickers_are_requested_and_primary_prices_unchanged(self):
        selected = []
        fresh = self.fresh
        def provider(loader):
            selected.extend(loader._requested_tickers)
            return fresh
        before = copy.deepcopy(self.base)
        with patch('performance_prices.MarketDataLoader._yfinance', provider):
            result, status = collect_performance_prices(self.ledger, self.base, self.config)
        self.assertEqual(selected, ['000001'])
        self.assertEqual(status['status'], '정상')
        self.assertEqual(result.set_index('ticker').loc['000002', 'adjusted_basis'], 'unknown')
        self.assertEqual(result.set_index('ticker').loc['000001', 'adjusted_close'], 55.)
        pd.testing.assert_frame_equal(self.base, before)

    def test_repeat_same_session_reuses_confirmed_prices(self):
        with patch('performance_prices.MarketDataLoader._yfinance', return_value=self.fresh) as provider:
            collect_performance_prices(self.ledger, self.base, self.config)
            _, status = collect_performance_prices(self.ledger, self.base, self.config)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(status['networkRequests'], 0)

    def test_failure_retains_previous_prices_and_original_dates(self):
        self.fresh.to_pickle(self.config['cache_dir']/'performance_prices.pkl.gz')
        with patch('performance_prices.MarketDataLoader._yfinance', side_effect=RuntimeError('failure')):
            result, status = collect_performance_prices(self.ledger, self.base, self.config)
        self.assertIn('실패', status['status'])
        self.assertEqual(result.set_index('ticker').loc['000001', 'adjusted_close'], 55.)

    def test_unverified_adjustments_are_not_saved(self):
        invalid = self.fresh.copy()
        invalid['price_date_verified'] = False
        with patch('performance_prices.MarketDataLoader._yfinance', return_value=invalid):
            result, status = collect_performance_prices(self.ledger, self.base, self.config)
        self.assertIn('실패', status['status'])
        self.assertFalse((self.config['cache_dir']/'performance_prices.pkl.gz').exists())
        pd.testing.assert_frame_equal(result, self.base)


if __name__ == '__main__':
    unittest.main()
