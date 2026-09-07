"""Offline regressions for source dates, period identity and incremental reads."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import dart_fundamentals as dart
import kis_consensus as kis


def report(year, quarter, sales, op, basis="CFS", receipt=None):
    return {"ticker": "000001", "name": "테스트", "sector": "제조", "report_year": year,
            "report_code": dart.QUARTER_REPORTS[quarter], "as_of": dart._period_end(year, quarter),
            "fs_div": basis, "receipt": receipt or f"{year}{quarter}", "sales_current": sales,
            "op_current": op, "quarter_value_verified": False}


def raw_report(year=2026, quarter=2, receipt="20260814000123", op="30"):
    common = {"stock_code": "000001", "fs_div": "CFS", "bsns_year": str(year),
              "reprt_code": dart.QUARTER_REPORTS[quarter], "rcept_no": receipt,
              "thstrm_dt": dart._period_end(year, quarter)}
    return [dict(common, account_nm="매출액", thstrm_amount="300", frmtrm_amount="200"),
            dict(common, account_nm="영업이익", thstrm_amount=op, frmtrm_amount="20")]


def estimate(date="20260831", period="2026.12E"):
    return {"rt_cd": "0", "output1": {"item_kor_nm": "테스트", "estdate": date},
            "output2": [{"data1": "100", "data2": "120"}, {},
                        {"data1": "10", "data2": "15"}, {}],
            "output3": [{}, {"data1": "1000", "data2": "1100"}, {},
                        {"data1": "100", "data2": "90"}],
            "output4": [{"dt": "2025.12"}, {"dt": period}]}


class DartIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.universe = pd.DataFrame([{"ticker": "000001", "corp_code": "12345678",
                                       "name": "테스트", "sector": "제조"}])

    def test_rejects_accounts_from_different_receipts(self):
        rows = raw_report()
        rows[1]["rcept_no"] = "20260814000999"
        self.assertIsNone(dart.parse_financial_payload({"status": "000", "list": rows},
                                                      "000001", "테스트", "제조", 2026, "11012", "CFS"))

    def test_unknown_standalone_is_not_cumulative_quarter(self):
        row = dart.parse_financial_payload({"status": "000", "list": raw_report()},
                                          "000001", "테스트", "제조", 2026, "11012", "CFS")
        self.assertFalse(row["quarter_value_verified"])
        self.assertEqual(row["receipt"], "20260814000123")

    def test_each_latest_quarter_uses_rolling_four_periods(self):
        reports = {}
        for year in (2025, 2026):
            total = 0
            for q in (1, 2, 3, 4):
                total += (year - 2024) * 10 + q
                reports[(year, dart.QUARTER_REPORTS[q])] = report(year, q, total * 10, total)

        def fetch(_universe, _key, _config, year, code):
            return pd.DataFrame([reports[(year, code)]]), []

        for quarter in (1, 2, 3, 4):
            latest = reports[(2026, dart.QUARTER_REPORTS[quarter])]
            with patch.object(dart, "_collect_bulk_period_values", side_effect=fetch):
                frame, failures = dart._attach_normalized_ttm(pd.DataFrame([latest]), self.universe, "unused", {})
            window = [pd.Period(f"2026Q{quarter}") - offset for offset in (3, 2, 1, 0)]
            expected = sum((p.year - 2024) * 10 + p.quarter for p in window)
            self.assertFalse(failures)
            self.assertEqual(frame.iloc[0]["normalized_ttm_op"], expected)
            self.assertEqual(frame.iloc[0]["normalization_as_of"], dart._period_end(2026, quarter))

    def test_correction_recomputes_even_with_complete_old_values(self):
        latest = report(2026, 2, 300, 30)
        latest.update(normalized_quarter_count=4, normalized_ttm_op=999,
                      normalization_as_of="2026-06-30")
        periods = {(2025, "11012"): report(2025, 2, 200, 20),
                   (2025, "11014"): report(2025, 3, 300, 30),
                   (2025, "11011"): report(2025, 4, 450, 45),
                   (2026, "11013"): report(2026, 1, 120, 12),
                   (2026, "11012"): report(2026, 2, 330, 33, receipt="corrected")}
        with patch.object(dart, "_collect_bulk_period_values", side_effect=lambda u, k, c, y, r: (pd.DataFrame([periods[(y, r)]]), [])):
            frame, failures = dart._attach_normalized_ttm(pd.DataFrame([latest]), self.universe, "unused", {})
        self.assertFalse(failures)
        self.assertEqual(frame.iloc[0]["normalized_ttm_op"], 58)
        self.assertIn("corrected", frame.iloc[0]["normalization_sources"])

    def test_basis_mismatch_preserves_original_window_and_date(self):
        latest = report(2026, 2, 300, 30)
        latest.update(normalized_quarter_count=4, normalized_ttm_op=48,
                      normalization_as_of="2026-03-31")
        with patch.object(dart, "_collect_bulk_period_values", side_effect=lambda u, k, c, y, r: (
            pd.DataFrame([report(y, dart.REPORT_QUARTERS[r], 100, 10, basis="OFS")]), [])):
            frame, failures = dart._attach_normalized_ttm(pd.DataFrame([latest]), self.universe, "unused", {})
        self.assertTrue(failures)
        self.assertEqual(frame.iloc[0]["normalized_ttm_op"], 48)
        self.assertEqual(frame.iloc[0]["normalization_as_of"], "2026-03-31")

    def test_response_cache_skips_calls_and_receipt_change_invalidates(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"cache_dir": folder, "dart_pause_seconds": 0, "_dart_receipt_generations": {"000001": "a"}}
            with patch.object(dart, "_request_json", return_value={"status": "000", "list": raw_report()}) as request:
                first, _ = dart._collect_bulk_period_values(self.universe, "unused", config, 2026, "11012")
                second, _ = dart._collect_bulk_period_values(self.universe, "unused", config, 2026, "11012")
                self.assertEqual(request.call_count, 1)
                self.assertEqual(first.iloc[0]["collected_at"], second.iloc[0]["collected_at"])
                config["_dart_receipt_generations"]["000001"] = "b"
                dart._collect_bulk_period_values(self.universe, "unused", config, 2026, "11012")
                self.assertEqual(request.call_count, 2)

    def test_failed_refresh_retains_values_and_original_verification_date(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"cache_dir": folder, "dart_pause_seconds": 0}
            with patch.object(dart, "_request_json", return_value={"status": "000", "list": raw_report()}):
                first, _ = dart._collect_bulk_period_values(self.universe, "unused", config, 2026, "11012")
            config["force_refresh"] = True
            config["_dart_refreshed_periods"] = []
            with patch.object(dart, "_request_json", side_effect=TimeoutError):
                second, failures = dart._collect_bulk_period_values(self.universe, "unused", config, 2026, "11012")
            self.assertTrue(failures)
            self.assertEqual(second.iloc[0]["op_current"], first.iloc[0]["op_current"])
            self.assertEqual(second.iloc[0]["last_verified_at"], first.iloc[0]["last_verified_at"])
            self.assertEqual(second.iloc[0]["verification_status"], "수집실패")

    def test_invalid_replacement_does_not_erase_verified_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"cache_dir": folder, "dart_pause_seconds": 0, "dart_single_fallback_limit": 0}
            with patch.object(dart, "_request_json", return_value={"status": "000", "list": raw_report()}):
                first, _ = dart._collect_bulk_period_values(self.universe, "unused", config, 2026, "11012")
            broken = raw_report()
            broken[1]["rcept_no"] = "different-report"
            config.update(force_refresh=True, _dart_refreshed_periods=[])
            with patch.object(dart, "_request_json", return_value={"status": "000", "list": broken}):
                second, failures = dart._collect_bulk_period_values(self.universe, "unused", config, 2026, "11012")
            self.assertTrue(failures)
            self.assertEqual(second.iloc[0]["receipt"], first.iloc[0]["receipt"])
            self.assertEqual(second.iloc[0]["collected_at"], first.iloc[0]["collected_at"])

    def test_partial_disclosure_scan_does_not_advance_watermark(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"cache_dir": folder, "dart_pause_seconds": 0}
            path = Path(folder) / "dart_disclosure_state.json"
            previous = {"checkedThrough": "2026-08-31", "receipts": {"000001": "a"}}
            path.write_text(json.dumps(previous), encoding="utf-8")
            pages = [{"status": "000", "total_page": 2, "list": [{"stock_code": "000001", "rcept_no": "b"}]}, TimeoutError()]
            with patch.object(dart, "_request_json", side_effect=pages):
                status = dart._scan_report_changes(self.universe, "unused", config)
            self.assertEqual(status["status"], "수집실패")
            self.assertEqual(json.loads(path.read_text("utf-8")), previous)
            self.assertEqual(config["_dart_receipt_generations"]["000001"], "b")


class KisIncrementalTests(unittest.TestCase):
    def test_dates_never_invent_day_or_today(self):
        self.assertIsNone(kis._date_text(None))
        self.assertIsNone(kis._date_text("20260230"))
        self.assertEqual(kis._date_text("202608"), "2026-08")
        self.assertEqual(kis.parse_estimate_payload(estimate(None), "000001")["as_of_precision"], "unknown")
        self.assertEqual(kis.parse_estimate_payload(estimate("202608"), "000001")["as_of"], "2026-08")

    def test_revision_does_not_compare_forecast_year_rollover(self):
        history = pd.DataFrame([{"estimate_period": "2025.12E", "forward_eps": 100,
                                 "fetched_at": "2026-08-20T18:00:00+09:00"}])
        self.assertTrue(np.isnan(kis._revision(150, history, 1, "2026.12E", ["2026-08-20", "2026-08-21"])))
        history["estimate_period"] = "2026.12E"
        self.assertAlmostEqual(kis._revision(150, history, 1, "2026.12E", ["2026-08-20", "2026-08-24"]), 50)

    def test_no_data_is_deferred_but_not_permanently_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"cache_dir": folder, "output_dir": folder, "kis_consensus_pause_seconds": 0,
                      "kis_consensus_batch_size": 1}
            prices = pd.DataFrame([{"ticker": "000001", "name": "테스트", "sector": "제조", "value": 1,
                                    "date": pd.Timestamp("2026-09-04")}])
            client = Mock()
            client.fetch_estimate.return_value = {"rt_cd": "0"}
            with patch.object(kis.KisConsensusClient, "from_environment", return_value=client):
                kis.collect_kis_consensus(prices, config)
                kis.collect_kis_consensus(prices, config)
                self.assertEqual(client.fetch_estimate.call_count, 1)
                config["force_full_refresh"] = True
                kis.collect_kis_consensus(prices, config)
                self.assertEqual(client.fetch_estimate.call_count, 2)
            state = json.loads((Path(folder) / "kis_consensus_state.json").read_text("utf-8"))
            self.assertEqual(state["byTicker"]["000001"]["status"], "미제공")
            self.assertIn("nextCheckAt", state["byTicker"]["000001"])

    def test_covered_cache_keeps_provider_date_and_saves_forecast_period_history(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"cache_dir": folder, "output_dir": folder, "kis_consensus_pause_seconds": 0,
                      "kis_consensus_batch_size": 1}
            prices = pd.DataFrame([{"ticker": "000001", "name": "테스트", "sector": "제조", "value": 1,
                                    "date": pd.Timestamp("2026-09-04")}])
            client = Mock()
            client.fetch_estimate.return_value = estimate("202608")
            with patch.object(kis.KisConsensusClient, "from_environment", return_value=client):
                first, report_one = kis.collect_kis_consensus(prices, config)
                second, report_two = kis.collect_kis_consensus(prices, config)
            self.assertEqual(client.fetch_estimate.call_count, 1)
            self.assertEqual(report_two["asOfDate"], "2026-08")
            self.assertEqual(report_two["newlyFetchedTickers"], [])
            self.assertEqual(first.iloc[0]["last_verified_at"], second.iloc[0]["last_verified_at"])
            history = pd.read_csv(Path(folder) / "kis_consensus_history.csv")
            self.assertEqual(history.iloc[0]["estimate_period"], "2026.12E")

    def test_transient_failure_retries_and_does_not_mark_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"cache_dir": folder, "output_dir": folder, "kis_consensus_pause_seconds": 0,
                      "kis_consensus_batch_size": 1}
            prices = pd.DataFrame([{"ticker": "000001", "name": "테스트", "sector": "제조", "value": 1,
                                    "date": pd.Timestamp("2026-09-04")}])
            client = Mock()
            client.fetch_estimate.side_effect = [TimeoutError(), estimate()]
            with patch.object(kis.KisConsensusClient, "from_environment", return_value=client):
                frame, status = kis.collect_kis_consensus(prices, config)
            self.assertEqual(len(frame), 1)
            self.assertEqual(client.fetch_estimate.call_count, 2)
            self.assertEqual(status["failed"], 0)
            self.assertEqual(status["unavailable"], 0)

    def test_authentication_reuses_unexpired_token_in_process(self):
        client = kis.KisConsensusClient("test", "test")
        client.access_token = "not-a-real-token"
        client.token_expires_at = kis.time.monotonic() + 600
        with patch.object(kis.urllib.request, "urlopen") as request:
            client.authenticate()
        request.assert_not_called()

    def test_legacy_history_is_retained_without_inventing_forecast_year(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"cache_dir": folder, "output_dir": folder, "kis_consensus_pause_seconds": 0,
                      "kis_consensus_batch_size": 1}
            pd.DataFrame([{"ticker": "000001", "forward_eps": 90, "fetched_at": "2026-08-20T18:00:00+09:00"},
                          {"ticker": "000001", "forward_eps": 100, "fetched_at": "2026-08-21T18:00:00+09:00"}]).to_csv(
                Path(folder) / "kis_consensus_history.csv", index=False)
            prices = pd.DataFrame([{"ticker": "000001", "name": "테스트", "sector": "제조", "value": 1,
                                    "date": pd.Timestamp("2026-09-04")}])
            client = Mock()
            client.fetch_estimate.return_value = estimate()
            with patch.object(kis.KisConsensusClient, "from_environment", return_value=client):
                frame, _ = kis.collect_kis_consensus(prices, config)
            history = pd.read_csv(Path(folder) / "kis_consensus_history.csv")
            self.assertEqual(len(history), 3)
            self.assertEqual(history["estimate_period"].isna().sum(), 2)
            self.assertTrue(np.isnan(frame.iloc[0]["consensus_change_1d"]))


if __name__ == "__main__":
    unittest.main()
