import unittest

import pandas as pd

from future_fundamentals import attach_future_fundamentals


class FutureFundamentalsTests(unittest.TestCase):
    def base(self):
        return {
            "ticker": "123456", "as_of": "2026-06-30",
            "normalization_periods": "2025Q3,2025Q4,2026Q1,2026Q2",
            "normalized_sales_q1": 120.0, "normalized_sales_q2": 180.0,
            "normalized_op_q1": 18.0, "normalized_op_q2": 36.0,
            "sales_previous": 200.0, "sales_quarter_previous": 110.0,
            "op_previous": 20.0, "op_quarter_previous": 11.0,
            "normalized_sales_q3": 120.0, "normalized_sales_q4": 180.0,
            "normalized_op_q3": 12.0, "normalized_op_q4": 18.0,
        }

    def test_current_year_forecast_wins_and_prior_year_is_unused(self):
        row = self.base() | {
            "consensus_sales_2026": 1_000.0, "consensus_op_2026": 160.0,
            "forecast_source_2026": "회사 공식 가이던스", "forecast_guidance_used_2026": True,
            "forecast_date_2026": "2026-09-10",
        }
        result = attach_future_fundamentals(pd.DataFrame([row]), current_year=2026).iloc[0]
        self.assertEqual(result.value_fundamental_sales, 100_000_000_000)
        self.assertEqual(result.value_fundamental_op, 16_000_000_000)
        self.assertEqual(result.value_fundamental_opm_pct, 16)
        self.assertFalse(bool(result.value_fundamental_seasonality_used))
        self.assertEqual(result.value_fundamental_prior_year_role, "미사용")

    def test_missing_forecast_uses_only_prior_year_seasonality_ratios(self):
        result = attach_future_fundamentals(
            pd.DataFrame([self.base()]), current_year=2026,
        ).iloc[0]
        # H1 sales scale is 300/200=1.5, so Q3/Q4 estimates are 180/270.
        self.assertEqual(result.value_fundamental_q3_sales_estimate, 180)
        self.assertEqual(result.value_fundamental_q4_sales_estimate, 270)
        self.assertEqual(result.value_fundamental_sales, 750)
        # H1 OP scale is 54/20=2.7, preserving only the prior quarter pattern.
        self.assertAlmostEqual(result.value_fundamental_q3_op_estimate, 32.4)
        self.assertAlmostEqual(result.value_fundamental_q4_op_estimate, 48.6)
        self.assertTrue(bool(result.value_fundamental_seasonality_used))
        self.assertEqual(result.value_fundamental_prior_year_role, "계절성 비율만")


if __name__ == "__main__":
    unittest.main()
