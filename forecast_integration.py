"""Join broker forecasts and official company guidance to reported fundamentals.

The reported-profit value score remains untouched.  These fields feed only the
existing growth-evidence path and retain the original source/date/status.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd


KST = timezone(timedelta(hours=9))
USABLE_STATUSES = {"유효 컨센서스", "참고 컨센서스", "개별 추정치", "외부 집계 컨센서스"}
CONFIDENCE = {"유효 컨센서스": 1.0, "참고 컨센서스": .85,
              "외부 집계 컨센서스": .80, "개별 추정치": .65}


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _read(path, default):
    path = Path(path)
    return json.loads(path.read_text("utf-8-sig")) if path.exists() else default


def _period_year(value):
    match = re.fullmatch(r"(20\d{2})(?:FY)?", str(value or ""))
    return int(match.group(1)) if match else None


def _source_url(row):
    estimates = row.get("estimates") or []
    estimates = sorted(estimates, key=lambda item: item.get("reportDate") or "", reverse=True)
    return next((item.get("sourceUrl") for item in estimates
                 if str(item.get("sourceUrl") or "").startswith("https://")), None)


def _forecast_point(row):
    return {
        "sales": _number(row.get("salesMedianKrw100m")),
        "op": _number(row.get("operatingProfitMedianKrw100m")),
        "date": row.get("latestReportDate"),
        "url": _source_url(row),
        "status": row.get("status"),
        "confidence": CONFIDENCE.get(row.get("status"), 0),
        "source": "FnGuide 공개 집계" if row.get("status") == "외부 집계 컨센서스" else "증권사 리포트",
        "guidance": False,
    }


def _guidance_point(row):
    def eok(item):
        value = _number((item or {}).get("mid"))
        return value / 100_000_000 if value is not None else None
    return {
        "sales": eok(row.get("sales")), "op": eok(row.get("operating_profit")),
        "date": row.get("published_at"), "url": row.get("source_url"),
        "status": "회사 공식 가이던스", "confidence": 1.0,
        "source": "회사 공식 가이던스", "guidance": True,
    }


def _overlay(base, official):
    if not base:
        return dict(official)
    result = dict(base)
    for key in ("sales", "op"):
        if official.get(key) is not None:
            result[key] = official[key]
    result.update(guidance=True, guidance_date=official.get("date"),
                  guidance_url=official.get("url"), guidance_basis="consolidated")
    result["date"] = max(filter(None, (base.get("date"), official.get("date"))), default=None)
    result["source"] = "회사 공식 가이던스 + " + base.get("source", "외부 전망")
    result["confidence"] = max(base.get("confidence", 0), official.get("confidence", 0))
    return result


def integrate_forecasts(fundamentals: pd.DataFrame, forecast_rows: list[dict],
                        guidance_rows: list[dict], *, today: date | None = None,
                        fetched_at: str | None = None) -> tuple[pd.DataFrame, dict]:
    """Return an enriched copy and an auditable integration status.

    Only annual current/next-year pairs produce growth evidence.  Disagreement
    rows are retained in the collector audit but are never admitted here.
    Consolidated annual guidance replaces the matching period metric-by-metric.
    """
    today = today or datetime.now(KST).date()
    fetched_at = fetched_at or datetime.now(KST).isoformat(timespec="seconds")
    current_year, next_year = today.year, today.year + 1
    annual, rejected = {}, []
    for raw in forecast_rows or []:
        year = _period_year(raw.get("period"))
        if raw.get("scope") != "annual" or year not in (current_year, next_year):
            continue
        ticker = str(raw.get("ticker") or "").zfill(6)
        if not re.fullmatch(r"\d{6}", ticker) or raw.get("status") not in USABLE_STATUSES:
            rejected.append({"ticker": ticker, "period": raw.get("period"), "status": raw.get("status")})
            continue
        annual[(ticker, year)] = _forecast_point(raw)

    official = {}
    for raw in guidance_rows or []:
        year = _period_year(raw.get("period"))
        ticker = str(raw.get("ticker") or "").zfill(6)
        if (year not in (current_year, next_year) or raw.get("basis") != "consolidated"
                or raw.get("active") is not True or raw.get("fetch_status") != "정상"
                or not re.fullmatch(r"\d{6}", ticker)):
            continue
        official[(ticker, year)] = _guidance_point(raw)

    for key, point in official.items():
        annual[key] = _overlay(annual.get(key), point)

    frame = fundamentals.copy()
    if frame.empty or "ticker" not in frame:
        return frame, {"status": "자료없음", "integratedTickers": 0,
                       "problem": "확정 실적 기본 프레임 없음"}
    frame["ticker"] = frame["ticker"].astype(str).str.zfill(6)
    # Preserve KIS revision observations for the rotation engine.  The report
    # consensus replaces forecast levels only and has no revision time series.
    for target, source in {
        "revision_consensus_fetched_at": "consensus_fetched_at",
        "revision_consensus_as_of_precision": "consensus_as_of_precision",
        "revision_consensus_change_20d": "consensus_change_20d",
        "revision_consensus_source": "consensus_source",
    }.items():
        if target not in frame:
            frame[target] = frame[source] if source in frame else None
    columns = {
        "consensus_as_of": None, "consensus_as_of_precision": None,
        "consensus_provider_date_raw": None, "consensus_fetched_at": None,
        "estimate_period": None, "prior_period": None, "next_estimate_period": None,
        "consensus_prior_sales": np.nan, "consensus_prior_op": np.nan,
        "consensus_forward_sales": np.nan, "consensus_forward_op": np.nan,
        "consensus_sales_1y_growth": np.nan, "consensus_op_1y_growth": np.nan,
        "consensus_sales_2026": np.nan, "consensus_op_2026": np.nan,
        "consensus_sales_2027": np.nan, "consensus_op_2027": np.nan,
        "consensus_source": None, "consensus_source_url": None,
        "consensus_estimate_status": None, "consensus_confidence_factor": np.nan,
        "consensus_status": None, "consensus_change_1d": np.nan,
        "consensus_change_5d": np.nan, "consensus_change_20d": np.nan,
        "consensus_op_turnaround": False, "guidance_used": False,
        "guidance_source_url": None,
    }
    for column, default in columns.items():
        if column not in frame:
            frame[column] = default
    for column in {
        "consensus_as_of", "consensus_as_of_precision", "consensus_provider_date_raw",
        "consensus_fetched_at", "estimate_period", "prior_period", "next_estimate_period",
        "consensus_source", "consensus_source_url", "consensus_estimate_status",
        "consensus_status", "guidance_source_url",
    }:
        frame[column] = frame[column].astype(object)

    integrated, guidance_used, annual_available = [], [], set()
    for ticker in frame["ticker"]:
        current, forward = annual.get((ticker, current_year)), annual.get((ticker, next_year))
        mask = frame["ticker"].eq(ticker)
        for year, point in ((current_year, current), (next_year, forward)):
            if not point:
                continue
            annual_available.add(ticker)
            if year in (2026, 2027):
                frame.loc[mask, f"consensus_sales_{year}"] = point.get("sales")
                frame.loc[mask, f"consensus_op_{year}"] = point.get("op")
        if not current or not forward:
            continue
        current_sales, next_sales = current.get("sales"), forward.get("sales")
        current_op, next_op = current.get("op"), forward.get("op")
        if current_sales is None or current_sales <= 0 or next_sales is None or next_op is None:
            continue
        sales_growth = (next_sales / current_sales - 1) * 100
        op_turnaround = current_op is not None and current_op <= 0 < next_op
        op_growth = ((next_op / current_op - 1) * 100
                     if current_op is not None and current_op > 0 else None)
        latest = max(filter(None, (current.get("date"), forward.get("date"))), default=None)
        used_guidance = bool(current.get("guidance") or forward.get("guidance"))
        source = "회사 공식 가이던스 + 증권사 전망" if used_guidance else "증권사 리포트·공개 집계 컨센서스"
        source_url = forward.get("url") or current.get("url")
        guidance_url = forward.get("guidance_url") or current.get("guidance_url")
        estimate_status = f"{current.get('status')} / {forward.get('status')}"
        confidence = min(current.get("confidence", 0), forward.get("confidence", 0))
        updates = {
            "consensus_as_of": latest, "consensus_as_of_precision": "day",
            "consensus_provider_date_raw": latest, "consensus_fetched_at": fetched_at,
            "estimate_period": f"{next_year}.12E", "prior_period": f"{current_year}.12",
            "next_estimate_period": None, "consensus_prior_sales": current_sales,
            "consensus_prior_op": current_op, "consensus_forward_sales": next_sales,
            "consensus_forward_op": next_op, "consensus_sales_1y_growth": sales_growth,
            "consensus_op_1y_growth": op_growth, "consensus_source": source,
            "consensus_source_url": source_url, "consensus_estimate_status": estimate_status,
            "consensus_confidence_factor": confidence, "consensus_status": "정상",
            "consensus_change_1d": np.nan, "consensus_change_5d": np.nan,
            "consensus_change_20d": np.nan, "consensus_op_turnaround": op_turnaround,
            "guidance_used": used_guidance, "guidance_source_url": guidance_url,
        }
        for column, value in updates.items():
            frame.loc[mask, column] = value
        integrated.append(ticker)
        if used_guidance:
            guidance_used.append(ticker)

    dates = [point.get("date") for point in annual.values() if point.get("date")]
    verified = {ticker: fetched_at for ticker in integrated}
    return frame, {
        "status": "정상", "source": "증권사 리포트·FnGuide 공개 집계 + 회사 공식 가이던스",
        "asOfDate": max(dates, default=None), "currentYear": current_year, "nextYear": next_year,
        "annualForecastTickers": len(annual_available), "integratedTickers": len(set(integrated)),
        "guidancePreferredTickers": len(set(guidance_used)),
        "rejectedDisagreementPoints": len(rejected), "rejected": rejected,
        "freshTickers": sorted(set(integrated)), "verifiedAtByTicker": verified,
        "policy": "연결 연간 가이던스가 있으면 같은 기간 수치를 우선하고, 없으면 외부 컨센서스를 사용",
    }


def integrate_from_files(fundamentals: pd.DataFrame, forecast_path, guidance_path,
                         *, today: date | None = None) -> tuple[pd.DataFrame, dict]:
    forecast_rows = _read(forecast_path, [])
    guidance = _read(guidance_path, {})
    guidance_rows = guidance.get("rows", []) if isinstance(guidance, dict) else []
    report = _read(Path(forecast_path).with_name("run_report.json"), {})
    fetched_at = report.get("generatedAtKST") or report.get("generatedAt") or report.get("generated_at")
    return integrate_forecasts(fundamentals, forecast_rows, guidance_rows,
                               today=today, fetched_at=fetched_at)
