import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rotation_screener import (
    audit_market_data, build_entry_board, build_value_board, canonical_sector,
    generate_sample_fundamentals, generate_sample_market, load_config, run_engine,
)


class RotationEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(None, "sample")
        cls.prices = generate_sample_market()
        cls.p11 = run_engine(cls.prices, cls.config, "unit-test-sample")
        cls.fundamentals = generate_sample_fundamentals(cls.prices)
        cls.p1 = build_entry_board(cls.prices, cls.p11["_allSectors"], cls.fundamentals, cls.config)
        cls.p2 = build_value_board(cls.fundamentals, cls.config,
                                   {"status": "정상", "asOfDate": "2026-08-27"})

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

    def test_value_engine_uses_whole_fundamental_universe(self):
        self.assertEqual(len(self.fundamentals), self.prices["ticker"].nunique())
        self.assertGreater(len(self.p2["rows"]), 0)
        self.assertTrue({"forwardPER", "sectorMedianPER", "consensus20D"}.issubset(self.p2["rows"][0]))

    def test_value_consensus_is_bounded_supplement_and_missing_is_neutral(self):
        frame = pd.DataFrame([
            {"ticker": "000001", "name": "상향", "sector": "테스트", "as_of": "2026-06-30",
             "sales_1y_growth": 20, "op_1y_growth": 30, "forward_pe": 10,
             "consensus_op_1y_growth": 80, "consensus_growth_delta": 50,
             "consensus_as_of": "2026-06-30", "consensus_change_1d": np.nan,
             "consensus_change_5d": np.nan, "consensus_change_20d": np.nan, "analyst_count": 0},
            {"ticker": "000002", "name": "하향", "sector": "테스트", "as_of": "2026-06-30",
             "sales_1y_growth": 20, "op_1y_growth": 30, "forward_pe": 10,
             "consensus_op_1y_growth": -20, "consensus_growth_delta": -50,
             "consensus_as_of": "2026-06-30", "consensus_change_1d": np.nan,
             "consensus_change_5d": np.nan, "consensus_change_20d": np.nan, "analyst_count": 0},
            {"ticker": "000003", "name": "미제공", "sector": "테스트", "as_of": "2026-06-30",
             "sales_1y_growth": 20, "op_1y_growth": 30, "forward_pe": np.nan,
             "consensus_op_1y_growth": np.nan, "consensus_growth_delta": np.nan,
             "consensus_as_of": np.nan, "consensus_change_1d": np.nan,
             "consensus_change_5d": np.nan, "consensus_change_20d": np.nan, "analyst_count": np.nan},
        ])
        board = build_value_board(frame, self.config, {"status": "정상", "asOfDate": "2026-06-30"})
        rows = {row["name"]: row for row in board["rows"]}
        self.assertGreater(rows["상향"]["consensusAdjustment"], 0)
        self.assertLess(rows["하향"]["consensusAdjustment"], 0)
        self.assertEqual(rows["미제공"]["consensusAdjustment"], 0)
        self.assertLessEqual(max(abs(row["consensusAdjustment"]) for row in board["rows"]), 10)

    def test_collection_audit(self):
        report = audit_market_data(self.prices, self.config, "sample")
        self.assertEqual(report["qualityStatus"], "정상")
        self.assertEqual(report["latestCoverageRatio"], 1.0)
        self.assertEqual(report["missingTickers"], [])

    def test_theme_sector_mapping(self):
        self.assertEqual(canonical_sector("달바글로벌", "기타 화학제품 제조업", "기초 화장품"), "화장품")
        self.assertEqual(canonical_sector("LS ELECTRIC", "전기장비 제조업", "변압기, 배전반"), "전력기기")
        self.assertEqual(canonical_sector("한화솔루션", "기초 화학물질 제조업", "무기화합물"), "기초 화학물질 제조업")


if __name__ == "__main__":
    unittest.main()

