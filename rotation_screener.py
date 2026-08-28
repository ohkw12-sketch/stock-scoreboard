"""KOSPI+KOSDAQ whole-market rotation screener.

The program never writes to data.json. It emits replaceable p11 data and a
full-board test copy under test_output/.
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
    "cache_dir": "cache",
    "output_dir": "test_output",
    "base_data_file": "data.json",
    "primary_source": "pykrx",
    "fallback_sources": ["cache", "yfinance"],
    "cache_max_age_hours": 36,
    "request_retries": 3,
    "request_pause_seconds": 0.35,
    "yfinance_chunk_size": 80,
    "default_cycle_days": 25,
    "sector_overrides_file": "sector_overrides.example.csv",
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
    for key in ("cache_dir", "output_dir", "base_data_file", "sector_overrides_file"):
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

    @property
    def cache_file(self) -> Path:
        return self.cache_dir / "krx_prices.csv.gz"

    @property
    def listing_cache_file(self) -> Path:
        return self.cache_dir / "krx_listing_desc.csv"

    def load(self) -> tuple[pd.DataFrame, str]:
        if self.config["mode"] == "sample":
            return generate_sample_market(), "deterministic-sample"
        sources = [self.config["primary_source"], *self.config["fallback_sources"]]
        seen: set[str] = set()
        errors: list[str] = []
        for source in sources:
            if source in seen:
                continue
            seen.add(source)
            try:
                if source == "pykrx":
                    return self._pykrx(), "pykrx"
                if source == "cache":
                    return self._cache(require_fresh=False), "cache"
                if source == "yfinance":
                    return self._yfinance(), "yfinance+FinanceDataReader"
                raise ValueError(f"unknown data source: {source}")
            except Exception as exc:
                errors.append(f"{source}: {exc}")
                log(f"source unavailable, trying next: {source}")
        raise RuntimeError("all market data sources failed\n- " + "\n- ".join(errors))

    def _cache(self, require_fresh: bool) -> pd.DataFrame:
        if not self.cache_file.exists():
            raise FileNotFoundError(f"cache not found: {self.cache_file}")
        age_hours = (time.time() - self.cache_file.stat().st_mtime) / 3600
        if require_fresh and age_hours > float(self.config["cache_max_age_hours"]):
            raise RuntimeError(f"cache is {age_hours:.1f} hours old")
        frame = normalize_prices(pd.read_csv(self.cache_file))
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
    work["ret1"] = work.groupby("ticker")["close"].pct_change()
    work["ret3"] = work.groupby("ticker")["close"].pct_change(3)
    work["ret5"] = work.groupby("ticker")["close"].pct_change(5)
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


def stock_rows(stock_data: pd.DataFrame, sector_results: pd.DataFrame, limit: int) -> list[dict]:
    latest_date = stock_data["date"].max()
    current = stock_data[stock_data["date"].eq(latest_date)].copy()
    sector_map = sector_results.set_index("name")
    active_sectors = sector_results.loc[sector_results["entryFit"].ne("제외/관찰"), "name"]
    current = current[current["sector"].isin(active_sectors)].copy()
    current["sector_ret3"] = current["sector"].map(sector_map["raw_ret3"])
    current["sector_ret5"] = current["sector"].map(sector_map["raw_ret5"])
    current["stock_excess3"] = current["ret3"] - current["sector_ret3"]
    current["stock_excess5"] = current["ret5"] - current["sector_ret5"]
    current["sector_entry_score"] = current["sector"].map(sector_map["entryScore"])
    current["overheated"] = (current["ret1"] > 0.10) | (current["ret3"] > 0.15) | (current["ret5"] > 0.25)
    current["stock_score"] = (
        current["sector_entry_score"].fillna(0) +
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
            "①초기": "관찰", "②확산": "눌림목관찰", "③주도": "분할관찰",
            "④눌림": "눌림대기", "⑤재반등": "기술대기", "⑥후반": "추격금지",
            "X조기이탈": "제외", "X종료": "제외",
        }[stage]
        if sector.get("entryFit") == "진입적합":
            signal = "진입관찰"
        if overheated:
            signal = "추격금지"
        marks = ["UP"] if relation == "선행" else ["OLD"] if relation == "후행" else []
        if float(sector["riskGauge"]) >= 70 or overheated:
            marks.append("RISK")
        row_entry_fit = "추격금지" if overheated else sector.get("entryFit", "관찰")
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
            "entryFit": row_entry_fit, "overheated": overheated,
            "stockEntryScore": round(float(item.stock_score), 1),
            "reason": f"{row_entry_fit}{' · 단기과열' if overheated else ''} · 섹터 대비 {relation}; 3일 초과수익 {excess * 100:+.2f}%p",
        })
    return rows


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
    sector_results = pd.DataFrame(results).sort_values(["entryScore", "score"], ascending=False).reset_index(drop=True)
    sector_results["rank"] = np.arange(1, len(sector_results) + 1)
    top = sector_results.head(int(config["top_sector_count"])).copy()
    rows = stock_rows(stock_data, top, int(config["top_stock_count"]))
    public_sectors = top.drop(columns=["raw_ret3", "raw_ret5"]).to_dict("records")
    stage_counts = top["stage"].value_counts().to_dict()
    entry_count = int((top["entryFit"] == "진입적합").sum())
    status = (
        f"전체시장 엔진: KOSPI+KOSDAQ {prices['ticker'].nunique():,}종목, "
        f"{len(sector_results):,}개 섹터 분석. 기준일 {latest_date:%Y-%m-%d}. "
        f"확산형과 선도주 견인형을 함께 반영했으며 신규 진입 적합 {entry_count}개, 단계 분포 {stage_counts}."
    )
    return {
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


def write_outputs(p11: dict, config: dict) -> tuple[Path, Path]:
    output_dir: Path = config["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    p11_path = output_dir / "p11.test.json"
    data_path = output_dir / "data.test.json"
    p11_path.write_text(json.dumps({"p11": p11}, ensure_ascii=False, indent=2), encoding="utf-8")
    base_path: Path = config["base_data_file"]
    if base_path.exists():
        board = json.loads(base_path.read_text(encoding="utf-8-sig"))
    else:
        board = {"meta": {"title": "주식 허브 테스트"}}
    board["p11"] = p11
    data_path.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    return p11_path, data_path


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
        prices, source = MarketDataLoader(config).load()
        log(f"loaded {prices['ticker'].nunique():,} stocks, {prices['date'].nunique()} trading days from {source}")
        p11 = run_engine(prices, config, source)
        p11_path, data_path = write_outputs(p11, config)
        after = sha256(base_path) if base_path.exists() else None
        if before != after:
            raise RuntimeError("safety check failed: base data.json changed")
        log(f"p11 output: {p11_path}")
        log(f"board test output: {data_path}")
        log("safety check: original data.json unchanged")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

