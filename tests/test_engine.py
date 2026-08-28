import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rotation_screener import canonical_sector, generate_sample_market, load_config, run_engine


class RotationEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(None, "sample")
        cls.prices = generate_sample_market()
        cls.p11 = run_engine(cls.prices, cls.config, "unit-test-sample")

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

    def test_theme_sector_mapping(self):
        self.assertEqual(canonical_sector("달바글로벌", "기타 화학제품 제조업", "기초 화장품"), "화장품")
        self.assertEqual(canonical_sector("LS ELECTRIC", "전기장비 제조업", "변압기, 배전반"), "전력기기")
        self.assertEqual(canonical_sector("한화솔루션", "기초 화학물질 제조업", "무기화합물"), "기초 화학물질 제조업")


if __name__ == "__main__":
    unittest.main()

