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
    "cache_dir": "cache",
    "output_dir": "test_output",
    "base_data_file": "data.json",
    "primary_source": "pykrx",
    "fallback_sources": ["cache", "yfinance"],
    "cache_max_age_hours": 30,
    "request_retries": 3,
    "request_pause_seconds": 0.35,
    "yfinance_chunk_size": 80,
    "default_cycle_days": 25,
    "sector_overrides_file": "sector_overrides.example.csv",
    "fundamentals_file": "consensus_cache.csv",
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
    renamed["ticker"] = ticker_text.where(ticker_text.str.fullmatch(r"\d{1,6}")).str.zfill(6)
    for column in ("open", "high", "low", "close", "volume", "value"):
        renamed[column] = pd.to_numeric(renamed[column], errors="coerce")
    renamed["sector"] = renamed["sector"].fillna("미분류").replace("", "미분류")
    renamed = renamed.dropna(subset=["date", "ticker", "close"])
    renamed = renamed[renamed["close"] > 0]
    return renamed.sort_values(["ticker", "date"]).reset_index(drop=True)


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
    coverage = len(latest_tickers) / max(1, len(all_tickers))
    unclassified = float(latest_rows["sector"].eq("미분류").mean()) if len(latest_rows) else 1.0
    market_counts = {
        market: int(latest_rows.loc[latest_rows["market"].eq(market), "ticker"].nunique())
        for market in ("KOSPI", "KOSDAQ")
    }
    previous_dates = sorted(prices.loc[prices["date"].lt(latest), "date"].unique())
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
    return {
        "source": source, "checkedAtKST": datetime.now(KST).isoformat(timespec="seconds"),
        "latestPriceDate": pd.Timestamp(latest).strftime("%Y-%m-%d"),
        "historicalTickerCount": len(all_tickers), "latestTickerCount": len(latest_tickers),
        "latestCoverageRatio": round(coverage, 4), "marketCounts": market_counts,
        "previousMarketCounts": previous_market_counts,
        "unclassifiedRatio": round(unclassified, 4), "missingTickers": missing,
        "missingStocks": missing_stocks,
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
                report = audit_market_data(merged, self.config, label) | {"attempts": list(errors)}
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
            self.report = audit_market_data(merged, self.config, "partial-recovery") | {
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
        business_days_old = int(np.busday_count(frame["date"].max().date(), datetime.now(KST).date()))
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
        report = audit_market_data(frame, self.config, "cache-write-check")
        if report["qualityStatus"] != "정상":
            log("cache not replaced because collection validation is incomplete")
            return
        frame.to_csv(self.cache_file, index=False, encoding="utf-8-sig", compression="gzip")

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
        if self.listing_cache_file.exists():
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
        listing = listing[listing["market"].isin(["KOSPI", "KOSDAQ"])].copy()
        listing["ticker"] = listing["ticker"].astype(str).str.strip()
        listing = listing[listing["ticker"].str.fullmatch(r"\d{6}")].copy()
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


def bounded(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return float(max(low, min(high, value)))


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
        rows.append({
            "ticker": item.ticker, "name": item.name, "sector": item.sector,
            "as_of": "2026-08-27", "sales_q3_growth": 8 + seed % 37,
            "sales_q4_growth": 6 + seed % 41, "sales_1y_growth": 10 + seed % 55,
            "op_1y_growth": -5 + seed % 80, "forward_pe": 7 + (seed % 310) / 10,
            "consensus_change_1d": ((seed % 9) - 3) / 10,
            "consensus_change_5d": ((seed % 19) - 5) / 10,
            "consensus_change_20d": ((seed % 31) - 8) / 10,
            "analyst_count": 2 + seed % 15, "source": "deterministic-sample", "status": "정상",
        })
    return pd.DataFrame(rows)


def load_fundamentals(prices: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, dict]:
    required = {"ticker", "as_of", "sales_1y_growth", "op_1y_growth", "forward_pe",
                "consensus_change_1d", "consensus_change_5d", "consensus_change_20d", "analyst_count"}
    if config["mode"] == "sample":
        frame = generate_sample_fundamentals(prices)
        return frame, {"status": "정상", "source": "deterministic-sample", "asOfDate": "2026-08-27"}
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


def build_value_board(fundamentals: pd.DataFrame, config: dict, status: dict) -> dict:
    if fundamentals.empty:
        return {"status": f"가치 엔진 미갱신: {status.get('problem', '자료 없음')}", "rows": [], "dataStatus": status}
    data = fundamentals.copy()
    for column in ("sales_1y_growth", "op_1y_growth", "forward_pe", "consensus_change_1d",
                   "consensus_change_5d", "consensus_change_20d", "analyst_count"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["sector_median_pe"] = data.groupby("sector")["forward_pe"].transform("median")
    data["discount"] = data["sector_median_pe"] / data["forward_pe"] - 1
    data["score"] = (rank_percentile(data["sales_1y_growth"]) * 22 +
                     rank_percentile(data["op_1y_growth"]) * 28 +
                     rank_percentile(data["consensus_change_20d"]) * 24 +
                     rank_percentile(data["discount"]) * 20 +
                     rank_percentile(data["analyst_count"]) * 6)
    data = data.dropna(subset=["forward_pe", "op_1y_growth"]).sort_values("score", ascending=False)
    rows = []
    for rank, item in enumerate(data.head(int(config["top_value_count"])).itertuples(), 1):
        premium = (item.forward_pe / item.sector_median_pe - 1) * 100 if item.sector_median_pe else np.nan
        rows.append({
            "rank": rank, "ticker": item.ticker, "name": item.name, "sector": item.sector,
            "sales27": f"{item.sales_1y_growth:+.1f}%", "opGrowth": f"{item.op_1y_growth:+.1f}%",
            "growth1Y": round(float(item.op_1y_growth), 1), "value": f"예상PER {item.forward_pe:.1f}배",
            "forwardPER": round(float(item.forward_pe), 1), "sectorMedian": f"{item.sector_median_pe:.1f}배",
            "sectorMedianPER": round(float(item.sector_median_pe), 1), "premium": f"{premium:+.1f}%",
            "consensus1D": round(float(item.consensus_change_1d), 1),
            "consensus5D": round(float(item.consensus_change_5d), 1),
            "consensus20D": round(float(item.consensus_change_20d), 1),
            "consensusDate": str(item.as_of)[:10], "signal": "가치 상위",
            "reason": f"1년 영업이익 {item.op_1y_growth:+.1f}% · 20일 컨센서스 {item.consensus_change_20d:+.1f}%",
        })
    return {"status": f"전체시장 가치 엔진 {len(data):,}종목 비교 · 컨센서스 기준 {status.get('asOfDate')}",
            "rows": rows, "dataStatus": status}


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
            growth = f"{float(f['op_1y_growth']):+.1f}%"
            consensus = f"20일 {float(f['consensus_change_20d']):+.1f}%"
            sector_median = fundamentals.loc[fundamentals["sector"].eq(item.sector), "forward_pe"].median()
            value_text = f"예상PER {float(f['forward_pe']):.1f}배 / 섹터 {sector_median:.1f}배"
            consensus_date = str(f["as_of"])[:10]
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
        log(f"loaded {prices['ticker'].nunique():,} stocks, {prices['date'].nunique()} trading days from {source}")
        p11 = run_engine(prices, config, source)
        fundamentals, fundamental_status = load_fundamentals(prices, config)
        p1 = build_entry_board(prices, p11["_allSectors"], fundamentals, config)
        p2 = build_value_board(fundamentals, config, fundamental_status)
        report = dict(loader.report)
        report["fundamentals"] = fundamental_status
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

