import unittest
from unittest.mock import patch

import pandas as pd

from dart_fundamentals import _attach_normalized_ttm, parse_financial_payload


class DartFundamentalsTest(unittest.TestCase):
    def test_parses_reported_growth(self):
        payload = {"status": "000", "list": [
            {"account_id": "ifrs-full_Revenue", "account_nm": "매출액",
             "thstrm_amount": "120,000", "frmtrm_amount": "100,000",
             "thstrm_dt": "2026.01.01 ~ 2026.06.30"},
            {"account_id": "dart_OperatingIncomeLoss", "account_nm": "영업이익",
             "thstrm_amount": "25,000", "frmtrm_amount": "20,000",
             "thstrm_dt": "2026.01.01 ~ 2026.06.30"},
        ]}
        row = parse_financial_payload(payload, "005930", "삼성전자", "반도체", 2026, "11012", "CFS")
        self.assertEqual(row["sales_1y_growth"], 20.0)
        self.assertEqual(row["op_1y_growth"], 25.0)
        self.assertEqual(row["sales_quarter_current"], 120000.0)
        self.assertEqual(row["op_quarter_current"], 25000.0)
        self.assertEqual(row["as_of"], "2026-06-30")

    def test_turnaround_is_rankable(self):
        payload = {"status": "000", "list": [
            {"account_nm": "매출액", "thstrm_amount": "110", "frmtrm_amount": "100"},
            {"account_nm": "영업이익", "thstrm_amount": "10", "frmtrm_amount": "-5"},
        ]}
        row = parse_financial_payload(payload, "000001", "테스트", "테스트", 2025, "11011", "OFS")
        self.assertEqual(row["op_1y_growth"], 999.0)
        self.assertEqual(row["op_growth_basis"], "흑자전환")

    def test_interim_uses_cumulative_amounts(self):
        payload = {"status": "000", "list": [
            {"account_nm": "매출액", "thstrm_amount": "60", "frmtrm_amount": None,
             "thstrm_add_amount": "120", "frmtrm_add_amount": "100"},
            {"account_nm": "영업이익", "thstrm_amount": "15", "frmtrm_amount": None,
             "thstrm_add_amount": "25", "frmtrm_add_amount": "20"},
        ]}
        row = parse_financial_payload(payload, "000002", "반기", "테스트", 2026, "11012", "CFS")
        self.assertEqual(row["sales_1y_growth"], 20.0)
        self.assertEqual(row["op_1y_growth"], 25.0)
        self.assertEqual(row["sales_quarter_current"], 60.0)
        self.assertEqual(row["op_quarter_current"], 15.0)

    def test_normalized_ttm_reconstructs_q1_and_q4(self):
        combined = pd.DataFrame([{
            "ticker": "000001", "sales_current": 220.0, "op_current": 22.0,
            "sales_quarter_current": 120.0, "op_quarter_current": 12.0,
        }])
        universe = pd.DataFrame([{"ticker": "000001", "corp_code": "12345678"}])
        q3 = pd.DataFrame([{
            "ticker": "000001", "sales_current": 300.0, "op_current": 30.0,
            "sales_quarter_current": 110.0, "op_quarter_current": 11.0,
        }])
        annual = pd.DataFrame([{"ticker": "000001", "sales_current": 430.0, "op_current": 45.0}])

        def period_values(_universe, _key, _config, _year, report_code):
            return (q3, []) if report_code == "11014" else (annual, [])

        with patch("dart_fundamentals._report_candidates", return_value=[(2026, "11012")]), patch(
            "dart_fundamentals._collect_bulk_period_values", side_effect=period_values,
        ):
            normalized, failures = _attach_normalized_ttm(combined, universe, "x" * 40, {})

        row = normalized.iloc[0]
        self.assertFalse(failures)
        self.assertEqual(row["normalized_op_q1"], 10.0)
        self.assertEqual(row["normalized_op_q2"], 12.0)
        self.assertEqual(row["normalized_op_q3"], 11.0)
        self.assertEqual(row["normalized_op_q4"], 15.0)
        self.assertEqual(row["normalized_ttm_op"], 48.0)
        self.assertEqual(row["normalized_quarter_count"], 4)


if __name__ == "__main__":
    unittest.main()
