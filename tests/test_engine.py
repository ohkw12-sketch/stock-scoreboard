import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rotation_screener import (
    align_to_verified_session, attach_market_snapshot, audit_market_data, build_entry_board, build_value_board, canonical_sector,
    generate_sample_fundamentals, generate_sample_market, load_config, run_engine,
)


class RotationEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(None, "sample")
        cls.prices = generate_sample_market()
        cls.prices, cls.market_status = attach_market_snapshot(cls.prices, cls.config)
        cls.p11 = run_engine(cls.prices, cls.config, "unit-test-sample")
        cls.fundamentals = generate_sample_fundamentals(cls.prices)
        cls.p1 = build_entry_board(cls.prices, cls.p11["_allSectors"], cls.fundamentals, cls.config)
        cls.p2 = build_value_board(cls.fundamentals, cls.config,
                                   {"status": "정상", "asOfDate": "2026-08-27"}, cls.prices)

    def test_whole_market_shape(self):
        self.assertEqual(self.prices["market"].nunique(), 2)
        self.assertGreaterEqual(self.prices["sector"].nunique(), 8)
        self.assertGreaterEqual(self.prices["ticker"].nunique(), 60)

    def test_backward_compatible_p11(self):
        self.assertTrue({"status", "events", "sectors", "rows"}.issubset(self.p11))
        self.assertTrue({"rank", "name", "stage"}.issubset(self.p11["sectors"][0]))
        legacy_row = {"rank", "name", "sector", "opGrowth", "value", "sectorMedian",
                      "premium", "marketState", "marketDetail", "change", "changeUntil",
                      "marks", "signal", "reason"}
        self.assertTrue(legacy_row.issubset(self.p11["rows"][0]))

    def test_required_metrics_and_classifications(self):
        sector = self.p11["sectors"][0]
        required = {"rs1Pct", "rs3Pct", "rs5Pct", "turnoverChangePct", "advanceRatioPct",
                    "leaderStrengthPct", "rotationStartDate", "averageCycleDays",
                    "elapsedBusinessDays", "positionPct", "riskGauge", "rotationType",
                    "entryScore", "entryFit"}
        self.assertTrue(required.issubset(sector))
        self.assertTrue(all(row["relation"] in {"선행", "동행", "후행"} for row in self.p11["rows"]))
        self.assertTrue(any("확산형" in sector["rotationType"] for sector in self.p11["sectors"]))
        self.assertTrue(any("선도주" in sector["rotationType"] for sector in self.p11["sectors"]))
        valid_stages = {"①초기", "②확산", "③주도", "④눌림", "⑤재반등", "⑥후반", "X조기이탈", "X종료"}
        self.assertTrue(all(sector["stage"] in valid_stages for sector in self.p11["sectors"]))
        self.assertTrue(all(0 <= sector["positionPct"] <= 100 for sector in self.p11["sectors"]))
        self.assertTrue(all(0 <= sector["riskGauge"] <= 100 for sector in self.p11["sectors"]))

    def test_json_serializable(self):
        json.dumps(self.p11, ensure_ascii=False)

    def test_entry_is_independent_and_actionable_only(self):
        self.assertGreater(len(self.p1["rows"]), 0)
        self.assertTrue(all(row["entryState"] in {"진입가능", "곧진입"} for row in self.p1["rows"]))
        self.assertTrue(all("추격금지" not in row["signal"] for row in self.p1["rows"]))
        required = {"currentPrice", "entryZone", "confirmation", "invalidationPrice", "stopPct",
                    "growth1Y", "consensus", "valueMultiple"}
        self.assertTrue(required.issubset(self.p1["rows"][0]))
        self.assertTrue(all(
            "nan" not in " ".join(str(row[field]).lower() for field in ("growth1Y", "consensus", "valueMultiple"))
            for row in self.p1["rows"]
        ))
        self.assertTrue(all(row["growth1Y"] not in {"+999.0%", "-999.0%"} for row in self.p1["rows"]))

    def test_value_engine_uses_whole_fundamental_universe(self):
        self.assertEqual(len(self.fundamentals), self.prices["ticker"].nunique())
        self.assertGreater(len(self.p2["rows"]), 0)
        required = {"futureType", "currentEvaluation", "priceEvaluation", "futureEvaluation",
                    "futurePriceEvaluation", "currentResult", "futureResult", "upside12M", "confidence"}
        self.assertTrue(required.issubset(self.p2["rows"][0]))
        self.assertTrue(all(row["futureType"] in {"A", "B"} for row in self.p2["rows"]))
        self.assertIn("turnaroundRows", self.p2)
        self.assertFalse(
            {row["ticker"] for row in self.p2["rows"]} &
            {row["ticker"] for row in self.p2["turnaroundRows"]}
        )

    def test_value_engine_filters_future_a_and_verified_t_plus(self):
        frame = pd.DataFrame([
            {"ticker": "000001", "name": "미래A", "sector": "테스트", "as_of": "2026-06-30",
             "sales_1y_growth": 20, "op_1y_growth": 30, "sales_current": 1200e8,
             "sales_previous": 1000e8, "op_current": 180e8, "op_previous": 100e8,
             "op_growth_basis": "증가율", "report_code": "11012", "forward_pe": 10, "forward_eps": 5000,
             "estimate_period": "2027.12E", "consensus_sales_1y_growth": 20, "consensus_op_1y_growth": 45,
             "consensus_as_of": "2026-06-30", "consensus_change_1d": np.nan,
             "consensus_change_5d": np.nan, "consensus_change_20d": np.nan, "analyst_count": 0},
            {"ticker": "000002", "name": "미래T", "sector": "테스트", "as_of": "2026-06-30",
             "sales_1y_growth": 20, "op_1y_growth": 999, "sales_current": 600e8,
             "sales_previous": 500e8, "op_current": -5e8, "op_previous": -20e8,
             "op_growth_basis": "적자축소", "report_code": "11012", "forward_pe": 12, "forward_eps": 3000,
             "estimate_period": "2027.12E", "consensus_sales_1y_growth": 15, "consensus_op_1y_growth": 60,
             "future_op_basis": "흑자전환", "consensus_prior_op": -10, "consensus_forward_op": 20,
             "consensus_next_op": 30, "consensus_next_sales": 700,
             "consensus_as_of": "2026-06-30", "consensus_change_1d": np.nan,
             "consensus_change_5d": np.nan, "consensus_change_20d": np.nan, "analyst_count": 0},
            {"ticker": "000003", "name": "미래B", "sector": "테스트", "as_of": "2026-06-30",
             "sales_1y_growth": 20, "op_1y_growth": 30, "sales_current": 1200e8,
             "sales_previous": 1000e8, "op_current": 120e8, "op_previous": 100e8,
             "op_growth_basis": "증가율", "report_code": "11012", "forward_pe": 10, "forward_eps": 4000,
             "estimate_period": "2027.12E", "consensus_sales_1y_growth": 30, "consensus_op_1y_growth": 20,
             "consensus_as_of": np.nan, "consensus_change_1d": np.nan,
             "consensus_change_5d": np.nan, "consensus_change_20d": np.nan, "analyst_count": np.nan},
            {"ticker": "000004", "name": "고배수제외", "sector": "테스트", "as_of": "2026-06-30",
             "sales_1y_growth": 5, "op_1y_growth": 5, "sales_current": 1050e8,
             "sales_previous": 1000e8, "op_current": 105e8, "op_previous": 100e8,
             "op_growth_basis": "증가율", "report_code": "11012", "forward_pe": 20, "forward_eps": 2000,
             "estimate_period": "2027.12E", "consensus_sales_1y_growth": -5, "consensus_op_1y_growth": -5,
             "consensus_as_of": np.nan, "consensus_change_1d": np.nan,
             "consensus_change_5d": np.nan, "consensus_change_20d": np.nan, "analyst_count": np.nan},
        ])
        price_rows = pd.DataFrame([
            {"ticker": "000001", "date": "2026-08-28", "close": 40000, "market_cap": 9000e8, "shares": 22_500_000},
            {"ticker": "000002", "date": "2026-08-28", "close": 24000, "market_cap": 3000e8, "shares": 12_500_000},
            {"ticker": "000003", "date": "2026-08-28", "close": 40000, "market_cap": 5000e8, "shares": 12_500_000},
            {"ticker": "000004", "date": "2026-08-28", "close": 40000, "market_cap": 4000e8, "shares": 10_000_000},
        ])
        board = build_value_board(frame, self.config, {"status": "정상", "asOfDate": "2026-06-30"}, price_rows)
        rows = {row["name"]: row for row in board["rows"]}
        turnaround = {row["name"]: row for row in board["turnaroundRows"]}
        self.assertEqual(rows["미래A"]["futureType"], "A")
        self.assertEqual(turnaround["미래T"]["futureType"], "T+")
        self.assertIn("1Q·2Q 흑자 지속", turnaround["미래T"]["result"])
        self.assertNotIn("기대 여유", rows["미래A"]["result"])
        self.assertEqual(rows["미래A"]["actualPeriod"], "2026 2Q")

    def test_market_snapshot_aligns_cap_to_verified_close(self):
        latest = self.prices.sort_values("date").groupby("ticker").tail(1)
        self.assertEqual(self.market_status["status"], "정상")
        self.assertTrue(np.allclose(latest["market_cap"], latest["close"] * latest["shares"]))

    def test_collection_audit(self):
        report = audit_market_data(self.prices, self.config, "sample")
        self.assertEqual(report["qualityStatus"], "정상")
        self.assertEqual(report["latestCoverageRatio"], 1.0)
        self.assertEqual(report["missingTickers"], [])

    def test_sparse_future_quotes_do_not_become_whole_market_session(self):
        base = self.prices.copy()
        sparse = base.sort_values("date").groupby("ticker").tail(1).head(2).copy()
        sparse["date"] = sparse["date"].max() + pd.offsets.BDay(1)
        aligned, details = align_to_verified_session(pd.concat([base, sparse], ignore_index=True), self.config)
        self.assertEqual(aligned["date"].max(), base["date"].max())
        self.assertEqual(details["ignoredSparseTickers"], 2)

    def test_latest_coverage_uses_prior_session_count_not_union(self):
        base = self.prices.copy()
        latest = base.sort_values("date").groupby("ticker").tail(1).head(62).copy()
        latest["date"] = pd.Timestamp("2026-09-01")
        newcomer = latest.iloc[[0]].copy()
        newcomer["ticker"], newcomer["name"] = "999999", "신규상장"
        combined = pd.concat([base, latest, newcomer], ignore_index=True)
        aligned, details = align_to_verified_session(
            combined, self.config, datetime.fromisoformat("2026-09-01T18:30:00+09:00")
        )
        self.assertEqual(aligned["date"].max(), pd.Timestamp("2026-09-01"))
        self.assertEqual(details["ignoredSparseTickers"], 0)

    def test_intraday_whole_market_rows_are_not_treated_as_close(self):
        base = self.prices.copy()
        prior_close = base.sort_values("date").groupby("ticker").tail(1).copy()
        prior_close["date"] = pd.Timestamp("2026-08-31")
        intraday = prior_close.copy()
        intraday["date"] = pd.Timestamp("2026-09-01")
        combined = pd.concat([base, prior_close, intraday], ignore_index=True)
        aligned, details = align_to_verified_session(
            combined, self.config, datetime.fromisoformat("2026-09-01T09:30:00+09:00")
        )
        self.assertEqual(aligned["date"].max(), pd.Timestamp("2026-08-31"))
        self.assertEqual(details["rawLatestPriceDate"], "2026-09-01")

    def test_theme_sector_mapping(self):
        self.assertEqual(canonical_sector("달바글로벌", "기타 화학제품 제조업", "기초 화장품"), "화장품")
        self.assertEqual(canonical_sector("LS ELECTRIC", "전기장비 제조업", "변압기, 배전반"), "전력기기")
        self.assertEqual(canonical_sector("한화솔루션", "기초 화학물질 제조업", "무기화합물"), "기초 화학물질 제조업")


if __name__ == "__main__":
    unittest.main()

