"""Forward-only recommendation evaluation fixtures; never download market data."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from combined_recommendations import build_combined
from recommendation_performance import PriceBook, empty_ledger, evaluate_legacy as evaluate, record_publication
from refresh_store import (json_write, read_json, run_lock, snapshot_files,
                           load_verified_frames, store_verified_frames)


class RecommendationPerformanceTest(unittest.TestCase):
    def board(self, generated='2026-09-04T18:00:00+09:00', tickers=('000001',), version='entry-v1'):
        return {'p1': {'generatedAt': generated, 'sourceDate': generated[:10], 'methodVersion': version,
                       'snapshotId': 'fixed-input',
                       'rows': [{'ticker': t, 'name': '종목'+t, 'sector': '산업', 'rank': i+1,
                                 'entryState': '진입가능'} for i, t in enumerate(tickers)]}}

    def ledger(self, observed='2026-09-04T19:00:00+09:00', **kwargs):
        return record_publication(empty_ledger(), self.board(**kwargs), {}, ['000001', '000002'],
                                  observed_at=observed, engine_version='engine-v1')

    def prices(self, end='2026-09-11', tickers=('000001', '000002')):
        rows = []
        for day in pd.bdate_range('2026-09-04', end):
            for ticker in tickers:
                rows.append({'date': day, 'ticker': ticker, 'open': 100., 'close': 110.,
                             'high': 112., 'low': 99., 'volume': 1000,
                             'adjusted': True, 'price_date_verified': True, 'Adj Close': 110.})
        return pd.DataFrame(rows)

    def result(self, ledger=None, prices=None, **kwargs):
        return evaluate(ledger if ledger is not None else self.ledger(),
                        prices if prices is not None else self.prices(),
                        generated_at=kwargs.pop('generated_at', '2026-09-11T19:00:00+09:00'),
                        trading_sessions=kwargs.pop('trading_sessions', pd.bdate_range('2026-09-04', '2026-09-11')),
                        **kwargs)

    def test_friday_publication_enters_monday_open(self):
        row = self.result()['rows'][0]
        self.assertEqual(row['entryDate'], '2026-09-07')
        self.assertEqual(row['entryPrice'], 100.)

    def test_monday_intraday_publication_never_uses_same_day_open(self):
        ledger = self.ledger(observed='2026-09-07T10:00:00+09:00', generated='2026-09-07T09:45:00+09:00')
        self.assertEqual(self.result(ledger)['rows'][0]['entryDate'], '2026-09-08')

    def test_horizon_five_counts_entry_day_as_day_one(self):
        result = self.result()['rows'][0]['horizons']['5']
        self.assertEqual(result['exitDate'], '2026-09-11')
        self.assertEqual(result['returnPct'], 10.)

    def test_unmatured_horizon_is_not_zero_return(self):
        row = self.result(prices=self.prices('2026-09-10'), generated_at='2026-09-10T19:00:00+09:00')['rows'][0]
        self.assertEqual(row['horizons']['5']['status'], '기간 미도래')
        self.assertIsNone(row['horizons']['5']['returnPct'])

    def test_ticker_missing_entry_price_does_not_shift_entry_date(self):
        prices = self.prices()
        prices = prices[~(prices.ticker.eq('000001') & prices.date.eq(pd.Timestamp('2026-09-07')))]
        row = self.result(prices=prices)['rows'][0]
        self.assertEqual(row['entryDate'], '2026-09-07')
        self.assertIsNone(row['entryPrice'])
        self.assertIsNone(row['currentReturnPct'])

    def test_entire_market_missing_entry_session_does_not_shift_date(self):
        prices = self.prices()
        prices = prices[~prices.date.eq(pd.Timestamp('2026-09-07'))]
        row = self.result(prices=prices)['rows'][0]
        self.assertEqual(row['entryDate'], '2026-09-07')
        self.assertIsNone(row['entryPrice'])

    def test_missing_exit_price_stays_missing(self):
        prices = self.prices()
        prices = prices[~(prices.ticker.eq('000001') & prices.date.eq(pd.Timestamp('2026-09-11')))]
        row = self.result(prices=prices)['rows'][0]
        self.assertIsNone(row['horizons']['5']['returnPct'])
        self.assertIsNone(row['currentReturnPct'])

    def test_split_uses_adjustment_factor_for_entry_open(self):
        prices = self.prices()
        entry = prices.date.eq(pd.Timestamp('2026-09-07')) & prices.ticker.eq('000001')
        prices.loc[entry, ['open', 'close', 'Adj Close']] = [200., 220., 110.]
        row = self.result(prices=prices)['rows'][0]
        self.assertEqual(row['entryPrice'], 100.)
        self.assertEqual(row['horizons']['5']['returnPct'], 10.)

    def test_new_provider_adjustment_metadata_is_supported(self):
        prices = self.prices().drop(columns=['adjusted', 'Adj Close'])
        prices['adjusted_basis'] = 'provider_adjusted_close'
        prices['adjusted_close'] = prices['close']
        self.assertEqual(self.result(prices=prices)['rows'][0]['entryPrice'], 100.)

    def test_unknown_adjustment_does_not_become_approved_from_adj_close_alone(self):
        prices = self.prices().drop(columns=['adjusted'])
        prices['adjusted_basis'] = 'unknown'
        row = self.result(prices=prices)['rows'][0]
        self.assertIsNone(row['entryPrice'])
        self.assertIn('수정주가', row['status'])

    def test_unverified_price_date_is_not_accepted(self):
        prices = self.prices()
        prices['price_date_verified'] = False
        self.assertIsNone(self.result(prices=prices)['rows'][0]['entryPrice'])

    def test_no_volume_is_not_a_tradable_entry(self):
        prices = self.prices()
        prices.loc[prices.date.eq(pd.Timestamp('2026-09-07')), 'volume'] = 0
        row = self.result(prices=prices)['rows'][0]
        self.assertIsNone(row['entryPrice'])
        self.assertIn('거래', row['status'])

    def test_missing_observations_excluded_from_winrate_denominator(self):
        ledger = self.ledger(tickers=('000001', '000002'))
        prices = self.prices()
        prices = prices[~(prices.ticker.eq('000002') & prices.date.eq(pd.Timestamp('2026-09-07')))]
        summary = next(s for s in self.result(ledger, prices)['summaries'] if s['horizon'] == 5)
        self.assertEqual(summary['total'], 2)
        self.assertEqual(summary['evaluated'], 1)
        self.assertEqual(summary['waitingOrMissing'], 1)
        self.assertEqual(summary['winRatePct'], 100.)

    def test_unmatured_observations_excluded_from_winrate_denominator(self):
        summary = next(s for s in self.result(prices=self.prices('2026-09-10'),
                                             generated_at='2026-09-10T19:00:00+09:00')['summaries'] if s['horizon'] == 5)
        self.assertEqual(summary['evaluated'], 0)
        self.assertIsNone(summary['winRatePct'])

    def test_incomplete_benchmark_never_becomes_survivor_only_average(self):
        result = self.result(prices=self.prices(tickers=('000001',)))['rows'][0]['horizons']['5']
        self.assertEqual(result['returnPct'], 10.)
        self.assertIsNone(result['excessPct'])
        self.assertEqual(result['benchmark']['coverage'], 1)
        self.assertEqual(result['benchmark']['requested'], 2)

    def test_repeat_publication_keeps_first_observed_time(self):
        ledger = self.ledger()
        again = record_publication(ledger, self.board(), {}, ['000001', '000002'],
                                   observed_at='2026-09-05T19:00:00+09:00', engine_version='engine-v1')
        self.assertEqual(ledger, again)
        self.assertEqual(len(again['cohorts']), 1)

    def test_publication_does_not_mutate_input_ledger(self):
        ledger = empty_ledger()
        original = copy.deepcopy(ledger)
        record_publication(ledger, self.board(), {}, [], observed_at='2026-09-04T19:00:00+09:00', engine_version='v1')
        self.assertEqual(ledger, original)

    def test_engine_version_changes_are_separate_cohorts(self):
        ledger = self.ledger()
        ledger = record_publication(ledger, self.board(), {}, ['000001', '000002'],
                                    observed_at='2026-09-04T19:01:00+09:00', engine_version='engine-v2')
        self.assertEqual(len(ledger['cohorts']), 2)
        summaries = [s for s in self.result(ledger)['summaries'] if s['horizon'] == 5]
        self.assertEqual(len(summaries), 2)

    def test_rule_version_changes_are_separate_cohorts(self):
        ledger = self.ledger()
        ledger = record_publication(ledger, self.board(version='entry-v2'), {}, ['000001', '000002'],
                                    observed_at='2026-09-04T19:01:00+09:00', engine_version='engine-v1')
        self.assertEqual(len(ledger['cohorts']), 2)

    def test_daily_final_recommendation_only_is_evaluated_but_ledger_retains_both(self):
        ledger = self.ledger()
        ledger = record_publication(ledger, self.board(generated='2026-09-04T19:15:00+09:00', tickers=('000002',)),
                                    {}, ['000001', '000002'], observed_at='2026-09-04T19:30:00+09:00',
                                    engine_version='engine-v1')
        original = copy.deepcopy(ledger)
        result = self.result(ledger)
        self.assertEqual(len(ledger['cohorts']), 2)
        self.assertEqual(ledger, original)
        self.assertEqual(result['recordCount'], 1)
        self.assertEqual(result['rows'][0]['ticker'], '000002')

    def test_future_publication_is_excluded_until_observed_time(self):
        ledger = record_publication(self.ledger(), self.board(generated='2026-09-08T18:00:00+09:00', tickers=('000002',)),
                                    {}, ['000001', '000002'], observed_at='2026-09-08T19:00:00+09:00',
                                    engine_version='engine-v1')
        before = self.result(ledger, generated_at='2026-09-08T18:59:00+09:00')
        after = self.result(ledger, generated_at='2026-09-08T19:01:00+09:00')
        self.assertEqual(before['recordCount'], 1)
        self.assertEqual(after['recordCount'], 2)

    def test_section_engine_version_overrides_global_engine_version(self):
        board = self.board()
        board['p1']['engineVersion'] = 'section-fallback-version'
        board['p1']['refreshState'] = {'engineVersion': 'actual-entry-engine-version'}
        ledger = record_publication(empty_ledger(), board, {}, ['000001'],
                                    observed_at='2026-09-04T19:00:00+09:00', engine_version='global-version')
        self.assertEqual(ledger['cohorts'][0]['engineVersion'], 'actual-entry-engine-version')

    def test_only_successful_sections_of_current_run_are_recorded(self):
        board = self.board()
        board['meta'] = {'runId': 'new-run'}
        board['p1']['refreshState'] = {'runId': 'new-run', 'status': '계산완료'}
        board['p11'] = copy.deepcopy(board['p1'])
        board['p11']['refreshState'] = {'runId': 'old-run', 'status': '계산완료'}
        board['p2'] = copy.deepcopy(board['p1'])
        board['p2']['refreshState'] = {'runId': 'new-run', 'status': '실패·이전유지'}
        ledger = record_publication(empty_ledger(), board, {}, ['000001'],
                                    observed_at='2026-09-04T19:00:00+09:00', engine_version='global')
        self.assertEqual([c['section'] for c in ledger['cohorts']], ['p1'])

    def test_no_records_has_no_fabricated_return(self):
        result = self.result(empty_ledger())
        self.assertEqual(result['recordCount'], 0)
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['summaries'], [])

    def test_empty_prices_and_empty_records_remain_valid(self):
        result = self.result(empty_ledger(), pd.DataFrame())
        self.assertEqual(result['recordCount'], 0)
        self.assertIsNone(result['priceDate'])

    def test_future_prices_after_evaluation_time_are_not_used(self):
        result = self.result(generated_at='2026-09-08T19:00:00+09:00')
        self.assertLessEqual(result['priceDate'], '2026-09-08')
        self.assertIsNone(result['rows'][0]['horizons']['5']['returnPct'])

    def test_missing_whole_market_exit_date_is_not_unmatured(self):
        row = self.result(prices=self.prices('2026-09-10'))['rows'][0]
        result = row['horizons']['5']
        self.assertEqual(result['exitDate'], '2026-09-11')
        self.assertNotEqual(result['status'], '기간 미도래')
        self.assertIsNone(result['returnPct'])

    def test_same_day_close_is_unavailable_before_market_close(self):
        row = self.result(generated_at='2026-09-11T13:00:00+09:00')['rows'][0]
        self.assertEqual(row['horizons']['5']['status'], '기간 미도래')

    def test_cost_is_applied_once_in_percentage_points(self):
        row = self.result(cost_bps=25)['rows'][0]
        self.assertEqual(row['horizons']['5']['returnPct'], 9.75)
        self.assertEqual(row['currentReturnPct'], 9.75)

    def test_negative_cost_is_rejected(self):
        with self.assertRaises(ValueError):
            self.result(cost_bps=-1)

    def test_publication_before_generation_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger(observed='2026-09-04T17:59:00+09:00')


class CombinedCandidateTest(unittest.TestCase):
    def inputs(self, n=25):
        entries, values = [], []
        for index in range(n):
            ticker = str(index+1).zfill(6)
            entries.append({'ticker': ticker, 'name': '후보'+ticker, 'entryState': '진입가능',
                            'entryScore': 70, 'priceDate': '2026-09-04', 'currentPrice': 100})
            values.append({'ticker': ticker, 'normalizedPOP': 5, 'normalizedPremiumPct': -20,
                           'confidence': 'A', 'normalizedQuarterCount': 4, 'priceDate': '2026-09-04',
                           'valueScore': 70+index/10})
        return entries, values

    def combine(self, entries, values):
        return build_combined(entries, values, [], [], source_date='2026-09-04',
                              snapshot_id='snap', generated_at='2026-09-04T18:00:00+09:00')

    def test_all_eligible_candidates_participate_before_top_ten(self):
        entries, values = self.inputs()
        result = self.combine(entries, values)
        self.assertEqual(result['candidateCount'], 25)
        self.assertEqual(len(result['rows']), 10)
        self.assertEqual(result['rows'][0]['ticker'], '000025')

    def test_stale_entry_date_cannot_join_current_values(self):
        entries, values = self.inputs(1)
        entries[0]['priceDate'] = '2026-09-03'
        self.assertEqual(self.combine(entries, values)['candidateCount'], 0)

    def test_stale_value_date_cannot_join_current_entry(self):
        entries, values = self.inputs(1)
        values[0]['priceDate'] = '2026-09-03'
        self.assertEqual(self.combine(entries, values)['candidateCount'], 0)

    def test_combination_does_not_mutate_original_scores(self):
        entries, values = self.inputs(1)
        original = copy.deepcopy((entries, values))
        self.combine(entries, values)
        self.assertEqual((entries, values), original)


class RefreshStoreTest(unittest.TestCase):
    def test_failed_generation_pointer_write_keeps_previous_complete_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, cache = root/'out', root/'cache'
            prices, fundamentals = pd.DataFrame({'close': [100.]}), pd.DataFrame({'op': [5.]})
            old_report = {'latestPriceDate': '2026-09-04', 'generation': 'old'}
            store_verified_frames(output, cache, prices, fundamentals, old_report, '2026-09-04T18:00:00+09:00')
            previous_pointer = read_json(output/'verified_snapshot.json')

            def fail_pointer(path, value):
                if Path(path).name == 'verified_snapshot.json':
                    raise OSError('simulated pointer publication interruption')
                return json_write(path, value)

            with patch('refresh_store.json_write', side_effect=fail_pointer):
                with self.assertRaises(OSError):
                    store_verified_frames(output, cache, pd.DataFrame({'close': [200.]}),
                                          pd.DataFrame({'op': [50.]}),
                                          {'latestPriceDate': '2026-09-07', 'generation': 'new'},
                                          '2026-09-07T18:00:00+09:00')
            self.assertEqual(read_json(output/'verified_snapshot.json'), previous_pointer)
            saved_prices, saved_fundamentals, report = load_verified_frames(output, cache)
            pd.testing.assert_frame_equal(saved_prices, prices)
            pd.testing.assert_frame_equal(saved_fundamentals, fundamentals)
            self.assertEqual(report, old_report)

    def test_corrupted_snapshot_object_is_rejected_by_sha_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, cache = root/'out', root/'cache'
            manifest = store_verified_frames(output, cache, pd.DataFrame({'close': [100.]}),
                                             pd.DataFrame({'op': [5.]}), {'latestPriceDate': '2026-09-04'},
                                             '2026-09-04T18:00:00+09:00')
            report_object = cache/'snapshots'/manifest['files']['report']['path']
            json_write(report_object, {'latestPriceDate': '1999-01-01'})
            with self.assertRaisesRegex(RuntimeError, '무결성'):
                load_verified_frames(output, cache)

    def test_identical_files_reuse_immutable_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'input.json'
            json_write(source, {'value': 1})
            store = root/'snapshots'
            first = snapshot_files(store, {'data': source}, {'sourceCutoff': '2026-09-04', 'firstStoredAt': 'first'})
            second = snapshot_files(store, {'data': source}, {'sourceCutoff': '2026-09-04', 'firstStoredAt': 'second'})
            self.assertEqual(first['snapshotId'], second['snapshotId'])
            self.assertEqual(len(list((store/'objects').iterdir())), 1)
            saved = read_json(store/'manifests'/f"{first['snapshotId']}.json")
            self.assertEqual(saved['firstStoredAt'], 'first')

    def test_concurrent_run_is_rejected_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            with run_lock(directory):
                with self.assertRaises(RuntimeError):
                    with run_lock(directory):
                        pass
            self.assertFalse((Path(directory)/'refresh.lock').exists())


if __name__ == '__main__':
    unittest.main()
