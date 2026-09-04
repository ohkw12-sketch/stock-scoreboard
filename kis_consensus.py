"""Free KIS Developers consensus collector for the scoreboard.

Only quotation/research endpoints are used.  No account or order endpoint is
present in this module.  Credentials are read from environment variables and
are never written to disk.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


KST = timezone(timedelta(hours=9))
TOKEN_URL = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
ESTIMATE_URL = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/estimate-perform"
ESTIMATE_TR_ID = "HHKST668300C0"
PRICE_URL = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price"
PRICE_TR_ID = "FHKST01010100"


def _number(value) -> float:
    text = str(value or "").strip().replace(",", "").replace("%", "")
    if not text or text in {"-", "--", "N/A"}:
        return np.nan
    try:
        return float(text)
    except ValueError:
        return np.nan


def _one_decimal(value) -> float:
    """Decode KIS estimate fields that use an implied one decimal place."""
    number = _number(value)
    return number / 10.0 if not np.isnan(number) else np.nan


def _date_text(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}-01"
    return datetime.now(KST).strftime("%Y-%m-%d")


def _period_year(period: str) -> int | None:
    digits = "".join(ch for ch in str(period or "") if ch.isdigit())
    return int(digits[:4]) if len(digits) >= 4 else None


def _add_exact_year_fields(frame: pd.DataFrame) -> pd.DataFrame:
    """Backfill exact 2026E/2027E fields in caches written by older versions."""
    if frame.empty:
        return frame
    result = frame.copy()
    for column in ("sales_2026", "op_2026", "sales_2027", "op_2027"):
        if column not in result:
            result[column] = np.nan
    for idx, row in result.iterrows():
        target_year = _period_year(row.get("estimate_period"))
        next_year = _period_year(row.get("next_estimate_period"))
        if target_year == 2026:
            result.at[idx, "sales_2026"] = row.get("forward_sales")
            result.at[idx, "op_2026"] = row.get("forward_op")
        elif target_year == 2027:
            result.at[idx, "sales_2027"] = row.get("forward_sales")
            result.at[idx, "op_2027"] = row.get("forward_op")
        if next_year == 2026:
            result.at[idx, "sales_2026"] = row.get("next_sales")
            result.at[idx, "op_2026"] = row.get("next_op")
        elif next_year == 2027:
            result.at[idx, "sales_2027"] = row.get("next_sales")
            result.at[idx, "op_2027"] = row.get("next_op")
    result["amount_unit"] = "억원"
    return result


def parse_estimate_payload(payload: dict, ticker: str, sector: str = "미분류") -> dict | None:
    """Convert KIS estimate-perform's fixed row arrays to one engine row."""
    if str(payload.get("rt_cd", "")) != "0":
        return None
    summary = payload.get("output1") or {}
    income = payload.get("output2") or []
    indicators = payload.get("output3") or []
    periods = [str(row.get("dt", "")) for row in (payload.get("output4") or [])]
    if not periods or len(income) < 4 or len(indicators) < 4:
        return None

    # KIS documents this response as fixed rows: sales, sales growth,
    # operating profit, operating-profit growth; and EBITDA, EPS, EPS growth,
    # PER.  data1..data5 align with output4's five fiscal periods.
    estimate_indexes = [i for i, period in enumerate(periods[:5]) if "E" in period.upper()]
    target = estimate_indexes[0] if estimate_indexes else len(periods[:5]) - 1
    key = f"data{target + 1}"
    prior_key = f"data{target}" if target > 0 else None
    next_key = f"data{target + 2}" if target + 1 < min(len(periods), 5) else None
    # Keep the provider's displayed growth fields for audit.  The engine uses
    # the change between adjacent amount rows whenever possible, which removes
    # any dependency on an undocumented percentage display scale.
    provider_sales_growth = _one_decimal(income[1].get(key))
    provider_op_growth = _one_decimal(income[3].get(key))
    # Amount rows are preserved as provider-native figures for audit/display.
    # Valuation uses EPS/PER, whose units are explicitly documented by KIS.
    forward_sales = _number(income[0].get(key))
    forward_op = _number(income[2].get(key))
    prior_sales = _number(income[0].get(prior_key)) if prior_key else np.nan
    prior_op = _number(income[2].get(prior_key)) if prior_key else np.nan
    next_sales = _number(income[0].get(next_key)) if next_key else np.nan
    next_op = _number(income[2].get(next_key)) if next_key else np.nan
    annual_amounts = {}
    for index, period in enumerate(periods[:5]):
        year = _period_year(period)
        if year in {2026, 2027}:
            period_key = f"data{index + 1}"
            annual_amounts[f"sales_{year}"] = _number(income[0].get(period_key))
            annual_amounts[f"op_{year}"] = _number(income[2].get(period_key))
    sales_growth = (
        round((forward_sales / prior_sales - 1) * 100, 4)
        if np.isfinite(prior_sales) and prior_sales > 0 and np.isfinite(forward_sales)
        else provider_sales_growth
    )
    if np.isfinite(prior_op) and prior_op > 0 and np.isfinite(forward_op):
        op_growth = round((forward_op / prior_op - 1) * 100, 4)
    elif np.isfinite(prior_op) and np.isfinite(forward_op) and prior_op <= 0 < forward_op:
        op_growth = 999.0
    else:
        op_growth = provider_op_growth
    eps = _one_decimal(indicators[1].get(key))
    forward_pe = _one_decimal(indicators[3].get(key))
    if np.isnan(forward_op):
        return None
    return {
        "ticker": str(ticker).zfill(6),
        "name": summary.get("item_kor_nm") or str(ticker).zfill(6),
        "sector": sector or "미분류",
        "as_of": _date_text(summary.get("estdate")),
        "estimate_period": periods[target],
        "prior_period": periods[target - 1] if target > 0 else None,
        "next_estimate_period": periods[target + 1] if target + 1 < len(periods) else None,
        "sales_1y_growth": sales_growth,
        "op_1y_growth": op_growth,
        "provider_sales_growth": provider_sales_growth,
        "provider_op_growth": provider_op_growth,
        "prior_sales": prior_sales,
        "prior_op": prior_op,
        "forward_sales": forward_sales,
        "forward_op": forward_op,
        "next_sales": next_sales,
        "next_op": next_op,
        "sales_2026": annual_amounts.get("sales_2026", np.nan),
        "op_2026": annual_amounts.get("op_2026", np.nan),
        "sales_2027": annual_amounts.get("sales_2027", np.nan),
        "op_2027": annual_amounts.get("op_2027", np.nan),
        "amount_unit": "억원",
        "future_op_basis": "흑자전환" if np.isfinite(prior_op) and np.isfinite(forward_op) and prior_op <= 0 < forward_op else "증가율",
        "forward_pe": forward_pe,
        "forward_eps": eps,
        "analyst_count": 0,
        "source": "KIS Developers 종목추정실적",
        "status": "정상",
    }


class KisConsensusClient:
    def __init__(self, app_key: str, app_secret: str, timeout: int = 15):
        self.app_key = app_key
        self.app_secret = app_secret
        self.timeout = timeout
        self.access_token: str | None = None

    @classmethod
    def from_environment(cls) -> "KisConsensusClient | None":
        key = os.getenv("KIS_APP_KEY", "").strip()
        secret = os.getenv("KIS_APP_SECRET", "").strip()
        if os.name == "nt" and (not key or not secret):
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                    if not key:
                        key = str(winreg.QueryValueEx(handle, "KIS_APP_KEY")[0]).strip()
                    if not secret:
                        secret = str(winreg.QueryValueEx(handle, "KIS_APP_SECRET")[0]).strip()
            except OSError:
                pass
        return cls(key, secret) if key and secret else None

    def authenticate(self) -> None:
        body = json.dumps({
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }).encode("utf-8")
        request = urllib.request.Request(TOKEN_URL, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        self.access_token = payload["access_token"]

    def fetch_estimate(self, ticker: str) -> dict:
        if not self.access_token:
            self.authenticate()
        query = urllib.parse.urlencode({"SHT_CD": str(ticker).zfill(6)})
        request = urllib.request.Request(f"{ESTIMATE_URL}?{query}", headers={
            "content-type": "application/json",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": ESTIMATE_TR_ID,
            "custtype": "P",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.access_token = None
            raise

    def fetch_price(self, ticker: str) -> dict:
        """Fetch a read-only domestic quote; no account or trading scope is used."""
        if not self.access_token:
            self.authenticate()
        query = urllib.parse.urlencode({
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).zfill(6),
        })
        request = urllib.request.Request(f"{PRICE_URL}?{query}", headers={
            "content-type": "application/json", "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key, "appsecret": self.app_secret,
            "tr_id": PRICE_TR_ID, "custtype": "P",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.access_token = None
            raise

    def fetch_daily_prices(self, ticker: str, start: str, end: str, adjusted: bool = True) -> dict:
        """Dated historical quotes; never infer a trade date from a live quote."""
        if not self.access_token:
            self.authenticate()
        params = {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': str(ticker).zfill(6),
                  'FID_INPUT_DATE_1': start.replace('-', ''), 'FID_INPUT_DATE_2': end.replace('-', ''),
                  'FID_PERIOD_DIV_CODE': 'D', 'FID_ORG_ADJ_PRC': '0' if adjusted else '1'}
        url = 'https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice'
        request = urllib.request.Request(url + '?' + urllib.parse.urlencode(params), headers={
            'content-type': 'application/json', 'authorization': f'Bearer {self.access_token}',
            'appkey': self.app_key, 'appsecret': self.app_secret, 'tr_id': 'FHKST03010100', 'custtype': 'P'})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)


def _revision(current: float, history: pd.DataFrame, days: int) -> float:
    if np.isnan(current) or history.empty:
        return np.nan
    cutoff = pd.Timestamp.now(tz=KST).tz_localize(None).normalize() - pd.offsets.BDay(days)
    # CSV history can contain +09:00 timestamps while older test/cache rows are
    # timezone-naive.  Compare one normalized representation so a valid KIS
    # response is not discarded with "Cannot compare tz-naive and tz-aware".
    fetched_at = pd.to_datetime(history["fetched_at"], errors="coerce", utc=True)
    fetched_at = fetched_at.dt.tz_convert(KST).dt.tz_localize(None)
    old = history[fetched_at <= cutoff]
    if old.empty:
        return np.nan
    previous = pd.to_numeric(old.iloc[-1]["forward_eps"], errors="coerce")
    if pd.isna(previous) or previous == 0:
        return np.nan
    return float((current / previous - 1) * 100)


def collect_kis_consensus(prices: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, dict]:
    """Refresh a bounded daily batch and merge it with verified cached rows."""
    cache_dir = Path(config["cache_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "kis_consensus_cache.csv"
    history_path = cache_dir / "kis_consensus_history.csv"
    failures_path = Path(config["output_dir"]) / "consensus_failures.test.json"
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    client = KisConsensusClient.from_environment()
    if client is None:
        return pd.DataFrame(), {
            "status": "설정필요", "source": "KIS Developers", "asOfDate": None,
            "problem": "KIS_APP_KEY와 KIS_APP_SECRET 환경변수가 없음",
        }

    cached = pd.read_csv(cache_path, dtype={"ticker": str}) if cache_path.exists() else pd.DataFrame()
    cached = _add_exact_year_fields(cached)
    history = pd.read_csv(history_path, dtype={"ticker": str}, parse_dates=["fetched_at"]) if history_path.exists() else pd.DataFrame()
    latest = prices.sort_values("date").groupby("ticker").tail(1).copy()
    latest["ticker"] = latest["ticker"].astype(str).str.zfill(6)
    latest = latest.sort_values("value", ascending=False)
    batch_size = int(config.get("kis_consensus_batch_size", 250))
    state_path = cache_dir / "kis_consensus_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"cursor": 0}
    tickers = latest["ticker"].tolist()
    cursor = int(state.get("cursor", 0)) % max(len(tickers), 1)
    discovery = tickers[cursor:cursor + batch_size]
    if len(discovery) < batch_size:
        discovery += tickers[:batch_size - len(discovery)]
    priority = latest.head(int(config.get("kis_consensus_priority_count", 120)))["ticker"].tolist()
    cached_tickers = cached["ticker"].astype(str).str.zfill(6).tolist() if not cached.empty else []
    targets = list(dict.fromkeys(priority + cached_tickers + discovery))
    sector_map = latest.set_index("ticker")["sector"].to_dict()

    try:
        client.authenticate()
    except Exception as exc:
        if cached.empty:
            return pd.DataFrame(), {
                "status": "수집실패", "source": "KIS Developers", "asOfDate": None,
                "problem": f"KIS 인증 실패: {type(exc).__name__}",
            }
        as_of = pd.to_datetime(cached["as_of"], errors="coerce").max()
        return cached, {
            "status": "캐시유지", "source": "KIS Developers 종목추정실적(이전 검증값)",
            "asOfDate": as_of.strftime("%Y-%m-%d") if pd.notna(as_of) else None,
            "attempted": 0, "collected": 0,
            "cachedCoverage": int(cached["ticker"].nunique()), "failed": len(targets),
            "problem": f"KIS 인증 실패로 이전 검증값 유지: {type(exc).__name__}",
        }

    rows, failures = [], []
    for ticker in targets:
        try:
            payload = client.fetch_estimate(ticker)
            row = parse_estimate_payload(payload, ticker, sector_map.get(ticker, "미분류"))
            if row:
                prior = history[history["ticker"].astype(str).str.zfill(6).eq(ticker)] if not history.empty else pd.DataFrame()
                eps = float(row.get("forward_eps", np.nan))
                row["consensus_change_1d"] = _revision(eps, prior, 1)
                row["consensus_change_5d"] = _revision(eps, prior, 5)
                row["consensus_change_20d"] = _revision(eps, prior, 20)
                rows.append(row)
            else:
                failures.append({"ticker": ticker, "name": latest.set_index("ticker").at[ticker, "name"], "reason": "KIS 추정실적 미제공"})
        except Exception as exc:
            failures.append({"ticker": ticker, "name": latest.set_index("ticker").at[ticker, "name"], "reason": f"호출실패: {type(exc).__name__}"})
        time.sleep(float(config.get("kis_consensus_pause_seconds", 0.12)))

    fresh = _add_exact_year_fields(pd.DataFrame(rows))
    now_text = datetime.now(KST).isoformat(timespec="seconds")
    if not fresh.empty:
        snapshots = fresh[["ticker", "forward_eps"]].copy()
        snapshots["fetched_at"] = now_text
        history = pd.concat([history, snapshots], ignore_index=True) if not history.empty else snapshots
        history.to_csv(history_path, index=False, encoding="utf-8-sig")
        combined = pd.concat([cached, fresh], ignore_index=True) if not cached.empty else fresh
        combined = combined.drop_duplicates("ticker", keep="last")
        combined.to_csv(cache_path, index=False, encoding="utf-8-sig")
    else:
        combined = cached
    state_path.write_text(json.dumps({"cursor": (cursor + batch_size) % max(len(tickers), 1)}, ensure_ascii=False, indent=2), encoding="utf-8")
    failures_path.write_text(json.dumps({"attemptedAt": now_text, "stocks": failures}, ensure_ascii=False, indent=2), encoding="utf-8")
    if combined.empty:
        return pd.DataFrame(), {"status": "수집실패", "source": "KIS Developers", "asOfDate": None,
                                "problem": f"{len(targets)}종목 시도했으나 추정실적 확보 0종목"}
    as_of = pd.to_datetime(combined["as_of"], errors="coerce").max()
    return combined, {
        "status": "정상" if not failures else "부분수집",
        "source": "KIS Developers 종목추정실적(월초 기준)",
        "asOfDate": as_of.strftime("%Y-%m-%d") if pd.notna(as_of) else None,
        "attempted": len(targets), "collected": len(fresh), "cachedCoverage": int(combined["ticker"].nunique()),
        "freshTickers": fresh["ticker"].tolist() if not fresh.empty else [],
        "failed": len(failures), "problem": None if not failures else "일부 종목은 KIS 추정실적 비대상 또는 호출 실패",
    }
