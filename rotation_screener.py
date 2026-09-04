"""KOSPI+KOSDAQ whole-market scoreboard engine.

The program never writes to data.json or deploys. It emits a test board with
independent rotation (p11), actionable entry (p1), and value (p2) results.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import random
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from dart_fundamentals import collect_dart_fundamentals
from kis_consensus import KisConsensusClient, collect_kis_consensus


ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
REQUIRED_PRICE_COLUMNS = {
    "date", "ticker", "name", "market", "sector", "open", "high", "low",
    "close", "volume", "value"
}


DEFAULTS = {
    "mode": "live",
    "lookback_business_days": 80,
    "rotation_scan_min_days": 20,
    "rotation_scan_max_days": 40,
    "minimum_sector_members": 3,
    "top_sector_count": 15,
    "top_stock_count": 20,
    "top_entry_count": 20,
    "top_value_count": 15,
    "minimum_value_sector_peers": 2,
    "market_snapshot_cache_max_days": 7,
    "cache_dir": "cache",
    "output_dir": "test_output",
    "base_data_file": "data.json",
    "primary_source": "pykrx",
    "fallback_sources": ["yfinance", "cache"],
    "cache_max_age_hours": 30,
    "request_retries": 3,
    "request_pause_seconds": 0.35,
    "yfinance_chunk_size": 80,
    "default_cycle_days": 25,
    "sector_overrides_file": "sector_overrides.example.csv",
    "fundamentals_file": "consensus_cache.csv",
    "fundamentals_provider": "auto",
    "consensus_provider": "file",
    "dart_cache_max_age_hours": 24,
    "dart_workers": 2,
    "dart_pause_seconds": 0.20,
    "dart_retry_batch_size": 300,
    "minimum_dart_fundamental_coverage_ratio": 0.65,
    "kis_consensus_batch_size": 250,
    "kis_consensus_priority_count": 120,
    "kis_consensus_pause_seconds": 0.12,
    "maximum_unclassified_ratio": 0.08,
    "minimum_latest_coverage_ratio": 0.98,
}


def log(message: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {message}", flush=True)


def load_config(path: Path | None, mode_override: str | None) -> dict:
    config = dict(DEFAULTS)
    if path:
        with path.open("r", encoding="utf-8") as handle:
            config.update(json.load(handle))
    if mode_override:
        config["mode"] = mode_override
    for key in ("cache_dir", "output_dir", "base_data_file", "sector_overrides_file", "fundamentals_file"):
        candidate = Path(config[key])
        config[key] = candidate if candidate.is_absolute() else ROOT / candidate
    if not 20 <= int(config["rotation_scan_min_days"]) <= int(config["rotation_scan_max_days"]) <= 40:
        raise ValueError("rotation scan range must satisfy 20 <= min <= max <= 40")
    return config


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def retry(call: Callable, attempts: int, pause: float, label: str):
    error: Exception | None = None
    for number in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:  # external libraries expose heterogeneous errors
            error = exc
            log(f"{label} failed ({number}/{attempts}): {exc}")
            if number < attempts:
                time.sleep(pause * number)
    raise RuntimeError(f"{label} failed after {attempts} attempts") from error


def normalize_prices(frame: pd.DataFrame) -> pd.DataFrame:
    renamed = frame.rename(columns={
        "Date": "date", "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume", "Value": "value",
        "Code": "ticker", "Name": "name", "Market": "market", "Sector": "sector",
    }).copy()
    missing = REQUIRED_PRICE_COLUMNS - set(renamed.columns)
    if missing:
        raise ValueError(f"price data missing columns: {sorted(missing)}")
    renamed["date"] = pd.to_datetime(renamed["date"]).dt.normalize()
    ticker_text = renamed["ticker"].astype(str).str.strip()
    # New KRX common shares and preferred shares can contain letters.
    valid = ticker_text.str.fullmatch(r"\d{1,6}|[0-9A-Z]{6}")
    renamed["ticker"] = ticker_text.where(valid).str.zfill(6)
    for column in ("open", "high", "low", "close", "volume", "value", "market_cap", "shares"):
        if column not in renamed:
            continue
        renamed[column] = pd.to_numeric(renamed[column], errors="coerce")
    renamed["sector"] = renamed["sector"].fillna("미분류").replace("", "미분류")
    renamed = renamed.dropna(subset=["date", "ticker", "close"])
    renamed = renamed[renamed["close"] > 0]
    return renamed.sort_values(["ticker", "date"]).reset_index(drop=True)


def attach_market_snapshot(prices: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, dict]:
    """Attach listed shares and price-date-aligned market cap without changing OHLC data.

    FinanceDataReader's KRX listing is used only for the latest listed-share count.
    Market cap is recomputed with the engine's verified closing price so the price and
    valuation dates cannot silently diverge.
    """
    latest_date = pd.Timestamp(prices["date"].max()).normalize()
    latest = prices.sort_values("date").groupby("ticker").tail(1)[["ticker", "close"]].copy()
    if config.get("mode") == "sample":
        latest["shares"] = latest["ticker"].astype(int).mod(90_000_000).add(10_000_000)
        latest["market_cap"] = latest["close"] * latest["shares"]
        source = "deterministic-sample"
    else:
        cache_path = Path(config["cache_dir"]) / "krx_market_snapshot.csv"
        snapshot = pd.DataFrame()
        errors = []
        try:
            import FinanceDataReader as fdr
            raw = fdr.StockListing("KRX")
            snapshot = raw.rename(columns={"Code": "ticker", "Stocks": "shares"})[["ticker", "shares"]].copy()
            snapshot["ticker"] = snapshot["ticker"].astype(str).str.zfill(6)
            snapshot["shares"] = pd.to_numeric(snapshot["shares"], errors="coerce")
            snapshot["fetched_at"] = datetime.now(KST).isoformat(timespec="seconds")
            snapshot.to_csv(cache_path, index=False, encoding="utf-8-sig")
            source = "KRX listing via FinanceDataReader"
        except Exception as exc:
            errors.append(f"live snapshot: {type(exc).__name__}")
            if cache_path.exists():
                snapshot = pd.read_csv(cache_path, dtype={"ticker": str})
                fetched = pd.to_datetime(snapshot.get("fetched_at"), errors="coerce", utc=True).max()
                if pd.notna(fetched):
                    fetched_local = fetched.tz_convert(KST).tz_localize(None).normalize()
                    now_local = pd.Timestamp.now(tz=KST).tz_localize(None).normalize()
                    age = (now_local - fetched_local).days
                else:
                    age = 999
                if age > int(config.get("market_snapshot_cache_max_days", 7)):
                    snapshot = pd.DataFrame()
                    errors.append(f"share cache age {age} days")
                source = "verified share-count cache"
            else:
                source = None
        if snapshot.empty:
            return prices.copy(), {
                "status": "자료없음", "source": source, "asOfDate": latest_date.strftime("%Y-%m-%d"),
                "coverageRatio": 0.0, "problem": "; ".join(errors) or "상장주식 수 자료 없음",
            }
        snapshot = snapshot[["ticker", "shares"]].drop_duplicates("ticker", keep="last")
        latest = latest.merge(snapshot, on="ticker", how="left")
        latest["market_cap"] = latest["close"] * latest["shares"]
    values = latest[["ticker", "shares", "market_cap"]].drop_duplicates("ticker")
    enriched = prices.drop(columns=["shares", "market_cap"], errors="ignore").merge(values, on="ticker", how="left")
    coverage = float(values["market_cap"].notna().mean()) if len(values) else 0.0
    return enriched, {
        "status": "정상" if coverage >= 0.98 else "부분수집", "source": source,
        "asOfDate": latest_date.strftime("%Y-%m-%d"), "coverageRatio": round(coverage, 4),
        "problem": None if coverage >= 0.98 else f"시가총액 커버리지 {coverage:.2%}",
    }


def _expected_completed_business_day(now: datetime | None = None) -> date:
    """Return the most recent weekday whose Korean close should be available.

    Before 18:00 KST the current session is not treated as complete.  This
    deliberately uses a weekday calendar: exchange holidays can make the
    result conservative, but can never make stale data look newer than it is.
    """
    current = now.astimezone(KST) if now is not None else datetime.now(KST)
    candidate = current.date()
    if current.hour < 18:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _business_session_lag(price_date: date, now: datetime | None = None) -> int:
    expected = _expected_completed_business_day(now)
    if price_date >= expected:
        return 0
    return int(np.busday_count(price_date, expected))


def align_to_verified_session(prices: pd.DataFrame, config: dict,
                              now: datetime | None = None) -> tuple[pd.DataFrame, dict]:
    """Discard stray later quotes and select the newest whole-market session.

    Free feeds sometimes publish a new date for a handful of securities before
    the complete KRX session is available.  Using the raw maximum date would
    reduce a 2,600-stock universe to those few rows.  A session is accepted only
    when it covers the same configured whole-market threshold used by the audit.
    """
    frame = normalize_prices(prices)
    raw_latest = pd.Timestamp(frame["date"].max()).normalize()
    daily_counts = frame.groupby("date")["ticker"].nunique().sort_index()
    # The user's coverage rule is explicitly day-over-day. A newly listed
    # ticker that exists only on the raw latest date must not enlarge the
    # denominator and make an otherwise >=98% session fail by one stock.
    prior_counts = daily_counts[daily_counts.index < raw_latest].tail(5)
    reference_count = int(prior_counts.max()) if not prior_counts.empty else int(frame["ticker"].nunique())
    minimum_count = math.ceil(reference_count * float(config["minimum_latest_coverage_ratio"]))
    completed_through = pd.Timestamp(_expected_completed_business_day(now))
    eligible = daily_counts[(daily_counts >= minimum_count) & (daily_counts.index <= completed_through)]
    if eligible.empty:
        anchor = raw_latest
    else:
        anchor = pd.Timestamp(eligible.index.max()).normalize()
    aligned = frame[frame["date"].le(anchor)].copy()
    ignored_rows = int(frame["date"].gt(anchor).sum())
    ignored_tickers = int(frame.loc[frame["date"].gt(anchor), "ticker"].nunique())
    return aligned, {
        "rawLatestPriceDate": raw_latest.strftime("%Y-%m-%d"),
        "verifiedSessionDate": anchor.strftime("%Y-%m-%d"),
        "priceBusinessSessionLag": _business_session_lag(anchor.date(), now),
        "ignoredSparseRows": ignored_rows,
        "ignoredSparseTickers": ignored_tickers,
    }


def audit_market_data(prices: pd.DataFrame, config: dict, source: str) -> dict:
    """Audit the last session without treating partial failure as unchanged data."""
    latest = prices["date"].max()
    latest_rows = prices[prices["date"].eq(latest)]
    all_tickers = set(prices["ticker"].unique())
    latest_tickers = set(latest_rows["ticker"].unique())
    missing = sorted(all_tickers - latest_tickers)
    last_identity = prices.sort_values("date").groupby("ticker").tail(1).set_index("ticker")
    missing_stocks = [{
        "ticker": ticker, "name": str(last_identity.loc[ticker, "name"]),
        "market": str(last_identity.loc[ticker, "market"]),
        "sector": str(last_identity.loc[ticker, "sector"]),
        "lastPriceDate": pd.Timestamp(last_identity.loc[ticker, "date"]).strftime("%Y-%m-%d"),
    } for ticker in missing]
    previous_dates = sorted(prices.loc[prices["date"].lt(latest), "date"].unique())
    recent_previous = prices[prices["date"].isin(previous_dates[-5:])] if previous_dates else prices.iloc[0:0]
    previous_reference_count = int(recent_previous.groupby("date")["ticker"].nunique().max()) if len(recent_previous) else len(all_tickers)
    coverage = len(latest_tickers) / max(1, previous_reference_count)
    coverage = min(1.0, coverage)
    unclassified = float(latest_rows["sector"].eq("미분류").mean()) if len(latest_rows) else 1.0
    market_counts = {
        market: int(latest_rows.loc[latest_rows["market"].eq(market), "ticker"].nunique())
        for market in ("KOSPI", "KOSDAQ")
    }
    previous_rows = prices[prices["date"].eq(previous_dates[-1])] if previous_dates else prices.iloc[0:0]
    previous_market_counts = {
        market: int(previous_rows.loc[previous_rows["market"].eq(market), "ticker"].nunique())
        for market in ("KOSPI", "KOSDAQ")
    }
    problems = []
    if coverage < float(config["minimum_latest_coverage_ratio"]):
        problems.append(f"최신 가격 종목 커버리지 {coverage:.2%} (기준 미달)")
    if unclassified > float(config["maximum_unclassified_ratio"]):
        problems.append(f"미분류 비율 {unclassified:.2%} (기준 초과)")
    session_lag = _business_session_lag(pd.Timestamp(latest).date())
    if config.get("mode") != "sample" and session_lag > 1:
        problems.append(f"가격 기준일이 완료 세션보다 {session_lag}영업일 지연 (1영업일 초과)")
    return {
        "source": source, "checkedAtKST": datetime.now(KST).isoformat(timespec="seconds"),
        "latestPriceDate": pd.Timestamp(latest).strftime("%Y-%m-%d"),
        "historicalTickerCount": len(all_tickers), "latestTickerCount": len(latest_tickers),
        "previousReferenceTickerCount": previous_reference_count,
        "latestCoverageRatio": round(coverage, 4), "marketCounts": market_counts,
        "previousMarketCounts": previous_market_counts,
        "unclassifiedRatio": round(unclassified, 4), "missingTickers": missing,
        "missingStocks": missing_stocks,
        "priceBusinessSessionLag": session_lag,
        "qualityStatus": "정상" if not problems else "부분실패",
        "problems": problems,
    }


def read_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(line for line in handle if not line.lstrip().startswith("#")):
            ticker, sector = str(row.get("ticker", "")).zfill(6), str(row.get("sector", "")).strip()
            if ticker.isdigit() and sector:
                result[ticker] = sector
    return result


def canonical_sector(name: str, industry: str, products: str = "") -> str:
    """Promote important rotation themes; otherwise retain the KRX industry."""
    text = " ".join(str(value or "") for value in (name, industry, products))
    rules = [
        ("화장품", ("화장품", "코스메틱")),
        ("2차전지/ESS", ("이차전지", "2차전지", "배터리", "축전지", "전지용 동박")),
        ("반도체", ("반도체", "웨이퍼", "파운드리", "솔더범핑")),
        ("전력기기", ("변압기", "송배전", "배전반", "고압차단기", "전력기기")),
        ("전선", ("전력용전선", "절연선 및 케이블", "초고압선", "통신케이블")),
        ("조선", ("선박 및 보트", "선박건조", "조선기자재", "해양플랜트")),
        ("방산", ("무기 및 총포탄", "방위산업", "방산제품", "정밀유도무기")),
        ("자동차", ("자동차 신품 부품", "자동차용 엔진", "자동차 차체", "완성차")),
        ("바이오/제약", ("의약품 제조업", "의료용 물질", "생물학적 제제", "신약개발")),
        ("인터넷/소프트웨어", ("소프트웨어 개발", "컴퓨터 프로그래밍", "포털 및 기타 인터넷", "게임 소프트웨어")),
        ("엔터/미디어", ("영화, 비디오물", "오디오물 출판", "방송 프로그램", "연예 매니지먼트")),
        ("금융", ("은행 및 저축기관", "보험업", "증권 및 선물 중개", "기타 금융업")),
    ]
    for sector, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return sector
    cleaned = str(industry or "").strip()
    return cleaned if cleaned and cleaned.lower() != "nan" else "미분류"


class MarketDataLoader:
    def __init__(self, config: dict):
        self.config = config
        self.cache_dir: Path = config["cache_dir"]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.report: dict = {"attempts": [], "unresolved": []}

    @property
    def cache_file(self) -> Path:
        return self.cache_dir / "krx_prices.csv.gz"

    @property
    def listing_cache_file(self) -> Path:
        return self.cache_dir / "krx_listing_desc.csv"

    def load(self) -> tuple[pd.DataFrame, str]:
        if self.config["mode"] == "sample":
            frame = generate_sample_market()
            self.report = audit_market_data(frame, self.config, "deterministic-sample")
            return frame, "deterministic-sample"
        sources = [self.config["primary_source"], *self.config["fallback_sources"]]
        seen: set[str] = set()
        errors: list[str] = []
        recovery_frames: list[pd.DataFrame] = []
        try:
            recovery_frames.append(self._cache(require_fresh=True))
        except Exception as exc:
            errors.append(f"recovery cache: {exc}")
        for source in sources:
            if source in seen:
                continue
            seen.add(source)
            try:
                if source == "pykrx":
                    frame = self._pykrx()
                    label = "pykrx"
                elif source == "cache":
                    frame = self._cache(require_fresh=True)
                    label = "fresh-cache"
                elif source == "yfinance":
                    frame = self._yfinance()
                    label = "yfinance+FinanceDataReader"
                else:
                    raise ValueError(f"unknown data source: {source}")
                merged = self._merge_recovery(frame, recovery_frames)
                merged, session_meta = align_to_verified_session(merged, self.config)
                report = audit_market_data(merged, self.config, label) | session_meta | {"attempts": list(errors)}
                recovery_frames.append(frame)
                if report["qualityStatus"] == "정상":
                    self.report = report
                    return merged, label if len(recovery_frames) == 1 else f"{label}+gap-recovery"
                errors.extend(report["problems"])
                log(f"{label} quality incomplete; attempting alternate source")
            except Exception as exc:
                errors.append(f"{source}: {exc}")
                log(f"source unavailable, trying next: {source}")
        if recovery_frames:
            merged = self._merge_recovery(recovery_frames[-1], recovery_frames[:-1])
            merged, session_meta = align_to_verified_session(merged, self.config)
            self.report = audit_market_data(merged, self.config, "partial-recovery") | session_meta | {
                "attempts": errors,
                "physicalLimitation": "모든 무료 가격원과 신선 캐시를 재시도했지만 일부 종목을 복구하지 못함",
            }
            return merged, "partial-recovery"
        raise RuntimeError("all market data sources failed\n- " + "\n- ".join(errors))

    @staticmethod
    def _merge_recovery(primary: pd.DataFrame, fallbacks: list[pd.DataFrame]) -> pd.DataFrame:
        parts = [primary, *fallbacks]
        merged = pd.concat(parts, ignore_index=True)
        # Primary values win; fallbacks only fill absent ticker/date observations.
        merged = merged.drop_duplicates(["ticker", "date"], keep="first")
        return normalize_prices(merged)

    def _cache(self, require_fresh: bool) -> pd.DataFrame:
        if not self.cache_file.exists():
            raise FileNotFoundError(f"cache not found: {self.cache_file}")
        frame = normalize_prices(pd.read_csv(self.cache_file))
        frame, _ = align_to_verified_session(frame, self.config)
        business_days_old = _business_session_lag(frame["date"].max().date())
        if require_fresh and business_days_old > 1:
            raise RuntimeError(f"cache latest price is {business_days_old} business days old")
        identity_conflicts = frame.groupby("ticker").agg(names=("name", "nunique"), markets=("market", "nunique"))
        if ((identity_conflicts["names"] > 1) | (identity_conflicts["markets"] > 1)).any():
            raise RuntimeError("cached universe contains duplicate ticker identities")
        frame = self._enrich_sectors(frame)
        return frame

    def _enrich_sectors(self, frame: pd.DataFrame) -> pd.DataFrame:
        if not self.listing_cache_file.exists():
            return frame
        listing = pd.read_csv(self.listing_cache_file, dtype={"Code": str})
        if not {"Code", "Name", "Industry"}.issubset(listing.columns):
            raise RuntimeError("KRX description cache has an unexpected schema")
        listing["ticker"] = listing["Code"].astype(str).str.zfill(6)
        if "Products" not in listing.columns:
            listing["Products"] = ""
        listing["classified_sector"] = listing.apply(
            lambda row: canonical_sector(row.get("Name", ""), row.get("Industry", ""), row.get("Products", "")),
            axis=1,
        )
        identity = listing.drop_duplicates("ticker").set_index("ticker")
        sector_map = identity["classified_sector"]
        enriched = frame.copy()
        mapped = enriched["ticker"].map(sector_map)
        enriched["sector"] = mapped.where(mapped.notna() & mapped.ne("미분류"), enriched["sector"])
        enriched["name"] = enriched["ticker"].map(identity["Name"]).fillna(enriched["name"])
        if "Market" in identity.columns:
            enriched["market"] = enriched["ticker"].map(identity["Market"]).fillna(enriched["market"])
        return normalize_prices(enriched)

    def _save_cache(self, frame: pd.DataFrame) -> None:
        frame, _ = align_to_verified_session(frame, self.config)
        report = audit_market_data(frame, self.config, "cache-write-check")
        if report["qualityStatus"] != "정상":
            log("cache not replaced because collection validation is incomplete")
            return
        frame.to_csv(self.cache_file, index=False, encoding="utf-8-sig", compression="gzip")

    def _repair_close_with_kis(self, frame: pd.DataFrame, listing: pd.DataFrame) -> pd.DataFrame:
        """Fill only missing completed-session quotes through KIS read-only prices."""
        target = pd.Timestamp(_expected_completed_business_day())
        session_tickers = set(frame.loc[frame["date"].eq(target), "ticker"])
        all_tickers = set(listing["ticker"])
        minimum = math.ceil(len(all_tickers) * float(self.config["minimum_latest_coverage_ratio"]))
        if len(session_tickers) >= minimum:
            return frame
        client = KisConsensusClient.from_environment()
        if client is None:
            return frame
        try:
            client.authenticate()
        except Exception as exc:
            log(f"KIS close recovery unavailable: {type(exc).__name__}")
            return frame
        identity = listing.drop_duplicates("ticker").set_index("ticker")
        recovered = []
        for ticker in sorted(all_tickers - session_tickers):
            try:
                payload = client.fetch_daily_prices(ticker, str(target.date()), str(target.date()), adjusted=False)
                quote = next((r for r in payload.get("output2", [])
                              if r.get("stck_bsop_date") == target.strftime("%Y%m%d")), {})
                if str(payload.get("rt_cd", "")) != "0":
                    continue
                def number(key):
                    try:
                        return float(str(quote.get(key) or "").replace(",", ""))
                    except ValueError:
                        return np.nan
                close = number("stck_clpr")
                if not np.isfinite(close) or close <= 0:
                    continue
                open_price, high, low = number("stck_oprc"), number("stck_hgpr"), number("stck_lwpr")
                volume, value = number("acml_vol"), number("acml_tr_pbmn")
                item = identity.loc[ticker]
                recovered.append({
                    "date": target, "ticker": ticker, "name": item["name"], "market": item["market"],
                    "sector": item["sector"], "open": open_price if open_price > 0 else close,
                    "high": high if high > 0 else close, "low": low if low > 0 else close,
                    "close": close, "volume": volume if np.isfinite(volume) else 0,
                    "value": value if np.isfinite(value) and value > 0 else close * max(volume, 0),
                    "Adj Close": close, "price_date_verified": True,
                })
            except Exception:
                continue
            time.sleep(float(self.config.get("kis_price_pause_seconds", 0.06)))
        if recovered:
            log(f"KIS close recovery: {len(recovered)} missing quotes repaired")
            return normalize_prices(pd.concat([frame, pd.DataFrame(recovered)], ignore_index=True))
        return frame

    def _pykrx(self) -> pd.DataFrame:
        try:
            from pykrx import stock
        except ImportError as exc:
            raise RuntimeError("pykrx is not installed; run setup_windows.bat") from exc

        end = datetime.now(KST).date()
        start = end - timedelta(days=int(self.config["lookback_business_days"]) * 2)
        trading_dates: list[date] = []
        cursor = end
        empty_streak = 0
        while cursor >= start and len(trading_dates) < int(self.config["lookback_business_days"]):
            day = cursor.strftime("%Y%m%d")
            tickers = retry(
                lambda d=day: stock.get_market_ticker_list(d, market="ALL"),
                int(self.config["request_retries"]), float(self.config["request_pause_seconds"]),
                f"pykrx calendar {day}",
            )
            if tickers:
                trading_dates.append(cursor)
                empty_streak = 0
            else:
                empty_streak += 1
                if not trading_dates and empty_streak >= 7:
                    raise RuntimeError("pykrx anonymous market access is unavailable")
            cursor -= timedelta(days=1)
        trading_dates.reverse()
        if len(trading_dates) < 45:
            raise RuntimeError(f"only {len(trading_dates)} trading days found")

        latest = trading_dates[-1].strftime("%Y%m%d")
        universe_parts = []
        for market in ("KOSPI", "KOSDAQ"):
            classification = retry(
                lambda m=market: stock.get_market_sector_classifications(latest, market=m),
                int(self.config["request_retries"]), float(self.config["request_pause_seconds"]),
                f"pykrx sector classification {market}",
            ).reset_index()
            classification.columns = [str(c).strip() for c in classification.columns]
            ticker_col = classification.columns[0]
            sector_col = next((c for c in classification.columns if "업종" in c), None)
            name_col = next((c for c in classification.columns if "종목명" in c), None)
            if not sector_col:
                raise RuntimeError("pykrx sector column not found")
            part = pd.DataFrame({
                "ticker": classification[ticker_col].astype(str).str.zfill(6),
                "sector": classification[sector_col].astype(str),
                "market": market,
            })
            # The classification response already contains names, avoiding thousands
            # of extra one-ticker requests. Keep a library fallback for schema drift.
            if name_col:
                part["name"] = classification[name_col].astype(str).to_numpy()
            else:
                part["name"] = part["ticker"].map(lambda t: stock.get_market_ticker_name(t))
            universe_parts.append(part)
        universe = pd.concat(universe_parts, ignore_index=True).drop_duplicates("ticker")

        daily_parts = []
        for number, trading_date in enumerate(trading_dates, 1):
            day = trading_date.strftime("%Y%m%d")
            daily = retry(
                lambda d=day: stock.get_market_ohlcv_by_ticker(d, market="ALL"),
                int(self.config["request_retries"]), float(self.config["request_pause_seconds"]),
                f"pykrx prices {day}",
            ).reset_index()
            if daily.empty:
                continue
            daily = daily.rename(columns={
                daily.columns[0]: "ticker", "시가": "open", "고가": "high", "저가": "low",
                "종가": "close", "거래량": "volume", "거래대금": "value",
            })
            daily["date"] = pd.Timestamp(trading_date)
            daily_parts.append(daily[["date", "ticker", "open", "high", "low", "close", "volume", "value"]])
            if number % 10 == 0:
                log(f"pykrx: {number}/{len(trading_dates)} trading days")
            time.sleep(float(self.config["request_pause_seconds"]))
        prices = pd.concat(daily_parts, ignore_index=True)
        frame = self._enrich_sectors(normalize_prices(prices.merge(universe, on="ticker", how="inner")))
        self._save_cache(frame)
        return frame

    def _yfinance(self) -> pd.DataFrame:
        try:
            import FinanceDataReader as fdr
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("fallback packages missing; run setup_windows.bat") from exc
        yf_cache = self.cache_dir / "yfinance"
        yf_cache.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(yf_cache))
        if self.listing_cache_file.exists() and not self.config.get("refresh_universe"):
            listing = pd.read_csv(self.listing_cache_file, dtype={"Code": str})
        else:
            listing = retry(
                lambda: fdr.StockListing("KRX-DESC"), int(self.config["request_retries"]),
                float(self.config["request_pause_seconds"]), "KRX descriptive listing",
            )
            listing.to_csv(self.listing_cache_file, index=False, encoding="utf-8-sig")
        listing = listing.rename(columns={
            "Code": "ticker", "Symbol": "ticker", "Name": "name", "Market": "market",
        })
        required_listing = {"ticker", "name", "market"}
        if not required_listing.issubset(listing.columns):
            raise RuntimeError(f"KRX listing schema changed: {sorted(listing.columns)}")
        listing["sector"] = listing.apply(
            lambda row: canonical_sector(row.get("name", ""), row.get("Industry", ""), row.get("Products", "")),
            axis=1,
        )
        listing = listing[listing["market"].astype(str).str.startswith(("KOSPI", "KOSDAQ"))].copy()
        listing["market"] = np.where(listing["market"].str.startswith("KOSPI"), "KOSPI", "KOSDAQ")
        listing["ticker"] = listing["ticker"].astype(str).str.strip()
        listing = listing[listing["ticker"].str.fullmatch(r"[0-9A-Z]{6}")].copy()
        listing = listing.drop_duplicates("ticker", keep=False)
        listing["yf"] = listing["ticker"] + np.where(listing["market"].eq("KOSPI"), ".KS", ".KQ")
        end = datetime.now(KST).date() + timedelta(days=1)
        start = end - timedelta(days=int(self.config["lookback_business_days"]) * 2)
        chunks = np.array_split(listing, max(1, math.ceil(len(listing) / int(self.config["yfinance_chunk_size"]))))
        parts = []
        for number, chunk in enumerate(chunks, 1):
            symbols = chunk["yf"].tolist()
            raw = retry(
                lambda s=symbols: yf.download(s, start=start, end=end, group_by="ticker", auto_adjust=False,
                                               progress=False, threads=True),
                int(self.config["request_retries"]), float(self.config["request_pause_seconds"]),
                f"yfinance chunk {number}",
            )
            for _, item in chunk.iterrows():
                try:
                    stock_frame = raw[item["yf"]].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
                    if stock_frame.empty or "Close" not in stock_frame.columns:
                        continue
                    stock_frame = stock_frame.reset_index()
                    stock_frame["ticker"], stock_frame["name"] = item["ticker"], item["name"]
                    stock_frame["market"], stock_frame["sector"] = item["market"], item.get("sector", "미분류")
                    stock_frame["value"] = pd.to_numeric(stock_frame["Close"], errors="coerce") * pd.to_numeric(stock_frame["Volume"], errors="coerce")
                    parts.append(stock_frame)
                except (KeyError, TypeError):
                    continue
            log(f"yfinance: {number}/{len(chunks)} chunks")
        if not parts:
            raise RuntimeError("yfinance returned no KRX prices")
        frame = self._enrich_sectors(normalize_prices(pd.concat(parts, ignore_index=True)))
        frame = self._repair_close_with_kis(frame, listing)
        self._save_cache(frame)
        return frame


def generate_sample_market() -> pd.DataFrame:
    """Create deterministic, structurally realistic data for offline tests."""
    rng = np.random.default_rng(20260828)
    dates = pd.bdate_range(end="2026-08-27", periods=85)
    profiles = {
        "전력기기": (0.0028, 23, "diffusion"), "반도체": (0.0020, 31, "leader"),
        "화장품": (0.0024, 18, "diffusion"), "2차전지/ESS": (0.0015, 34, "rebound"),
        "조선": (0.0012, 38, "pullback"), "방산": (0.0008, 27, "late"),
        "바이오": (-0.0004, 15, "exit"), "자동차": (-0.0008, 40, "ended"),
    }
    rows: list[dict] = []
    ticker_number = 1000
    market_base = rng.normal(0.0004, 0.008, len(dates))
    for sector_idx, (sector, (alpha, start_ago, style)) in enumerate(profiles.items()):
        for member in range(8):
            ticker_number += 1
            ticker = str(ticker_number).zfill(6)
            name = f"{sector.replace('/', '')}샘플{member + 1}"
            price = 15000 + sector_idx * 3500 + member * 700
            start_index = len(dates) - start_ago
            leader_bonus = 0.008 if style == "leader" and member < 2 else 0.0
            for index, trading_date in enumerate(dates):
                active = index >= start_index
                daily_alpha = alpha + leader_bonus if active else -0.0001
                if active and style == "pullback" and index >= len(dates) - 4:
                    daily_alpha -= 0.010
                if active and style == "rebound" and len(dates) - 9 <= index < len(dates) - 4:
                    daily_alpha -= 0.008
                if active and style == "rebound" and index >= len(dates) - 4:
                    daily_alpha += 0.012
                if active and style == "late" and index >= len(dates) - 5:
                    daily_alpha -= 0.004
                if active and style == "exit" and index >= len(dates) - 5:
                    daily_alpha -= 0.020
                if active and style == "ended" and index >= len(dates) - 12:
                    daily_alpha -= 0.012
                breadth_noise = rng.normal(0, 0.004 if style == "diffusion" else 0.009)
                ret = market_base[index] + daily_alpha + breadth_noise
                open_price = price * (1 + rng.normal(0, 0.003))
                close_price = max(1000, price * (1 + ret))
                high = max(open_price, close_price) * (1 + abs(rng.normal(0, 0.004)))
                low = min(open_price, close_price) * (1 - abs(rng.normal(0, 0.004)))
                volume = int((250_000 + member * 25_000) * (1.7 if active else 1.0) * rng.lognormal(0, 0.25))
                rows.append({
                    "date": trading_date, "ticker": ticker, "name": name,
                    "market": "KOSPI" if member < 4 else "KOSDAQ", "sector": sector,
                    "open": round(open_price), "high": round(high), "low": round(low),
                    "close": round(close_price), "volume": volume,
                    "value": round(close_price * volume),
                })
                price = close_price
    return normalize_prices(pd.DataFrame(rows))


def rank_percentile(series: pd.Series) -> pd.Series:
    if len(series) <= 1:
        return pd.Series(0.5, index=series.index)
    return series.rank(pct=True).fillna(0.5)


def symmetric_rank(series: pd.Series) -> pd.Series:
    """Map valid values symmetrically to 0..1 while leaving missing values neutral."""
    valid_count = int(series.notna().sum())
    if valid_count <= 1:
        return pd.Series(0.5, index=series.index)
    return ((series.rank(method="average") - 1) / (valid_count - 1)).fillna(0.5)


def bounded(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return float(max(low, min(high, value)))


def absolute_multiple_score(series: pd.Series, cheap: float = 3.0, expensive: float = 20.0) -> pd.Series:
    """Score positive valuation multiples against fixed absolute anchors."""
    values = pd.to_numeric(series, errors="coerce")
    scores = ((expensive - values) / (expensive - cheap) * 100).clip(0, 100)
    return scores.where(values > 0, 0).fillna(0)


def relative_discount_score(series: pd.Series) -> pd.Series:
    """Map sector premium/discount to 0..100; parity is neutral at 50."""
    values = pd.to_numeric(series, errors="coerce")
    return (50 - values / 2).clip(0, 100).fillna(0)


def trailing_return(group: pd.DataFrame, days: int) -> float:
    if len(group) <= days or group["close"].iloc[-days - 1] <= 0:
        return np.nan
    return float(group["close"].iloc[-1] / group["close"].iloc[-days - 1] - 1)


def build_daily_features(prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = prices.copy()
    by_ticker = work.groupby("ticker", group_keys=False)
    work["prev_close"] = by_ticker["close"].shift(1)
    work["ret1"] = work.groupby("ticker")["close"].pct_change()
    work["ret3"] = work.groupby("ticker")["close"].pct_change(3)
    work["ret5"] = work.groupby("ticker")["close"].pct_change(5)
    work["ma5"] = by_ticker["close"].transform(lambda s: s.rolling(5).mean())
    work["ma20"] = by_ticker["close"].transform(lambda s: s.rolling(20).mean())
    work["volume_ma20"] = by_ticker["volume"].transform(lambda s: s.rolling(20).mean())
    work["volume_ratio"] = work["volume"] / work["volume_ma20"]
    true_range = pd.concat([
        work["high"] - work["low"],
        (work["high"] - work["prev_close"]).abs(),
        (work["low"] - work["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    work["atr14"] = true_range.groupby(work["ticker"]).transform(lambda s: s.rolling(14).mean())
    work["high20_prev"] = by_ticker["high"].transform(lambda s: s.shift(1).rolling(20).max())
    market = work.groupby("date").agg(market_ret1=("ret1", "mean")).sort_index()
    market["market_index"] = (1 + market["market_ret1"].fillna(0)).cumprod()
    market["market_ret3"] = market["market_index"].pct_change(3)
    market["market_ret5"] = market["market_index"].pct_change(5)

    sector_daily = work.groupby(["date", "sector"]).agg(
        ret1=("ret1", "mean"), ret3=("ret3", "mean"), ret5=("ret5", "mean"),
        breadth=("ret1", lambda values: float((values > 0).mean())),
        turnover=("value", "sum"), members=("ticker", "nunique"),
        leader_ret5=("ret5", lambda values: float(values.nlargest(max(1, math.ceil(len(values) * 0.2))).mean())),
    ).reset_index().merge(market.reset_index(), on="date", how="left")
    for horizon in (1, 3, 5):
        sector_daily[f"rs{horizon}"] = sector_daily[f"ret{horizon}"] - sector_daily[f"market_ret{horizon}"]
    sector_daily["leader_strength"] = sector_daily["leader_ret5"] - sector_daily["market_ret5"]
    sector_daily["turnover_ma3"] = sector_daily.groupby("sector")["turnover"].transform(lambda s: s.rolling(3).mean())
    sector_daily["turnover_prev10"] = sector_daily.groupby("sector")["turnover"].transform(lambda s: s.shift(3).rolling(10).mean())
    sector_daily["turnover_change"] = sector_daily["turnover_ma3"] / sector_daily["turnover_prev10"] - 1
    return work, sector_daily


def composite_history(sector_daily: pd.DataFrame) -> pd.DataFrame:
    history = sector_daily.copy()
    components = []
    for column in ("rs1", "rs3", "rs5", "turnover_change", "breadth", "leader_strength"):
        ranked = history.groupby("date")[column].transform(rank_percentile)
        components.append(ranked)
    history["composite"] = (
        components[0] * 0.10 + components[1] * 0.16 + components[2] * 0.24 +
        components[3] * 0.16 + components[4] * 0.18 + components[5] * 0.16
    )
    return history


def episode_lengths(group: pd.DataFrame, include_open: bool = False) -> list[int]:
    active = (group["composite"] >= 0.58) & (group["rs5"] > 0)
    lengths: list[int] = []
    current = 0
    for flag in active.fillna(False):
        if flag:
            current += 1
        elif current:
            lengths.append(current)
            current = 0
    if current and include_open:
        lengths.append(current)
    return lengths


def infer_start(group: pd.DataFrame, min_days: int, max_days: int) -> pd.Timestamp:
    group = group.sort_values("date").reset_index(drop=True)
    if len(group) < min_days:
        raise ValueError(f"at least {min_days} trading days are required to infer rotation start")
    window = group.tail(max_days + 6).copy()
    signal = (window["composite"] >= 0.58) & (window["rs3"] > 0) & (
        (window["breadth"] >= 0.52) | (window["leader_strength"] >= 0.025)
    )
    sustained = signal & signal.shift(-1, fill_value=False)
    # The active cycle starts at the latest sustained re-entry after a quiet day,
    # not at the first strong reading anywhere in the observation window.
    onsets = sustained & ~signal.shift(1, fill_value=False)
    candidates = window.loc[onsets, "date"]
    if not candidates.empty:
        start = candidates.iloc[-1]
    else:
        eligible = window.tail(max_days)
        positive = eligible[eligible["rs5"].fillna(-1) > 0]
        start = positive["date"].iloc[0] if not positive.empty else eligible["date"].iloc[-min(min_days, len(eligible))]
    # min_days is the required evidence length, not a minimum age for a cycle.
    # A valid cycle may have started yesterday inside a 20-40 day observation window.
    earliest = group["date"].iloc[max(0, len(group) - max_days)]
    latest = group["date"].iloc[-1]
    return min(max(start, earliest), latest)


def classify_stage(latest: pd.Series, previous: pd.Series, elapsed: int, cycle: int, drawdown: float) -> tuple[str, str]:
    position = elapsed / max(cycle, 1)
    weakening = latest["rs3"] < -0.012 or latest["composite"] < 0.35
    if position < 0.40 and weakening and latest["rs5"] < -0.02:
        return "X조기이탈", "조기 이탈"
    if (position >= 1.0 and weakening) or (latest["rs5"] < -0.035 and latest["breadth"] < 0.35):
        return "X종료", "종료"
    if latest["rs1"] > 0 and latest["rs3"] > 0 and previous["rs3"] <= 0:
        return "⑤재반등", "재반등"
    if latest["rs1"] < 0 and latest["rs5"] > 0 and drawdown >= 0.025:
        return "④눌림", "눌림"
    if position >= 0.82 or drawdown >= 0.10:
        return "⑥후반", "후반"
    if position <= 0.22:
        return "①초기", "초기"
    if latest["breadth"] >= 0.58 and latest["turnover_change"] > 0:
        return "②확산", "확산"
    return "③주도", "주도"


def rotation_rows(stock_data: pd.DataFrame, sector_results: pd.DataFrame, limit: int) -> list[dict]:
    latest_date = stock_data["date"].max()
    current = stock_data[stock_data["date"].eq(latest_date)].copy()
    sector_map = sector_results.set_index("name")
    current = current[current["sector"].isin(sector_results["name"])].copy()
    current["sector_ret3"] = current["sector"].map(sector_map["raw_ret3"])
    current["sector_ret5"] = current["sector"].map(sector_map["raw_ret5"])
    current["stock_excess3"] = current["ret3"] - current["sector_ret3"]
    current["stock_excess5"] = current["ret5"] - current["sector_ret5"]
    current["sector_rotation_score"] = current["sector"].map(sector_map["score"])
    current["overheated"] = (current["ret1"] > 0.10) | (current["ret3"] > 0.15) | (current["ret5"] > 0.25)
    current["stock_score"] = (
        current["sector_rotation_score"].fillna(0) +
        current["stock_excess3"].fillna(0).clip(-0.08, 0.08) * 120 -
        current["overheated"].astype(int) * 32
    )
    current = current[current["ret5"].notna() & (current["value"] >= 100_000_000)]
    current = current.sort_values(["stock_score", "value"], ascending=False)
    # Avoid allowing one hot theme to occupy the entire candidate board.
    current = current[current.groupby("sector").cumcount() < 4].head(limit)
    rows = []
    for rank, item in enumerate(current.itertuples(), 1):
        sector = sector_map.loc[item.sector]
        excess = float(item.stock_excess3 or 0)
        relation = "선행" if excess >= 0.015 else "후행" if excess <= -0.015 else "동행"
        overheated = bool(item.overheated)
        stage = sector["stage"]
        signal = {
            "①초기": "순환 초기", "②확산": "확산 진행", "③주도": "주도 지속",
            "④눌림": "순환 눌림", "⑤재반등": "순환 재반등", "⑥후반": "순환 후반",
            "X조기이탈": "순환 조기이탈", "X종료": "순환 종료",
        }[stage]
        marks = ["UP"] if relation == "선행" else ["OLD"] if relation == "후행" else []
        if float(sector["riskGauge"]) >= 70 or overheated:
            marks.append("RISK")
        market_state = f"{sector['rotationType']} · {stage}"
        rows.append({
            "rank": rank, "name": item.name, "ticker": item.ticker, "sector": item.sector,
            "relation": relation, "sectorLeadLag": relation,
            "stockRs3Pct": round(float(item.stock_excess3) * 100, 2),
            "stockRs5Pct": round(float(item.stock_excess5) * 100, 2),
            "opGrowth": "미산출", "value": "가격·수급 기준", "sectorMedian": f"섹터 RS5 {sector['rs5Pct']:+.2f}%p",
            "premium": relation, "marketState": market_state,
            "marketDetail": f"3일 {float(item.ret3) * 100:+.2f}% · 5일 {float(item.ret5) * 100:+.2f}%",
            "change": "엔진 신규", "changeUntil": (latest_date + pd.offsets.BDay(5)).strftime("%Y-%m-%d"),
            "marks": marks, "signal": signal,
            "entryFit": sector.get("entryFit", "관찰"), "overheated": overheated,
            "stockEntryScore": round(float(item.stock_score), 1),
            "reason": f"순환 단계 {stage} · 섹터 대비 {relation}; 3일 초과수익 {excess * 100:+.2f}%p",
        })
    return rows


def generate_sample_fundamentals(prices: pd.DataFrame) -> pd.DataFrame:
    latest = prices.sort_values("date").groupby("ticker").tail(1)
    rows = []
    for number, item in enumerate(latest.itertuples(), 1):
        seed = int(item.ticker) * 17
        sales_previous = float(100_000_000_000 + (seed % 500) * 1_000_000_000)
        sales_growth = float(10 + seed % 55)
        sales_current = sales_previous * (1 + sales_growth / 100)
        turnaround = number % 9 == 0
        op_previous = -5_000_000_000.0 if turnaround else sales_previous * (0.04 + (seed % 7) / 100)
        op_growth = 999.0 if turnaround else float(-5 + seed % 80)
        op_current = sales_current * (0.07 + (seed % 9) / 100) if turnaround else op_previous * (1 + op_growth / 100)
        future_sales_growth = float(8 + seed % 37)
        future_op_growth = float(15 + seed % 90)
        future_turnaround = number % 11 == 0
        consensus_prior_op = -float(40 + seed % 90) if future_turnaround else float(100 + seed % 800)
        consensus_forward_op = float(60 + seed % 250) if future_turnaround else consensus_prior_op * (1 + future_op_growth / 100)
        consensus_next_op = consensus_forward_op * (1.15 + (seed % 10) / 100)
        rows.append({
            "ticker": item.ticker, "name": item.name, "sector": item.sector,
            "as_of": "2026-08-27", "sales_q3_growth": 8 + seed % 37,
            "sales_q4_growth": 6 + seed % 41, "sales_1y_growth": sales_growth,
            "op_1y_growth": op_growth, "sales_current": sales_current, "sales_previous": sales_previous,
            "op_current": op_current, "op_previous": op_previous,
            "sales_quarter_current": sales_current / 2, "sales_quarter_previous": sales_previous / 2,
            "op_quarter_current": op_current / 2, "op_quarter_previous": op_previous / 2,
            "quarter_as_of": "2026-06-30",
            "sales_growth_basis": "증가율", "op_growth_basis": "흑자전환" if turnaround else "증가율",
            "report_code": "11012", "fs_div": "CFS",
            "forward_pe": 7 + (seed % 310) / 10,
            "forward_eps": 500 + seed % 7000, "estimate_period": "2027.12E",
            "consensus_sales_1y_growth": future_sales_growth,
            "consensus_op_1y_growth": future_op_growth,
            "consensus_prior_sales": float(500 + seed % 1200),
            "consensus_prior_op": consensus_prior_op,
            "consensus_forward_sales": float(500 + seed % 1200) * (1 + future_sales_growth / 100),
            "consensus_forward_op": consensus_forward_op,
            "consensus_sales_2026": sales_current * 2 / 100_000_000,
            "consensus_op_2026": op_current * 2 / 100_000_000,
            "consensus_sales_2027": sales_current * 2 * (1 + future_sales_growth / 100) / 100_000_000,
            "consensus_op_2027": max(op_current * 2 * (1 + future_op_growth / 100), 5_000_000_000) / 100_000_000,
            "consensus_next_sales": float(500 + seed % 1200) * (1 + future_sales_growth / 100) * 1.08,
            "consensus_next_op": consensus_next_op,
            "future_op_basis": "흑자전환" if future_turnaround else "증가율",
            "prior_period": "2026.12", "next_estimate_period": "2028.12E",
            "consensus_as_of": "2026-08-27",
            "consensus_change_1d": ((seed % 9) - 3) / 10,
            "consensus_change_5d": ((seed % 19) - 5) / 10,
            "consensus_change_20d": ((seed % 31) - 8) / 10,
            "analyst_count": 2 + seed % 15, "source": "deterministic-sample", "status": "정상",
        })
    return pd.DataFrame(rows)


def merge_fundamental_sources(dart: pd.DataFrame, consensus: pd.DataFrame,
                              dart_status: dict, consensus_status: dict) -> tuple[pd.DataFrame, dict]:
    """Keep DART reported growth as the base and add KIS fields without excluding missing rows."""
    if dart.empty:
        fallback = consensus.copy()
        return fallback, {
            "status": consensus_status.get("status", "부분실패"),
            "source": consensus_status.get("source"),
            "asOfDate": consensus_status.get("asOfDate"),
            "consensusAsOfDate": consensus_status.get("asOfDate"),
            "problem": "DART 기본 성장률 없음; KIS 제공 종목만 임시 사용",
            "dart": dart_status, "consensus": consensus_status,
        }
    merged = dart.copy()
    if not consensus.empty:
        consensus = consensus.copy()
        for column in (
            "estimate_period", "prior_period", "next_estimate_period", "prior_sales", "prior_op",
            "forward_sales", "forward_op", "next_sales", "next_op", "future_op_basis", "forward_eps",
            "sales_2026", "op_2026", "sales_2027", "op_2027", "amount_unit",
        ):
            if column not in consensus:
                consensus[column] = np.nan
        supplement = consensus[[
            "ticker", "as_of", "estimate_period", "prior_period", "next_estimate_period",
            "sales_1y_growth", "op_1y_growth", "prior_sales", "prior_op",
            "forward_sales", "forward_op", "next_sales", "next_op", "future_op_basis",
            "sales_2026", "op_2026", "sales_2027", "op_2027", "amount_unit",
            "forward_pe", "forward_eps",
            "consensus_change_1d", "consensus_change_5d", "consensus_change_20d",
            "analyst_count",
        ]].copy().rename(columns={
            "as_of": "consensus_as_of",
            "sales_1y_growth": "consensus_sales_1y_growth",
            "op_1y_growth": "consensus_op_1y_growth",
            "prior_sales": "consensus_prior_sales",
            "prior_op": "consensus_prior_op",
            "forward_sales": "consensus_forward_sales",
            "forward_op": "consensus_forward_op",
            "next_sales": "consensus_next_sales",
            "next_op": "consensus_next_op",
            "sales_2026": "consensus_sales_2026",
            "op_2026": "consensus_op_2026",
            "sales_2027": "consensus_sales_2027",
            "op_2027": "consensus_op_2027",
            "forward_pe": "kis_forward_pe",
            "analyst_count": "kis_analyst_count",
        })
        merged = merged.drop(columns=[
            "consensus_change_1d", "consensus_change_5d", "consensus_change_20d",
        ], errors="ignore").merge(supplement, on="ticker", how="left")
        merged["forward_pe"] = merged["kis_forward_pe"]
        merged["analyst_count"] = merged["kis_analyst_count"]
        merged["consensus_growth_delta"] = (
            merged["consensus_op_1y_growth"] - merged["op_1y_growth"]
        )
    else:
        for column in (
            "consensus_as_of", "estimate_period", "prior_period", "next_estimate_period",
            "consensus_sales_1y_growth", "consensus_op_1y_growth", "consensus_prior_sales",
            "consensus_prior_op", "consensus_forward_sales", "consensus_forward_op",
            "consensus_next_sales", "consensus_next_op", "future_op_basis", "forward_eps",
            "consensus_sales_2026", "consensus_op_2026", "consensus_sales_2027", "consensus_op_2027",
            "consensus_growth_delta", "consensus_change_1d", "consensus_change_5d",
            "consensus_change_20d",
        ):
            merged[column] = np.nan
    merged["source"] = np.where(
        merged["consensus_op_1y_growth"].notna(),
        "OpenDART 공시실적 + KIS Developers 컨센서스", "OpenDART 공시실적",
    )
    dart_ok = dart_status.get("status") == "정상"
    consensus_ok = consensus_status.get("status") in {"정상", "부분수집", "캐시유지"}
    return merged, {
        "status": "정상" if dart_ok and consensus_ok else "부분수집",
        "source": "OpenDART 기본 성장률 + KIS 컨센서스 보정",
        "asOfDate": dart_status.get("asOfDate"),
        "consensusAsOfDate": consensus_status.get("asOfDate"),
        "requested": dart_status.get("requested"),
        "collected": dart_status.get("collected", len(dart)),
        "coverageRatio": dart_status.get("coverageRatio"),
        "consensusCoverage": int(consensus["ticker"].nunique()) if not consensus.empty else 0,
        "problem": None if dart_ok and consensus_ok else "DART 또는 KIS가 목표 커버리지에 미달",
        "dart": dart_status, "consensus": consensus_status,
    }


def load_fundamentals(prices: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, dict]:
    required = {"ticker", "as_of", "sales_1y_growth", "op_1y_growth", "forward_pe",
                "consensus_change_1d", "consensus_change_5d", "consensus_change_20d", "analyst_count"}
    if config["mode"] == "sample":
        frame = generate_sample_fundamentals(prices)
        return frame, {"status": "정상", "source": "deterministic-sample", "asOfDate": "2026-08-27"}
    provider = str(config.get("fundamentals_provider", "auto")).lower()
    dart_frame, dart_status = pd.DataFrame(), {
        "status": "자료없음", "source": "OpenDART", "asOfDate": None, "problem": "DART 미설정",
    }
    dart_available = provider == "dart" or (provider == "auto" and bool(__import__("os").getenv("DART_API_KEY")))
    if provider == "auto" and __import__("os").name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                winreg.QueryValueEx(handle, "DART_API_KEY")
            dart_available = True
        except OSError:
            pass
    if dart_available:
        dart_frame, dart_status = collect_dart_fundamentals(prices, config)
    if str(config.get("consensus_provider", "file")).lower() == "kis":
        consensus_frame, consensus_status = collect_kis_consensus(prices, config)
        return merge_fundamental_sources(dart_frame, consensus_frame, dart_status, consensus_status)
    if not dart_frame.empty:
        return dart_frame, dart_status
    path: Path = config["fundamentals_file"]
    if not path.exists():
        return pd.DataFrame(), {"status": "자료없음", "source": None, "asOfDate": None,
                                "problem": "컨센서스 공급원/API 또는 최신 consensus_cache.csv가 없음"}
    frame = pd.read_csv(path, dtype={"ticker": str})
    missing = required - set(frame.columns)
    if missing:
        return pd.DataFrame(), {"status": "부분실패", "source": str(path), "asOfDate": None,
                                "problem": f"컨센서스 파일 필수 열 누락: {sorted(missing)}"}
    frame["ticker"] = frame["ticker"].str.zfill(6)
    frame["as_of"] = pd.to_datetime(frame["as_of"], errors="coerce")
    frame = frame.sort_values("as_of").drop_duplicates("ticker", keep="last")
    latest = frame["as_of"].max()
    stale_days = (pd.Timestamp.now().normalize() - latest).days if pd.notna(latest) else 999
    status = "정상" if stale_days <= 7 else "오래된자료"
    return frame, {"status": status, "source": str(path),
                   "asOfDate": latest.strftime("%Y-%m-%d") if pd.notna(latest) else None,
                   "problem": None if status == "정상" else f"컨센서스 기준일이 {stale_days}일 경과"}


def _annualized_amount(amount: pd.Series, report_code: pd.Series) -> pd.Series:
    factors = report_code.astype(str).map({"11013": 4.0, "11012": 2.0, "11014": 4.0 / 3.0, "11011": 1.0}).fillna(1.0)
    return pd.to_numeric(amount, errors="coerce") * factors


def _amount_text(value: float) -> str:
    if not np.isfinite(value):
        return "자료없음"
    eok = value / 100_000_000
    if abs(eok) >= 10_000:
        return f"{eok / 10_000:,.2f}조원"
    return f"{eok:,.0f}억원"


def _pct(value: float, suffix: str = "%") -> str:
    return f"{value:+.1f}{suffix}" if np.isfinite(value) else "자료없음"


def build_value_board(fundamentals: pd.DataFrame, config: dict, status: dict,
                      prices: pd.DataFrame | None = None) -> dict:
    """Build normalized-value A/B rankings; growth discovery is a separate board.

    A/B ordering combines absolute and sector-relative valuation using trailing
    four-quarter normalized operating profit, then applies earnings-quality and
    source-confidence controls. Growth discovery uses independently verified evidence.
    """
    if fundamentals.empty:
        return {"status": f"가치 엔진 미갱신: {status.get('problem', '자료 없음')}",
                "rows": [], "dataStatus": status}
    data = fundamentals.copy()
    numeric_columns = (
        "sales_1y_growth", "op_1y_growth", "sales_current", "sales_previous", "op_current", "op_previous",
        "sales_quarter_current", "sales_quarter_previous", "op_quarter_current", "op_quarter_previous",
        "consensus_prior_sales", "consensus_prior_op", "consensus_forward_sales", "consensus_forward_op",
        "consensus_next_sales", "consensus_next_op", "consensus_sales_2026", "consensus_op_2026",
        "consensus_sales_2027", "consensus_op_2027", "consensus_change_1d",
        "consensus_change_5d", "consensus_change_20d", "analyst_count",
        "normalized_sales_q3", "normalized_sales_q4", "normalized_sales_q1", "normalized_sales_q2",
        "normalized_op_q3", "normalized_op_q4", "normalized_op_q1", "normalized_op_q2",
        "normalized_ttm_sales", "normalized_ttm_op", "normalized_quarter_count",
    )
    for column in numeric_columns:
        if column not in data:
            data[column] = np.nan
        data[column] = pd.to_numeric(data[column], errors="coerce")
    for column in ("report_code", "op_growth_basis", "estimate_period", "prior_period",
                   "next_estimate_period", "consensus_as_of", "quarter_as_of", "normalization_as_of"):
        if column not in data:
            data[column] = np.nan
    data["ticker"] = data["ticker"].astype(str).str.zfill(6)

    if prices is not None and not prices.empty:
        latest = prices.copy()
        latest["ticker"] = latest["ticker"].astype(str).str.zfill(6)
        latest = latest.sort_values("date").groupby("ticker").tail(1)
        keep = [column for column in ("ticker", "date", "close", "market_cap", "shares") if column in latest]
        latest = latest[keep].rename(columns={"date": "price_date", "close": "current_price"})
        data = data.merge(latest, on="ticker", how="left")
    else:
        data["price_date"], data["current_price"], data["market_cap"], data["shares"] = np.nan, np.nan, np.nan, np.nan
    for column in ("current_price", "market_cap", "shares"):
        if column not in data:
            data[column] = np.nan
        data[column] = pd.to_numeric(data[column], errors="coerce")

    # Backfill exact annual fields from old KIS cache columns. No 2025 value is
    # consumed by this calculation.
    estimate_year = data["estimate_period"].astype(str).str.extract(r"(20\d{2})", expand=False)
    prior_year = data["prior_period"].astype(str).str.extract(r"(20\d{2})", expand=False)
    next_year = data["next_estimate_period"].astype(str).str.extract(r"(20\d{2})", expand=False)
    for year in (2026, 2027):
        sales_column, op_column = f"consensus_sales_{year}", f"consensus_op_{year}"
        data.loc[data[sales_column].isna() & estimate_year.eq(str(year)), sales_column] = data["consensus_forward_sales"]
        data.loc[data[op_column].isna() & estimate_year.eq(str(year)), op_column] = data["consensus_forward_op"]
        data.loc[data[sales_column].isna() & prior_year.eq(str(year)), sales_column] = data["consensus_prior_sales"]
        data.loc[data[op_column].isna() & prior_year.eq(str(year)), op_column] = data["consensus_prior_op"]
        data.loc[data[sales_column].isna() & next_year.eq(str(year)), sales_column] = data["consensus_next_sales"]
        data.loc[data[op_column].isna() & next_year.eq(str(year)), op_column] = data["consensus_next_op"]

    data["direct_q2"] = data["sales_quarter_current"].notna() & data["op_quarter_current"].notna()
    data["q2_sales"] = data["sales_quarter_current"].where(data["direct_q2"], data["sales_current"] / 2)
    data["q2_op"] = data["op_quarter_current"].where(data["direct_q2"], data["op_current"] / 2)
    data["q2_sales_previous"] = data["sales_quarter_previous"].where(
        data["sales_quarter_previous"].notna(), data["sales_previous"] / 2,
    )
    data["q2_op_previous"] = data["op_quarter_previous"].where(
        data["op_quarter_previous"].notna(), data["op_previous"] / 2,
    )
    data["q1_sales"] = data["sales_current"] - data["q2_sales"]
    data["q1_op"] = data["op_current"] - data["q2_op"]
    data["q2_margin"] = data["q2_op"].div(data["q2_sales"].replace(0, np.nan)) * 100
    data["q2_sales_growth"] = (data["q2_sales"].div(data["q2_sales_previous"].replace(0, np.nan)) - 1) * 100
    data["q2_op_growth"] = (data["q2_op"].div(data["q2_op_previous"].where(data["q2_op_previous"] > 0)) - 1) * 100

    amount_scale = 100_000_000.0  # KIS amount rows are 억원.
    data["direct_2026"] = data["consensus_sales_2026"].notna() & data["consensus_op_2026"].notna()
    data["direct_2027"] = data["consensus_sales_2027"].notna() & data["consensus_op_2027"].notna()
    data["sales_2026e"] = (data["consensus_sales_2026"] * amount_scale).where(
        data["consensus_sales_2026"].notna(), data["sales_current"] * 2,
    )
    data["op_2026e"] = (data["consensus_op_2026"] * amount_scale).where(
        data["consensus_op_2026"].notna(), data["op_current"] * 2,
    )
    sales_trend = data["sales_1y_growth"].clip(-20, 80).fillna(0)
    op_trend = data["op_1y_growth"].where(data["op_1y_growth"].between(-20, 100), sales_trend).fillna(0)
    data["sales_2027e"] = (data["consensus_sales_2027"] * amount_scale).where(
        data["consensus_sales_2027"].notna(), data["sales_2026e"] * (1 + sales_trend / 100),
    )
    data["op_2027e"] = (data["consensus_op_2027"] * amount_scale).where(
        data["consensus_op_2027"].notna(), data["op_2026e"] * (1 + op_trend / 100),
    )
    data["q3_sales"] = (data["sales_2026e"] - data["sales_current"]) / 2
    data["q4_sales"] = data["q3_sales"]
    data["q3_op"] = (data["op_2026e"] - data["op_current"]) / 2
    data["q4_op"] = data["q3_op"]

    op_ratio = data["q1_op"].abs().div(data["q2_op"].abs().replace(0, np.nan))
    data["severe_base"] = (
        (data["q1_op"] <= 0) | (data["q2_op"] <= 0) | op_ratio.gt(3) | op_ratio.lt(1 / 3) |
        data["op_growth_basis"].astype(str).str.contains("전환", na=False)
    )
    sales_q1_share = data["q1_sales"].div(data["sales_2026e"].replace(0, np.nan))
    sales_q2_share = data["q2_sales"].div(data["sales_2026e"].replace(0, np.nan))
    op_q1_share = data["q1_op"].div(data["op_2026e"].replace(0, np.nan))
    op_q2_share = data["q2_op"].div(data["op_2026e"].replace(0, np.nan))
    data["q1_2027_sales"] = np.where(data["severe_base"], data["sales_2027e"] / 4,
                                      data["sales_2027e"] * sales_q1_share)
    data["q2_2027_sales"] = np.where(data["severe_base"], data["sales_2027e"] / 4,
                                      data["sales_2027e"] * sales_q2_share)
    data["q1_2027_op"] = np.where(data["severe_base"], data["op_2027e"] / 4,
                                   data["op_2027e"] * op_q1_share)
    data["q2_2027_op"] = np.where(data["severe_base"], data["op_2027e"] / 4,
                                   data["op_2027e"] * op_q2_share)
    invalid_alloc = (
        (data[["q1_2027_sales", "q2_2027_sales"]] <= 0).any(axis=1) |
        data[["q1_2027_sales", "q2_2027_sales", "q1_2027_op", "q2_2027_op"]].isna().any(axis=1)
    )
    data.loc[invalid_alloc, "severe_base"] = True
    for column, annual in (("q1_2027_sales", "sales_2027e"), ("q2_2027_sales", "sales_2027e"),
                           ("q1_2027_op", "op_2027e"), ("q2_2027_op", "op_2027e")):
        data.loc[invalid_alloc, column] = data.loc[invalid_alloc, annual] / 4

    data["future_avg_sales"] = data[["q3_sales", "q4_sales", "q1_2027_sales", "q2_2027_sales"]].mean(axis=1)
    data["future_avg_op"] = data[["q3_op", "q4_op", "q1_2027_op", "q2_2027_op"]].mean(axis=1)
    data["future_sales_growth"] = (data["future_avg_sales"].div(data["q2_sales"].where(data["q2_sales"] > 0)) - 1) * 100
    data["future_op_growth"] = (data["future_avg_op"].div(data["q2_op"].where(data["q2_op"] > 0)) - 1) * 100
    data["future_margin"] = data["future_avg_op"].div(data["future_avg_sales"].replace(0, np.nan)) * 100
    data["future_margin_delta"] = data["future_margin"] - data["q2_margin"]

    data["current_pop"] = data["market_cap"].div((data["q2_op"] * 4).where(data["q2_op"] > 0))
    data["future_pop"] = data["market_cap"].div(data["op_2027e"].where(data["op_2027e"] > 0))
    valid_current = data["current_pop"].replace([np.inf, -np.inf], np.nan).notna() & data["current_pop"].between(0.1, 300)
    valid_future = data["future_pop"].replace([np.inf, -np.inf], np.nan).notna() & data["future_pop"].between(0.1, 300)
    current_stats = data[valid_current].groupby("sector")["current_pop"].agg(["median", "count"])
    future_stats = data[valid_future].groupby("sector")["future_pop"].agg(["median", "count"])
    data["sector_median_pop"] = data["sector"].map(current_stats["median"] if not current_stats.empty else {})
    data["sector_peer_count"] = data["sector"].map(current_stats["count"] if not current_stats.empty else {}).fillna(0)
    data["sector_future_pop"] = data["sector"].map(future_stats["median"] if not future_stats.empty else {})
    data["future_peer_count"] = data["sector"].map(future_stats["count"] if not future_stats.empty else {}).fillna(0)
    minimum_peers = int(config.get("minimum_value_sector_peers", 2))
    current_sparse = data["sector_peer_count"] < minimum_peers
    future_sparse = data["future_peer_count"] < minimum_peers
    data.loc[current_sparse, "sector_median_pop"] = np.nan
    data.loc[future_sparse, "sector_future_pop"] = np.nan
    data["current_basis"] = np.where(current_sparse, "섹터 표본부족", "섹터 중앙")
    data["future_basis"] = np.where(future_sparse, "섹터 표본부족", "섹터 중앙")
    data["current_premium_pct"] = (data["current_pop"].div(data["sector_median_pop"]) - 1) * 100
    data["future_premium_pct"] = (data["future_pop"].div(data["sector_future_pop"]) - 1) * 100
    data["current_sector_rank"] = data[valid_current].groupby("sector")["current_pop"].rank(method="min").reindex(data.index)
    data["future_sector_rank"] = data[valid_future].groupby("sector")["future_pop"].rank(method="min").reindex(data.index)
    data["future_target_price"] = (data["sector_future_pop"] * data["op_2027e"]).div(
        data["shares"].where(data["shares"] > 0),
    )
    data["upside_12m"] = (data["future_target_price"].div(data["current_price"]) - 1) * 100

    normalized_op_columns = [f"normalized_op_q{quarter}" for quarter in (3, 4, 1, 2)]
    normalized_sales_columns = [f"normalized_sales_q{quarter}" for quarter in (3, 4, 1, 2)]
    reconstructed_count = data[normalized_op_columns].notna().sum(axis=1).astype(float)
    data["normalized_quarter_count"] = data["normalized_quarter_count"].where(
        data["normalized_quarter_count"].notna(), reconstructed_count,
    )
    data["normalized_complete"] = data["normalized_quarter_count"].eq(4)
    # Incomplete histories retain the prior Q2 annualization only as a low-confidence
    # fallback. Complete histories use reconstructed trailing-four-quarter profit.
    data["normalized_op"] = data["normalized_ttm_op"].where(
        data["normalized_complete"], data["q2_op"] * 4,
    )
    data["normalized_sales"] = data["normalized_ttm_sales"].where(
        data["normalized_complete"], data["q2_sales"] * 4,
    )
    data["normalization_adjustment_pct"] = (
        data["normalized_ttm_op"].div((data["q2_op"] * 4).replace(0, np.nan)) - 1
    ).mul(100).where(data["normalized_complete"])
    data["normalized_pop"] = data["market_cap"].div(data["normalized_op"].where(data["normalized_op"] > 0))
    valid_normalized = data["normalized_pop"].replace([np.inf, -np.inf], np.nan).notna() & data["normalized_pop"].between(0.1, 300)
    normalized_stats = data[valid_normalized].groupby("sector")["normalized_pop"].agg(["median", "count"])
    data["sector_normalized_pop"] = data["sector"].map(normalized_stats["median"] if not normalized_stats.empty else {})
    data["normalized_peer_count"] = data["sector"].map(normalized_stats["count"] if not normalized_stats.empty else {}).fillna(0)
    normalized_sparse = data["normalized_peer_count"] < minimum_peers
    data.loc[normalized_sparse, "sector_normalized_pop"] = np.nan
    data["normalized_premium_pct"] = (data["normalized_pop"].div(data["sector_normalized_pop"]) - 1) * 100

    op_quarters = data[normalized_op_columns]
    positive_ratio = op_quarters.gt(0).sum(axis=1).div(4).where(data["normalized_complete"], 0)
    op_mean = op_quarters.mean(axis=1)
    op_cv = op_quarters.std(axis=1).div(op_mean.abs().replace(0, np.nan))
    consistency = (100 / (1 + op_cv.clip(lower=0))).where(data["normalized_complete"], 0).fillna(0)
    data["normalization_quality"] = (
        data["normalized_quarter_count"].clip(0, 4).div(4) * 40
        + positive_ratio * 30
        + consistency * 0.30
    ).clip(0, 100)

    revision_weights = {"consensus_change_20d": 0.60, "consensus_change_5d": 0.25,
                        "consensus_change_1d": 0.15}
    revision_total = sum(data[column].fillna(0) * weight for column, weight in revision_weights.items())
    available_weight = sum(data[column].notna().astype(float) * weight for column, weight in revision_weights.items())
    data["consensus_signal"] = revision_total.div(available_weight.replace(0, np.nan))
    actual_available = data[["q2_sales", "q2_op", "sales_2026e", "op_2026e", "sales_2027e", "op_2027e"]].notna().all(axis=1)
    # A/B is the continuing-profit population. Losses and tiny (<=1%) Q2
    # margins belong only to the separate T+ evaluation, so the two tables can
    # never show the same company.
    improving = actual_available & (data["q2_sales"] > 0) & (data["q2_op"] > 0) & (data["q2_margin"] > 1) & (
        data["future_sales_growth"] > 0
    ) & (data["future_op_growth"] > 0)
    future_a = improving & (data["future_margin_delta"] > 0)
    future_b = improving & ~future_a
    data["future_type"] = np.select([future_a, future_b], ["A", "B"], default="제외")
    revision_positive = data["consensus_signal"].fillna(0) >= 0
    data["confidence"] = np.select(
        [data["normalized_complete"] & data["direct_2027"] & revision_positive,
         data["normalized_complete"], data["direct_2027"]], ["A", "B", "C"], default="D",
    )
    candidates = data[data["future_type"].isin(["A", "B"])].copy()
    candidates["absolute_value_score"] = (
        absolute_multiple_score(candidates["normalized_pop"]) * 0.65
        + absolute_multiple_score(candidates["future_pop"]) * 0.35
    )
    candidates["sector_value_score"] = (
        relative_discount_score(candidates["normalized_premium_pct"]) * 0.65
        + relative_discount_score(candidates["future_premium_pct"]) * 0.35
    )
    growth_signal = np.log1p(candidates["future_op_growth"].clip(lower=0, upper=300) / 100)
    growth_for_price = growth_signal.div(candidates["future_pop"].where(candidates["future_pop"] > 0))
    candidates["growth_price_score"] = growth_for_price.rank(method="average", pct=True).fillna(0) * 100
    margin_level = candidates["future_margin"].rank(method="average", pct=True).fillna(0) * 100
    margin_change = candidates["future_margin_delta"].rank(method="average", pct=True).fillna(0) * 100
    candidates["margin_score"] = (margin_level + margin_change) / 2
    candidates["value_score_before_confidence"] = (
        candidates["absolute_value_score"] * 0.30
        + candidates["sector_value_score"] * 0.30
        + candidates["normalization_quality"] * 0.25
        + candidates["growth_price_score"] * 0.10
        + candidates["margin_score"] * 0.05
    )
    candidates["confidence_multiplier"] = candidates["confidence"].map(
        {"A": 1.00, "B": 0.92, "C": 0.82, "D": 0.70},
    ).fillna(0.70)
    holding_like = (
        candidates["name"].astype(str).str.contains("홀딩스|지주", regex=True, na=False)
        | candidates["sector"].astype(str).str.contains("회사 본부|경영 컨설팅", regex=True, na=False)
    )
    finance_like = candidates["sector"].astype(str).str.contains("금융", regex=False, na=False)
    candidates["structure_multiplier"] = np.select(
        [finance_like, holding_like], [0.75, 0.80], default=1.00,
    )
    candidates["structure_warning"] = np.select(
        [finance_like, holding_like], ["금융업 P/OP 비교제한", "지주·연결실적 검토"], default="",
    )
    candidates["value_score"] = (
        candidates["value_score_before_confidence"]
        * candidates["confidence_multiplier"]
        * candidates["structure_multiplier"]
    )
    # Sub-1x P/OP, especially when the future number is not direct consensus,
    # is more likely to be a consolidation/unit artefact than a durable bargain.
    extreme_multiple = (candidates["normalized_pop"] < 1) | (
        (candidates["future_pop"] < 0.5) & ~candidates["direct_2027"]
    )
    candidates.loc[extreme_multiple, "value_score"] = candidates.loc[extreme_multiple, "value_score"].clip(upper=69)
    candidates.loc[extreme_multiple & candidates["structure_warning"].eq(""), "structure_warning"] = "1배 미만 배수 검증필요"
    candidates.loc[candidates["future_premium_pct"] > 20, "value_score"] = candidates.loc[
        candidates["future_premium_pct"] > 20, "value_score"
    ].clip(upper=69)
    ranked = candidates.sort_values(
        ["value_score", "normalized_premium_pct", "future_premium_pct"],
        ascending=[False, True, True], na_position="last",
    ).copy()
    ranked["type_rank"] = np.arange(1, len(ranked) + 1)
    # Valuation ranks compare each company's discount/premium to its own
    # sector median, then rank those relative percentages across the A/B
    # candidate universe. The largest sector discount is current/future rank 1.
    # Growth rank remains independent and continues to control table order.
    ranked["current_value_rank"] = ranked["current_premium_pct"].rank(
        method="min", ascending=True, na_option="keep",
    )
    ranked["future_value_rank"] = ranked["future_premium_pct"].rank(
        method="min", ascending=True, na_option="keep",
    )
    ranked["normalized_value_rank"] = ranked["normalized_premium_pct"].rank(
        method="min", ascending=True, na_option="keep",
    )

    # Growth discovery is a separate evidence engine and never enters this table.
    top_count = int(config["top_value_count"])
    selected = ranked.head(top_count)

    def rounded(value):
        return round(float(value), 1) if np.isfinite(value) else None

    def relative_result(rank_value, premium, peers, basis, prefix):
        if not np.isfinite(premium) or not np.isfinite(rank_value):
            return f"{prefix} 산출불가({basis} · {int(peers)}개)"
        label = "할인" if premium < 0 else "프리미엄"
        return f"{prefix} {int(rank_value)}위({abs(premium):.1f}% {label})"

    def quarter_text(label, sales, op, q2_sales, q2_op):
        sales_change = (sales / q2_sales - 1) * 100 if np.isfinite(sales) and q2_sales > 0 else np.nan
        op_change = (op / q2_op - 1) * 100 if np.isfinite(op) and q2_op > 0 else np.nan
        margin = op / sales * 100 if np.isfinite(op) and np.isfinite(sales) and sales else np.nan
        op_display = _pct(op_change) if np.isfinite(op_change) else ("흑자" if np.isfinite(op) and op > 0 else "적자")
        return f"{label} 매출 {_amount_text(sales)}({_pct(sales_change)}) · OP {_amount_text(op)}({op_display}) · 마진 {_pct(margin)}"

    def period_average_text(label, sales_values, op_values, q2_sales, q2_op):
        valid_sales = [float(value) for value in sales_values if np.isfinite(value)]
        valid_op = [float(value) for value in op_values if np.isfinite(value)]
        average_sales = float(np.mean(valid_sales)) if valid_sales else np.nan
        average_op = float(np.mean(valid_op)) if valid_op else np.nan
        return quarter_text(f"{label} 평균", average_sales, average_op, q2_sales, q2_op)

    def make_row(item):
        current_result = relative_result(item.current_value_rank, item.current_premium_pct,
                                         item.sector_peer_count, item.current_basis, "현재")
        future_result = relative_result(item.future_value_rank, item.future_premium_pct,
                                        item.future_peer_count, item.future_basis, "미래")
        normalized_result = relative_result(item.normalized_value_rank, item.normalized_premium_pct,
                                            item.normalized_peer_count, "섹터 중앙", "정상화")
        q2_op_text = _pct(item.q2_op_growth) if np.isfinite(item.q2_op_growth) else (
            "흑자전환" if item.q2_op > 0 >= item.q2_op_previous else "비교불가"
        )
        current_text = (
            f"현재(2Q) · 매출 {_amount_text(item.q2_sales)}({_pct(item.q2_sales_growth)}) · "
            f"OP {_amount_text(item.q2_op)}({q2_op_text}) · 마진 {_pct(item.q2_margin)}"
        )
        future_text = " · ".join([
            period_average_text("26년 3Q·4Q", (item.q3_sales, item.q4_sales),
                                (item.q3_op, item.q4_op), item.q2_sales, item.q2_op),
            period_average_text("27년 1Q·2Q", (item.q1_2027_sales, item.q2_2027_sales),
                                (item.q1_2027_op, item.q2_2027_op), item.q2_sales, item.q2_op),
        ])
        current_price_text = "적자·현재배수 산출불가" if not np.isfinite(item.current_pop) else (
            f"종목 P/OP {_pct(item.current_pop, '배').replace('+', '')} · {item.current_basis} "
            f"{_pct(item.sector_median_pop, '배').replace('+', '')} · {current_result}"
        )
        future_price_text = (
            f"종목 2027 P/OP {_pct(item.future_pop, '배').replace('+', '')} · {item.future_basis} "
            f"{_pct(item.sector_future_pop, '배').replace('+', '')} · {future_result}"
        )
        future_source = "KIS" if item.direct_2027 else "추정"
        estimate_badge = "균등분배 추정" if item.severe_base else "분기비율 추정"
        result = f"{current_result} · {future_result}"
        actual_date = str(item.quarter_as_of)[:10] if pd.notna(item.quarter_as_of) else str(item.as_of)[:10]
        return {
            "rank": int(item.type_rank), "ticker": item.ticker, "name": item.name, "sector": item.sector,
            "futureType": item.future_type, "typeRank": int(item.type_rank),
            "confidence": item.confidence, "actualPeriod": "2026 2Q", "actualDate": actual_date,
            "actualSourceBadge": "DART Q2" if item.direct_q2 else "반기균등 추정",
            "salesAmount": _amount_text(item.q2_sales), "salesGrowth": rounded(item.q2_sales_growth),
            "opAmount": _amount_text(item.q2_op), "reportedOpGrowth": rounded(item.q2_op_growth),
            "opMargin": rounded(item.q2_margin), "currentEvaluation": current_text,
            "currentPrice": round(float(item.current_price)) if np.isfinite(item.current_price) else None,
            "priceDate": str(item.price_date)[:10] if pd.notna(item.price_date) else None,
            "companyCurrentPOP": rounded(item.current_pop), "sectorMedianPOP": rounded(item.sector_median_pop),
            "sectorPeerCount": int(item.sector_peer_count), "multipleBasis": item.current_basis,
            "pricePremiumPct": rounded(item.current_premium_pct), "priceEvaluation": current_price_text,
            "currentRank": int(item.current_value_rank) if np.isfinite(item.current_value_rank) else None,
            "currentSectorRank": int(item.current_sector_rank) if np.isfinite(item.current_sector_rank) else None,
            "currentResult": current_result, "futurePeriod": "2026 3Q~2027 2Q",
            "futureSalesGrowth": rounded(item.future_sales_growth), "futureOpGrowth": rounded(item.future_op_growth),
            "futureMargin": rounded(item.future_margin), "futureMarginDelta": rounded(item.future_margin_delta),
            "futureEvaluation": future_text, "futureSourceBadge": future_source,
            "estimateBadge": estimate_badge, "companyFuturePOP": rounded(item.future_pop),
            "sectorFuturePOP": rounded(item.sector_future_pop), "futurePeerCount": int(item.future_peer_count),
            "futureMultipleBasis": item.future_basis, "futurePremiumPct": rounded(item.future_premium_pct),
            "futureRank": int(item.future_value_rank) if np.isfinite(item.future_value_rank) else None,
            "futureSectorRank": int(item.future_sector_rank) if np.isfinite(item.future_sector_rank) else None,
            "futurePriceEvaluation": future_price_text, "futureResult": future_result, "result": result,
            "normalizedPeriod": str(item.normalization_as_of)[:10] if pd.notna(item.normalization_as_of) else None,
            "normalizedQuarterCount": int(item.normalized_quarter_count),
            "normalizedPOP": rounded(item.normalized_pop), "sectorNormalizedPOP": rounded(item.sector_normalized_pop),
            "normalizedPremiumPct": rounded(item.normalized_premium_pct), "normalizedResult": normalized_result,
            "normalizationAdjustmentPct": rounded(item.normalization_adjustment_pct),
            "normalizationQuality": rounded(item.normalization_quality),
            "absoluteValueScore": rounded(item.absolute_value_score),
            "sectorValueScore": rounded(item.sector_value_score),
            "growthPriceScore": rounded(item.growth_price_score), "marginScore": rounded(item.margin_score),
            "valueScoreBeforeConfidence": rounded(item.value_score_before_confidence),
            "confidenceMultiplier": round(float(item.confidence_multiplier), 2) if np.isfinite(item.confidence_multiplier) else None,
            "structureMultiplier": round(float(item.structure_multiplier), 2) if np.isfinite(item.structure_multiplier) else None,
            "structureWarning": item.structure_warning, "valueScore": rounded(item.value_score),
            "targetPrice12M": round(float(item.future_target_price)) if np.isfinite(item.future_target_price) else None,
            "upside12M": rounded(item.upside_12m), "consensus1D": rounded(item.consensus_change_1d),
            "consensus5D": rounded(item.consensus_change_5d), "consensus20D": rounded(item.consensus_change_20d),
            "consensusDate": str(item.consensus_as_of)[:10] if pd.notna(item.consensus_as_of) else None,
            "consensusStatus": "KIS 2026E·2027E" if future_source == "KIS" else "공시추세 추정",
            "signal": ("핵심" if future_source == "KIS" and item.confidence == "A" else "관찰") +
                      f" {item.future_type}",
            "reason": f"{current_text} · {future_text} · {result}",
        }

    rows = [make_row(item) for item in selected.itertuples(index=False)]
    ab_count = int(len(ranked))
    price_dates = (pd.to_datetime(data["price_date"], errors="coerce").dropna()
                   if "price_date" in data.columns else pd.Series(dtype="datetime64[ns]"))
    price_basis = price_dates.max().strftime("%Y-%m-%d") if not price_dates.empty else None
    price_basis_text = f" · 가격 기준 {price_basis}" if price_basis else ""
    return {
        "status": f"정상화 가치 A·B {ab_count}개 중 상위 {len(rows)}개{price_basis_text}",
        "method": "절대 저평가 30% + 섹터 상대 저평가 30% + 최근 4분기 정상화 품질 25% + 성장 대비 가격 10% + 미래 마진 5% · 신뢰도 및 금융·지주 구조 배수 적용",
        "rows": rows,
        "events": [{
            "name": "가치 엔진", "date": datetime.now(KST).strftime("%Y-%m-%d"),
            "event": "최근 4분기 정상화 이익을 기준으로 절대·섹터 상대 저평가를 결합하고 자료 신뢰도 배수를 적용",
            "tone": "정보", "impact": "일회성 분기 급증, 자체 추정 성장률, 금융·지주 연결실적이 가치 순위를 지배하지 못하도록 제한",
        }],
        "dataStatus": status | {"futureCandidateCount": len(data), "futureABCount": ab_count,
                                "direct2027Count": int(data["direct_2027"].sum()),
                                "directQ2Count": int(data["direct_q2"].sum()),
                                "normalizedCompleteCount": int(data["normalized_complete"].sum())},
    }


def build_entry_board(prices: pd.DataFrame, all_sectors: list[dict], fundamentals: pd.DataFrame,
                      config: dict) -> dict:
    """Select only immediately actionable or near-entry stocks from the entire universe."""
    stock_data, _ = build_daily_features(prices)
    latest_date = stock_data["date"].max()
    current = stock_data[stock_data["date"].eq(latest_date)].copy()
    sectors = pd.DataFrame(all_sectors).set_index("name")
    current = current[current["sector"].isin(sectors.index)].copy()
    for field in ("stage", "rotationType", "riskGauge", "score", "raw_ret3", "raw_ret5"):
        current[f"sector_{field}"] = current["sector"].map(sectors[field])
    current["excess3"] = current["ret3"] - current["sector_raw_ret3"]
    current["overheated"] = (current["ret1"] > .10) | (current["ret3"] > .15) | (current["ret5"] > .25)
    current["zone_low"] = np.minimum(current["ma5"], current["ma20"]) - current["atr14"] * .25
    current["zone_high"] = np.maximum(current["ma5"], current["ma20"]) + current["atr14"] * .35
    current["distance"] = np.where(
        current["close"] < current["zone_low"], current["zone_low"] / current["close"] - 1,
        np.where(current["close"] > current["zone_high"], current["close"] / current["zone_high"] - 1, 0),
    )
    current["trend_ok"] = (current["ma5"] >= current["ma20"] * .98) & (current["close"] >= current["ma20"] * .96)
    current["confirm"] = (current["ret1"] > 0) & (current["close"] >= current["ma5"]) & (current["volume_ratio"] >= .85)
    allowed = ~current["sector_stage"].isin(["⑥후반", "X조기이탈", "X종료"])
    liquid = current["value"] >= 100_000_000
    valid = allowed & liquid & current["trend_ok"] & ~current["overheated"] & current["ret5"].notna()
    current["entryState"] = ""
    inside = current["distance"].eq(0)
    current.loc[valid & inside & current["confirm"], "entryState"] = "진입가능"
    current.loc[valid & current["entryState"].eq("") & (current["distance"] <= .03), "entryState"] = "곧진입"
    current = current[current["entryState"].ne("")].copy()
    current["entry_score"] = (current["sector_score"] + current["excess3"].clip(-.05, .05) * 160 +
                              current["confirm"].astype(int) * 12 - current["distance"] * 200)
    current["entry_priority"] = current["entryState"].map({"진입가능": 0, "곧진입": 1})
    current = current.sort_values(["entry_priority", "entry_score", "value"], ascending=[True, False, False])
    current = current[current.groupby("sector").cumcount() < 4].head(int(config["top_entry_count"]))
    fundamental_map = fundamentals.set_index("ticker") if not fundamentals.empty else pd.DataFrame()
    rows = []
    for rank, item in enumerate(current.itertuples(), 1):
        relation = "선행" if item.excess3 >= .015 else "후행" if item.excess3 <= -.015 else "동행"
        if item.sector_stage == "⑤재반등":
            signal = "재반등 진입"
        elif item.sector_stage == "④눌림":
            signal = "눌림 반등" if item.confirm else "진입가격 접근 중"
        elif pd.notna(item.high20_prev) and item.close >= item.high20_prev * .98:
            signal = "돌파 후 지지"
        else:
            signal = "1차 분할진입" if item.entryState == "진입가능" else "진입가격 접근 중"
        invalidation = min(item.zone_low, item.ma20) - item.atr14 * .7
        stop_pct = (invalidation / item.close - 1) * 100
        growth = consensus = value_text = "자료없음"
        consensus_date = None
        if not fundamentals.empty and item.ticker in fundamental_map.index:
            f = fundamental_map.loc[item.ticker]
            op_growth = pd.to_numeric(f.get("op_1y_growth"), errors="coerce")
            if pd.notna(op_growth) and np.isfinite(float(op_growth)):
                growth_basis = str(f.get("op_growth_basis", ""))
                if growth_basis == "흑자전환" or float(op_growth) >= 900:
                    growth = "흑자전환"
                elif growth_basis == "적자전환" or float(op_growth) <= -900:
                    growth = "적자전환"
                else:
                    growth = f"{float(op_growth):+.1f}%"
            revision_20d = pd.to_numeric(f.get("consensus_change_20d"), errors="coerce")
            consensus_as_of = f.get("consensus_as_of")
            has_consensus = pd.notna(consensus_as_of)
            if pd.notna(revision_20d) and np.isfinite(float(revision_20d)):
                consensus = f"20일 {float(revision_20d):+.1f}%"
            elif has_consensus:
                consensus = "변경이력 없음"
            forward_pe = pd.to_numeric(f.get("forward_pe"), errors="coerce")
            sector_values = pd.to_numeric(
                fundamentals.loc[fundamentals["sector"].eq(item.sector), "forward_pe"], errors="coerce"
            ).replace([np.inf, -np.inf], np.nan).dropna()
            sector_median = float(sector_values.median()) if not sector_values.empty else np.nan
            if pd.notna(forward_pe) and np.isfinite(float(forward_pe)):
                peer_text = f"{sector_median:.1f}배" if np.isfinite(sector_median) else "자료없음"
                value_text = f"예상PER {float(forward_pe):.1f}배 / 섹터 {peer_text}"
            consensus_date = str(consensus_as_of)[:10] if has_consensus else None
        rows.append({
            "rank": rank, "ticker": item.ticker, "name": item.name, "sector": item.sector,
            "entryState": item.entryState, "signal": signal, "currentPrice": int(round(item.close)),
            "entryZone": f"{int(round(item.zone_low)):,}~{int(round(item.zone_high)):,}원",
            "confirmation": "당일 상승·5일선 회복·거래량 20일 평균 85% 이상",
            "invalidationPrice": int(round(invalidation)), "stopPct": round(float(stop_pct), 1),
            "relation": relation, "marketState": f"{item.sector_rotationType} · {item.sector_stage}",
            "growth1Y": growth, "consensus": consensus, "consensusDate": consensus_date,
            "valueMultiple": value_text, "entryScore": round(float(item.entry_score), 1),
            "reason": f"전체시장 진입필터 통과 · 섹터 대비 {relation} · 과열 아님",
        })
    return {"status": f"전체 {prices['ticker'].nunique():,}종목에서 진입가능/곧진입만 선별 · 기준일 {latest_date:%Y-%m-%d}",
            "rows": rows, "selectionRule": "과열·후반·종료 제외, 추세 유지, 진입구간 안 또는 3% 이내"}


def run_engine(prices: pd.DataFrame, config: dict, source_name: str) -> dict:
    overrides = read_overrides(config["sector_overrides_file"])
    if overrides:
        prices["sector"] = prices.apply(lambda row: overrides.get(row["ticker"], row["sector"]), axis=1)
    stock_data, sector_daily = build_daily_features(prices)
    history = composite_history(sector_daily)
    latest_date = history["date"].max()
    eligible = history.groupby("sector")["members"].max()
    eligible = eligible[eligible >= int(config["minimum_sector_members"])].index
    history = history[history["sector"].isin(eligible)]
    results = []
    for sector, group in history.groupby("sector"):
        group = group.sort_values("date").reset_index(drop=True)
        latest = group.iloc[-1]
        previous = group.iloc[-2] if len(group) > 1 else latest
        start = infer_start(group, int(config["rotation_scan_min_days"]), int(config["rotation_scan_max_days"]))
        elapsed = int((group["date"] >= start).sum())
        completed = [length for length in episode_lengths(group.iloc[:-1], include_open=False) if length >= 3]
        avg_cycle = int(round(float(np.median(completed)))) if len(completed) >= 2 else int(config["default_cycle_days"])
        avg_cycle = max(10, min(40, avg_cycle))
        sector_prices = stock_data[(stock_data["sector"].eq(sector)) & (stock_data["date"] >= start)]
        sector_curve = sector_prices.groupby("date")["close"].mean()
        drawdown = float(1 - sector_curve.iloc[-1] / sector_curve.max()) if not sector_curve.empty else 0.0
        stage, short_stage = classify_stage(latest, previous, elapsed, avg_cycle, drawdown)
        leader_led = latest["leader_strength"] >= 0.025 and latest["breadth"] < 0.58
        diffusion = latest["breadth"] >= 0.58 and latest["turnover_change"] > -0.05
        rotation_type = "확산형+선도주" if leader_led and diffusion else "선도주 견인형" if leader_led else "확산형" if diffusion else "혼합/관찰"
        concentration_risk = 18 if leader_led else 4
        risk = bounded(
            min(35, elapsed / max(avg_cycle, 1) * 35) + min(30, drawdown * 250) +
            concentration_risk + (18 if latest["rs3"] < 0 else 0)
        )
        position = bounded(elapsed / max(avg_cycle, 1) * 100)
        stage_bonus = {
            "①초기": 12, "②확산": 14, "③주도": 5, "④눌림": 3,
            "⑤재반등": 12, "⑥후반": -16, "X조기이탈": -35, "X종료": -45,
        }[stage]
        entry_score = bounded(float(latest["composite"] * 75) + stage_bonus - risk * 0.22 +
                              max(-8, min(8, float(latest["rs3"] * 180))))
        if stage in {"①초기", "②확산", "⑤재반등"} and entry_score >= 55 and risk < 65:
            entry_fit = "진입적합"
        elif stage in {"④눌림", "③주도"} and entry_score >= 45 and risk < 72:
            entry_fit = "눌림/분할"
        elif stage == "⑥후반":
            entry_fit = "추격금지"
        else:
            entry_fit = "제외/관찰"
        results.append({
            "name": sector, "stage": stage, "stageLabel": short_stage,
            "rotationType": rotation_type, "score": round(float(latest["composite"] * 100), 1),
            "entryScore": round(entry_score, 1), "entryFit": entry_fit,
            "rs1Pct": round(float(latest["rs1"] * 100), 2),
            "rs3Pct": round(float(latest["rs3"] * 100), 2),
            "rs5Pct": round(float(latest["rs5"] * 100), 2),
            "turnoverChangePct": round(float(latest["turnover_change"] * 100), 1),
            "advanceRatioPct": round(float(latest["breadth"] * 100), 1),
            "leaderStrengthPct": round(float(latest["leader_strength"] * 100), 2),
            "rotationStartDate": pd.Timestamp(start).strftime("%Y-%m-%d"),
            "averageCycleDays": avg_cycle, "elapsedBusinessDays": elapsed,
            "positionPct": round(position, 1), "riskGauge": round(risk, 1),
            "memberCount": int(latest["members"]), "drawdownPct": round(drawdown * 100, 2),
            "raw_ret3": float(latest["ret3"]), "raw_ret5": float(latest["ret5"]),
        })
    sector_results = pd.DataFrame(results).sort_values(["score", "rs5Pct", "leaderStrengthPct"], ascending=False).reset_index(drop=True)
    sector_results["rank"] = np.arange(1, len(sector_results) + 1)
    top = sector_results.head(int(config["top_sector_count"])).copy()
    rows = rotation_rows(stock_data, top, int(config["top_stock_count"]))
    public_sectors = top.drop(columns=["raw_ret3", "raw_ret5"]).to_dict("records")
    stage_counts = top["stage"].value_counts().to_dict()
    status = (
        f"전체시장 엔진: KOSPI+KOSDAQ {prices['ticker'].nunique():,}종목, "
        f"{len(sector_results):,}개 섹터 분석. 기준일 {latest_date:%Y-%m-%d}. "
        f"진입 위치와 무관하게 확산형과 선도주 견인형을 함께 반영했으며 단계 분포 {stage_counts}."
    )
    result = {
        "status": status,
        "engine": {
            "version": "1.0.0", "generatedAtKST": datetime.now(KST).isoformat(timespec="seconds"),
            "asOfDate": latest_date.strftime("%Y-%m-%d"), "source": source_name,
            "universe": ["KOSPI", "KOSDAQ"], "stockCount": int(prices["ticker"].nunique()),
            "sectorCount": int(len(sector_results)), "lookbackTradingDays": int(prices["date"].nunique()),
            "startDateScanBusinessDays": [int(config["rotation_scan_min_days"]), int(config["rotation_scan_max_days"])],
        },
        "sectors": public_sectors,
        "rows": rows,
        "events": [{
            "name": "전체시장 순환매 엔진", "date": latest_date.strftime("%Y-%m-%d"),
            "event": f"{prices['ticker'].nunique():,}종목 전수 구조로 섹터 상대강도·수급·확산·선도주 강도 계산",
            "tone": "정보", "impact": "자동 산출 결과이며 투자 판단·주문 신호가 아닙니다.",
        }],
    }
    result["_allSectors"] = sector_results.to_dict("records")
    return result


def write_outputs(p1: dict, p11: dict, p2: dict, report: dict, config: dict) -> tuple[Path, Path, Path]:
    output_dir: Path = config["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    p11_path = output_dir / "p11.test.json"
    data_path = output_dir / "data.test.json"
    report_path = output_dir / "collection_report.test.json"
    public_p11 = {key: value for key, value in p11.items() if not key.startswith("_")}
    p11_path.write_text(json.dumps({"p11": public_p11}, ensure_ascii=False, indent=2), encoding="utf-8")
    base_path: Path = config["base_data_file"]
    if base_path.exists():
        board = json.loads(base_path.read_text(encoding="utf-8-sig"))
    else:
        board = {"meta": {"title": "주식 허브 테스트"}}
    board.setdefault("meta", {})
    board["meta"]["updatedKST"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    board["meta"]["masterBasis"] = f"{report.get('latestPriceDate', '기준일 미확인')} KRX 마감 전체시장 엔진"
    board["meta"]["note"] = "순환은 진입 위치와 무관한 전체시장 순환 강도, 진입은 진입가능·곧진입만 표시합니다. 단타 탭은 제거했습니다."
    board["meta"]["sourceSummary"] = (
        f"가격 {report.get('source', '자료원 미확인')} · 커버리지 {float(report.get('latestCoverageRatio', 0)):.2%} · "
        f"컨센서스 {report.get('fundamentals', {}).get('status', '상태 미확인')}"
    )
    board["p1"], board["p11"] = p1, public_p11
    if p2.get("rows"):
        board["p2"] = p2
    else:
        retained = board.get("p2", {"rows": []})
        previous_status = retained.get("status", "기존 검증값")
        retained["status"] = f"{p2.get('status', '가치 엔진 미갱신')} · 기존 검증값 유지: {previous_status}"
        retained["dataStatus"] = p2.get("dataStatus", {})
        board["p2"] = retained
    board.pop("p12", None)
    data_path.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    missing_path = output_dir / "missing_stocks.test.json"
    missing_path.write_text(json.dumps({"asOfDate": report.get("latestPriceDate"),
                                        "stocks": report.get("missingStocks", [])},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
    return p11_path, data_path, report_path


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KOSPI+KOSDAQ whole-market rotation screener")
    parser.add_argument("--config", type=Path, help="JSON configuration file")
    parser.add_argument("--mode", choices=["live", "sample"], help="override configuration mode")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config, args.mode)
        base_path: Path = config["base_data_file"]
        before = sha256(base_path) if base_path.exists() else None
        log(f"mode={config['mode']}; loading KOSPI+KOSDAQ market data")
        loader = MarketDataLoader(config)
        prices, source = loader.load()
        prices, market_snapshot_status = attach_market_snapshot(prices, config)
        log(f"loaded {prices['ticker'].nunique():,} stocks, {prices['date'].nunique()} trading days from {source}")
        p11 = run_engine(prices, config, source)
        fundamentals, fundamental_status = load_fundamentals(prices, config)
        p1 = build_entry_board(prices, p11["_allSectors"], fundamentals, config)
        p2 = build_value_board(fundamentals, config, fundamental_status, prices)
        report = dict(loader.report)
        report["fundamentals"] = fundamental_status
        report["marketSnapshot"] = market_snapshot_status
        report["recoveryPolicy"] = [
            "누락 종목만 재시도", "대체 가격원 사용", "1영업일 이내 캐시로 빈칸만 보완",
            "미분류 재매핑", "실패 영역은 기준일과 실패 사유 표시", "다음 실행에서 자동 재시도",
        ]
        p11_path, data_path, report_path = write_outputs(p1, p11, p2, report, config)
        after = sha256(base_path) if base_path.exists() else None
        if before != after:
            raise RuntimeError("safety check failed: base data.json changed")
        log(f"p11 output: {p11_path}")
        log(f"board test output: {data_path}")
        log(f"collection report: {report_path}")
        log("safety check: original data.json unchanged")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

