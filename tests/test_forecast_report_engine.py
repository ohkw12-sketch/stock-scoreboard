import argparse
import json
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from datetime import date
from pathlib import Path

from forecast_report_engine import (
    ForecastObservation,
    NaverResearchRecord,
    ReportRecord,
    SourceRateLimited,
    _request_bytes,
    _extract_labeled_text,
    _extract_structured_tables,
    _pdf_identity_matches,
    aggregate_consensus,
    classify_uncovered_tickers,
    load_cached_report_index,
    merge_reprocessed_observations,
    naver_research_detail_url,
    parse_naver_pdf_detail,
    parse_hankyung_index_page,
    parse_fnguide_consensus,
    parse_naver_research_items,
    parse_last_page,
    select_latest_reports,
    select_naver_pdf_backfill,
    extract_naver_summary,
)


class ForecastReportEngineTest(unittest.TestCase):
    def record(self, **changes):
        base = dict(
            report_id="652448",
            report_date="2026-09-21",
            ticker="195870",
            name="해성디에스",
            title="해성디에스(195870) 마지막 약점을 채우다",
            broker="메리츠증권",
            analyst="양승수",
            pdf_url="https://consensus.hankyung.com/analysis/downpdf?report_idx=652448",
        )
        base.update(changes)
        return ReportRecord(**base)

    def naver_record(self, **changes):
        base = dict(
            report_id="naver-96260", report_date="2026-09-21", ticker="028050",
            name="삼성E&A", title="첨단의 끝판왕", broker="유안타증권",
            content=(
                "매출액은 2026년 10.8조원, 2027년 12.9조원, "
                "영업이익은 각각 9,776억원, 1조 1,627억원으로 증가할 전망이다."
            ),
            source_url="https://stock.naver.com/research/company/96260",
        )
        base.update(changes)
        return NaverResearchRecord(**base)

    def test_parses_index_and_excludes_nonstandard_codes(self):
        page = """
        <table><tr class="first">
          <td class="first txt_number">2026-09-21</td>
          <td><a href="/analysis/downpdf?report_idx=652448">해성디에스(195870) 마지막 약점을 채우다</a></td>
          <td>0</td><td>BUY</td><td>양승수</td><td>메리츠증권</td><td></td>
        </tr><tr>
          <td>2026-09-20</td><td><a href="/analysis/downpdf?report_idx=1">아크릴(0007C0) AI</a></td>
          <td>0</td><td>-</td><td>A</td><td>B</td>
        </tr></table>
        <a href="/analysis/list?now_page=12" class="btn last">last</a>
        """
        rows, skipped = parse_hankyung_index_page(page)
        self.assertEqual(parse_last_page(page), 12)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ticker, "195870")
        self.assertEqual(rows[0].broker, "메리츠증권")
        self.assertEqual(len(skipped), 1)

    def test_parses_naver_research_item(self):
        rows = parse_naver_research_items([{
            "nid": "96260", "writeDate": "2026-09-21", "itemCode": "028050",
            "itemName": "삼성E&A", "title": "첨단의 끝판왕", "brokerName": "유안타증권",
            "content": "<p>2026년 영업이익 9,776억원</p>",
        }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].report_id, "naver-96260")
        self.assertEqual(rows[0].content, "2026년 영업이익 9,776억원")

    def test_parses_naver_original_pdf_detail(self):
        detail = parse_naver_pdf_detail(self.naver_record(), {
            "nid": "96260", "writeDate": "2026-09-21", "itemCode": "028050",
            "itemName": "삼성E&A", "title": "첨단의 끝판왕",
            "brokerName": "유안타증권",
            "attachUrl": "https://stock.pstatic.net/stock-research/company/18/report.pdf",
        })
        self.assertEqual(detail.report_id, "naver-pdf-96260")
        self.assertEqual(detail.ticker, "028050")
        self.assertTrue(detail.pdf_url.endswith("report.pdf"))
        self.assertEqual(
            naver_research_detail_url("naver-96260"),
            "https://stock.naver.com/api/stockSecurity/researches/v2/company/96260",
        )

    def test_rejects_naver_pdf_detail_for_another_ticker(self):
        with self.assertRaisesRegex(ValueError, "종목코드 불일치"):
            parse_naver_pdf_detail(self.naver_record(), {
                "nid": "96260", "itemCode": "000000",
                "attachUrl": "https://stock.pstatic.net/report.pdf",
            })

    def test_selects_recent_naver_pdf_backfill_only_for_targets(self):
        rows = [
            self.naver_record(report_id="naver-3", report_date="2026-09-20"),
            self.naver_record(report_id="naver-2", report_date="2026-09-10"),
            self.naver_record(report_id="naver-1", report_date="2026-05-01"),
            self.naver_record(report_id="naver-9", ticker="000001", report_date="2026-09-21"),
            self.naver_record(report_id="naver-8", broker="대신증권", report_date="2026-09-19"),
        ]
        selected = select_naver_pdf_backfill(
            rows, {"028050"}, as_of=date(2026, 9, 21), freshness_days=90,
            per_ticker_broker=1,
        )
        self.assertEqual({row.report_id for row in selected}, {"naver-3", "naver-8"})

    def test_parses_fnguide_public_aggregate_consensus(self):
        page = '''<span>단위 : 억원, 배</span><script>
        consensus.init({
          perforTrend: {"data":[
            {"NAME":"매출액","VAL1":"44434.00","VAL2":"45805.00"},
            {"NAME":"영업이익","VAL1":"1919.00","VAL2":"2028.00"}],
            "header":[
              {"YYMM":"2026/12","EP_CHK":"E","CD":"VAL1"},
              {"YYMM":"2027/12","EP_CHK":"E","CD":"VAL2"}]},
          cnsTrend: {"data":[],"header":[{"ID":"ACC_NM","NM":"2026/09/18"}]}
        });</script>'''
        rows, audit = parse_fnguide_consensus(
            "001680", "대상", page, as_of=date(2026, 9, 21), freshness_days=90,
        )
        self.assertEqual(
            [(row.period, row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows],
            [("2026FY", 44434.0, 1919.0), ("2027FY", 45805.0, 2028.0)],
        )
        self.assertEqual(rows[0].broker, "FnGuide 집계")
        self.assertEqual(audit["reportDate"], "2026-09-18")

    def test_fnguide_aggregate_has_separate_consensus_status(self):
        item = ForecastObservation(
            ticker="001680", name="대상", broker="FnGuide 집계",
            report_id="fnguide-001680-20260918", report_date="2026-09-18",
            period="2026FY", scope="annual", sales_krw_100m=44434.0,
            operating_profit_krw_100m=1919.0,
            source_url="https://kwcomp.fnguide.com/CompanyInfo/Consensus?cmp_cd=001680",
            extraction_method="FnGuide 공개 집계 컨센서스", confidence=0.9, stale=False,
        )
        rows = aggregate_consensus([item], as_of=date(2026, 9, 21))
        self.assertEqual(rows[0]["status"], "외부 집계 컨센서스")

    def test_extracts_metric_first_naver_summary_with_each_pair(self):
        rows, audit = extract_naver_summary(
            self.naver_record(), as_of=date(2026, 9, 21), freshness_days=90
        )
        values = {row.period: (row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows}
        self.assertEqual(values["2026FY"], (108000.0, 9776.0))
        self.assertEqual(values["2027FY"], (129000.0, 11627.0))
        self.assertEqual(audit["status"], "정상")

    def test_extracts_period_first_naver_summary_and_loss(self):
        record = self.naver_record(content=(
            "2026년 매출액은 4.0조원, 영업이익 5,404억원을 전망한다. "
            "2027년 매출액 4.7조원, 영업손실 120억원으로 추정한다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        values = {row.period: (row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows}
        self.assertEqual(values["2026FY"], (40000.0, 5404.0))
        self.assertEqual(values["2027FY"], (47000.0, -120.0))

    def test_extracts_naver_each_pair_with_trillion_sales(self):
        record = self.naver_record(content=(
            "BGF리테일의 2Q26 연결 기준 매출 및 영업이익은 각각 "
            "2.4조원, 849억원으로 전망치를 상회했다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.period, row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows],
            [("2026Q2", 24000.0, 849.0)],
        )

    def test_extracts_naver_slash_pair_and_dotted_thousands(self):
        slash = self.naver_record(content=(
            "2분기 매출액/영업이익은 1.18조원/88억원이었다."
        ))
        dotted = self.naver_record(content=(
            "2Q26 매출액 및 영업이익은 각각 1조 2,902억원, 4.109억원이었다."
        ))
        slash_rows, _audit = extract_naver_summary(
            slash, as_of=date(2026, 9, 21), freshness_days=90
        )
        dotted_rows, _audit = extract_naver_summary(
            dotted, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.sales_krw_100m, row.operating_profit_krw_100m) for row in slash_rows],
            [(11800.0, 88.0)],
        )
        self.assertEqual(
            [(row.sales_krw_100m, row.operating_profit_krw_100m) for row in dotted_rows],
            [(12902.0, 4109.0)],
        )

    def test_rejects_malformed_thousand_unit_and_segment_only_profit(self):
        malformed = self.naver_record(content=(
            "1Q26 매출액 및 영업이익은 각각 9,349천억원, 1,691억원을 기록했다."
        ))
        segment_only = self.naver_record(content=(
            "2027년 연간 영업이익에 약 300억원 기여할 전망이다."
        ))
        malformed_rows, malformed_audit = extract_naver_summary(
            malformed, as_of=date(2026, 9, 21), freshness_days=300
        )
        segment_rows, segment_audit = extract_naver_summary(
            segment_only, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(malformed_rows, [])
        self.assertEqual(segment_rows, [])
        self.assertEqual(malformed_audit["observationCount"], 0)
        self.assertEqual(segment_audit["observationCount"], 0)

    def test_accepts_direct_period_company_op_without_sales_at_lower_confidence(self):
        annual = self.naver_record(content=(
            "목표주가를 상향한다. 2026년 영업이익은 1.52조원으로 전망한다."
        ))
        op_label = self.naver_record(content=(
            "26년 OP 추정치를 128조원으로 17.9% 상향했다."
        ))
        annual_rows, _audit = extract_naver_summary(
            annual, as_of=date(2026, 9, 21), freshness_days=90
        )
        op_rows, _audit = extract_naver_summary(
            op_label, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.period, row.sales_krw_100m, row.operating_profit_krw_100m) for row in annual_rows],
            [("2026FY", None, 15200.0)],
        )
        self.assertEqual(
            [(row.period, row.sales_krw_100m, row.operating_profit_krw_100m) for row in op_rows],
            [("2026FY", None, 1280000.0)],
        )
        self.assertLess(annual_rows[0].confidence, 0.92)

    def test_monthly_company_result_is_not_mislabeled_as_full_year(self):
        record = self.naver_record(content=(
            "오리온의 2026년 7월 주요 법인 합산 매출액은 3,036억원, "
            "영업이익은 459억원을 기록했다."
        ))
        rows, audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(rows, [])
        self.assertEqual(audit["observationCount"], 0)

    def test_half_year_profit_is_not_mislabeled_as_full_year(self):
        record = self.naver_record(content=(
            "2026년 하반기 영업이익은 1,700억원으로 상반기 대비 증가할 전망이다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(rows, [])

    def test_segment_sales_from_another_sentence_is_not_paired_with_company_op(self):
        record = self.naver_record(content=(
            "소캠 매출은 '28년에 7,000~8,000억원에 이를 전망이다. "
            "당사는 '28년 영업이익을 5,420억원으로 전망한다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.period, row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows],
            [("2028FY", None, 5420.0)],
        )

    def test_maps_each_multi_year_op_value_to_its_year(self):
        record = self.naver_record(content=(
            "'26년과 '27년 영업이익 전망치를 각각 2,410억원, 3,820억원으로 전망한다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.period, row.operating_profit_krw_100m) for row in rows],
            [("2026FY", 2410.0), ("2027FY", 3820.0)],
        )

    def test_comparison_each_after_amount_does_not_remap_periods(self):
        record = self.naver_record(content=(
            "2026년 2Q 영업이익은 628.9억원으로 종전 추정 465억원과 "
            "컨센서스 473억원을 각각 35%, 33% 상회했다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.period, row.operating_profit_krw_100m) for row in rows],
            [("2026Q2", 628.9)],
        )

    def test_generic_combo_heading_does_not_pair_intervening_sales_number_as_op(self):
        record = self.naver_record(content=(
            "2Q26 Review: 매출 및 영업이익 모두 기대치 상회 "
            "매출 1,360억원으로 9분기 연속 분기 매출 1,000억원 이상 기록) "
            "영업이익 216억원으로 시장 기대치를 상회했다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows],
            [(1360.0, 216.0)],
        )

    def test_specific_company_sales_replaces_project_sales_in_same_sentence(self):
        record = self.naver_record(content=(
            "4Q26은 프로젝트 매출 430억원이 인식되며 매출액 2,318억원, "
            "영업이익 508억원을 전망한다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows],
            [(2318.0, 508.0)],
        )

    def test_amount_immediately_before_loss_is_not_used_as_sales(self):
        record = self.naver_record(content=(
            "2분기 연결 매출은 전년 대비 증가했으나, 430억원 영업손실을 냈다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows],
            [],
        )

    def test_implausible_positive_margin_pair_falls_back_to_direct_op(self):
        record = self.naver_record(content=(
            "올해 2분기 제품 매출 120억원으로 증가했다. "
            "올해 2분기 동사 매출액은 단위 표기 누락, 영업이익 117억원을 기록했다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.sales_krw_100m, row.operating_profit_krw_100m) for row in rows],
            [(None, 117.0)],
        )

    def test_relative_quarter_is_not_also_emitted_as_full_year(self):
        record = self.naver_record(content=(
            "올해 2분기 동사 영업이익은 117억원을 기록했다."
        ))
        rows, _audit = extract_naver_summary(
            record, as_of=date(2026, 9, 21), freshness_days=90
        )
        self.assertEqual(
            [(row.period, row.operating_profit_krw_100m) for row in rows],
            [("2026Q2", 117.0)],
        )

    def test_selects_latest_two_per_ticker_and_broker(self):
        rows = [
            self.record(report_id="3", report_date="2026-09-03"),
            self.record(report_id="2", report_date="2026-09-02"),
            self.record(report_id="1", report_date="2026-09-01"),
            self.record(report_id="9", report_date="2026-08-01", broker="대신증권"),
        ]
        selected = select_latest_reports(rows, 2)
        self.assertEqual({row.report_id for row in selected}, {"3", "2", "9"})

    def test_pdf_identity_requires_indexed_ticker(self):
        self.assertTrue(_pdf_identity_matches("해성디에스 (195870) 보고서", self.record()))
        self.assertTrue(_pdf_identity_matches("해성디에스 기업분석 보고서", self.record()))
        self.assertFalse(_pdf_identity_matches("한섬 (020000) 보고서", self.record()))

    def test_http_403_stops_collection_without_retries(self):
        error = urllib.error.HTTPError("https://example.com", 403, "Forbidden", {}, None)
        with patch("forecast_report_engine.urllib.request.urlopen", side_effect=error) as opened:
            with self.assertRaises(SourceRateLimited):
                _request_bytes("https://example.com", attempts=3)
        self.assertEqual(opened.call_count, 1)

    def test_loads_cached_index_for_requested_date_range(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report_index.json"
            rows = [
                self.record(report_id="1", report_date="2025-12-31"),
                self.record(report_id="2", report_date="2026-01-02"),
                self.record(report_id="3", report_date="2026-09-21"),
            ]
            path.write_text(
                json.dumps([row.__dict__ for row in rows], ensure_ascii=False),
                encoding="utf-8",
            )
            cached, audit = load_cached_report_index(
                path, "2026-01-01", "2026-09-21", source_error="HTTP 403"
            )
        self.assertEqual([row.report_id for row in cached], ["3", "2"])
        self.assertEqual(audit["sourceMode"], "cached-fallback")
        self.assertEqual(audit["reportCount"], 2)

    def test_extracts_labeled_annual_table_and_converts_bn_to_100m(self):
        text = """(단위: 십억 원, 원, %, 배)
재무정보 2024 2025 2026E 2027E
매출액 4,044 4,216 4,512 4,739
영업이익 157 110 220 252
EBITDA 361 321 448 493
"""
        rows = _extract_labeled_text(self.record(), [text], 10.0, date(2026, 9, 21), 90)
        values = {row.period: row.operating_profit_krw_100m for row in rows}
        self.assertEqual(values, {"2026FY": 2200.0, "2027FY": 2520.0})

    def test_labeled_table_uses_header_alignment_not_trailing_prose_year(self):
        text = """재무정보 2024 2025 2026E 2027E
매출액 4,044 4,216 4,512 4,739 판매는 2025년 이후 회복
영업이익 157 110 220 252
"""
        rows = _extract_labeled_text(self.record(), [text], 10.0, date(2026, 9, 21), 90)
        sales = {row.period: row.sales_krw_100m for row in rows}
        self.assertEqual(sales, {"2026FY": 45120.0, "2027FY": 47390.0})

    def test_labeled_tables_use_each_pages_explicit_unit(self):
        pages = [
            """(십억원, 원, %, 배)\n2025 2026E\n매출액 180,000 190,000\n영업이익 10,000 11,000""",
            """(단위: 억원)\n2025 2026F\n매출액 1,800,000 1,900,000\n영업이익 100,000 110,000""",
        ]
        rows = _extract_labeled_text(self.record(), pages, 10.0, date(2026, 9, 21), 90)
        candidates = [row for row in rows if row.period == "2026FY"]
        values = [row.operating_profit_krw_100m for row in candidates]
        self.assertEqual(values, [110000.0, 110000.0])
        self.assertGreater(candidates[1].confidence, candidates[0].confidence)

    def test_labeled_table_skips_repeated_old_new_consensus_periods(self):
        text = """3Q26E 4Q26E 2026E 2027E 3Q26E 4Q26E 2026E 2027E
매출액 1 2 3 4 5 6 7 8
영업이익 1 2 3 4 5 6 7 8
"""
        self.assertEqual(
            _extract_labeled_text(self.record(), [text], 10.0, date(2026, 9, 21), 90),
            [],
        )

    def test_extracts_income_statement_beside_balance_sheet(self):
        text = """추정재무제표 (K-IFRS 연결)
손익계산서 (단위: 억원) 재무상태표 (단위: 억원)
결산 2024A 2025A 2026F 2027F 결산 2024A 2025A 2026F 2027F
매출액 38,851 42,528 47,387 51,500 유동자산 17,348 20,037 20,724 22,669
영업이익 2,205 3,358 4,960 5,570 비유동자산 50,487 49,575 48,804 48,109
"""
        rows = _extract_labeled_text(self.record(), [text], 10.0, date(2026, 9, 21), 90)
        values = {row.period: row.operating_profit_krw_100m for row in rows}
        self.assertEqual(values, {"2026FY": 4960.0, "2027FY": 5570.0})

    def test_labeled_table_skips_short_header_with_revision_blocks(self):
        text = """2026E 2027E
매출액 24,507.8 25,465.2 -3.8 25,910.3 26,000.0 -0.3
영업이익 1,087.8 1,278.7 -14.9 1,364.3 1,533.7 -11.0
"""
        self.assertEqual(
            _extract_labeled_text(self.record(), [text], 10.0, date(2026, 9, 21), 90),
            [],
        )

    def test_labeled_table_skips_when_profit_values_are_fewer_than_periods(self):
        text = """(단위: 십억원)
2024 2025 2026E 2027E
매출액 1,000 1,100 1,200 1,300
영업이익 100 110 120
"""
        self.assertEqual(
            _extract_labeled_text(self.record(), [text], 10.0, date(2026, 9, 21), 90),
            [],
        )

    def test_year_row_rejects_peer_comparison_columns(self):
        text = """시가총액 P/E P/B EPS 증가율 ROE 매출액 영업이익 EV/EBITDA
2027E 2028E 2027E 2028E 2027E 2028E 2027E 2028E 2,521 2,892 388 553 9.9 7.9
"""
        self.assertEqual(
            _extract_labeled_text(self.record(), [text], 10.0, date(2026, 9, 21), 90),
            [],
        )

    def test_extracts_compact_year_rows(self):
        text = """(단위: 십억원)
연도 매출액 영업이익 순이익 EPS PER PBR ROE EV/EBITDA
2026E 2,000 240 180 100 10 2 20 8
2027E 2,400 380 300 150 8 1.8 22 6
"""
        rows = _extract_labeled_text(self.record(), [text], 10.0, date(2026, 9, 21), 90)
        values = {row.period: row.operating_profit_krw_100m for row in rows}
        self.assertEqual(values, {"2026FY": 2400.0, "2027FY": 3800.0})

    def test_extracts_meritz_quarter_and_annual_revision_tables(self):
        tables = [
            [
                ["3Q26E", "3Q25 (% YoY) 2Q26 (% QoQ)", "컨센서스"],
                ["250.2", "178.6", "241.6"],
                ["34.5", None, None],
                ["36.8", None, None],
            ],
            [
                ["2026E", None, None],
                ["신규 추정치", "기존", "신규 추정치"],
                ["894.2", "876.4", "1,088.7"],
                ["102.1", None, "165.3"],
            ],
        ]
        rows = _extract_structured_tables(
            self.record(), tables, 10.0, "메리츠 기업보고서 기본단위 십억원 추론",
            date(2026, 9, 21), 90,
        )
        values = {row.period: row.operating_profit_krw_100m for row in rows}
        self.assertEqual(values["2026Q3"], 345.0)
        self.assertEqual(values["2026FY"], 1021.0)
        self.assertEqual(values["2027FY"], 1653.0)

    def test_aggregates_latest_per_broker_and_labels_consensus(self):
        def observation(broker, value, report_date="2026-09-01"):
            return ForecastObservation(
                ticker="195870", name="해성디에스", broker=broker,
                report_id=f"{broker}-{report_date}", report_date=report_date,
                period="2026Q3", scope="quarter", sales_krw_100m=2500,
                operating_profit_krw_100m=value, source_url=f"https://example.com/{broker}",
                extraction_method="test", confidence=0.95, stale=False,
            )
        rows = [
            observation("A", 300, "2026-08-01"), observation("A", 330, "2026-09-01"),
            observation("B", 340), observation("C", 350),
        ]
        result = aggregate_consensus(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "유효 컨센서스")
        self.assertEqual(result[0]["freshBrokerCount"], 3)
        self.assertEqual(result[0]["operatingProfitMedianKrw100m"], 340.0)

    def test_current_consensus_excludes_finished_quarters(self):
        row = ForecastObservation(
            ticker="195870", name="해성디에스", broker="A", report_id="1",
            report_date="2026-08-15", period="2026Q2", scope="quarter",
            sales_krw_100m=2500, operating_profit_krw_100m=300,
            source_url="https://example.com/1", extraction_method="test",
            confidence=0.95, stale=False,
        )
        self.assertEqual(aggregate_consensus([row], as_of=date(2026, 9, 21)), [])

    def test_current_consensus_stops_at_next_year(self):
        row = ForecastObservation(
            ticker="195870", name="해성디에스", broker="A", report_id="1",
            report_date="2026-09-01", period="2028FY", scope="annual",
            sales_krw_100m=4000, operating_profit_krw_100m=500,
            source_url="https://example.com/1", extraction_method="test",
            confidence=0.95, stale=False,
        )
        self.assertEqual(aggregate_consensus([row], as_of=date(2026, 9, 21)), [])

    def test_consensus_excludes_old_relative_estimate_and_numeric_outlier(self):
        def observation(broker, value, report_date):
            return ForecastObservation(
                ticker="195870", name="해성디에스", broker=broker,
                report_id=f"{broker}-{report_date}", report_date=report_date,
                period="2027FY", scope="annual", sales_krw_100m=2500,
                operating_profit_krw_100m=value,
                source_url=f"https://example.com/{broker}", extraction_method="test",
                confidence=0.95, stale=False,
            )
        rows = [
            observation("OLD", 320, "2026-06-01"),
            observation("A", 330, "2026-09-01"),
            observation("B", 340, "2026-09-02"),
            observation("BAD", 5000, "2026-09-03"),
        ]
        result = aggregate_consensus(rows, as_of=date(2026, 9, 21))
        self.assertEqual(result[0]["freshBrokerCount"], 2)
        self.assertEqual(result[0]["operatingProfitMedianKrw100m"], 335.0)
        self.assertEqual(result[0]["excludedEstimateCount"], 2)
        reasons = {row["reason"] for row in result[0]["excludedEstimates"]}
        self.assertEqual(reasons, {
            "같은 기간 최신 보고서보다 오래된 추정치",
            "다중 증권사 중앙값 대비 수치 이상치",
        })

    def test_two_broker_large_gap_is_marked_for_review(self):
        rows = [
            ForecastObservation(
                ticker="365340", name="성일하이텍", broker=broker,
                report_id=broker, report_date="2026-09-01", period="2027FY",
                scope="annual", sales_krw_100m=3000,
                operating_profit_krw_100m=value,
                source_url=f"https://example.com/{broker}", extraction_method="test",
                confidence=0.95, stale=False,
            )
            for broker, value in (("A", 10), ("B", 50))
        ]
        result = aggregate_consensus(rows, as_of=date(2026, 9, 21))
        self.assertEqual(result[0]["status"], "불일치 검토")

    def test_stale_and_low_confidence_values_do_not_enter_consensus(self):
        rows = [
            ForecastObservation(
                ticker="000001", name="A", broker="A", report_id="1", report_date="2026-01-01",
                period="2027FY", scope="annual", sales_krw_100m=100,
                operating_profit_krw_100m=10, source_url="https://example.com/1",
                extraction_method="test", confidence=0.99, stale=True,
            ),
            ForecastObservation(
                ticker="000001", name="A", broker="B", report_id="2", report_date="2026-09-01",
                period="2027FY", scope="annual", sales_krw_100m=100,
                operating_profit_krw_100m=10, source_url="https://example.com/2",
                extraction_method="test", confidence=0.5, stale=False,
            ),
        ]
        self.assertEqual(aggregate_consensus(rows), [])

    def test_reprocessed_report_removes_old_false_positive(self):
        old = ForecastObservation(
            ticker="222800", name="심텍", broker="메리츠증권", report_id="bad-report",
            report_date="2026-08-26", period="2027FY", scope="annual",
            sales_krw_100m=20280, operating_profit_krw_100m=20270,
            source_url="https://example.com/bad", extraction_method="old parser",
            confidence=0.94, stale=False,
        )
        previous = {(old.report_id, old.ticker, old.period, old.broker): old}
        merged = merge_reprocessed_observations(previous, [], {"bad-report"})
        self.assertEqual(merged, {})

    def test_source_blocked_ticker_is_pending_not_missing(self):
        missing, pending = classify_uncovered_tickers(
            {"000001": "A", "000002": "B", "000003": "C"},
            {"000001"},
            [
                {"ticker": "000002", "retryable": True},
                {"ticker": "000003", "retryable": False},
            ],
        )
        self.assertEqual([row["ticker"] for row in pending], ["000002"])
        self.assertEqual([row["ticker"] for row in missing], ["000003"])


if __name__ == "__main__":
    unittest.main()
