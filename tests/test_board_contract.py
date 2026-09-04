import copy
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from board_contract import load_contract, validate_board, validate_html
from promote_sections import promote
from refresh_all import refresh_holdings


class BoardContractTest(unittest.TestCase):
    def test_current_html_matches_locked_contract(self):
        self.assertEqual(validate_html(ROOT / "index.html", load_contract()), [])

    def test_value_payload_requires_locked_fields(self):
        contract = load_contract()
        board = {"meta": {"uiContractVersion": contract["version"]}, "p2": {"rows": [{}]}}
        errors = validate_board(board, contract)
        self.assertTrue(errors and "필드 누락" in errors[0])

    def test_value_rendering_order_is_locked(self):
        html = (ROOT / "index.html").read_text("utf-8").replace("${multiple(x.normalizedPOP)}", "${x.normalizedPOP}")
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "index.html"
            changed.write_text(html, "utf-8")
            self.assertTrue(any("값 표시형식" in error for error in validate_html(changed, load_contract())))

    def test_growth_only_promotion_preserves_every_other_section_and_holdings(self):
        live = {
            "p1": {"rows": [1]}, "p11": {"rows": [2]}, "p2": {"rows": [3]},
            "growth": {"rows": ["old"]},
            "p3": {"rows": [{"name": "보유주", "qty": 7, "avg": 1234, "judgment": "유지"}]},
            "meta": {"updatedKST": "old"}
        }
        candidate = copy.deepcopy(live)
        candidate["growth"] = {"rows": ["new"]}
        candidate["p2"] = {"rows": ["accidental"]}
        result, report = promote(live, candidate, ["growth"])
        self.assertEqual(result["growth"], {"rows": ["new"]})
        for key in ("p1", "p11", "p2", "p3", "meta"):
            self.assertEqual(result[key], live[key])
        self.assertEqual(report["protectedSections"], ["meta", "p1", "p11", "p2", "p3"])

    def test_holding_inputs_cannot_change_during_promotion(self):
        live = {key: {} for key in ("p1", "p11", "p2", "growth", "meta")}
        live["p3"] = {"rows": [{"name": "보유주", "qty": 7, "avg": 1234}]}
        candidate = copy.deepcopy(live)
        candidate["p3"]["rows"][0]["qty"] = 8
        with self.assertRaisesRegex(RuntimeError, "보유 수량"):
            promote(live, candidate, ["p3"])

    def test_holdings_refresh_preserves_user_owned_display_fields(self):
        previous = {
        "rows": [{
            "name": "보유주", "qty": 7, "avg": 1000, "judgment": "자동 덮어쓰기 값",
            "action": "자동 덮어쓰기 행동", "fairRange": "산출하지 않음", "marks": ["유지"],
            "previousAssessment": {"judgment": "사용자 판단", "action": "보유", "fairRange": "1200~1500원"}
        }],
        "exposure": {"sectors": [], "topAxes": []}
    }
        prices = pd.DataFrame([{
        "ticker": "000001", "name": "보유주", "date": pd.Timestamp("2026-09-04"),
        "close": 1100.0, "high": 1150.0, "market_cap": 10000.0
    }])
        fundamentals = pd.DataFrame([{
        "ticker": "000001", "sector": "테스트", "normalized_ttm_op": 1000.0,
        "op_current": 120.0, "op_previous": 100.0, "as_of": "2026-06-30"
    }])
        result = refresh_holdings(previous, prices, fundamentals)
        row = result["rows"][0]
        self.assertEqual((row["judgment"], row["action"], row["fairRange"]), ("사용자 판단", "보유", "1200~1500원"))
        self.assertEqual(row["marks"], ["유지"])
        self.assertEqual((row["qty"], row["avg"]), (7, 1000))


if __name__ == "__main__":
    unittest.main()
