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
from rotation_screener import normalize_prices


class RotationEngineTest(unittest.TestCase):
    def test_alphanumeric_krx_ticker_is_preserved(self):
        frame=pd.DataFrame([dict(Date='2026-09-04',Code='00104K',Name='우선주',Market='KOSPI',Sector='금융',
                                 Open=1,High=1,Low=1,Close=1,Volume=1,Value=1)])
        self.assertEqual(normalize_prices(frame).iloc[0].ticker,'00104K')

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
        required = {"confidence", "normalizedPOP", "sectorNormalizedPOP", "normalizedPremiumPct",
                    "normalizationQuality", "absoluteValueScore", "sectorValueScore",
                    "confidenceMultiplier", "valueScore", "normalizationAdjustmentPct"}
        self.assertTrue(required.issubset(self.p2["rows"][0]))
        self.assertTrue(all(not any(key.lower().startswith("future") for key in row) for row in self.p2["rows"]))
        self.assertTrue(all("consensus" not in key.lower() for row in self.p2["rows"] for key in row))
        self.assertNotIn("turnaroundRows", self.p2)
        self.assertNotIn("T+", self.p2["status"])
        self.assertFalse(self.p2["dataStatus"]["forwardEstimateUsed"])

    def test_value_engine_removes_every_future_and_t_plus_output(self):
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
        self.assertNotIn("turnaroundRows", board)
        self.assertNotIn("미래T", rows)
        self.assertFalse(any(key.lower().startswith("future") for key in rows["미래A"]))
        self.assertNotIn("성장 대비", board["method"])
        self.assertIn("미래 추정치 미사용", board["method"])

    def test_current_value_rank_uses_discount_to_each_sector_median(self):
        def fundamental(ticker, name, sector):
            return {
                "ticker": ticker, "name": name, "sector": sector, "as_of": "2026-06-30",
                "quarter_as_of": "2026-06-30", "sales_1y_growth": 20, "op_1y_growth": 30,
                "sales_current": 200e8, "sales_previous": 180e8,
                "op_current": 20e8, "op_previous": 18e8,
                "sales_quarter_current": 100e8, "sales_quarter_previous": 90e8,
                "op_quarter_current": 10e8, "op_quarter_previous": 9e8,
                "op_growth_basis": "증가율", "report_code": "11012",
                "consensus_sales_2026": 400, "consensus_op_2026": 40,
                "consensus_sales_2027": 500, "consensus_op_2027": 60,
                "consensus_as_of": "2026-06-30", "consensus_change_1d": np.nan,
                "consensus_change_5d": np.nan, "consensus_change_20d": np.nan,
                "analyst_count": 0,
            }

        frame = pd.DataFrame([
            fundamental("100001", "저배수50할인", "저배수섹터"),
            fundamental("100002", "저배수50프리미엄", "저배수섹터"),
            fundamental("200001", "고배수9할인", "고배수섹터"),
            fundamental("200002", "고배수9프리미엄", "고배수섹터"),
        ])
        # Current P/OP: 5, 15, 50, 60. Ranking must not compare these raw
        # multiples across sectors. It compares -50%, +50%, -9.1%, +9.1%.
        market_caps = [200e8, 600e8, 2000e8, 2400e8]
        price_rows = pd.DataFrame([
            {"ticker": ticker, "date": "2026-09-01", "close": 10000,
             "market_cap": market_cap, "shares": market_cap / 10000}
            for ticker, market_cap in zip(frame["ticker"], market_caps)
        ])
        board = build_value_board(
            frame, self.config, {"status": "정상", "asOfDate": "2026-06-30"}, price_rows,
        )
        rows = {row["name"]: row for row in board["rows"]}
        self.assertEqual(rows["저배수50할인"]["normalizedResult"], "정상화 1위(50.0% 할인)")
        self.assertEqual(rows["고배수9할인"]["normalizedResult"], "정상화 2위(9.1% 할인)")
        self.assertEqual(rows["고배수9프리미엄"]["normalizedResult"], "정상화 3위(9.1% 프리미엄)")
        self.assertEqual(rows["저배수50프리미엄"]["normalizedResult"], "정상화 4위(50.0% 프리미엄)")

    def test_normalized_value_beats_extreme_growth_at_a_high_price(self):
        def fundamental(ticker, name, op_2027):
            return {
                "ticker": ticker, "name": name, "sector": "테스트", "as_of": "2026-06-30",
                "quarter_as_of": "2026-06-30", "sales_1y_growth": 10, "op_1y_growth": 10,
                "sales_current": 200e8, "sales_previous": 180e8,
                "op_current": 20e8, "op_previous": 18e8,
                "sales_quarter_current": 100e8, "sales_quarter_previous": 90e8,
                "op_quarter_current": 10e8, "op_quarter_previous": 9e8,
                "normalized_sales_q3": 90e8, "normalized_sales_q4": 95e8,
                "normalized_sales_q1": 100e8, "normalized_sales_q2": 100e8,
                "normalized_op_q3": 8e8, "normalized_op_q4": 9e8,
                "normalized_op_q1": 10e8, "normalized_op_q2": 10e8,
                "normalized_ttm_sales": 385e8, "normalized_ttm_op": 37e8,
                "normalized_quarter_count": 4, "normalization_as_of": "2026-06-30",
                "op_growth_basis": "증가율", "report_code": "11012",
                "consensus_sales_2026": 440, "consensus_op_2026": 44,
                "consensus_sales_2027": 480, "consensus_op_2027": op_2027,
                "consensus_as_of": "2026-06-30", "consensus_change_1d": 0,
                "consensus_change_5d": 0, "consensus_change_20d": 0, "analyst_count": 3,
            }

        frame = pd.DataFrame([
            fundamental("300001", "정상화저평가", 48),
            fundamental("300002", "고평가고성장", 200),
        ])
        prices = pd.DataFrame([
            {"ticker": "300001", "date": "2026-09-01", "close": 10000,
             "market_cap": 300e8, "shares": 3_000_000},
            {"ticker": "300002", "date": "2026-09-01", "close": 10000,
             "market_cap": 3000e8, "shares": 30_000_000},
        ])
        board = build_value_board(frame, self.config, {"status": "정상"}, prices)
        self.assertEqual(board["rows"][0]["name"], "정상화저평가")
        self.assertGreater(board["rows"][0]["valueScore"], board["rows"][1]["valueScore"])
        self.assertEqual(board["rows"][0]["normalizedQuarterCount"], 4)

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

