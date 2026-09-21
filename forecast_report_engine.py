"""Standalone brokerage-report forecast collector.

This module deliberately does not import or mutate the scoreboard engine.  It
builds a separate, auditable store of report-level revenue and operating-profit
estimates from Naver Finance research summaries, linked brokerage PDFs,
Hankyung Consensus PDFs, and a clearly labeled FnGuide aggregate fallback.  A
later integration step can decide how (or whether) to use those facts.

PDFs are kept only in the ignored cache directory; outputs contain extracted
numeric facts and source links, never copies of the reports.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import json
import logging
import math
import os
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence


KST = timezone(timedelta(hours=9))
logging.getLogger("pdfminer").setLevel(logging.ERROR)
HANKYUNG_INDEX_URL = "https://consensus.hankyung.com/analysis/list"
HANKYUNG_BASE_URL = "https://consensus.hankyung.com"
NAVER_RESEARCH_API = "https://stock.naver.com/api/stockSecurity/researches/v2/company"
NAVER_RESEARCH_BASE = "https://stock.naver.com/research/company"
FNGUIDE_CONSENSUS_BASE = "https://kwcomp.fnguide.com/CompanyInfo/Consensus"
DEFAULT_USER_AGENT = "stock-scoreboard-forecast-engine/1.0 (read-only research collector)"
NAVER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
YEAR_PERIOD_RE = re.compile(r"^(20\d{2})[EF]$", re.I)
QUARTER_PERIOD_RE = re.compile(r"^([1-4])Q(\d{2})[EF]$", re.I)
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\(?[-+]?\d[\d,]*(?:\.\d+)?\)?")
KOREAN_AMOUNT_RE = re.compile(
    r"(?P<sign>[-+])?\s*(?:"
    r"(?P<jo>\d[\d,]*(?:\.\d+)?)\s*조(?:\s*원)?(?:\s*(?P<jo_eok>\d[\d,]*(?:\.\d+)?)\s*억(?:\s*원)?)?"
    r"|(?P<cheon>\d[\d,]*(?:\.\d+)?)\s*천억(?:\s*원)?"
    r"|(?P<plain>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>십억원|백만원|억원|억\s*원|억)"
    r")"
)
PERIOD_MENTION_RE = re.compile(
    r"(?P<relative>올해|금년|내년)(?:\s*(?P<relativequarter>[1-4])\s*분기)?"
    r"|(?P<year>20\d{2}|['’]?\d{2})\s*년(?:\s*(?P<quarter>[1-4])\s*분기)?"
    r"|(?P<q>[1-4])Q\s*(?P<qyear>20\d{2}|\d{2})?"
    r"|(?P<barequarter>[1-4])\s*분기",
    re.I,
)


class SourceRateLimited(RuntimeError):
    """The remote report source asked the collector to stop."""


@dataclass(frozen=True)
class ReportRecord:
    report_id: str
    report_date: str
    ticker: str
    name: str
    title: str
    broker: str
    analyst: str
    pdf_url: str


@dataclass(frozen=True)
class ForecastObservation:
    ticker: str
    name: str
    broker: str
    report_id: str
    report_date: str
    period: str
    scope: str
    sales_krw_100m: float | None
    operating_profit_krw_100m: float
    source_url: str
    extraction_method: str
    confidence: float
    stale: bool


@dataclass(frozen=True)
class NaverResearchRecord:
    report_id: str
    report_date: str
    ticker: str
    name: str
    title: str
    broker: str
    content: str
    source_url: str


def _json_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _json_read(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _csv_write(path: Path, rows: Sequence[dict], fieldnames: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _strip_html(fragment: str) -> str:
    fragment = re.sub(r"<script\b.*?</script>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b.*?</style>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_lib.unescape(fragment)).strip()


def _decode_html(payload: bytes) -> str:
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _request_bytes(
    url: str,
    timeout: int = 30,
    attempts: int = 3,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    headers: dict[str, str] | None = None,
) -> bytes:
    request_headers = {"User-Agent": user_agent}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    last_error = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 429}:
                raise SourceRateLimited(f"source returned HTTP {exc.code}: {url}") from exc
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.7 * (2**attempt))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.7 * (2**attempt))
    raise RuntimeError(f"fetch failed after {attempts} attempts: {url}: {last_error}")


def hankyung_page_url(start_date: str, end_date: str, page: int) -> str:
    query = urllib.parse.urlencode({
        "skinType": "business",
        "sdate": start_date,
        "edate": end_date,
        "order_type": "",
        "now_page": int(page),
    })
    return f"{HANKYUNG_INDEX_URL}?{query}"


def parse_last_page(page_html: str) -> int:
    matches = re.findall(r"now_page=(\d+)[^\"]*\"\s+class=\"btn last\"", page_html, flags=re.I)
    if matches:
        return max(int(value) for value in matches)
    pages = [int(value) for value in re.findall(r"[?&]now_page=(\d+)", page_html)]
    return max(pages, default=1)


def parse_hankyung_index_page(page_html: str) -> tuple[list[ReportRecord], list[dict]]:
    records: list[ReportRecord] = []
    skipped: list[dict] = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page_html, flags=re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.I | re.S)
        if len(cells) < 2:
            continue
        report_date = _strip_html(cells[0])
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", report_date):
            continue
        anchor = re.search(
            r"href=\"(/analysis/downpdf\?report_idx=(\d+))\"[^>]*>(.*?)</a>",
            row,
            flags=re.I | re.S,
        )
        if not anchor:
            continue
        report_id = anchor.group(2)
        title = _strip_html(anchor.group(3))
        code_match = re.search(r"\(([0-9A-Z]{6})\)", title, flags=re.I)
        if not code_match or not code_match.group(1).isdigit():
            skipped.append({
                "reportId": report_id,
                "reportDate": report_date,
                "title": title,
                "reason": "정상 6자리 상장 종목코드 없음",
            })
            continue
        ticker = code_match.group(1)
        name = title[: code_match.start()].strip()
        analyst = _strip_html(cells[4]) if len(cells) > 4 else ""
        broker = _strip_html(cells[5]) if len(cells) > 5 else ""
        records.append(ReportRecord(
            report_id=report_id,
            report_date=report_date,
            ticker=ticker,
            name=name,
            title=title,
            broker=broker or "제공처 미확인",
            analyst=analyst,
            pdf_url=urllib.parse.urljoin(HANKYUNG_BASE_URL, anchor.group(1)),
        ))
    return records, skipped


def collect_hankyung_index(
    start_date: str,
    end_date: str,
    *,
    workers: int = 4,
    timeout: int = 30,
) -> tuple[list[ReportRecord], list[dict], dict]:
    first_html = _decode_html(_request_bytes(hankyung_page_url(start_date, end_date, 1), timeout))
    last_page = parse_last_page(first_html)
    pages: dict[int, str] = {1: first_html}
    failures: list[dict] = []

    def fetch(page: int) -> tuple[int, str]:
        return page, _decode_html(_request_bytes(hankyung_page_url(start_date, end_date, page), timeout))

    if last_page > 1:
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 6))) as executor:
            futures = {executor.submit(fetch, page): page for page in range(2, last_page + 1)}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    page_number, page_html = future.result()
                    pages[page_number] = page_html
                except Exception as exc:  # per-page failure is retained for audit
                    failures.append({"page": page, "reason": str(exc)})

    by_id: dict[str, ReportRecord] = {}
    skipped: list[dict] = []
    for page in sorted(pages):
        rows, page_skipped = parse_hankyung_index_page(pages[page])
        skipped.extend(page_skipped)
        for row in rows:
            by_id[row.report_id] = row
    records = sorted(by_id.values(), key=lambda item: (item.report_date, int(item.report_id)), reverse=True)
    audit = {
        "source": "한경 컨센서스 기업분석 목록",
        "startDate": start_date,
        "endDate": end_date,
        "pageCount": last_page,
        "pagesFetched": len(pages),
        "pageFailures": failures,
        "reportCount": len(records),
        "uniqueTickerCount": len({row.ticker for row in records}),
        "nonstandardCodeReportCount": len(skipped),
    }
    return records, skipped, audit


def load_cached_report_index(
    path: Path,
    start_date: str,
    end_date: str,
    *,
    source_error: str,
) -> tuple[list[ReportRecord], dict]:
    """Load the last successful index when the source temporarily blocks access."""
    rows = _json_read(path, [])
    records: list[ReportRecord] = []
    for row in rows:
        try:
            record = ReportRecord(**row)
        except (TypeError, ValueError):
            continue
        if start_date <= record.report_date <= end_date:
            records.append(record)
    if not records:
        raise SourceRateLimited(
            f"{source_error}; 사용할 수 있는 저장 목록도 없습니다: {path}"
        )
    records.sort(key=lambda item: (item.report_date, int(item.report_id)), reverse=True)
    audit = {
        "source": "한경 컨센서스 기업분석 목록",
        "sourceMode": "cached-fallback",
        "sourceFetchError": source_error,
        "startDate": start_date,
        "endDate": end_date,
        "pageCount": None,
        "pagesFetched": 0,
        "pageFailures": [],
        "reportCount": len(records),
        "uniqueTickerCount": len({row.ticker for row in records}),
        "nonstandardCodeReportCount": None,
    }
    return records, audit


def naver_research_url(start_date: str, end_date: str, index: int, size: int = 50) -> str:
    query = urllib.parse.urlencode({
        "index": int(index),
        "size": int(size),
        "startDate": start_date,
        "endDate": end_date,
    })
    return f"{NAVER_RESEARCH_API}?{query}"


def parse_naver_research_items(items: Sequence[dict]) -> list[NaverResearchRecord]:
    records: list[NaverResearchRecord] = []
    for item in items:
        ticker = str(item.get("itemCode") or "").strip()
        report_date = str(item.get("writeDate") or "").strip()
        nid = str(item.get("nid") or "").strip()
        if not re.fullmatch(r"\d{6}", ticker):
            continue
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", report_date) or not nid:
            continue
        records.append(NaverResearchRecord(
            report_id=f"naver-{nid}",
            report_date=report_date,
            ticker=ticker,
            name=str(item.get("itemName") or "").strip(),
            title=str(item.get("title") or "").strip(),
            broker=str(item.get("brokerName") or "제공처 미확인").strip(),
            content=_strip_html(str(item.get("content") or "")),
            source_url=f"{NAVER_RESEARCH_BASE}/{nid}",
        ))
    return records


def collect_naver_research_index(
    start_date: str,
    end_date: str,
    *,
    timeout: int = 30,
    page_size: int = 50,
    pause_seconds: float = 0.15,
) -> tuple[list[NaverResearchRecord], dict]:
    def fetch(index: int) -> dict:
        payload = _request_bytes(
            naver_research_url(start_date, end_date, index, page_size),
            timeout,
            user_agent=NAVER_USER_AGENT,
            headers={"Referer": NAVER_RESEARCH_BASE},
        )
        return json.loads(payload.decode("utf-8"))

    first = fetch(0)
    total_count = int(first.get("totalCount") or 0)
    page_count = max(1, math.ceil(total_count / page_size))
    items = list(first.get("items") or [])
    failures: list[dict] = []
    for index in range(1, page_count):
        if pause_seconds:
            time.sleep(max(0.0, float(pause_seconds)))
        try:
            page = fetch(index)
            items.extend(page.get("items") or [])
        except SourceRateLimited:
            raise
        except Exception as exc:
            failures.append({"pageIndex": index, "reason": str(exc)})
    by_id = {record.report_id: record for record in parse_naver_research_items(items)}
    records = sorted(
        by_id.values(), key=lambda item: (item.report_date, item.report_id), reverse=True
    )
    audit = {
        "source": "네이버 증권 국내종목 리서치 공개 목록",
        "sourceMode": "live",
        "startDate": start_date,
        "endDate": end_date,
        "pageCount": page_count,
        "pagesFetched": page_count - len(failures),
        "pageFailures": failures,
        "reportCount": len(records),
        "uniqueTickerCount": len({row.ticker for row in records}),
    }
    return records, audit


def load_cached_naver_index(
    path: Path,
    start_date: str,
    end_date: str,
    *,
    source_error: str,
    source_mode: str = "cached-fallback",
) -> tuple[list[NaverResearchRecord], dict]:
    records: list[NaverResearchRecord] = []
    for row in _json_read(path, []):
        try:
            record = NaverResearchRecord(**row)
        except (TypeError, ValueError):
            continue
        if start_date <= record.report_date <= end_date:
            records.append(record)
    if not records:
        raise SourceRateLimited(
            f"{source_error}; 사용할 수 있는 네이버 리서치 저장 목록도 없습니다: {path}"
        )
    records.sort(key=lambda item: (item.report_date, item.report_id), reverse=True)
    audit = {
        "source": "네이버 증권 국내종목 리서치 공개 목록",
        "sourceMode": source_mode,
        "sourceFetchError": source_error,
        "startDate": start_date,
        "endDate": end_date,
        "pageCount": None,
        "pagesFetched": 0,
        "pageFailures": [],
        "reportCount": len(records),
        "uniqueTickerCount": len({row.ticker for row in records}),
    }
    return records, audit


def naver_research_detail_url(report_id: str) -> str:
    nid = str(report_id).removeprefix("naver-").removeprefix("naver-pdf-")
    if not nid.isdigit():
        raise ValueError(f"invalid Naver research id: {report_id}")
    return f"{NAVER_RESEARCH_API}/{nid}"


def parse_naver_pdf_detail(record: NaverResearchRecord, payload: dict) -> ReportRecord:
    """Turn a Naver research detail response into an auditable PDF record."""
    detail = payload.get("researchContent") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        detail = payload
    if not isinstance(detail, dict):
        raise ValueError("네이버 리서치 상세 응답 형식 오류")
    ticker = str(detail.get("itemCode") or "").strip()
    if ticker != record.ticker:
        raise ValueError(f"네이버 PDF 종목코드 불일치: {ticker or '없음'} != {record.ticker}")
    attach_url = str(detail.get("attachUrl") or "").strip()
    parsed = urllib.parse.urlparse(attach_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.lower().endswith(".pdf"):
        raise ValueError("네이버 원문 PDF 주소 없음")
    nid = str(detail.get("nid") or record.report_id.removeprefix("naver-")).strip()
    if not nid.isdigit():
        raise ValueError("네이버 리서치 상세 ID 오류")
    return ReportRecord(
        report_id=f"naver-pdf-{nid}",
        report_date=str(detail.get("writeDate") or record.report_date).strip(),
        ticker=record.ticker,
        name=str(detail.get("itemName") or record.name).strip(),
        title=str(detail.get("title") or record.title).strip(),
        broker=str(detail.get("brokerName") or record.broker or "제공처 미확인").strip(),
        analyst=str(detail.get("analystName") or "").strip(),
        pdf_url=attach_url,
    )


def fetch_naver_pdf_detail(record: NaverResearchRecord, timeout: int = 30) -> ReportRecord:
    payload = _request_bytes(
        naver_research_detail_url(record.report_id),
        timeout,
        user_agent=NAVER_USER_AGENT,
        headers={"Referer": record.source_url},
    )
    return parse_naver_pdf_detail(record, json.loads(payload.decode("utf-8")))


def select_naver_pdf_backfill(
    records: Sequence[NaverResearchRecord],
    target_tickers: set[str],
    *,
    as_of: date,
    freshness_days: int,
    per_ticker_broker: int = 1,
) -> list[NaverResearchRecord]:
    """Select recent original PDFs only for observed tickers lacking current forecasts."""
    cutoff = as_of - timedelta(days=max(0, int(freshness_days)))
    grouped: dict[tuple[str, str], list[NaverResearchRecord]] = {}
    for record in records:
        if record.ticker not in target_tickers:
            continue
        report_day = date.fromisoformat(record.report_date)
        if report_day < cutoff or report_day > as_of:
            continue
        grouped.setdefault((record.ticker, record.broker), []).append(record)
    selected: list[NaverResearchRecord] = []
    for group in grouped.values():
        group.sort(key=lambda item: (item.report_date, item.report_id), reverse=True)
        selected.extend(group[: max(1, int(per_ticker_broker))])
    return sorted(selected, key=lambda item: (item.report_date, item.report_id), reverse=True)


def fnguide_consensus_url(ticker: str) -> str:
    if not re.fullmatch(r"\d{6}", str(ticker)):
        raise ValueError(f"invalid ticker for FnGuide: {ticker}")
    return f"{FNGUIDE_CONSENSUS_BASE}?{urllib.parse.urlencode({'cmp_cd': ticker})}"


def _embedded_json(page_html: str, marker: str) -> dict | list | None:
    marker_index = page_html.find(marker)
    if marker_index < 0:
        return None
    starts = [index for index in (
        page_html.find("{", marker_index + len(marker)),
        page_html.find("[", marker_index + len(marker)),
    ) if index >= 0]
    if not starts:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(page_html[min(starts):])
        return value
    except (json.JSONDecodeError, TypeError):
        return None


def parse_fnguide_consensus(
    ticker: str,
    name: str,
    page_html: str,
    *,
    as_of: date,
    freshness_days: int,
) -> tuple[list[ForecastObservation], dict]:
    """Read FnGuide's public aggregate annual consensus, whose displayed unit is 억원."""
    performance = _embedded_json(page_html, "perforTrend:")
    trend = _embedded_json(page_html, "cnsTrend:")
    source_url = fnguide_consensus_url(ticker)
    if not isinstance(performance, dict):
        return [], {
            "ticker": ticker, "name": name, "sourceUrl": source_url,
            "status": "FnGuide 예상실적 자료 없음", "observationCount": 0,
        }
    if "단위 : 억원" not in page_html and "단위 : 억원, 배" not in page_html:
        return [], {
            "ticker": ticker, "name": name, "sourceUrl": source_url,
            "status": "FnGuide 금액단위 확인 실패", "observationCount": 0,
        }

    date_text = ""
    if isinstance(trend, dict):
        headers = trend.get("header") or []
        if headers:
            date_text = str(headers[0].get("NM") or "")
    try:
        report_day = datetime.strptime(date_text, "%Y/%m/%d").date()
    except ValueError:
        return [], {
            "ticker": ticker, "name": name, "sourceUrl": source_url,
            "status": "FnGuide 컨센서스 기준일 확인 실패", "observationCount": 0,
        }
    if report_day > as_of:
        return [], {
            "ticker": ticker, "name": name, "sourceUrl": source_url,
            "status": "FnGuide 컨센서스 기준일이 실행 기준일보다 미래", "observationCount": 0,
        }

    data_rows = {
        str(row.get("NAME") or "").strip(): row
        for row in performance.get("data") or []
        if isinstance(row, dict)
    }
    sales_row = data_rows.get("매출액") or {}
    op_row = data_rows.get("영업이익") or {}
    observations: list[ForecastObservation] = []
    for header in performance.get("header") or []:
        if not isinstance(header, dict) or str(header.get("EP_CHK") or "").strip().upper() != "E":
            continue
        yymm = str(header.get("YYMM") or "")
        year_match = re.fullmatch(r"(20\d{2})/12", yymm)
        if not year_match:
            continue
        period = f"{year_match.group(1)}FY"
        if not _period_is_open(period, as_of):
            continue
        column = str(header.get("CD") or "")
        sales = _number(sales_row.get(column))
        op = _number(op_row.get(column))
        if not _valid_amounts(sales, op):
            continue
        observations.append(ForecastObservation(
            ticker=ticker,
            name=name,
            broker="FnGuide 집계",
            report_id=f"fnguide-{ticker}-{report_day:%Y%m%d}",
            report_date=report_day.isoformat(),
            period=period,
            scope="annual",
            sales_krw_100m=round(float(sales), 4) if sales is not None else None,
            operating_profit_krw_100m=round(float(op), 4),
            source_url=source_url,
            extraction_method="FnGuide 공개 집계 컨센서스",
            confidence=0.9,
            stale=(as_of - report_day).days > freshness_days,
        ))
    return observations, {
        "ticker": ticker,
        "name": name,
        "sourceUrl": source_url,
        "reportDate": report_day.isoformat(),
        "observationCount": len(observations),
        "status": "정상" if observations else "올해·내년 집계 예상실적 없음",
    }


def fetch_fnguide_consensus(
    ticker: str,
    name: str,
    *,
    as_of: date,
    freshness_days: int,
    timeout: int = 30,
) -> tuple[list[ForecastObservation], dict]:
    source_url = fnguide_consensus_url(ticker)
    page_html = _decode_html(_request_bytes(
        source_url,
        timeout,
        user_agent=NAVER_USER_AGENT,
        headers={"Referer": "https://kwcomp.fnguide.com/"},
    ))
    return parse_fnguide_consensus(
        ticker, name, page_html, as_of=as_of, freshness_days=freshness_days,
    )


def select_latest_reports(records: Sequence[ReportRecord], per_ticker_broker: int = 2) -> list[ReportRecord]:
    """Keep the newest N reports for each ticker/broker pair.

    Keeping two by default lets extraction fall back when the newest note has no
    earnings table, without downloading the full report history.
    """
    grouped: dict[tuple[str, str], list[ReportRecord]] = {}
    for record in records:
        grouped.setdefault((record.ticker, record.broker), []).append(record)
    selected: list[ReportRecord] = []
    for group in grouped.values():
        group.sort(key=lambda item: (item.report_date, int(item.report_id)), reverse=True)
        selected.extend(group[: max(1, int(per_ticker_broker))])
    return sorted(selected, key=lambda item: (item.report_date, int(item.report_id)), reverse=True)


def download_report(record: ReportRecord, pdf_dir: Path, timeout: int = 45) -> Path:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    destination = pdf_dir / f"{record.report_id}.pdf"
    if destination.exists() and destination.stat().st_size > 1_000:
        with destination.open("rb") as stream:
            if stream.read(4) == b"%PDF":
                return destination
    request_options = {}
    if record.report_id.startswith("naver-pdf-"):
        request_options = {
            "user_agent": NAVER_USER_AGENT,
            "headers": {"Referer": NAVER_RESEARCH_BASE},
        }
    payload = _request_bytes(record.pdf_url, timeout=timeout, **request_options)
    if not payload.startswith(b"%PDF"):
        raise ValueError("response is not a PDF")
    temporary = destination.with_suffix(".pdf.part")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text or text in {"-", "--", "n/a", "N/A"} or "%" in text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").lstrip("+")
    try:
        result = float(text)
    except ValueError:
        return None
    return -result if negative else result


def _first_number(value: str | None) -> float | None:
    if not value:
        return None
    match = NUMBER_RE.search(str(value))
    return _number(match.group(0)) if match else None


def _numbers(value: str | None) -> list[float]:
    if not value:
        return []
    result = []
    for match in NUMBER_RE.finditer(str(value)):
        number = _number(match.group(0))
        if number is not None:
            result.append(number)
    return result


def _normalize_period(token: str) -> tuple[str, str] | None:
    token = re.sub(r"\s+", "", str(token or "")).upper()
    annual = YEAR_PERIOD_RE.fullmatch(token)
    if annual:
        return f"{annual.group(1)}FY", "annual"
    quarter = QUARTER_PERIOD_RE.fullmatch(token)
    if quarter:
        year = 2000 + int(quarter.group(2))
        return f"{year}Q{quarter.group(1)}", "quarter"
    return None


def _summary_amounts(text: str) -> list[tuple[int, int, float]]:
    amounts: list[tuple[int, int, float]] = []
    for match in KOREAN_AMOUNT_RE.finditer(text):
        sign = -1.0 if match.group("sign") == "-" else 1.0
        if match.group("jo") is not None:
            value = float(match.group("jo").replace(",", "")) * 10_000
            if match.group("jo_eok") is not None:
                value += float(match.group("jo_eok").replace(",", ""))
        elif match.group("cheon") is not None:
            raw = match.group("cheon")
            value = float(raw.replace(",", ""))
            # Company-report summaries sometimes contain malformed strings
            # such as "9,349천억원" where the intended unit is 억원.  Do not
            # turn that typo into a multi-trillion estimate.
            if value > 100 or (match.start() > 0 and text[match.start() - 1] in ",0123456789"):
                continue
            value *= 1_000
        else:
            raw = match.group("plain")
            unit = re.sub(r"\s+", "", match.group("unit") or "")
            if unit in {"억원", "억"} and re.fullmatch(r"\d{1,3}\.\d{3}", raw):
                value = float(raw.replace(".", ""))
            else:
                value = float(raw.replace(",", ""))
            if unit == "십억원":
                value *= 10
            elif unit == "백만원":
                value *= 0.01
        amounts.append((match.start(), match.end(), round(sign * value, 4)))
    return amounts


def _summary_period_mentions(text: str, report_year: int) -> list[tuple[int, int, str]]:
    mentions: list[tuple[int, int, str]] = []
    for match in PERIOD_MENTION_RE.finditer(text):
        relative = match.group("relative")
        quarter = None
        if relative:
            year = report_year + (1 if relative == "내년" else 0)
            quarter = match.group("relativequarter")
        elif match.group("year"):
            # "2026년 8월" is a monthly result, not a 2026 full-year value.
            if re.match(r"\s*(?:\d{1,2}\s*월|상반기|하반기|[12]H|[1-4]\s*Q)", text[match.end():], re.I):
                continue
            raw_year = match.group("year").lstrip("'’")
            year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
            quarter = match.group("quarter")
        elif match.group("q"):
            raw_year = match.group("qyear")
            year = report_year if not raw_year else int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
            quarter = match.group("q")
        else:
            year = report_year
            quarter = match.group("barequarter")
        if not report_year - 1 <= year <= report_year + 2:
            continue
        period = f"{year}Q{quarter}" if quarter else f"{year}FY"
        mentions.append((match.start(), match.end(), period))
    return mentions


def extract_naver_summary(
    record: NaverResearchRecord,
    *,
    as_of: date,
    freshness_days: int = 90,
) -> tuple[list[ForecastObservation], dict]:
    text = re.sub(r"\s+", " ", f"{record.title}. {record.content}").strip()
    report_year = date.fromisoformat(record.report_date).year
    facts: dict[str, dict[str, float | int | str]] = {}
    standalone_company_op: dict[str, float] = {}
    current_sentence_index = -1
    metric_re = re.compile(r"영업이익|영업손실|영업수익|매출액|매출|OP(?!M)(?:\s*추정치)?", re.I)
    partial_business_re = re.compile(r"기여|부문|사업부|선대|제품|공장|라인")
    company_scope_re = re.compile(r"동사|전사|연결(?:\s*기준)?")

    def metric_key(label: str) -> str:
        return "op" if label in {"영업이익", "영업손실"} or label.upper().startswith("OP") else "sales"

    def put(period: str, label: str, value: float) -> None:
        key = metric_key(label)
        if label == "영업손실" and value > 0:
            value = -value
        bucket = facts.setdefault(period, {})
        replace_generic_sales = (
            key == "sales"
            and bucket.get("sales_label") == "매출"
            and label in {"매출액", "영업수익"}
        )
        if key not in bucket or replace_generic_sales:
            bucket[key] = value
            bucket[f"{key}_sentence"] = current_sentence_index
            bucket[f"{key}_label"] = label

    def put_standalone(period: str, label: str, value: float) -> None:
        if label == "영업손실" and value > 0:
            value = -value
        standalone_company_op.setdefault(period, value)

    def company_context_is_safe(context: str) -> bool:
        partials = list(partial_business_re.finditer(context))
        if not partials:
            return True
        company_scopes = list(company_scope_re.finditer(context))
        return bool(company_scopes and company_scopes[-1].start() > partials[-1].start())

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    last_single_period: str | None = None
    for current_sentence_index, sentence in enumerate(sentences):
        periods = _summary_period_mentions(sentence, report_year)
        metrics = list(metric_re.finditer(sentence))
        explicit_periods = list(dict.fromkeys(period for _start, _end, period in periods))
        if explicit_periods:
            last_single_period = explicit_periods[0] if len(explicit_periods) == 1 else None
        elif (
            last_single_period
            and re.match(r"^[\s\"'\[\]■▶-]*(?:영업이익|영업손실|매출액|매출|OP(?!M))", sentence, re.I)
        ):
            periods = [(0, 0, last_single_period)]
        if not periods or not metrics:
            continue

        # Some useful reports state only a company's period OP, for example
        # "2026년 영업이익은 1.52조원".  Keep those direct facts while
        # rejecting nearby segment/product/contribution wording.
        for metric_index, metric in enumerate(metrics):
            label = metric.group(0)
            if metric_key(label) != "op":
                continue
            paired_prefix = sentence[max(0, metric.start() - 30):metric.start()]
            if re.search(r"(?:매출액|매출|영업수익)\s*(?:및|과|와|/)\s*$", paired_prefix):
                continue
            segment_end = metrics[metric_index + 1].start() if metric_index + 1 < len(metrics) else len(sentence)
            following_periods = [
                (start, end, period)
                for start, end, period in periods
                if 0 <= start - metric.end() <= 25 and start < segment_end
            ]
            if following_periods:
                for period_index, (period_start, period_end, period) in enumerate(following_periods):
                    amount_end = (
                        following_periods[period_index + 1][0]
                        if period_index + 1 < len(following_periods)
                        else segment_end
                    )
                    amounts = _summary_amounts(sentence[period_end:amount_end])
                    if not amounts:
                        continue
                    amount = amounts[0]
                    absolute_amount_end = period_end + amount[1]
                    context = sentence[max(0, metric.start() - 35):min(len(sentence), absolute_amount_end + 25)]
                    if company_context_is_safe(context):
                        put_standalone(period, label, amount[2])
                continue
            preceding_periods = [
                (start, end, period)
                for start, end, period in periods
                if 0 <= metric.start() - end <= 25
            ]
            if not preceding_periods:
                all_preceding = [
                    (start, end, period)
                    for start, end, period in periods
                    if start < metric.start()
                ]
                if len({period for _start, _end, period in all_preceding}) == 1:
                    preceding_periods = all_preceding[-1:]
            if not preceding_periods:
                continue
            amounts = _summary_amounts(sentence[metric.end():segment_end])
            if not amounts:
                period_start, period_end, period = preceding_periods[-1]
                prefix = sentence[period_end:metric.start()]
                prefix_amounts = _summary_amounts(prefix)
                if prefix_amounts:
                    amount = prefix_amounts[-1]
                    suffix = prefix[amount[1]:]
                    context = sentence[
                        max(0, period_start - 35):min(len(sentence), metric.end() + 25)
                    ]
                    if not suffix.strip() and company_context_is_safe(context):
                        put_standalone(period, label, amount[2])
                continue
            unique_preceding = list(dict.fromkeys(period for _start, _end, period in preceding_periods))
            standalone_segment = sentence[metric.end():segment_end]
            each_position = standalone_segment.find("각각")
            if (
                0 <= each_position < amounts[0][0]
                and len(unique_preceding) >= 2
                and len(amounts) >= len(unique_preceding)
            ):
                for period, amount in zip(unique_preceding, amounts):
                    put_standalone(period, label, amount[2])
                continue
            period_start, _period_end, period = preceding_periods[-1]
            amount = amounts[0]
            absolute_amount_end = metric.end() + amount[1]
            context = sentence[max(0, period_start - 35):min(len(sentence), absolute_amount_end + 25)]
            if company_context_is_safe(context):
                put_standalone(period, label, amount[2])

        # Frequent review style: "매출액 및 영업이익은 각각 2.4조원,
        # 849억원" or "매출액/영업이익 1.18조원/88억원".
        combo_re = re.compile(
            r"(?:매출액|매출|영업수익)\s*(?P<join>및|과|와|/)\s*영업(?P<result>이익|손실)"
        )
        for combo in combo_re.finditer(sentence):
            next_period = min(
                (start for start, _end, _period in periods if start > combo.end()),
                default=len(sentence),
            )
            combo_amounts = _summary_amounts(sentence[combo.end():next_period])
            if combo_amounts:
                lead = sentence[combo.end():combo.end() + combo_amounts[0][0]]
                if combo.group("join") != "/" and "각각" not in lead:
                    continue
            preceding = [period for start, _end, period in periods if start < combo.start()]
            following = [period for start, _end, period in periods if start >= combo.end()]
            period = preceding[-1] if preceding else following[0] if following else None
            if period and len(combo_amounts) >= 2:
                put(period, "매출액", combo_amounts[0][2])
                put(period, f"영업{combo.group('result')}", combo_amounts[1][2])

        # Period-first prose: "2026년 매출액 ..., 영업이익 ...".
        for index, (period_start, period_end, period) in enumerate(periods):
            window_end = periods[index + 1][0] if index + 1 < len(periods) else len(sentence)
            fragment = sentence[period_end:window_end]
            fragment_metrics = list(metric_re.finditer(fragment))
            for metric_index, metric in enumerate(fragment_metrics):
                amount_end = (
                    fragment_metrics[metric_index + 1].start()
                    if metric_index + 1 < len(fragment_metrics)
                    else len(fragment)
                )
                metric_tail = fragment[metric.end():amount_end]
                amounts = _summary_amounts(metric_tail)
                each_position = metric_tail.find("각각")
                if len(amounts) > 1 and 0 <= each_position < amounts[0][0]:
                    continue
                if (
                    amounts
                    and metric_index + 1 < len(fragment_metrics)
                    and metric_key(metric.group(0)) == "sales"
                    and metric_key(fragment_metrics[metric_index + 1].group(0)) == "op"
                ):
                    suffix = metric_tail[amounts[0][1]:]
                    if not re.search(r"[,;/·]|(?:및|과|와)", suffix):
                        continue
                if amounts:
                    put(period, metric.group(0), amounts[0][2])

        # Metric-first prose: "매출액은 2026년 ..., 2027년 ...".
        for metric_index, metric in enumerate(metrics):
            segment_end = metrics[metric_index + 1].start() if metric_index + 1 < len(metrics) else len(sentence)
            segment = sentence[metric.end():segment_end]
            if (
                metric_index + 1 < len(metrics)
                and metric_key(metric.group(0)) == "sales"
                and metric_key(metrics[metric_index + 1].group(0)) == "op"
            ):
                segment_amounts = _summary_amounts(segment)
                if segment_amounts:
                    suffix = segment[segment_amounts[0][1]:]
                    if not re.search(r"[,;/·]|(?:및|과|와)", suffix):
                        continue
            segment_periods = _summary_period_mentions(segment, report_year)
            if segment_periods:
                for period_index, (_start, end, period) in enumerate(segment_periods):
                    amount_window_end = (
                        segment_periods[period_index + 1][0]
                        if period_index + 1 < len(segment_periods)
                        else len(segment)
                    )
                    amounts = _summary_amounts(segment[end:amount_window_end])
                    if amounts:
                        put(period, metric.group(0), amounts[0][2])
                continue
            amounts = _summary_amounts(segment)
            unique_periods = list(dict.fromkeys(period for _start, _end, period in periods))
            each_position = segment.find("각각")
            if (
                amounts
                and 0 <= each_position < amounts[0][0]
                and len(unique_periods) >= 2
                and len(amounts) >= len(unique_periods)
            ):
                for period, amount in zip(unique_periods, amounts):
                    put(period, metric.group(0), amount[2])
                continue
            preceding = [period for start, _end, period in periods if start < metric.start()]
            future_period_starts = [start - metric.end() for start, _end, _period in periods if start > metric.end()]
            first_future = min(future_period_starts, default=len(segment) + 1)
            early_amounts = [amount for amount in amounts if amount[0] < first_future]
            if preceding and early_amounts:
                put(preceding[-1], metric.group(0), early_amounts[0][2])

    observations: list[ForecastObservation] = []
    age = (as_of - date.fromisoformat(record.report_date)).days
    for period, values in sorted(facts.items()):
        op = values.get("op")
        sales = values.get("sales")
        # Prefer a same-period sales/OP pair.  A directly dated company OP fact
        # is also accepted at lower confidence after the partial-business
        # wording check above.
        paired = (
            isinstance(op, (int, float))
            and isinstance(sales, (int, float))
            and values.get("op_sentence") == values.get("sales_sentence")
            and _valid_amounts(float(sales), float(op))
            and (float(op) <= 0 or float(op) <= float(sales) * 0.85)
        )
        if not paired:
            op = standalone_company_op.get(period)
            sales = None
        if not isinstance(op, (int, float)) or not _valid_amounts(
            float(sales) if isinstance(sales, (int, float)) else None,
            float(op),
        ):
            continue
        observations.append(ForecastObservation(
            ticker=record.ticker,
            name=record.name,
            broker=record.broker,
            report_id=record.report_id,
            report_date=record.report_date,
            period=period,
            scope="quarter" if "Q" in period else "annual",
            sales_krw_100m=float(sales) if isinstance(sales, (int, float)) else None,
            operating_profit_krw_100m=float(op),
            source_url=record.source_url,
            extraction_method=(
                "네이버 증권 리서치 요약문 매출·영업이익 쌍"
                if paired else "네이버 증권 리서치 요약문 기간 명시 영업이익"
            ),
            confidence=0.92 if paired else 0.86,
            stale=age > freshness_days,
        ))
    audit = {
        "reportId": record.report_id,
        "ticker": record.ticker,
        "broker": record.broker,
        "reportDate": record.report_date,
        "sourceUrl": record.source_url,
        "observationCount": len(observations),
        "status": "정상" if observations else "검증 가능한 기간별 예상 영업이익 없음",
    }
    return observations, audit


def _detect_unit_factor(text: str, broker: str) -> tuple[float | None, str]:
    compact = re.sub(r"\s+", "", text)
    if re.search(r"(?:단위[:：]?|(?:매출액|영업이익)\()십억원\)?|\(십억원(?:,|\))", compact):
        return 10.0, "PDF 표기 십억원"
    if re.search(r"(?:단위[:：]?|(?:매출액|영업이익)\()억원\)?|\(억원(?:,|\))", compact):
        return 1.0, "PDF 표기 억원"
    if re.search(r"(?:단위[:：]?|(?:매출액|영업이익)\()백만원\)?|\(백만원(?:,|\))", compact):
        return 0.01, "PDF 표기 백만원"
    if re.search(r"단위[:：]?조원", compact):
        return 10_000.0, "PDF 표기 조원"
    # Meritz's company-brief forecast templates consistently use KRW bn.  Keep
    # this provider-specific fallback explicit and lower its confidence.
    if "메리츠" in broker:
        return 10.0, "메리츠 기업보고서 기본단위 십억원 추론"
    return None, "금액단위 확인 실패"


def _pdf_identity_matches(text: str, record: ReportRecord) -> bool:
    """Require the indexed ticker inside the downloaded report.

    This catches source-index mistakes such as a title pointing to another
    company's PDF.  Numeric ticker codes survive font-map failures more
    reliably than Korean company names.
    """
    if re.search(rf"(?<!\d){re.escape(record.ticker)}(?!\d)", text):
        return True
    normalized_text = re.sub(r"\s+", "", text).casefold()
    normalized_name = re.sub(r"\s+", "", record.name).casefold()
    return len(normalized_name) >= 3 and normalized_name in normalized_text


def _valid_amounts(sales: float | None, op: float | None) -> bool:
    if op is None or not math.isfinite(op):
        return False
    if sales is not None and (not math.isfinite(sales) or sales <= 0 or abs(op) > sales * 1.5):
        return False
    return abs(op) < 10_000_000 and (sales is None or sales < 100_000_000)


def _observation(
    record: ReportRecord,
    period: str,
    scope: str,
    sales: float | None,
    op: float | None,
    factor: float,
    method: str,
    confidence: float,
    as_of: date,
    freshness_days: int,
) -> ForecastObservation | None:
    sales_converted = round(sales * factor, 4) if sales is not None else None
    op_converted = round(op * factor, 4) if op is not None else None
    if not _valid_amounts(sales_converted, op_converted):
        return None
    age = (as_of - date.fromisoformat(record.report_date)).days
    return ForecastObservation(
        ticker=record.ticker,
        name=record.name,
        broker=record.broker,
        report_id=record.report_id,
        report_date=record.report_date,
        period=period,
        scope=scope,
        sales_krw_100m=sales_converted,
        operating_profit_krw_100m=op_converted,
        source_url=record.pdf_url,
        extraction_method=method,
        confidence=round(confidence, 2),
        stale=age > freshness_days,
    )


def _extract_labeled_text(
    record: ReportRecord,
    page_texts: Sequence[str],
    factor: float,
    as_of: date,
    freshness_days: int,
) -> list[ForecastObservation]:
    observations: list[ForecastObservation] = []

    def metric_row(line: str, label: str) -> tuple[str, list[float]] | None:
        match = re.match(rf"^{label}(?:\(([^)]*)\))?\s+(.+)$", line)
        if not match:
            return None
        qualifier = (match.group(1) or "").strip()
        tail = match.group(2).strip()
        if tail.startswith(("증가율", "성장률", "마진", "률")):
            return None
        return qualifier, _numbers(tail)

    for text in page_texts:
        # Broker PDFs can mix units by page (for example a cover summary in
        # KRW bn and a detailed earnings table in KRW 100m).  Prefer the unit
        # printed on the current page and use the document-level factor only
        # when that page has no explicit unit.
        page_factor, _page_unit_reason = _detect_unit_factor(text, record.broker)
        page_factor = page_factor if page_factor is not None else factor
        has_explicit_page_unit = bool(re.search(
            r"단위\s*[:：]?\s*(?:십억원|억원|백만원|조원)", text
        ))
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
        for index, header in enumerate(lines):
            tokens = re.findall(r"(?:20\d{2}(?:[EF])?|[1-4]Q\d{2}(?:[EF])?)", header, flags=re.I)
            if len(tokens) < 2 or not any(re.search(r"[EF]$", token, flags=re.I) for token in tokens):
                continue
            context = " ".join(lines[max(0, index - 2) : index + 1])
            normalized_tokens = [token.upper() for token in tokens]
            # Side-by-side old/new/consensus tables repeat the same periods.
            # They need a provider template; a generic parser cannot know which
            # block is the analyst's current estimate.
            side_by_side_income_statement = False
            if len(set(normalized_tokens)) != len(normalized_tokens):
                half = len(tokens) // 2
                duplicated_halves = (
                    len(tokens) % 2 == 0
                    and normalized_tokens[:half] == normalized_tokens[half:]
                )
                # A full financial-statement page often prints the income
                # statement and balance sheet beside each other under the same
                # years.  The revenue and operating-profit rows belong to the
                # first block, so that case is unambiguous.
                if duplicated_halves and "손익계산서" in context:
                    tokens = tokens[:half]
                    normalized_tokens = normalized_tokens[:half]
                    side_by_side_income_statement = True
                else:
                    continue
            sales_rows = []
            op_rows = []
            for line in lines[index + 1 : index + 18]:
                sales_row = metric_row(line, r"(?:매출액|매출)")
                if sales_row:
                    sales_rows.append(sales_row)
                op_row = metric_row(line, r"영업이익")
                if op_row:
                    op_rows.append(op_row)
            if not sales_rows or not op_rows:
                continue
            # Prefer consolidated rows, then an unqualified company total.  A
            # standalone row is used only when no consolidated total exists.
            sales_rows.sort(key=lambda row: ("연결" not in row[0], "별도" in row[0]))
            op_rows.sort(key=lambda row: ("연결" not in row[0], "별도" in row[0]))
            sales_values = sales_rows[0][1]
            op_values = op_rows[0][1]
            if side_by_side_income_statement:
                sales_values = sales_values[: len(tokens)]
                op_values = op_values[: len(tokens)]
            # Revision tables often put current, previous and consensus blocks
            # under one short year header.  Extra operating-profit values mean
            # the generic parser cannot identify the current block safely.
            if len(op_values) != len(tokens):
                # Multi-year cover tables can acquire one or two footnote
                # numbers from the adjacent PDF column.  They remain aligned
                # when four or more period columns are explicit.  Short
                # two-year revision headers stay rejected because their extra
                # values represent old/current/consensus blocks.
                if (
                    len(op_values) < len(tokens)
                    or len(tokens) < 4
                    or len(op_values) > len(tokens) + 3
                ):
                    continue
            # Read the values aligned with every header period, including the
            # historical columns.  Taking the right-most values is unsafe when
            # PDF extraction appends prose containing another year to the row.
            op_values = op_values[: len(tokens)]
            sales_values = sales_values[: len(tokens)] if len(sales_values) >= len(tokens) else []
            has_quarter_columns = any("Q" in token.upper() for token in tokens)
            explicit_company_summary = bool(re.search(
                r"(?:재무정보|forecast earnings|영업실적 및 주요 투자지표|실적 추이 및 전망|추정재무제표|손익계산서)",
                context,
                flags=re.I,
            ))
            table_confidence = 0.98 if has_quarter_columns or explicit_company_summary else 0.93
            if has_explicit_page_unit:
                table_confidence = min(0.99, table_confidence + 0.01)
            for position, token in enumerate(tokens):
                normalized = _normalize_period(token)
                if not normalized:
                    continue
                period, scope = normalized
                observation = _observation(
                    record,
                    period,
                    scope,
                    sales_values[position] if sales_values else None,
                    op_values[position],
                    page_factor,
                    "PDF 기간-지표 재무표",
                    table_confidence,
                    as_of,
                    freshness_days,
                )
                if observation:
                    observations.append(observation)

        # Some one-page summaries put metrics in the header and one forecast
        # year per row (for example: "2026E 28,393 845 ...").
        for index, header in enumerate(lines):
            labels = re.findall(r"매출액|영업이익|순이익|EPS|PER|PBR|ROE|EV/EBITDA", header, flags=re.I)
            if "매출액" not in labels or "영업이익" not in labels:
                continue
            sales_column = labels.index("매출액")
            op_column = labels.index("영업이익")
            for line in lines[index + 1 : index + 10]:
                match = re.match(r"^(20\d{2}[EF])\s+(.+)$", line, flags=re.I)
                if not match:
                    continue
                normalized = _normalize_period(match.group(1))
                values = _numbers(match.group(2))
                # Peer-comparison tables can start their next line with a year
                # while carrying many repeated valuation columns.  A genuine
                # one-year summary row stays close to the number of named
                # metrics in its header.
                if (
                    not normalized
                    or len(values) <= max(sales_column, op_column)
                    or len(values) > len(labels) + 2
                ):
                    continue
                period, scope = normalized
                observation = _observation(
                    record,
                    period,
                    scope,
                    values[sales_column],
                    values[op_column],
                    page_factor,
                    "PDF 연도별 요약행",
                    0.95 if has_explicit_page_unit else 0.94,
                    as_of,
                    freshness_days,
                )
                if observation:
                    observations.append(observation)
    return observations


def _extract_structured_tables(
    record: ReportRecord,
    tables: Sequence[list[list[str | None]]],
    factor: float,
    unit_reason: str,
    as_of: date,
    freshness_days: int,
) -> list[ForecastObservation]:
    observations: list[ForecastObservation] = []
    inferred_unit = "추론" in unit_reason
    for table in tables:
        if len(table) < 4:
            continue
        column_count = max((len(row) for row in table), default=0)
        first = next((str(cell).strip() for cell in table[0] if cell and str(cell).strip()), "")
        period = _normalize_period(first)
        if not period:
            continue
        normalized_period, scope = period
        if scope == "quarter" and 4 <= len(table) <= 12 and column_count == 3:
            sales = _first_number(table[1][0] if table[1] else None)
            op = _first_number(table[2][0] if table[2] else None)
            observation = _observation(
                record,
                normalized_period,
                scope,
                sales,
                op,
                factor,
                "PDF 분기 Preview 표",
                0.86 if inferred_unit else 0.94,
                as_of,
                freshness_days,
            )
            if observation:
                observations.append(observation)
        elif scope == "annual" and 4 <= len(table) <= 12 and column_count == 3:
            year = int(normalized_period[:4])
            columns = [0]
            if max((len(row) for row in table), default=0) >= 3:
                columns.append(2)
            for offset, column in enumerate(columns):
                sales = _first_number(table[2][column] if len(table[2]) > column else None)
                op = _first_number(table[3][column] if len(table[3]) > column else None)
                observation = _observation(
                    record,
                    f"{year + offset}FY",
                    "annual",
                    sales,
                    op,
                    factor,
                    "PDF 연간 추정치 변경표",
                    0.86 if inferred_unit else 0.94,
                    as_of,
                    freshness_days,
                )
                if observation:
                    observations.append(observation)
    return observations


def extract_report_pdf(
    record: ReportRecord,
    pdf_path: Path,
    *,
    as_of: date,
    freshness_days: int = 90,
    max_pages: int = 24,
) -> tuple[list[ForecastObservation], dict]:
    try:
        import pdfplumber
        import pymupdf
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError("pdfplumber와 pymupdf가 필요합니다. requirements.txt를 설치하세요.") from exc

    page_texts: list[str] = []
    inspected_pages: list[int] = []
    document = pymupdf.open(pdf_path)
    page_count = len(document)
    if page_count <= max_pages:
        inspected_pages = list(range(page_count))
    else:
        head_count = max(1, math.ceil(max_pages * 0.7))
        tail_count = max_pages - head_count
        inspected_pages = list(range(head_count)) + list(range(page_count - tail_count, page_count))
    for page_index in inspected_pages:
        page_texts.append(document[page_index].get_text("text", sort=True) or "")
    document.close()
    all_text = "\n".join(page_texts)
    if not _pdf_identity_matches(all_text, record):
        return [], {
            "reportId": record.report_id,
            "ticker": record.ticker,
            "broker": record.broker,
            "reason": "PDF 종목코드 불일치",
            "pageCount": page_count,
            "textCharacters": len(all_text),
            "pagesInspected": [page + 1 for page in inspected_pages],
            "codesFound": sorted(set(re.findall(r"(?<!\d)(\d{6})(?!\d)", all_text)))[:20],
        }
    factor, unit_reason = _detect_unit_factor(all_text, record.broker)
    if factor is None:
        return [], {
            "reportId": record.report_id,
            "ticker": record.ticker,
            "broker": record.broker,
            "reason": unit_reason,
            "pageCount": page_count,
            "textCharacters": len(all_text),
        }

    observations = _extract_labeled_text(record, page_texts, factor, as_of, freshness_days)
    has_quarter = any(item.scope == "quarter" for item in observations)
    preview_without_quarter = (
        not has_quarter
        and bool(re.search(r"\b[1-4]Q\d{2}E?\s+Preview\b", all_text, flags=re.I))
    )
    # Table geometry extraction is the expensive path.  Most providers expose
    # clean period and metric rows in the text layer, so inspect table geometry
    # only when text extraction found nothing or a Preview lost its quarter.
    if not observations or preview_without_quarter:
        tables: list[list[list[str | None]]] = []
        with pdfplumber.open(pdf_path) as pdf:
            if not observations:
                table_indexes = list(dict.fromkeys(inspected_pages[:6] + inspected_pages[-2:]))
            else:
                table_indexes = [page for page in inspected_pages if page < 8]
            for page_index in table_indexes:
                page = pdf.pages[page_index]
                try:
                    tables.extend(page.extract_tables() or [])
                except Exception:
                    continue
        observations.extend(_extract_structured_tables(
            record, tables, factor, unit_reason, as_of, freshness_days,
        ))
    # Prefer explicit labeled rows over positional templates for the same fact.
    best: dict[tuple[str, str], ForecastObservation] = {}
    for item in observations:
        key = (item.period, item.broker)
        current = best.get(key)
        if current is None or item.confidence > current.confidence:
            best[key] = item
    result = sorted(best.values(), key=lambda item: (item.period, item.broker))
    audit = {
        "reportId": record.report_id,
        "ticker": record.ticker,
        "broker": record.broker,
        "pageCount": page_count,
        "pagesInspected": [page + 1 for page in inspected_pages],
        "textCharacters": len(all_text),
        "unitFactorToKrw100m": factor,
        "unitBasis": unit_reason,
        "observationCount": len(result),
        "status": "정상" if result else "예상실적 표 추출 실패",
    }
    return result, audit


def load_supplements(path: Path | None, as_of: date, freshness_days: int) -> list[ForecastObservation]:
    """Load verified article/official/broker supplements in the same schema.

    Supplements are intentionally explicit: every row needs a source URL and
    date.  The collector never invents a number to fill a missing report.
    """
    if path is None or not path.exists():
        return []
    if path.suffix.lower() == ".json":
        rows = _json_read(path, [])
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    observations = []
    for row in rows:
        required = ("ticker", "name", "broker", "report_date", "period", "operating_profit_krw_100m", "source_url")
        if any(str(row.get(field, "")).strip() == "" for field in required):
            raise ValueError(f"supplement row missing required field: {row}")
        source_url = str(row["source_url"]).strip()
        if not source_url.startswith(("https://", "http://")):
            raise ValueError(f"supplement source_url must be http(s): {source_url}")
        report_date = date.fromisoformat(str(row["report_date"]))
        period = str(row["period"]).upper().replace(" ", "")
        if not re.fullmatch(r"20\d{2}(?:FY|Q[1-4])", period):
            raise ValueError(f"invalid supplement period: {period}")
        op = float(row["operating_profit_krw_100m"])
        sales_raw = row.get("sales_krw_100m")
        sales = float(sales_raw) if sales_raw not in (None, "") else None
        if not _valid_amounts(sales, op):
            raise ValueError(f"implausible supplement amounts: {row}")
        observations.append(ForecastObservation(
            ticker=str(row["ticker"]).zfill(6),
            name=str(row["name"]),
            broker=str(row["broker"]),
            report_id=str(row.get("report_id") or hashlib.sha256(source_url.encode()).hexdigest()[:16]),
            report_date=report_date.isoformat(),
            period=period,
            scope="quarter" if "Q" in period else "annual",
            sales_krw_100m=sales,
            operating_profit_krw_100m=op,
            source_url=source_url,
            extraction_method=str(row.get("extraction_method") or "검증된 보충자료"),
            confidence=float(row.get("confidence") or 0.95),
            stale=(as_of - report_date).days > freshness_days,
        ))
    return observations


def aggregate_consensus(
    observations: Sequence[ForecastObservation],
    *,
    minimum_confidence: float = 0.8,
    as_of: date | None = None,
    recency_window_days: int = 45,
    outlier_factor: float = 3.0,
) -> list[dict]:
    # Latest fact wins for the same broker/ticker/period.
    latest: dict[tuple[str, str, str], ForecastObservation] = {}
    for item in observations:
        if item.stale or item.confidence < minimum_confidence:
            continue
        key = (item.ticker, item.period, item.broker)
        current = latest.get(key)
        if current is None or (item.report_date, item.report_id) > (current.report_date, current.report_id):
            latest[key] = item
    grouped: dict[tuple[str, str], list[ForecastObservation]] = {}
    for item in latest.values():
        grouped.setdefault((item.ticker, item.period), []).append(item)
    output = []
    for (ticker, period), items in sorted(grouped.items()):
        items.sort(key=lambda item: item.broker)
        if as_of is not None and not _period_is_open(period, as_of):
            continue

        newest_report_date = max(date.fromisoformat(item.report_date) for item in items)
        cutoff = newest_report_date - timedelta(days=recency_window_days)
        excluded = []
        current_items = []
        for item in items:
            if date.fromisoformat(item.report_date) < cutoff:
                excluded.append(_excluded_estimate(item, "같은 기간 최신 보고서보다 오래된 추정치"))
            else:
                current_items.append(item)

        # A single malformed source number must not pull a multi-broker median.
        # Only apply this rule when at least three same-period estimates exist;
        # with one or two brokers, disagreement is shown rather than guessed.
        if len(current_items) >= 3:
            raw_values = [item.operating_profit_krw_100m for item in current_items]
            median_op = float(statistics.median(raw_values))
            kept = []
            for item in current_items:
                value = item.operating_profit_krw_100m
                same_sign = (median_op > 0 and value > 0) or (median_op < 0 and value < 0)
                ratio = max(abs(value), abs(median_op)) / max(min(abs(value), abs(median_op)), 1e-9)
                if same_sign and ratio > outlier_factor:
                    excluded.append(_excluded_estimate(item, "다중 증권사 중앙값 대비 수치 이상치"))
                else:
                    kept.append(item)
            if len(kept) >= 2:
                current_items = kept

        items = sorted(current_items, key=lambda item: item.broker)
        if not items:
            continue
        op_values = [item.operating_profit_krw_100m for item in items]
        sales_values = [item.sales_krw_100m for item in items if item.sales_krw_100m is not None]
        count = len(items)
        disagreement_ratio = _disagreement_ratio(op_values)
        sign_disagreement = min(op_values) < 0 < max(op_values)
        if count >= 2 and (sign_disagreement or disagreement_ratio > outlier_factor):
            status = "불일치 검토"
        elif count == 1 and items[0].extraction_method == "FnGuide 공개 집계 컨센서스":
            status = "외부 집계 컨센서스"
        else:
            status = "유효 컨센서스" if count >= 3 else "참고 컨센서스" if count == 2 else "개별 추정치"
        output.append({
            "ticker": ticker,
            "name": items[0].name,
            "period": period,
            "scope": items[0].scope,
            "status": status,
            "freshBrokerCount": count,
            "operatingProfitMedianKrw100m": round(float(statistics.median(op_values)), 4),
            "operatingProfitMinKrw100m": round(min(op_values), 4),
            "operatingProfitMaxKrw100m": round(max(op_values), 4),
            "salesMedianKrw100m": round(float(statistics.median(sales_values)), 4) if sales_values else None,
            "latestReportDate": max(item.report_date for item in items),
            "disagreementRatio": round(disagreement_ratio, 4),
            "excludedEstimateCount": len(excluded),
            "excludedEstimates": excluded,
            "estimates": [
                {
                    "broker": item.broker,
                    "reportDate": item.report_date,
                    "operatingProfitKrw100m": item.operating_profit_krw_100m,
                    "salesKrw100m": item.sales_krw_100m,
                    "sourceUrl": item.source_url,
                    "confidence": item.confidence,
                    "method": item.extraction_method,
                }
                for item in items
            ],
        })
    return output


def _period_is_open(period: str, as_of: date) -> bool:
    annual = re.fullmatch(r"(20\d{2})FY", period)
    if annual:
        return as_of.year <= int(annual.group(1)) <= as_of.year + 1
    quarter = re.fullmatch(r"(20\d{2})Q([1-4])", period)
    if not quarter:
        return False
    year = int(quarter.group(1))
    if year > as_of.year + 1:
        return False
    quarter_number = int(quarter.group(2))
    end_month = quarter_number * 3
    next_month = date(year + (end_month == 12), 1 if end_month == 12 else end_month + 1, 1)
    quarter_end = next_month - timedelta(days=1)
    return quarter_end >= as_of


def _disagreement_ratio(values: Sequence[float]) -> float:
    absolute = [abs(value) for value in values if value != 0]
    if len(absolute) < 2:
        return 1.0
    return max(absolute) / max(min(absolute), 1e-9)


def _excluded_estimate(item: ForecastObservation, reason: str) -> dict:
    return {
        "broker": item.broker,
        "reportDate": item.report_date,
        "operatingProfitKrw100m": item.operating_profit_krw_100m,
        "salesKrw100m": item.sales_krw_100m,
        "sourceUrl": item.source_url,
        "reason": reason,
    }


def _observation_key(item: ForecastObservation) -> tuple[str, str, str, str]:
    return item.report_id, item.ticker, item.period, item.broker


def merge_reprocessed_observations(
    previous: dict[tuple[str, str, str, str], ForecastObservation],
    extracted: Sequence[ForecastObservation],
    reprocessed_report_ids: set[str],
) -> dict[tuple[str, str, str, str], ForecastObservation]:
    """Replace every fact from reports that were successfully re-read.

    Parser improvements may correctly turn an old false positive into no
    observations.  Removing prior rows for audited reports prevents that old
    value from surviving forever in incremental storage.
    """
    combined = {
        key: item
        for key, item in previous.items()
        if item.report_id not in reprocessed_report_ids
    }
    for item in extracted:
        combined[_observation_key(item)] = item
    return combined


def classify_uncovered_tickers(
    indexed_tickers: dict[str, str],
    extracted_tickers: set[str],
    failures: Sequence[dict],
) -> tuple[list[dict], list[dict]]:
    """Separate source-blocked tickers from reports that were read but empty."""
    retryable_tickers = {
        str(row.get("ticker"))
        for row in failures
        if row.get("ticker") and row.get("retryable")
    }
    uncovered = set(indexed_tickers) - extracted_tickers
    pending = [
        {
            "ticker": ticker,
            "name": indexed_tickers[ticker],
            "reason": "원문 소스 차단으로 미처리; 다음 증분 실행에서 재시도",
        }
        for ticker in sorted(uncovered & retryable_tickers)
    ]
    missing = [
        {
            "ticker": ticker,
            "name": indexed_tickers[ticker],
            "reason": "확보한 보고서를 판독했으나 예상 영업이익 미추출",
        }
        for ticker in sorted(uncovered - retryable_tickers)
    ]
    return missing, pending


def run_engine(args: argparse.Namespace) -> dict:
    as_of = date.fromisoformat(args.end_date)
    output_dir = Path(args.output_dir)
    cache_dir = Path(args.cache_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    naver_source_limited = False
    if args.reuse_naver_index:
        naver_records, naver_audit = load_cached_naver_index(
            output_dir / "naver_research_index.json",
            args.start_date,
            args.end_date,
            source_error="사용자가 직전 정상 목록 재사용을 지정",
            source_mode="cached-reuse",
        )
    else:
        try:
            naver_records, naver_audit = collect_naver_research_index(
                args.start_date,
                args.end_date,
                timeout=args.timeout,
                page_size=args.naver_page_size,
                pause_seconds=args.naver_pause_seconds,
            )
        except SourceRateLimited as exc:
            naver_source_limited = True
            naver_records, naver_audit = load_cached_naver_index(
                output_dir / "naver_research_index.json",
                args.start_date,
                args.end_date,
                source_error=str(exc),
            )
    _json_write(output_dir / "naver_research_index.json", [asdict(row) for row in naver_records])
    _csv_write(
        output_dir / "naver_research_index.csv",
        [asdict(row) for row in naver_records],
        NaverResearchRecord.__dataclass_fields__,
    )

    index_source_limited = False
    try:
        records, nonstandard, index_audit = collect_hankyung_index(
            args.start_date,
            args.end_date,
            workers=args.index_workers,
            timeout=args.timeout,
        )
        index_audit["sourceMode"] = "live"
    except SourceRateLimited as exc:
        index_source_limited = True
        records, index_audit = load_cached_report_index(
            output_dir / "report_index.json",
            args.start_date,
            args.end_date,
            source_error=str(exc),
        )
        nonstandard = _json_read(output_dir / "report_index_exclusions.json", [])
    _json_write(output_dir / "report_index.json", [asdict(row) for row in records])
    _csv_write(output_dir / "report_index.csv", [asdict(row) for row in records], ReportRecord.__dataclass_fields__)
    _json_write(output_dir / "report_index_exclusions.json", nonstandard)

    selected = (
        []
        if args.skip_hankyung_pdf_backfill
        else select_latest_reports(records, args.reports_per_ticker_broker)
    )
    if args.max_reports:
        selected = selected[: args.max_reports]
    _json_write(output_dir / "selected_reports.json", [asdict(row) for row in selected])

    if args.index_only:
        report = {
            "status": "목록 캐시 사용" if index_source_limited or naver_source_limited else "목록 수집 완료",
            "generatedAtKST": datetime.now(KST).isoformat(timespec="seconds"),
            "index": index_audit,
            "naverIndex": naver_audit,
            "selectedReportCount": len(selected),
            "selectedTickerCount": len({row.ticker for row in selected}),
            "sourceTickerCount": len({row.ticker for row in records} | {row.ticker for row in naver_records}),
            "scoreboardIntegrated": False,
        }
        _json_write(output_dir / "run_report.json", report)
        return report

    previous_rows = _json_read(output_dir / "forecast_observations.json", [])
    previous: dict[tuple[str, str, str, str], ForecastObservation] = {}
    for row in previous_rows:
        try:
            item = ForecastObservation(**row)
            item = replace(
                item,
                stale=(as_of - date.fromisoformat(item.report_date)).days > args.freshness_days,
            )
            previous[_observation_key(item)] = item
        except (TypeError, ValueError):
            continue
    already_processed = {item.report_id for item in previous.values()}
    failures: list[dict] = []
    audits: list[dict] = []
    extracted: list[ForecastObservation] = []
    naver_audits: list[dict] = []
    for record in naver_records:
        items, audit = extract_naver_summary(
            record,
            as_of=as_of,
            freshness_days=args.freshness_days,
        )
        extracted.extend(items)
        naver_audits.append(audit)

    pdf_dir = cache_dir / "forecast_reports" / "pdfs"
    naver_pdf_audits: list[dict] = []
    naver_pdf_detail_audits: list[dict] = []
    naver_pdf_failures: list[dict] = []
    naver_pdf_download_lock = threading.Lock()
    naver_pdf_stop = threading.Event()
    naver_pdf_download_state = {
        "lastAt": 0.0, "count": 0, "rateLimited": False, "reason": None,
    }

    # The list endpoint contains only a short summary.  Build the backfill set
    # from tickers for which the engine has extracted some forecast evidence
    # but still has no current-year/next-year point.  This avoids downloading
    # every PDF in the market while covering the exact gap the user identified.
    provisional_map = merge_reprocessed_observations(
        previous,
        extracted,
        {record.report_id for record in naver_records},
    )
    provisional_observations = list(provisional_map.values())
    provisional_consensus = aggregate_consensus(
        provisional_observations,
        minimum_confidence=args.minimum_confidence,
        as_of=as_of,
        recency_window_days=args.consensus_window_days,
    )
    provisional_observation_tickers = {row.ticker for row in provisional_observations}
    provisional_current_tickers = {str(row["ticker"]) for row in provisional_consensus}
    naver_pdf_target_tickers = provisional_observation_tickers - provisional_current_tickers
    if args.skip_naver_pdf_backfill:
        naver_pdf_candidates: list[NaverResearchRecord] = []
    else:
        naver_pdf_candidates = select_naver_pdf_backfill(
            naver_records,
            naver_pdf_target_tickers,
            as_of=as_of,
            freshness_days=args.freshness_days,
            per_ticker_broker=args.naver_pdf_reports_per_ticker_broker,
        )
        if args.max_naver_pdf_reports:
            naver_pdf_candidates = naver_pdf_candidates[: args.max_naver_pdf_reports]

    cached_naver_pdf_details: dict[str, ReportRecord] = {}
    for row in _json_read(output_dir / "naver_pdf_index.json", []):
        try:
            detail = ReportRecord(**row)
        except (TypeError, ValueError):
            continue
        if detail.report_id.startswith("naver-pdf-"):
            cached_naver_pdf_details[detail.report_id.replace("naver-pdf-", "naver-")] = detail
    resolved_naver_pdf_details = dict(cached_naver_pdf_details)

    def cached_naver_pdf(record: ReportRecord) -> Path | None:
        path = pdf_dir / f"{record.report_id}.pdf"
        if path.exists() and path.stat().st_size > 1_000:
            with path.open("rb") as stream:
                if stream.read(4) == b"%PDF":
                    return path
        return None

    def process_naver_pdf(summary_record: NaverResearchRecord):
        detail = cached_naver_pdf_details.get(summary_record.report_id)
        detail_source = "cache"
        if detail is None:
            detail = fetch_naver_pdf_detail(summary_record, args.timeout)
            detail_source = "live"
        pdf_path = cached_naver_pdf(detail)
        if pdf_path is None:
            if naver_pdf_stop.is_set():
                raise SourceRateLimited(
                    naver_pdf_download_state["reason"] or "Naver PDF collection stopped"
                )
            if args.naver_pdf_pause_seconds:
                time.sleep(max(0.0, float(args.naver_pdf_pause_seconds)))
            try:
                pdf_path = download_report(detail, pdf_dir, args.timeout)
            except SourceRateLimited as exc:
                with naver_pdf_download_lock:
                    naver_pdf_download_state["rateLimited"] = True
                    naver_pdf_download_state["reason"] = str(exc)
                    naver_pdf_stop.set()
                raise
            finally:
                with naver_pdf_download_lock:
                    naver_pdf_download_state["lastAt"] = time.monotonic()
            with naver_pdf_download_lock:
                naver_pdf_download_state["count"] += 1
        items, audit = extract_report_pdf(
            detail,
            pdf_path,
            as_of=as_of,
            freshness_days=args.freshness_days,
            max_pages=args.max_pdf_pages,
        )
        detail_audit = {
            "reportId": summary_record.report_id,
            "pdfReportId": detail.report_id,
            "ticker": detail.ticker,
            "name": detail.name,
            "broker": detail.broker,
            "reportDate": detail.report_date,
            "sourceUrl": detail.pdf_url,
            "detailSource": detail_source,
        }
        audit["source"] = "네이버 증권사 원문 PDF"
        return detail, items, audit, detail_audit

    if naver_pdf_candidates:
        with ThreadPoolExecutor(max_workers=max(1, min(args.naver_pdf_workers, 6))) as executor:
            futures = {
                executor.submit(process_naver_pdf, record): record
                for record in naver_pdf_candidates
            }
            for future in as_completed(futures):
                summary_record = futures[future]
                try:
                    detail, items, audit, detail_audit = future.result()
                    resolved_naver_pdf_details[summary_record.report_id] = detail
                    extracted.extend(items)
                    naver_pdf_audits.append(audit)
                    naver_pdf_detail_audits.append(detail_audit)
                    if not items:
                        naver_pdf_failures.append({
                            "reportId": detail.report_id,
                            "ticker": detail.ticker,
                            "name": detail.name,
                            "broker": detail.broker,
                            "reportDate": detail.report_date,
                            "sourceUrl": detail.pdf_url,
                            "reason": audit.get("status") or audit.get("reason") or "추출 결과 없음",
                        })
                except Exception as exc:
                    naver_pdf_failures.append({
                        "reportId": summary_record.report_id,
                        "ticker": summary_record.ticker,
                        "name": summary_record.name,
                        "broker": summary_record.broker,
                        "reportDate": summary_record.report_date,
                        "sourceUrl": summary_record.source_url,
                        "reason": str(exc),
                        "retryable": isinstance(exc, SourceRateLimited),
                    })
    _json_write(
        output_dir / "naver_pdf_index.json",
        [
            asdict(row)
            for row in sorted(
                resolved_naver_pdf_details.values(),
                key=lambda item: (item.report_date, item.report_id),
                reverse=True,
            )
        ],
    )
    failures.extend(naver_pdf_failures)

    download_lock = threading.Lock()
    source_stop = threading.Event()
    download_state = {"lastAt": 0.0, "count": 0, "rateLimited": False, "reason": None}

    def cached_pdf(record: ReportRecord) -> Path | None:
        path = pdf_dir / f"{record.report_id}.pdf"
        if path.exists() and path.stat().st_size > 1_000:
            with path.open("rb") as stream:
                if stream.read(4) == b"%PDF":
                    return path
        return None

    def process(record: ReportRecord):
        pdf_path = cached_pdf(record)
        if pdf_path is None:
            with download_lock:
                if source_stop.is_set():
                    raise SourceRateLimited(download_state["reason"] or "source collection stopped")
                elapsed = time.monotonic() - float(download_state["lastAt"])
                pause = max(0.0, float(args.download_pause_seconds) - elapsed)
                if pause:
                    time.sleep(pause)
                try:
                    pdf_path = download_report(record, pdf_dir, args.timeout)
                except SourceRateLimited as exc:
                    download_state["rateLimited"] = True
                    download_state["reason"] = str(exc)
                    source_stop.set()
                    raise
                finally:
                    download_state["lastAt"] = time.monotonic()
                download_state["count"] += 1
        return record, extract_report_pdf(
            record,
            pdf_path,
            as_of=as_of,
            freshness_days=args.freshness_days,
            max_pages=args.max_pdf_pages,
        )

    selected_groups: dict[tuple[str, str], list[ReportRecord]] = {}
    for record in selected:
        selected_groups.setdefault((record.ticker, record.broker), []).append(record)
    for group in selected_groups.values():
        group.sort(key=lambda item: (item.report_date, int(item.report_id)), reverse=True)

    def process_group(group: list[ReportRecord]):
        group_items: list[ForecastObservation] = []
        group_audits: list[dict] = []
        group_failures: list[dict] = []
        attempted = 0
        for record in group:
            # A prior successful observation for this report means the group is
            # already covered.  This is the daily incremental fast path.
            if args.incremental and record.report_id in already_processed:
                break
            # A source block must not prevent parsing PDFs that are already in
            # the local cache.  Skip only uncached candidates and keep looking
            # for an older cached report in the same ticker/broker group.
            if source_stop.is_set() and cached_pdf(record) is None:
                group_failures.append({
                    "reportId": record.report_id,
                    "ticker": record.ticker,
                    "name": record.name,
                    "broker": record.broker,
                    "reportDate": record.report_date,
                    "sourceUrl": record.pdf_url,
                    "reason": download_state["reason"] or "source collection stopped",
                    "retryable": True,
                })
                continue
            attempted += 1
            try:
                _record, (items, audit) = process(record)
                group_audits.append(audit)
                if items:
                    group_items.extend(items)
                    break
                group_failures.append({
                    "reportId": record.report_id,
                    "ticker": record.ticker,
                    "name": record.name,
                    "broker": record.broker,
                    "reportDate": record.report_date,
                    "sourceUrl": record.pdf_url,
                    "reason": audit.get("status") or audit.get("reason") or "추출 결과 없음",
                })
            except SourceRateLimited as exc:
                source_stop.set()
                group_failures.append({
                    "reportId": record.report_id,
                    "ticker": record.ticker,
                    "name": record.name,
                    "broker": record.broker,
                    "reportDate": record.report_date,
                    "sourceUrl": record.pdf_url,
                    "reason": str(exc),
                    "retryable": True,
                })
                break
            except Exception as exc:
                group_failures.append({
                    "reportId": record.report_id,
                    "ticker": record.ticker,
                    "name": record.name,
                    "broker": record.broker,
                    "reportDate": record.report_date,
                    "sourceUrl": record.pdf_url,
                    "reason": str(exc),
                })
        return group_items, group_audits, group_failures, attempted

    processed_report_count = 0
    groups = list(selected_groups.values())
    with ThreadPoolExecutor(max_workers=max(1, min(args.pdf_workers, 4))) as executor:
        futures = {executor.submit(process_group, group): group for group in groups}
        for future in as_completed(futures):
            try:
                items, group_audits, group_failures, attempted = future.result()
                extracted.extend(items)
                audits.extend(group_audits)
                failures.extend(group_failures)
                processed_report_count += attempted
            except Exception as exc:  # defensive: individual reports are caught above
                group = futures[future]
                record = group[0]
                failures.append({
                    "reportId": record.report_id,
                    "ticker": record.ticker,
                    "name": record.name,
                    "broker": record.broker,
                    "reportDate": record.report_date,
                    "sourceUrl": record.pdf_url,
                    "reason": f"group processing failed: {exc}",
                })

    pre_fnguide_reprocessed_ids = {
        str(row["reportId"])
        for row in [*audits, *naver_pdf_audits]
        if row.get("reportId") is not None
    }
    pre_fnguide_reprocessed_ids.update(record.report_id for record in naver_records)
    pre_fnguide_map = merge_reprocessed_observations(
        previous, extracted, pre_fnguide_reprocessed_ids,
    )
    pre_fnguide_observations = list(pre_fnguide_map.values())
    pre_fnguide_consensus = aggregate_consensus(
        pre_fnguide_observations,
        minimum_confidence=args.minimum_confidence,
        as_of=as_of,
        recency_window_days=args.consensus_window_days,
    )
    pre_fnguide_current_tickers = {str(row["ticker"]) for row in pre_fnguide_consensus}
    pre_fnguide_observation_tickers = {row.ticker for row in pre_fnguide_observations}
    fnguide_target_tickers = pre_fnguide_observation_tickers - pre_fnguide_current_tickers
    indexed_names = {row.ticker: row.name for row in naver_records}
    indexed_names.update({row.ticker: row.name for row in records})
    for row in pre_fnguide_observations:
        indexed_names.setdefault(row.ticker, row.name)
    fnguide_candidates = [] if args.skip_fnguide_backfill else sorted(fnguide_target_tickers)
    if args.max_fnguide_tickers:
        fnguide_candidates = fnguide_candidates[: args.max_fnguide_tickers]
    fnguide_audits: list[dict] = []
    fnguide_failures: list[dict] = []
    fnguide_observations: list[ForecastObservation] = []

    def process_fnguide(ticker: str):
        if args.fnguide_pause_seconds:
            time.sleep(max(0.0, float(args.fnguide_pause_seconds)))
        return fetch_fnguide_consensus(
            ticker,
            indexed_names.get(ticker, ticker),
            as_of=as_of,
            freshness_days=args.freshness_days,
            timeout=args.timeout,
        )

    if fnguide_candidates:
        with ThreadPoolExecutor(max_workers=max(1, min(args.fnguide_workers, 8))) as executor:
            futures = {executor.submit(process_fnguide, ticker): ticker for ticker in fnguide_candidates}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    items, audit = future.result()
                    fnguide_audits.append(audit)
                    fnguide_observations.extend(items)
                    if not items:
                        fnguide_failures.append({
                            "reportId": f"fnguide-{ticker}",
                            "ticker": ticker,
                            "name": indexed_names.get(ticker, ticker),
                            "sourceUrl": fnguide_consensus_url(ticker),
                            "reason": audit.get("status") or "올해·내년 집계 예상실적 없음",
                        })
                except Exception as exc:
                    fnguide_failures.append({
                        "reportId": f"fnguide-{ticker}",
                        "ticker": ticker,
                        "name": indexed_names.get(ticker, ticker),
                        "sourceUrl": fnguide_consensus_url(ticker),
                        "reason": str(exc),
                        "retryable": isinstance(exc, (SourceRateLimited, TimeoutError, OSError)),
                    })
    extracted.extend(fnguide_observations)
    failures.extend(fnguide_failures)

    reprocessed_report_ids = {
        str(row["reportId"])
        for row in [*audits, *naver_pdf_audits]
        if row.get("reportId") is not None
    }
    reprocessed_report_ids.update(record.report_id for record in naver_records)
    reprocessed_report_ids.update(item.report_id for item in fnguide_observations)
    combined = merge_reprocessed_observations(previous, extracted, reprocessed_report_ids)
    for item in load_supplements(Path(args.supplements) if args.supplements else None, as_of, args.freshness_days):
        combined[_observation_key(item)] = item
    observations = sorted(
        combined.values(),
        key=lambda item: (item.ticker, item.period, item.broker, item.report_date),
    )
    consensus = aggregate_consensus(
        observations,
        minimum_confidence=args.minimum_confidence,
        as_of=as_of,
        recency_window_days=args.consensus_window_days,
    )
    indexed_tickers = {row.ticker: row.name for row in naver_records}
    indexed_tickers.update({row.ticker: row.name for row in records})
    extracted_tickers = {row.ticker for row in observations}
    missing_tickers, pending_tickers = classify_uncovered_tickers(
        indexed_tickers, extracted_tickers, failures
    )
    current_consensus_tickers = {str(row["ticker"]) for row in consensus}
    current_gap_tickers = extracted_tickers - current_consensus_tickers
    recent_naver_tickers = {row.ticker for row in naver_pdf_candidates}
    naver_pdf_attempted_tickers = {
        str(row.get("ticker"))
        for row in [*naver_pdf_audits, *naver_pdf_failures]
        if row.get("ticker")
    }
    fnguide_failure_by_ticker = {
        str(row["ticker"]): str(row.get("reason") or "집계 예상실적 없음")
        for row in fnguide_failures
        if row.get("ticker")
    }
    current_consensus_gaps = []
    for ticker in sorted(current_gap_tickers):
        if ticker in fnguide_failure_by_ticker:
            reason = f"FnGuide 공개 집계에도 올해·내년 수치 없음: {fnguide_failure_by_ticker[ticker]}"
        elif ticker not in recent_naver_tickers:
            reason = (
                f"최근 {args.freshness_days}일 네이버 증권사 보고서 없음; "
                "다른 외부 기사·보고서 탐색 필요"
            )
        elif ticker not in naver_pdf_attempted_tickers:
            reason = "최근 원문 PDF 미시도; 다음 보충 실행 대상"
        elif naver_pdf_download_state["rateLimited"]:
            reason = "원문 PDF 소스 차단으로 일부 미처리"
        else:
            reason = "최근 증권사 원문을 판독했으나 현재 연도·내년 영업이익 수치 미추출"
        current_consensus_gaps.append({
            "ticker": ticker,
            "name": indexed_tickers.get(ticker) or next(
                (row.name for row in observations if row.ticker == ticker), ticker
            ),
            "reason": reason,
        })
    recovered_sources: dict[str, set[str]] = {}
    for row in consensus:
        ticker = str(row["ticker"])
        for estimate in row.get("estimates") or []:
            source_url = str(estimate.get("sourceUrl") or "")
            if "stock.pstatic.net" in source_url:
                recovered_sources.setdefault(ticker, set()).add("네이버 증권사 원문 PDF")
            elif "kwcomp.fnguide.com" in source_url:
                recovered_sources.setdefault(ticker, set()).add("FnGuide 공개 집계")
    recovered_tickers = set(recovered_sources)
    current_consensus_recovered = [
        {
            "ticker": ticker,
            "name": next((row["name"] for row in consensus if row["ticker"] == ticker), ticker),
            "periods": sorted(row["period"] for row in consensus if row["ticker"] == ticker),
            "sources": sorted(recovered_sources[ticker]),
        }
        for ticker in sorted(recovered_tickers)
    ]

    _json_write(output_dir / "forecast_observations.json", [asdict(row) for row in observations])
    _csv_write(
        output_dir / "forecast_observations.csv",
        [asdict(row) for row in observations],
        ForecastObservation.__dataclass_fields__,
    )
    _json_write(output_dir / "forecast_consensus.json", consensus)
    consensus_exclusions = [
        {
            "ticker": row["ticker"],
            "name": row["name"],
            "period": row["period"],
            **excluded,
        }
        for row in consensus
        for excluded in row["excludedEstimates"]
    ]
    _json_write(output_dir / "consensus_exclusions.json", consensus_exclusions)
    _csv_write(
        output_dir / "forecast_consensus.csv",
        consensus,
        (
            "ticker", "name", "period", "scope", "status", "freshBrokerCount",
            "operatingProfitMedianKrw100m", "operatingProfitMinKrw100m",
            "operatingProfitMaxKrw100m", "salesMedianKrw100m", "latestReportDate",
        ),
    )
    _json_write(output_dir / "extraction_audit.json", sorted(audits, key=lambda row: row["reportId"]))
    _json_write(
        output_dir / "naver_extraction_audit.json",
        sorted(naver_audits, key=lambda row: row["reportId"]),
    )
    _json_write(
        output_dir / "naver_pdf_detail_audit.json",
        sorted(naver_pdf_detail_audits, key=lambda row: row["reportId"]),
    )
    _json_write(
        output_dir / "naver_pdf_extraction_audit.json",
        sorted(naver_pdf_audits, key=lambda row: row["reportId"]),
    )
    _json_write(
        output_dir / "naver_pdf_failures.json",
        sorted(naver_pdf_failures, key=lambda row: row["reportId"]),
    )
    _json_write(
        output_dir / "fnguide_consensus_audit.json",
        sorted(fnguide_audits, key=lambda row: row["ticker"]),
    )
    _json_write(
        output_dir / "fnguide_consensus_failures.json",
        sorted(fnguide_failures, key=lambda row: row["ticker"]),
    )
    _json_write(output_dir / "extraction_failures.json", sorted(failures, key=lambda row: row["reportId"]))
    _json_write(output_dir / "missing_tickers.json", missing_tickers)
    _json_write(output_dir / "pending_tickers.json", pending_tickers)
    _json_write(output_dir / "current_consensus_gaps.json", current_consensus_gaps)
    _json_write(output_dir / "current_consensus_recovered.json", current_consensus_recovered)
    if download_state["rateLimited"] or naver_pdf_download_state["rateLimited"]:
        run_status = "PDF 소스 차단으로 부분완료"
    elif index_audit["pageFailures"] or naver_audit["pageFailures"]:
        run_status = "목록 부분실패"
    elif index_source_limited or naver_source_limited:
        run_status = "목록 캐시 사용"
    else:
        run_status = "정상"
    report = {
        "status": run_status,
        "generatedAtKST": datetime.now(KST).isoformat(timespec="seconds"),
        "index": index_audit,
        "naverIndex": naver_audit,
        "selectedReportCount": len(selected),
        "hankyungPdfBackfillSkipped": bool(args.skip_hankyung_pdf_backfill),
        "selectedTickerCount": len({row.ticker for row in selected}),
        "sourceTickerCount": len(indexed_tickers),
        "reportsProcessedThisRun": processed_report_count,
        "pdfDownloadsThisRun": download_state["count"],
        "naverPdfTargetTickerCount": len(naver_pdf_target_tickers),
        "naverPdfCandidateReportCount": len(naver_pdf_candidates),
        "naverPdfDetailSuccessCount": len(naver_pdf_detail_audits),
        "naverPdfDownloadsThisRun": naver_pdf_download_state["count"],
        "naverPdfExtractionSuccessCount": sum(bool(row.get("observationCount")) for row in naver_pdf_audits),
        "naverPdfRecoveredTickerCount": len(pre_fnguide_current_tickers - provisional_current_tickers),
        "fnguideTargetTickerCount": len(fnguide_target_tickers),
        "fnguideCandidateTickerCount": len(fnguide_candidates),
        "fnguideExtractionSuccessCount": sum(bool(row.get("observationCount")) for row in fnguide_audits),
        "fnguideRecoveredTickerCount": len(current_consensus_tickers - pre_fnguide_current_tickers),
        "sourceSupplementedTickerCount": len(recovered_tickers),
        "naverPdfCurrentTickerCount": sum(
            "네이버 증권사 원문 PDF" in sources for sources in recovered_sources.values()
        ),
        "fnguideCurrentTickerCount": sum(
            "FnGuide 공개 집계" in sources for sources in recovered_sources.values()
        ),
        "observedTickerWithoutCurrentConsensusCount": len(current_gap_tickers),
        "indexSourceRateLimited": index_source_limited,
        "naverSourceRateLimited": naver_source_limited,
        "sourceRateLimited": download_state["rateLimited"],
        "sourceRateLimitReason": download_state["reason"],
        "naverPdfSourceRateLimited": naver_pdf_download_state["rateLimited"],
        "naverPdfSourceRateLimitReason": naver_pdf_download_state["reason"],
        "observationCount": len(observations),
        "observationTickerCount": len(extracted_tickers),
        "freshConsensusPointCount": len(consensus),
        "freshConsensusTickerCount": len({row["ticker"] for row in consensus}),
        "validConsensusPointCount": sum(row["status"] == "유효 컨센서스" for row in consensus),
        "referenceConsensusPointCount": sum(row["status"] == "참고 컨센서스" for row in consensus),
        "externalAggregateConsensusPointCount": sum(row["status"] == "외부 집계 컨센서스" for row in consensus),
        "individualEstimatePointCount": sum(row["status"] == "개별 추정치" for row in consensus),
        "disagreementReviewPointCount": sum(row["status"] == "불일치 검토" for row in consensus),
        "consensusExcludedEstimateCount": len(consensus_exclusions),
        "missingTickerCount": len(missing_tickers),
        "pendingTickerCount": len(pending_tickers),
        "reportFailureCount": len(failures),
        "naverSummaryReportCount": len(naver_records),
        "naverSummarySuccessCount": sum(bool(row["observationCount"]) for row in naver_audits),
        "freshnessDays": args.freshness_days,
        "consensusWindowDays": args.consensus_window_days,
        "minimumConfidence": args.minimum_confidence,
        "scoreboardIntegrated": False,
        "scoreboardIntegrationNote": "사용자와 연결 방식 확정 전까지 별도 출력만 생성",
    }
    _json_write(output_dir / "run_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    today = datetime.now(KST).date()
    parser = argparse.ArgumentParser(description="증권사 보고서 예상실적 별도 수집 엔진")
    parser.add_argument("--start-date", default=f"{today.year}-01-01")
    parser.add_argument("--end-date", default=today.isoformat())
    parser.add_argument("--output-dir", default="test_output/forecast_engine")
    parser.add_argument("--cache-dir", default="cache")
    parser.add_argument("--supplements", help="검증된 기사/공식자료 JSON 또는 CSV")
    parser.add_argument("--freshness-days", type=int, default=90)
    parser.add_argument("--minimum-confidence", type=float, default=0.8)
    parser.add_argument("--consensus-window-days", type=int, default=45)
    parser.add_argument("--reports-per-ticker-broker", type=int, default=2)
    parser.add_argument("--skip-hankyung-pdf-backfill", action="store_true", help="한경 PDF 재수집을 생략하고 저장 관측값 유지")
    parser.add_argument("--index-workers", type=int, default=4)
    parser.add_argument("--naver-page-size", type=int, default=50)
    parser.add_argument("--naver-pause-seconds", type=float, default=0.15)
    parser.add_argument("--reuse-naver-index", action="store_true", help="직전 정상 저장 목록을 재판독")
    parser.add_argument("--skip-naver-pdf-backfill", action="store_true", help="최신값 누락 종목의 네이버 원문 PDF 보충 생략")
    parser.add_argument("--naver-pdf-workers", type=int, default=4)
    parser.add_argument("--naver-pdf-pause-seconds", type=float, default=0.15)
    parser.add_argument("--naver-pdf-reports-per-ticker-broker", type=int, default=1)
    parser.add_argument("--max-naver-pdf-reports", type=int, default=0, help="원문 PDF 보충 시험 제한; 0은 제한 없음")
    parser.add_argument("--skip-fnguide-backfill", action="store_true", help="FnGuide 공개 집계 컨센서스 보충 생략")
    parser.add_argument("--fnguide-workers", type=int, default=6)
    parser.add_argument("--fnguide-pause-seconds", type=float, default=0.05)
    parser.add_argument("--max-fnguide-tickers", type=int, default=0, help="FnGuide 보충 시험 제한; 0은 제한 없음")
    parser.add_argument("--pdf-workers", type=int, default=2)
    parser.add_argument("--download-pause-seconds", type=float, default=1.5)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--max-pdf-pages", type=int, default=14)
    parser.add_argument("--max-reports", type=int, default=0, help="시험 실행용; 0은 제한 없음")
    parser.add_argument("--index-only", action="store_true")
    parser.add_argument("--incremental", action="store_true", help="이미 성공 저장된 report_id 건너뛰기")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_engine(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"정상", "목록 수집 완료", "목록 캐시 사용"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
