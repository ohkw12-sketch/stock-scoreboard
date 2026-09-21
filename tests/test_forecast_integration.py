import unittest
from datetime import date

import pandas as pd

from forecast_integration import integrate_forecasts


def forecast(ticker, year, sales, op, status="유효 컨센서스", day="2026-09-10"):
    return {"ticker": ticker, "period": f"{year}FY", "scope": "annual", "status": status,
            "salesMedianKrw100m": sales, "operatingProfitMedianKrw100m": op,
            "latestReportDate": day, "estimates": [{"reportDate": day,
                "sourceUrl": "https://example.com/report.pdf"}]}


def guidance(ticker, year, sales, op, basis="consolidated", day="2026-02-01"):
    item = lambda value: {"low": value * 1e8, "high": value * 1e8, "mid": value * 1e8}
    return {"ticker": ticker, "period": str(year), "basis": basis, "active": True,
            "fetch_status": "정상", "published_at": day,
            "source_url": "https://dart.fss.or.kr/example", "sales": item(sales),
            "operating_profit": item(op)}


class ForecastIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = pd.DataFrame([{"ticker": "123456", "name": "샘플", "op_1y_growth": 3.0,
            "consensus_change_20d": 7.0, "consensus_fetched_at": "2026-09-01T12:00:00+09:00",
            "consensus_as_of_precision": "day", "consensus_source": "KIS"}])

    def test_forecast_pair_builds_next_year_growth(self):
        frame, status = integrate_forecasts(self.base, [forecast("123456", 2026, 100, 10),
            forecast("123456", 2027, 120, 15)], [], today=date(2026, 9, 21), fetched_at="2026-09-21T18:00:00+09:00")
        row = frame.iloc[0]
        self.assertAlmostEqual(row.consensus_sales_1y_growth, 20)
        self.assertAlmostEqual(row.consensus_op_1y_growth, 50)
        self.assertEqual(row.estimate_period, "2027.12E")
        self.assertEqual(row.revision_consensus_change_20d, 7.0)
        self.assertEqual(row.revision_consensus_source, "KIS")
        self.assertEqual(status["integratedTickers"], 1)

    def test_newer_consensus_wins_but_guidance_remains_reference(self):
        rows = [forecast("123456", 2026, 100, 10), forecast("123456", 2027, 120, 15)]
        frame, status = integrate_forecasts(self.base, rows, [guidance("123456", 2026, 110, 11)],
                                             today=date(2026, 9, 21))
        row = frame.iloc[0]
        self.assertEqual(row.consensus_prior_sales, 100)
        self.assertEqual(row.consensus_forward_sales, 120)
        self.assertFalse(bool(row.guidance_used))
        self.assertEqual(status["guidancePreferredTickers"], 0)
        self.assertEqual(status["newerConsensusPreferredTickers"], 1)

    def test_newer_or_same_day_guidance_replaces_same_period_only(self):
        rows = [forecast("123456", 2026, 100, 10), forecast("123456", 2027, 120, 15)]
        frame, status = integrate_forecasts(
            self.base, rows, [guidance("123456", 2026, 110, 11, day="2026-09-10")],
            today=date(2026, 9, 21))
        row = frame.iloc[0]
        self.assertEqual(row.consensus_prior_sales, 110)
        self.assertEqual(row.consensus_forward_sales, 120)
        self.assertTrue(bool(row.guidance_used))
        self.assertEqual(status["guidancePreferredTickers"], 1)
        self.assertEqual(status["newerConsensusPreferredTickers"], 0)

    def test_newer_consensus_keeps_guidance_for_missing_metric(self):
        rows = [forecast("123456", 2026, None, 10), forecast("123456", 2027, 120, 15)]
        frame, status = integrate_forecasts(
            self.base, rows, [guidance("123456", 2026, 110, 11)],
            today=date(2026, 9, 21))
        row = frame.iloc[0]
        self.assertEqual(row.consensus_prior_sales, 110)
        self.assertEqual(row.consensus_prior_op, 10)
        self.assertTrue(bool(row.guidance_used))
        self.assertEqual(status["guidancePreferredTickers"], 1)
        self.assertEqual(status["newerConsensusPreferredTickers"], 1)

    def test_separate_guidance_and_disagreement_do_not_drive_growth(self):
        rows = [forecast("123456", 2026, 100, 10),
                forecast("123456", 2027, 120, 15, status="불일치 검토")]
        frame, status = integrate_forecasts(self.base, rows, [guidance("123456", 2026, 110, 11, "separate")],
                                             today=date(2026, 9, 21))
        self.assertTrue(pd.isna(frame.iloc[0].get("consensus_sales_1y_growth")))
        self.assertEqual(status["rejectedDisagreementPoints"], 1)

    def test_loss_to_profit_is_explicit_without_fake_growth_rate(self):
        frame, _ = integrate_forecasts(self.base, [forecast("123456", 2026, 100, -5),
            forecast("123456", 2027, 130, 7)], [], today=date(2026, 9, 21))
        row = frame.iloc[0]
        self.assertTrue(bool(row.consensus_op_turnaround))
        self.assertTrue(pd.isna(row.consensus_op_1y_growth))


if __name__ == "__main__":
    unittest.main()
