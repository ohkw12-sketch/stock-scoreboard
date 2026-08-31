"""OpenDART reported-fundamental collector for the whole KRX universe.

Secrets are read from DART_API_KEY and are never written to cache or logs.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


KST = timezone(timedelta(hours=9))
API_ROOT = "https://opendart.fss.or.kr/api"
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


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
    # Interim income statements expose cumulative YTD values in *_add_amount.
    # Annual reports use the ordinary thstrm/frmtrm fields.
    sales_now = _amount(sales.get("thstrm_add_amount") or sales.get("thstrm_amount"))
    sales_prev = _amount(sales.get("frmtrm_add_amount") or sales.get("frmtrm_amount"))
    op_now = _amount(operating.get("thstrm_add_amount") or operating.get("thstrm_amount"))
    op_prev = _amount(operating.get("frmtrm_add_amount") or operating.get("frmtrm_amount"))
    sales_growth, sales_basis = _growth(sales_now, sales_prev)
    op_growth, op_basis = _growth(op_now, op_prev)
    if not np.isfinite(sales_growth) or not np.isfinite(op_growth):
        return None
    period_text = str(sales.get("thstrm_dt") or operating.get("thstrm_dt") or "")
    dates = re.findall(r"\d{4}[.-]\d{2}[.-]\d{2}", period_text)
    report_month_day = {"11013": "03-31", "11012": "06-30", "11014": "09-30", "11011": "12-31"}
    as_of = dates[-1].replace(".", "-") if dates else f"{report_year}-{report_month_day.get(report_code, '12-31')}"
    return {
        "ticker": ticker, "name": name, "sector": sector, "as_of": as_of,
        "sales_1y_growth": round(sales_growth, 4), "op_1y_growth": round(op_growth, 4),
        "sales_current": sales_now, "sales_previous": sales_prev,
        "op_current": op_now, "op_previous": op_prev,
        "sales_growth_basis": sales_basis, "op_growth_basis": op_basis,
        "report_year": report_year, "report_code": report_code, "fs_div": fs_div,
        "forward_pe": np.nan, "consensus_change_1d": np.nan,
        "consensus_change_5d": np.nan, "consensus_change_20d": np.nan,
        "analyst_count": np.nan, "source": "OpenDART 공시실적", "status": "정상",
        "collected_at": datetime.now(KST).isoformat(timespec="seconds"),
    }


def _report_candidates(now: datetime) -> list[tuple[int, str]]:
    year = now.year
    if now.month >= 11:
        candidates = [(year, "11014"), (year, "11012"), (year, "11013")]
    elif now.month >= 8:
        candidates = [(year, "11012"), (year, "11013")]
    elif now.month >= 5:
        candidates = [(year, "11013")]
    else:
        candidates = []
    candidates.append((year - 1, "11011"))
    return candidates


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
    cached = pd.read_csv(cache_path, dtype={"ticker": str}) if cache_path.exists() else pd.DataFrame()
    if not cached.empty:
        cached["ticker"] = cached["ticker"].astype(str).str.zfill(6)
        cached["collected_at"] = pd.to_datetime(cached["collected_at"], errors="coerce", utc=True)
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=float(config.get("dart_cache_max_age_hours", 24)))
        fresh = cached[cached["collected_at"].ge(cutoff)].drop_duplicates("ticker", keep="last")
    else:
        fresh = pd.DataFrame()
    fresh_tickers = set(fresh["ticker"]) if not fresh.empty else set()
    pending = universe[~universe["ticker"].isin(fresh_tickers)].copy()
    failures = []
    missing_codes = pending[pending["corp_code"].isna()]
    for item in missing_codes.itertuples():
        failures.append({"ticker": item.ticker, "name": item.name, "reason": "DART corp_code 매핑 없음"})
    pending = pending[pending["corp_code"].notna()]

    # Retry a rotating slice instead of bursting every unresolved company at
    # OpenDART. Successful rows leave the pending set on the next run, while
    # the cursor advances through persistent failures and missing statements.
    retry_batch_size = max(1, int(config.get("dart_retry_batch_size", 300)))
    state_path = cache_dir / "dart_retry_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"cursor": 0}
    except (OSError, ValueError):
        state = {"cursor": 0}
    pending = pending.sort_values("value", ascending=False).reset_index(drop=True)
    cursor = int(state.get("cursor", 0)) % max(len(pending), 1)
    targets = pd.concat([pending.iloc[cursor:], pending.iloc[:cursor]], ignore_index=True).head(retry_batch_size)
    next_cursor = (cursor + len(targets)) % max(len(pending), 1)
    state_path.write_text(json.dumps({"cursor": next_cursor}, indent=2), encoding="utf-8")

    rows = []
    workers = max(1, min(4, int(config.get("dart_workers", 2))))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, row._asdict(), key, config): row for row in targets.itertuples(index=False)}
        for future in as_completed(futures):
            item = futures[future]
            try:
                parsed, problem = future.result()
            except Exception as exc:
                parsed, problem = None, f"호출 실패: {type(exc).__name__}"
            if parsed:
                rows.append(parsed)
            else:
                failures.append({"ticker": item.ticker, "name": item.name, "reason": problem})

    collected = pd.DataFrame(rows)
    combined = pd.concat([fresh, collected], ignore_index=True) if not fresh.empty else collected
    if not combined.empty:
        combined["collected_at"] = pd.to_datetime(combined["collected_at"], errors="coerce", utc=True)
        combined = combined.sort_values("collected_at").drop_duplicates("ticker", keep="last")
        combined.to_csv(cache_path, index=False, encoding="utf-8-sig")
        combined = latest.merge(combined.drop(columns=["name", "sector"], errors="ignore"), on="ticker", how="inner")
    (output_dir / "dart_failures.test.json").write_text(
        json.dumps({"count": len(failures), "items": failures}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    coverage = len(combined) / max(1, len(universe))
    as_of = pd.to_datetime(combined["as_of"], errors="coerce").max() if not combined.empty else pd.NaT
    status = "정상" if coverage >= float(config.get("minimum_dart_fundamental_coverage_ratio", 0.65)) else "부분실패"
    return combined, {
        "status": status, "source": "OpenDART 공시실적",
        "asOfDate": as_of.strftime("%Y-%m-%d") if pd.notna(as_of) else None,
        "requested": len(universe), "collected": len(combined), "coverageRatio": round(coverage, 4),
        "attemptedThisRun": len(targets), "failed": len(failures),
        "problem": None if status == "정상" else "공시실적 수집 커버리지 기준 미달",
    }
