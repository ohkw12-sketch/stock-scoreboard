"""OpenDART reported-fundamental collector for the whole KRX universe.

Secrets are read from DART_API_KEY and are never written to cache or logs.
"""
from __future__ import annotations

import io
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


KST = timezone(timedelta(hours=9))
API_ROOT = "https://opendart.fss.or.kr/api"
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
REPORT_QUARTERS = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
QUARTER_REPORTS = {quarter: code for code, quarter in REPORT_QUARTERS.items()}


def _json_save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _json_load(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _period_end(year: int, quarter: int) -> str:
    return str(pd.Period(f"{year}Q{quarter}", freq="Q").end_time.date())


def _api_key() -> str | None:
    key = os.getenv("DART_API_KEY", "").strip()
    if key:
        return key
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value, _ = winreg.QueryValueEx(handle, "DART_API_KEY")
            return str(value).strip() or None
        except OSError:
            return None
    return None


def _request_bytes(endpoint: str, params: dict, timeout: int = 30) -> bytes:
    url = f"{API_ROOT}/{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "stock-scoreboard/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _request_json(endpoint: str, params: dict, retries: int, pause: float) -> dict:
    global _LAST_REQUEST_AT
    error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            # OpenDART may close connections when a whole-market run bursts requests.
            # Enforce one shared interval across all workers, including successful calls.
            with _RATE_LOCK:
                wait = pause - (time.monotonic() - _LAST_REQUEST_AT)
                if wait > 0:
                    time.sleep(wait)
                _LAST_REQUEST_AT = time.monotonic()
            return json.loads(_request_bytes(endpoint, params).decode("utf-8"))
        except Exception as exc:
            error = exc
            if attempt < retries:
                time.sleep(pause * attempt)
    raise RuntimeError(f"OpenDART {endpoint} failed after {retries} attempts") from error


def _load_corp_codes(cache_dir: Path, key: str, max_age_days: int = 7) -> pd.DataFrame:
    path = cache_dir / "dart_corp_codes.csv"
    if path.exists():
        age = datetime.now().timestamp() - path.stat().st_mtime
        if age <= max_age_days * 86400:
            return pd.read_csv(path, dtype=str).fillna("")

    payload = _request_bytes("corpCode.xml", {"crtfc_key": key}, timeout=60)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        xml_name = archive.namelist()[0]
        root = ET.fromstring(archive.read(xml_name))
    rows = []
    for item in root.findall("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        if stock_code:
            rows.append({
                "ticker": stock_code.zfill(6),
                "corp_code": (item.findtext("corp_code") or "").strip(),
                "corp_name": (item.findtext("corp_name") or "").strip(),
                "modify_date": (item.findtext("modify_date") or "").strip(),
            })
    frame = pd.DataFrame(rows).drop_duplicates("ticker", keep="last")
    cache_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return frame


def _amount(value) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text or text == "-":
        return np.nan
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text)
    except ValueError:
        return np.nan


def _growth(current: float, previous: float) -> tuple[float, str]:
    if not np.isfinite(current) or not np.isfinite(previous) or previous == 0:
        return np.nan, "비교불가"
    if previous < 0 < current:
        return 999.0, "흑자전환"
    if previous > 0 > current:
        return -999.0, "적자전환"
    if previous < 0 and current < 0:
        return (current - previous) / abs(previous) * 100, "적자개선율"
    return (current / previous - 1) * 100, "증가율"


def _pick_account(rows: list[dict], kind: str) -> dict | None:
    if kind == "sales":
        ids = {"ifrs-full_Revenue", "ifrs_Revenue"}
        exact = {"매출액", "수익(매출액)", "영업수익"}
        contains = ("매출액", "영업수익")
    else:
        ids = {"dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"}
        exact = {"영업이익", "영업이익(손실)"}
        contains = ("영업이익",)
    def completeness(row: dict) -> int:
        fields = ("thstrm_add_amount", "frmtrm_add_amount", "thstrm_amount", "frmtrm_amount")
        amount_score = sum(bool(str(row.get(field) or "").strip()) for field in fields)
        statement_score = 5 if str(row.get("sj_div", "")).upper() == "IS" else 0
        return statement_score + amount_score

    id_matches = [row for row in rows if str(row.get("account_id", "")) in ids]
    if id_matches:
        return max(id_matches, key=completeness)
    exact_matches = [row for row in rows if str(row.get("account_nm", "")).strip() in exact]
    if exact_matches:
        return max(exact_matches, key=completeness)
    contains_matches = [row for row in rows if any(token in str(row.get("account_nm", "")) for token in contains)]
    return max(contains_matches, key=completeness) if contains_matches else None


def parse_financial_payload(payload: dict, ticker: str, name: str, sector: str,
                            report_year: int, report_code: str, fs_div: str) -> dict | None:
    if payload.get("status") != "000" or not payload.get("list"):
        return None
    rows = payload["list"]
    sales = _pick_account(rows, "sales")
    operating = _pick_account(rows, "operating")
    if not sales or not operating:
        return None
    selected = [sales, operating]
    # Both accounts must describe the same report and statement basis.
    for field, expected in (("fs_div", fs_div), ("bsns_year", str(report_year)),
                            ("reprt_code", report_code)):
        if any(str(row.get(field) or expected) != str(expected) for row in selected):
            return None
    receipts = {str(row.get("rcept_no")) for row in selected if row.get("rcept_no")}
    if len(receipts) > 1:
        return None
    # Interim income statements expose cumulative YTD values in *_add_amount.
    # Annual reports use the ordinary thstrm/frmtrm fields.
    sales_now = _amount(sales.get("thstrm_add_amount") or sales.get("thstrm_amount"))
    sales_prev = _amount(sales.get("frmtrm_add_amount") or sales.get("frmtrm_amount"))
    op_now = _amount(operating.get("thstrm_add_amount") or operating.get("thstrm_amount"))
    op_prev = _amount(operating.get("frmtrm_add_amount") or operating.get("frmtrm_amount"))
    # For interim income statements DART also exposes the standalone quarter.
    # Keep it next to the cumulative value; the value board must use Q2 alone,
    # never the H1 cumulative amount, for its current valuation.
    sales_quarter_now = _amount(sales.get("thstrm_amount"))
    sales_quarter_prev = _amount(sales.get("frmtrm_q_amount") or sales.get("frmtrm_amount"))
    op_quarter_now = _amount(operating.get("thstrm_amount"))
    op_quarter_prev = _amount(operating.get("frmtrm_q_amount") or operating.get("frmtrm_amount"))
    sales_growth, sales_basis = _growth(sales_now, sales_prev)
    op_growth, op_basis = _growth(op_now, op_prev)
    if not np.isfinite(sales_now) or not np.isfinite(op_now):
        return None
    period_text = str(sales.get("thstrm_dt") or operating.get("thstrm_dt") or "")
    dates = re.findall(r"\d{4}[.-]\d{2}[.-]\d{2}", period_text)
    report_month_day = {"11013": "03-31", "11012": "06-30", "11014": "09-30", "11011": "12-31"}
    as_of = dates[-1].replace(".", "-") if dates else f"{report_year}-{report_month_day.get(report_code, '12-31')}"
    if as_of != _period_end(report_year, REPORT_QUARTERS[report_code]):
        return None
    return {
        "ticker": ticker, "name": name, "sector": sector, "as_of": as_of,
        "sales_1y_growth": round(sales_growth, 4), "op_1y_growth": round(op_growth, 4),
        "sales_current": sales_now, "sales_previous": sales_prev,
        "op_current": op_now, "op_previous": op_prev,
        "sales_quarter_current": sales_quarter_now, "sales_quarter_previous": sales_quarter_prev,
        "op_quarter_current": op_quarter_now, "op_quarter_previous": op_quarter_prev,
        "sales_growth_basis": sales_basis, "op_growth_basis": op_basis,
        "report_year": report_year, "report_code": report_code, "fs_div": fs_div,
        "receipt": next(iter(receipts), None),
        "quarter_as_of": as_of,
        "quarter_value_verified": report_code == "11013" or (
            report_code != "11011" and all(str(row.get("thstrm_add_amount") or "").strip()
                                          for row in selected)),
        "forward_pe": np.nan, "consensus_change_1d": np.nan,
        "consensus_change_5d": np.nan, "consensus_change_20d": np.nan,
        "analyst_count": np.nan, "source": "OpenDART 공시실적", "status": "정상",
        "collected_at": datetime.now(KST).isoformat(timespec="seconds"),
    }


def _parse_multi_account_rows(rows: list[dict], universe: pd.DataFrame,
                              report_year: int, report_code: str) -> pd.DataFrame:
    """Parse fnlttMultiAcnt rows, preferring consolidated statements per stock."""
    if not rows:
        return pd.DataFrame()
    lookup = universe.set_index("ticker")[["name", "sector"]].to_dict("index")
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        ticker = str(row.get("stock_code") or "").strip().zfill(6)
        if ticker in lookup:
            grouped.setdefault(ticker, []).append(row)
    parsed = []
    for ticker, stock_rows in grouped.items():
        info = lookup[ticker]
        result = None
        for basis in ("CFS", "OFS"):
            selected = [row for row in stock_rows if str(row.get("fs_div", "")).upper() == basis]
            if not selected:
                continue
            result = parse_financial_payload({"status": "000", "list": selected}, ticker,
                                             info["name"], info["sector"], report_year, report_code, basis)
            if result:
                break
        if result:
            result["quarter_as_of"] = result["as_of"]
            parsed.append(result)
    return pd.DataFrame(parsed)


def _collect_bulk_period_values(universe: pd.DataFrame, key: str, config: dict,
                                report_year: int, report_code: str) -> tuple[pd.DataFrame, list[dict]]:
    """Refresh due companies only; retain versioned responses for each period."""
    eligible = universe[universe["corp_code"].notna()].copy()
    eligible["ticker"] = eligible["ticker"].astype(str).str.zfill(6)
    period_path = Path(config.get("cache_dir", "cache")) / "dart_periods" / f"{report_year}_{report_code}.json"
    stored = _json_load(period_path, {"version": 1, "companies": {}})
    companies = stored.get("companies", {})
    now = pd.Timestamp.now(tz="UTC")
    generations = config.get("_dart_receipt_generations", {})
    ttl = pd.Timedelta(float(config.get("dart_period_cache_hours", 168)), unit="h")
    refreshed = config.setdefault("_dart_refreshed_periods", [])
    force = config.get("force_refresh") or config.get("force_full_refresh")
    due = []
    for ticker in eligible["ticker"]:
        entry = companies.get(ticker, {})
        checked = pd.to_datetime(entry.get("checkedAt"), errors="coerce", utc=True)
        retry_at = pd.to_datetime(entry.get("nextRetryAt"), errors="coerce", utc=True)
        changed = entry.get("generation") != generations.get(ticker)
        new_change = changed and entry.get("lastAttemptGeneration") != generations.get(ticker)
        forced = force and f"{report_year}/{report_code}/{ticker}" not in refreshed
        if new_change or forced or (
            (changed or pd.isna(checked) or now - checked >= ttl) and (pd.isna(retry_at) or now >= retry_at)
        ):
            due.append(ticker)
    pending = eligible[eligible["ticker"].isin(due)]
    failures = []
    retries = int(config.get("request_retries", 3))
    pause = float(config.get("dart_pause_seconds", 0.08))
    chunk_size = max(1, min(100, int(config.get("dart_multi_company_chunk_size", 100))))
    metrics = config.setdefault("_dart_period_metrics", {"httpRequests": 0, "companyPeriodsRequested": 0})
    for start in range(0, len(pending), chunk_size):
        chunk = pending.iloc[start:start + chunk_size]
        refreshed.extend(f"{report_year}/{report_code}/{ticker}" for ticker in chunk["ticker"])
        config.setdefault("_dart_requested_tickers", []).extend(chunk["ticker"].tolist())
        try:
            metrics["httpRequests"] += 1
            metrics["companyPeriodsRequested"] += len(chunk)
            payload = _request_json("fnlttMultiAcnt.json", {
                "crtfc_key": key,
                "corp_code": ",".join(chunk["corp_code"].astype(str)),
                "bsns_year": report_year,
                "reprt_code": report_code,
            }, retries, pause)
            if payload.get("status") not in {"000", "013"}:
                raise RuntimeError(f"DART {payload.get('status')}")
            grouped = {}
            for row in payload.get("list") or []:
                ticker = str(row.get("stock_code") or "").strip().zfill(6)
                grouped.setdefault(ticker, []).append(row)
            for ticker in chunk["ticker"]:
                previous = companies.get(ticker, {})
                raw = grouped.get(ticker, [])
                entry = dict(previous, lastAttemptAt=now.isoformat(), lastAttemptGeneration=generations.get(ticker))
                stock_universe = chunk[chunk["ticker"].eq(ticker)]
                parsed = _parse_multi_account_rows(raw, stock_universe, report_year, report_code)
                if parsed.empty and config.get("_dart_single_fallback_used", 0) < int(config.get("dart_single_fallback_limit", 25)):
                    config["_dart_single_fallback_used"] = config.get("_dart_single_fallback_used", 0) + 1
                    item = stock_universe.iloc[0]
                    for basis in ("CFS", "OFS"):
                        try:
                            metrics["httpRequests"] += 1
                            fallback = _request_json("fnlttSinglAcntAll.json", {
                                "crtfc_key": key, "corp_code": item["corp_code"], "bsns_year": report_year,
                                "reprt_code": report_code, "fs_div": basis,
                            }, retries, pause)
                            candidate = parse_financial_payload(fallback, ticker, item["name"], item["sector"],
                                                                report_year, report_code, basis)
                            if candidate:
                                raw = [dict(row, stock_code=ticker, fs_div=basis) for row in fallback["list"]]
                                parsed = pd.DataFrame([candidate])
                                break
                        except Exception as exc:
                            failures.append({"ticker": ticker, "name": item["name"],
                                             "reason": f"상세계정 대체조회 호출 실패: {type(exc).__name__}"})
                if not parsed.empty:
                    signature = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
                    entry.update(rows=raw, signature=signature, checkedAt=now.isoformat(),
                                 collectedAt=previous.get("collectedAt", now.isoformat())
                                 if signature == previous.get("signature") else now.isoformat(),
                                 generation=generations.get(ticker), nextRetryAt=None, status="정상")
                else:
                    entry.update(nextRetryAt=(now + pd.Timedelta(float(
                        config.get("dart_no_data_retry_hours", 24)), unit="h")).isoformat(), status="미제공")
                    if not previous.get("rows"):
                        entry.update(generation=generations.get(ticker))
                    failures.append({"ticker": ticker, "name": "공시 기간", "reason":
                                     f"{report_year}/{report_code} 미제공 또는 계정·기간 검증 실패; 기존 값 보존"})
                companies[ticker] = entry
        except Exception as exc:
            for ticker in chunk["ticker"]:
                entry = dict(companies.get(ticker, {}))
                entry.update(lastAttemptAt=now.isoformat(), status="수집실패",
                             lastAttemptGeneration=generations.get(ticker),
                             nextRetryAt=(now + pd.Timedelta(float(
                                 config.get("dart_failure_retry_minutes", 15)), unit="m")).isoformat())
                companies[ticker] = entry
                failures.append({"ticker": ticker, "name": "공시 기간", "reason": f"호출 실패: {type(exc).__name__}"})
    if len(pending):
        _json_save(period_path, {"version": 1, "companies": companies})
    rows = []
    for ticker in eligible["ticker"]:
        entry = companies.get(ticker, {})
        if not entry.get("rows"):
            continue
        parsed = _parse_multi_account_rows(entry["rows"], eligible[eligible["ticker"].eq(ticker)], report_year, report_code)
        if not parsed.empty:
            parsed["collected_at"] = entry.get("collectedAt")
            parsed["last_verified_at"] = entry.get("checkedAt")
            parsed["verification_status"] = entry.get("status", "캐시유지")
            rows.append(parsed)
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()), failures


def _collect_bulk_quarter_values(universe: pd.DataFrame, key: str, config: dict) -> tuple[pd.DataFrame, list[dict]]:
    """Compatibility wrapper for the most recent candidate reporting period."""
    candidates = _report_candidates(datetime.now(KST))
    if not candidates:
        return pd.DataFrame(), []
    return _collect_bulk_period_values(universe, key, config, *candidates[0])


def _attach_normalized_ttm(combined: pd.DataFrame, universe: pd.DataFrame, key: str,
                           config: dict) -> tuple[pd.DataFrame, list[dict]]:
    """Reconstruct four quarters ending at each company's actual latest report.

    The existing q1..q4 field names remain calendar-quarter labels. Every window
    contains each label once; period provenance records the corresponding year.
    Only source response caches are reused, so corrections always recompute TTM.
    """
    if combined.empty:
        return combined, []
    result = combined.copy()
    fallback_year, fallback_code = _report_candidates(datetime.now(KST))[0]
    periods = {}
    requested = {}
    for index, row in result.iterrows():
        year = int(row.get("report_year", fallback_year))
        quarter = REPORT_QUARTERS.get(str(row.get("report_code", fallback_code)), 0)
        if not quarter:
            continue
        end = pd.Period(f"{year}Q{quarter}", freq="Q")
        periods[index] = [end - n for n in (3, 2, 1, 0)]
        for period in periods[index]:
            # Q2/Q3 can require the previous cumulative report; Q4 always does.
            for q in (period.quarter, max(1, period.quarter - 1)):
                requested.setdefault((period.year, QUARTER_REPORTS[q]), set()).add(row["ticker"])
    lookup = {}
    failures = []
    for (year, code), tickers in sorted(requested.items()):
        frame, errors = _collect_bulk_period_values(universe[universe["ticker"].isin(tickers)], key, config, year, code)
        failures.extend(errors)
        for row in frame.to_dict("records"):
            lookup[(row["ticker"], year, REPORT_QUARTERS[code])] = row
    # The chosen latest report is authoritative when its historical cache is absent.
    for row in result.to_dict("records"):
        year = int(row.get("report_year", fallback_year))
        q = REPORT_QUARTERS.get(str(row.get("report_code", fallback_code)))
        if q:
            lookup.setdefault((row["ticker"], year, q), row)

    for index, window in periods.items():
        stock = result.loc[index]
        ticker = stock["ticker"]
        basis = str(stock.get("fs_div", "CFS"))
        values, provenance = {}, []
        issue = None
        for period in window:
            current = lookup.get((ticker, period.year, period.quarter))
            previous = lookup.get((ticker, period.year, period.quarter - 1))
            # Q1 is also safely derivable from the same H1 report's cumulative
            # and verified standalone Q2 values, including a corrected filing.
            half = lookup.get((ticker, period.year, 2)) if period.quarter == 1 else None
            def matches(source, expected_quarter):
                if not source or str(source.get("fs_div", basis)) != basis:
                    return False
                date = source.get("as_of")
                return not date or str(date) == _period_end(period.year, expected_quarter)

            if not matches(current, period.quarter):
                current = None
            if not matches(previous, period.quarter - 1):
                previous = None
            if not matches(half, 2):
                half = None
            sources = []
            for kind in ("sales", "op"):
                value = np.nan
                if period.quarter == 1 and half and str(half.get("quarter_value_verified")) == "True":
                    value = _amount(half.get(f"{kind}_current")) - _amount(half.get(f"{kind}_quarter_current"))
                    sources = [half]
                elif current:
                    sources = [current]
                    if period.quarter == 1:
                        value = _amount(current.get(f"{kind}_current"))
                    elif period.quarter != 4 and str(current.get("quarter_value_verified")) == "True":
                        value = _amount(current.get(f"{kind}_quarter_current"))
                    elif previous:
                        value = _amount(current.get(f"{kind}_current")) - _amount(previous.get(f"{kind}_current"))
                        sources = [previous, current]
                if not np.isfinite(value):
                    issue = "기간별 공시 또는 동일 연결/별도 자료 부족"
                values[f"normalized_{kind}_q{period.quarter}"] = value
            for source in sources:
                provenance.append({"quarter": str(period), "receipt": source.get("receipt"),
                                   "reportYear": source.get("report_year"), "reportCode": source.get("report_code"),
                                   "fsDiv": source.get("fs_div", basis), "asOf": source.get("as_of"),
                                   "verifiedAt": source.get("last_verified_at")})
        if issue:
            # Retain the previous complete window only with its original dates.
            if _amount(stock.get("normalized_quarter_count")) == 4:
                result.at[index, "normalization_status"] = "이전 검증값 유지 · 최신 기간 재구성 실패"
            else:
                for column, value in values.items():
                    result.at[index, column] = value
                result.at[index, "normalized_quarter_count"] = sum(np.isfinite(values[f"normalized_op_q{q}"]) for q in (1, 2, 3, 4))
                result.at[index, "normalized_ttm_sales"] = np.nan
                result.at[index, "normalized_ttm_op"] = np.nan
                result.at[index, "normalization_status"] = "자료부족"
            failures.append({"ticker": ticker, "name": str(stock.get("name", ticker)), "reason": issue})
            continue
        for column, value in values.items():
            result.at[index, column] = value
        result.at[index, "normalized_ttm_sales"] = sum(values[f"normalized_sales_q{q}"] for q in (1, 2, 3, 4))
        result.at[index, "normalized_ttm_op"] = sum(values[f"normalized_op_q{q}"] for q in (1, 2, 3, 4))
        result.at[index, "normalized_quarter_count"] = 4
        result.at[index, "normalization_as_of"] = str(window[-1].end_time.date())
        result.at[index, "normalization_periods"] = ",".join(str(p) for p in window)
        result.at[index, "normalization_sources"] = json.dumps(provenance, ensure_ascii=False, default=str)
        result.at[index, "normalization_status"] = "정상"
        # Public legacy names mean the latest standalone quarter, not always Q2.
        result.at[index, "sales_quarter_current"] = values[f"normalized_sales_q{window[-1].quarter}"]
        result.at[index, "op_quarter_current"] = values[f"normalized_op_q{window[-1].quarter}"]
        result.at[index, "quarter_value_verified"] = True
        result.at[index, "quarter_as_of"] = str(window[-1].end_time.date())
    return result, failures


def _report_candidates(now: datetime) -> list[tuple[int, str]]:
    # Probe completed periods, including early filers; no-data cache bounds
    # repeated queries before a company's filing becomes available.
    latest = pd.Period(now.date(), freq="Q") - 1
    return [(period.year, QUARTER_REPORTS[period.quarter])
            for period in (latest - offset for offset in range(6))]


def _scan_report_changes(universe: pd.DataFrame, key: str, config: dict) -> dict:
    """Overlap disclosure-list scans detect corrections without refreshing all statements."""
    path = Path(config["cache_dir"]) / "dart_disclosure_state.json"
    state = _json_load(path, {"receipts": {}, "checkedThrough": None})
    receipts = state.get("receipts", {})
    today = datetime.now(KST).date()
    previous = pd.to_datetime(state.get("checkedThrough"), errors="coerce")
    overlap = max(1, min(30, int(config.get("dart_disclosure_overlap_days", 3))))
    first = today - timedelta(days=overlap if pd.isna(previous) else 0)
    if pd.notna(previous):
        first = previous.date() - timedelta(days=overlap)
    # A bounded three-month window avoids the all-company API's range limit.
    first = max(first, today - timedelta(days=89))
    page, changes = 1, 0
    try:
        while True:
            response = _request_json("list.json", {
                "crtfc_key": key, "bgn_de": first.strftime("%Y%m%d"),
                "end_de": today.strftime("%Y%m%d"), "pblntf_ty": "A",
                "last_reprt_at": "N", "page_no": page, "page_count": 100,
            }, int(config.get("request_retries", 3)), float(config.get("dart_pause_seconds", .2)))
            if response.get("status") == "013":
                break
            if response.get("status") != "000":
                raise RuntimeError(f"DART list {response.get('status')}")
            for filing in response.get("list") or []:
                ticker = str(filing.get("stock_code") or "").strip().zfill(6)
                receipt = str(filing.get("rcept_no") or "")
                if ticker != "000000" and receipt > str(receipts.get(ticker, "")):
                    receipts[ticker] = receipt
                    changes += 1
            if page >= int(response.get("total_page", 1)):
                break
            page += 1
        _json_save(path, {"receipts": receipts, "checkedThrough": today.isoformat()})
        scan = {"status": "정상", "checkedThrough": today.isoformat(), "changedCompanies": changes,
                "overlapDays": overlap, "pages": page}
    except Exception as exc:
        # Do not advance the watermark on a partial scan. Receipt generations
        # already observed are safe to use; the overlap is retried next time.
        scan = {"status": "수집실패", "checkedThrough": state.get("checkedThrough"),
                "problem": type(exc).__name__}
    config["_dart_receipt_generations"] = receipts
    return scan


def _fetch_one(item: dict, key: str, config: dict) -> tuple[dict | None, str | None]:
    retries = int(config.get("request_retries", 3))
    pause = float(config.get("dart_pause_seconds", 0.08))
    for report_year, report_code in _report_candidates(datetime.now(KST)):
        for fs_div in ("CFS", "OFS"):
            payload = _request_json("fnlttSinglAcntAll.json", {
                "crtfc_key": key, "corp_code": item["corp_code"],
                "bsns_year": report_year, "reprt_code": report_code, "fs_div": fs_div,
            }, retries, pause)
            parsed = parse_financial_payload(payload, item["ticker"], item["name"], item["sector"],
                                             report_year, report_code, fs_div)
            if parsed:
                return parsed, None
            if payload.get("status") not in {"000", "013"}:
                return None, f"DART {payload.get('status')}: {payload.get('message')}"
    return None, "매출액·영업이익 비교 공시자료 없음"


def collect_dart_fundamentals(prices: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, dict]:
    key = _api_key()
    if not key or len(key) != 40:
        return pd.DataFrame(), {"status": "자료없음", "source": "OpenDART", "asOfDate": None,
                                "problem": "DART_API_KEY가 없거나 40자리가 아님"}
    cache_dir = Path(config["cache_dir"])
    output_dir = Path(config["output_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    corp_codes = _load_corp_codes(cache_dir, key)
    latest = prices.sort_values("date").groupby("ticker").tail(1)[["ticker", "name", "sector", "value"]].copy()
    latest = latest.sort_values("value", ascending=False)
    universe = latest.merge(corp_codes[["ticker", "corp_code"]], on="ticker", how="left")

    cache_path = cache_dir / "dart_fundamentals_cache.csv"
    cached = pd.read_csv(cache_path, dtype={"ticker": str, "report_code": str, "receipt": str}) if cache_path.exists() else pd.DataFrame()
    scan = _scan_report_changes(universe, key, config)
    config["_dart_period_metrics"] = {"httpRequests": 0, "companyPeriodsRequested": 0}
    config["_dart_refreshed_periods"] = []
    config["_dart_requested_tickers"] = []
    config["_dart_single_fallback_used"] = 0
    failures = [{"ticker": item.ticker, "name": item.name, "reason": "DART corp_code 매핑 없음"}
                for item in universe[universe["corp_code"].isna()].itertuples()]
    rows = []
    unresolved = universe[universe["corp_code"].notna()].copy()
    for year, code in _report_candidates(datetime.now(KST)):
        if unresolved.empty:
            break
        period, errors = _collect_bulk_period_values(unresolved, key, config, year, code)
        failures.extend(errors)
        if not period.empty:
            rows.extend(period.to_dict("records"))
            unresolved = unresolved[~unresolved["ticker"].isin(period["ticker"])]
    collected = pd.DataFrame(rows)
    # Preserve whole verified rows on errors, and preserve an old complete TTM
    # with its original window until a replacement has all required periods.
    combined = cached.copy()
    if not collected.empty:
        old = {row["ticker"]: row for row in cached.to_dict("records")} if not cached.empty else {}
        merged = []
        for row in collected.to_dict("records"):
            previous_row = old.pop(row["ticker"], {})
            previous_date = pd.to_datetime(previous_row.get("as_of"), errors="coerce")
            next_date = pd.to_datetime(row.get("as_of"), errors="coerce")
            if pd.notna(previous_date) and pd.notna(next_date) and next_date < previous_date:
                previous_row["verification_status"] = "캐시유지 · 최신 보고서 재확인 실패"
                merged.append(previous_row)
                continue
            preserved = {k: v for k, v in previous_row.items() if k.startswith("normalized_") or k.startswith("normalization_")}
            merged.append(dict(preserved, **row))
        merged.extend(old.values())
        combined = pd.DataFrame(merged)
    if not combined.empty:
        combined = combined[combined["ticker"].isin(universe["ticker"])].copy()
        combined, normalization_failures = _attach_normalized_ttm(combined, universe, key, config)
        failures.extend(normalization_failures)
        combined.to_csv(cache_path, index=False, encoding="utf-8-sig")
        combined = latest.merge(combined.drop(columns=["name", "sector"], errors="ignore"), on="ticker", how="inner")
    (output_dir / "dart_failures.test.json").write_text(
        json.dumps({"count": len(failures), "items": failures}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    coverage = len(combined) / max(1, len(universe))
    as_of = pd.to_datetime(combined["as_of"], errors="coerce").max() if not combined.empty else pd.NaT
    quarter_collected = int(combined.get("op_quarter_current", pd.Series(dtype=float)).notna().sum()) if not combined.empty else 0
    quarter_coverage = quarter_collected / max(1, len(universe))
    normalized_collected = int(pd.to_numeric(
        combined.get("normalized_quarter_count", pd.Series(dtype=float)), errors="coerce",
    ).eq(4).sum()) if not combined.empty else 0
    request_failed = any("호출 실패" in item["reason"] for item in failures)
    status = "정상" if coverage >= float(config.get("minimum_dart_fundamental_coverage_ratio", 0.65)) and scan["status"] == "정상" and not request_failed else "부분실패"
    return combined, {
        "status": status, "source": "OpenDART 공시실적",
        "asOfDate": as_of.strftime("%Y-%m-%d") if pd.notna(as_of) else None,
        "requested": len(universe), "collected": len(combined), "coverageRatio": round(coverage, 4),
        "quarterRequested": len(universe), "quarterCollected": quarter_collected,
        "quarterCoverageRatio": round(quarter_coverage, 4),
        "normalizedRequested": len(universe), "normalizedCollected": normalized_collected,
        "normalizedCoverageRatio": round(normalized_collected / max(1, len(universe)), 4),
        "attemptedThisRun": len(set(config["_dart_requested_tickers"])), "failed": len(failures),
        "periodCache": config["_dart_period_metrics"], "disclosureScan": scan,
        "problem": None if status == "정상" else "공시실적 커버리지 또는 변경공시 확인 미완료",
    }
