import unittest

import numpy as np
import pandas as pd

from kis_consensus import _revision, parse_estimate_payload


class KisConsensusParserTests(unittest.TestCase):
    def test_parses_first_estimate_period(self):
        payload = {
            "rt_cd": "0",
            "output1": {"item_kor_nm": "테스트", "estdate": "20260801"},
            "output2": [
                {"data1": "100", "data2": "110", "data3": "120", "data4": "130", "data5": "140"},
                {"data1": "10", "data2": "100", "data3": "90", "data4": "80", "data5": "70"},
                {"data1": "10", "data2": "20", "data3": "30", "data4": "40", "data5": "50"},
                {"data1": "20", "data2": "250", "data3": "200", "data4": "150", "data5": "100"},
            ],
            "output3": [
                {"data1": "1", "data2": "2", "data3": "3", "data4": "4", "data5": "5"},
                {"data1": "1000", "data2": "2000", "data3": "3000", "data4": "4000", "data5": "5000"},
                {"data1": "1", "data2": "2", "data3": "3", "data4": "4", "data5": "5"},
                {"data1": "70", "data2": "80", "data3": "90", "data4": "100", "data5": "110"},
            ],
            "output4": [{"dt": "202412"}, {"dt": "202512E"}, {"dt": "202612E"}, {"dt": "202712E"}, {"dt": "202812E"}],
        }
        row = parse_estimate_payload(payload, "5930", "반도체")
        self.assertEqual(row["ticker"], "005930")
        self.assertEqual(row["estimate_period"], "202512E")
        self.assertEqual(row["sales_1y_growth"], 10.0)
        self.assertEqual(row["op_1y_growth"], 100.0)
        self.assertEqual(row["provider_op_growth"], 25.0)
        self.assertEqual(row["forward_pe"], 8.0)
        self.assertEqual(row["prior_op"], 10.0)
        self.assertEqual(row["forward_op"], 20.0)
        self.assertEqual(row["next_op"], 30.0)
        self.assertEqual(row["future_op_basis"], "증가율")
        self.assertEqual(row["as_of"], "2026-08-01")

    def test_rejects_failed_payload(self):
        self.assertIsNone(parse_estimate_payload({"rt_cd": "1"}, "005930"))

    def test_decodes_kis_implied_decimal_scale(self):
        payload = {
            "rt_cd": "0",
            "output1": {"item_kor_nm": "삼성전자", "estdate": "20260630"},
            "output2": [
                {"data4": "7079979.0"},
                {"data4": "1122.0"},
                {"data4": "3767778.0"},
                {"data4": "7641.0"},
            ],
            "output3": [
                {"data4": "4306887.0"},
                {"data4": "443617.0"},
                {"data4": "5716.0"},
                {"data4": "61.0"},
            ],
            "output4": [
                {"dt": "2023.12"}, {"dt": "2024.12"}, {"dt": "2025.12"},
                {"dt": "2026.12E"}, {"dt": "2027.12E"},
            ],
        }
        row = parse_estimate_payload(payload, "005930", "반도체")
        self.assertEqual(row["sales_1y_growth"], 112.2)
        self.assertEqual(row["op_1y_growth"], 764.1)
        self.assertEqual(row["forward_eps"], 44361.7)
        self.assertEqual(row["forward_pe"], 6.1)

    def test_detects_future_turnaround_from_estimate_amounts(self):
        payload = {
            "rt_cd": "0",
            "output1": {"item_kor_nm": "미래전환", "estdate": "20260801"},
            "output2": [
                {"data1": "100", "data2": "110", "data3": "120"},
                {"data1": "0", "data2": "10", "data3": "9"},
                {"data1": "-10", "data2": "20", "data3": "30"},
                {"data1": "0", "data2": "300", "data3": "50"},
            ],
            "output3": [
                {"data1": "1", "data2": "2", "data3": "3"},
                {"data1": "100", "data2": "200", "data3": "300"},
                {"data1": "1", "data2": "2", "data3": "3"},
                {"data1": "100", "data2": "80", "data3": "70"},
            ],
            "output4": [{"dt": "2025.12"}, {"dt": "2026.12E"}, {"dt": "2027.12E"}],
        }
        row = parse_estimate_payload(payload, "1")
        self.assertEqual(row["future_op_basis"], "흑자전환")
        self.assertEqual(row["prior_op"], -10.0)
        self.assertEqual(row["next_op"], 30.0)

    def test_revision_accepts_timezone_aware_history(self):
        history = pd.DataFrame({
            "fetched_at": [pd.Timestamp.now(tz="Asia/Seoul") - pd.offsets.BDay(3)],
            "forward_eps": [100.0],
        })
        self.assertTrue(np.isclose(_revision(110.0, history, 1), 10.0))


if __name__ == "__main__":
    unittest.main()
