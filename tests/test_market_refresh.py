"""Offline checks for economical collection and untruncated candidate exports."""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rotation_screener import (
    MarketDataLoader, audit_market_data, completed_session_info, load_config,
    normalize_prices, generate_sample_market, generate_sample_fundamentals,
    attach_market_snapshot, run_engine, build_entry_board, build_value_board, write_outputs,
    merge_fundamental_sources,
)


class MarketRefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = load_config(None, "sample")
        self.config["cache_dir"] = Path(self.temp.name) / "cache"
        self.config["output_dir"] = Path(self.temp.name) / "out"
        self.config["base_data_file"] = Path(self.temp.name) / "no-board.json"
        self.loader = MarketDataLoader(self.config)

    def test_incremental_history_is_not_refetched(self):
        history = generate_sample_market()
        self.loader._history = history
        self.assertEqual(self.loader._fetch_start(date(2026, 8, 31)), date(2026, 8, 22))
        self.assertEqual(self.loader.refresh_plan["mode"], "incremental-with-overlap")
        self.config["force_full_prices"] = True
        repair_start = self.loader._fetch_start(date(2026, 8, 31))
        self.assertLessEqual(repair_start, history["date"].min().date())
        self.assertEqual(self.loader.refresh_plan["mode"], "targeted-full-history")

    def test_exact_requested_universe_detects_never_collected_stock(self):
        prices = generate_sample_market()
        self.config["_requested_universe"] = (
            prices[["ticker", "name", "market", "sector"]].drop_duplicates("ticker").to_dict("records")
            + [{"ticker": "999999", "name": "누락신규상장", "market": "KOSDAQ", "sector": "기타"}]
        )
        report = audit_market_data(prices, self.config, "fixture")
        self.assertEqual(report["requestedMissingTickers"], ["999999"])
        self.assertEqual(report["missingStocks"][-1]["name"], "누락신규상장")
        self.assertIsNone(report["missingStocks"][-1]["lastPriceDate"])

    def test_legacy_values_do_not_claim_verified_adjustment_or_turnover(self):
        frame = normalize_prices(generate_sample_market())
        self.assertEqual(set(frame.price_basis), {"unknown"})
        self.assertEqual(set(frame.adjusted_basis), {"unknown"})
        self.assertEqual(set(frame.value_basis), {"unknown"})

    def test_correction_overlap_detects_adjusted_history_scale_change(self):
        history = generate_sample_market()
        history["adjusted_close"] = history["close"].astype(float)
        self.loader._history = history
        fresh = history[history["date"].eq(history["date"].max())].copy()
        ticker = fresh.iloc[0].ticker
        fresh.loc[fresh["ticker"].eq(ticker), "adjusted_close"] *= .5
        self.assertEqual(self.loader._correction_tickers(fresh), {ticker})

    def test_primary_correction_wins_and_missing_history_is_preserved(self):
        history = generate_sample_market()
        fresh = history.tail(1).copy()
        fresh["close"] *= 1.01
        merged = self.loader._merge_recovery(fresh, [history])
        self.assertEqual(len(merged), len(history))
        matching = merged[(merged.ticker == fresh.iloc[0].ticker) & (merged.date == fresh.iloc[0].date)]
        self.assertEqual(matching.iloc[0].close, fresh.iloc[0].close)

    def test_partial_collection_cannot_replace_good_cache(self):
        prices = generate_sample_market()
        self.loader._save_cache(prices)
        previous = self.loader.cache_file.read_bytes()
        broken = prices.copy()
        broken["sector"] = "미분류"
        self.loader._save_cache(broken)
        self.assertEqual(previous, self.loader.cache_file.read_bytes())

    def test_calendar_failure_is_explicit(self):
        with patch.dict(sys.modules, {"exchange_calendars": None}):
            result = completed_session_info(datetime.fromisoformat("2026-09-07T12:00:00+09:00"))
        self.assertEqual(result["calendarStatus"], "unknown")
        self.assertEqual(result["expectedCompletedSession"], "2026-09-04")
        with patch.dict(sys.modules, {"exchange_calendars": None}):
            after_close = completed_session_info(datetime.fromisoformat("2026-09-07T15:40:00+09:00"))
        self.assertEqual(after_close["expectedCompletedSession"], "2026-09-07")

    def test_one_missing_ticker_uses_targeted_fallback_and_keeps_history(self):
        prices = generate_sample_market()
        latest_date = prices["date"].max()
        latest = prices[prices["date"].eq(latest_date)].copy()
        historical = prices[prices["date"].lt(latest_date)].copy()
        missing = latest.iloc[-1].ticker
        universe = latest[["ticker", "name", "market", "sector"]]
        self.config.update(mode="live", primary_source="pykrx", fallback_sources=["yfinance"])

        def primary():
            self.loader._record_universe(universe, "fixture")
            return latest[~latest["ticker"].eq(missing)]

        def fallback():
            self.assertEqual(self.loader._requested_tickers, {missing})
            return latest[latest["ticker"].eq(missing)]

        info = {"expectedCompletedSession": "2026-08-27", "calendarStatus": "unknown", "calendarSource": "fixture"}
        with patch.object(self.loader, "_cache", return_value=historical), \
                patch.object(self.loader, "_pykrx", side_effect=primary), \
                patch.object(self.loader, "_yfinance", side_effect=fallback), \
                patch("rotation_screener.completed_session_info", return_value=info):
            merged, source = self.loader.load()
        self.assertEqual(len(merged), len(prices))
        self.assertEqual(self.loader.report["requestedMissingTickers"], [])
        self.assertIn("yfinance", source)

    def test_invalid_latest_ohlc_is_reported(self):
        prices = generate_sample_market()
        prices.loc[prices.index[-1], "high"] = 1
        report = audit_market_data(prices, self.config, "fixture")
        self.assertEqual(report["qualityStatus"], "부분실패")
        self.assertEqual(len(report["invalidPriceTickers"]), 1)

    def test_q3_cumulative_fallback_cannot_be_annualized_as_half_year(self):
        prices, _ = attach_market_snapshot(generate_sample_market(), self.config)
        fundamentals = generate_sample_fundamentals(prices)
        fundamentals['report_code'] = '11014'
        fundamentals['quarter_value_verified'] = False
        result = build_value_board(fundamentals, self.config, {'status': '정상'}, prices)
        self.assertEqual(result['rows'], [])
        self.assertTrue(all(not r['eligible'] for r in result['_eligibility']))

    def test_provider_consensus_date_precision_is_preserved(self):
        prices = generate_sample_market()
        dart = generate_sample_fundamentals(prices)
        consensus = dart.copy()
        consensus['as_of_precision'] = 'month'
        consensus['provider_date_raw'] = '2026.08'
        merged, _ = merge_fundamental_sources(dart, consensus, {'status': '정상'}, {'status': '정상'})
        self.assertEqual(set(merged['consensus_as_of_precision']), {'month'})
        self.assertEqual(set(merged['consensus_provider_date_raw']), {'2026.08'})

    def test_full_candidates_exist_before_display_caps_and_stay_private(self):
        prices, _ = attach_market_snapshot(generate_sample_market(), self.config)
        fundamentals = generate_sample_fundamentals(prices)
        self.config.update(top_stock_count=2, top_entry_count=2, top_value_count=2)
        p11 = run_engine(prices, self.config, "fixture")
        p1 = build_entry_board(prices, p11["_allSectors"], fundamentals, self.config)
        p2 = build_value_board(fundamentals, self.config, {"status": "정상"}, prices)
        for section in (p1, p11, p2):
            self.assertLessEqual(len(section["rows"]), 2)
            self.assertGreater(len(section["_allRows"]), 2)
            self.assertEqual(len(section["_eligibility"]), prices.ticker.nunique())
            self.assertTrue(all("score" in row for row in section["_eligibility"]))
        _, board_path, _ = write_outputs(p1, p11, p2, {"latestPriceDate": "2026-08-27"}, self.config)
        board = json.loads(board_path.read_text(encoding="utf-8"))
        self.assertNotIn("_allRows", board["p1"])
        self.assertNotIn("_allRows", board["p2"])
        exported = json.loads((self.config["output_dir"] / "full_candidates.json").read_text(encoding="utf-8"))
        self.assertGreater(len(exported["sections"]["p1"]["rows"]), 2)
        self.assertGreater(len(exported["sections"]["p2"]["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
