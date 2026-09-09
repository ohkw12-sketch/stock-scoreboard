"""Attach compact recommendation-history labels to formula-driven boards."""
from __future__ import annotations

from copy import deepcopy
from datetime import date

import pandas as pd


SCOPES = ("p1", "p11", "p2", "growth", "growth-sector", "combined")


def _day(value) -> date | None:
    if not value:
        return None
    stamp = pd.Timestamp(value)
    return None if pd.isna(stamp) else stamp.date()


def _cohort_scope(section: str, record: dict) -> str:
    if section != "growth":
        return section
    return "growth-sector" if str(record.get("group", "")).startswith("성장섹터") else "growth"


def _published_rosters(ledger: dict) -> dict[str, dict[date, set[str]]]:
    """Keep the last confirmed publication for each project and source date."""
    daily: dict[tuple[str, date], tuple[pd.Timestamp, set[str]]] = {}
    for cohort in ledger.get("cohorts", []):
        observed = cohort.get("observedPublishedAt")
        section = cohort.get("section")
        source_day = _day(cohort.get("sourceDate") or cohort.get("generatedAt"))
        if not observed or section not in {"p1", "p11", "p2", "growth", "combined"} or source_day is None:
            continue
        observed_stamp = pd.Timestamp(observed)
        by_scope: dict[str, set[str]] = {}
        for record in cohort.get("records", []):
            ticker = str(record.get("ticker", "")).zfill(6)
            if not ticker.strip("0"):
                continue
            by_scope.setdefault(_cohort_scope(section, record), set()).add(ticker)
        expected = ("growth", "growth-sector") if section == "growth" else (section,)
        for scope in expected:
            key = (scope, source_day)
            if key not in daily or observed_stamp > daily[key][0]:
                daily[key] = (observed_stamp, by_scope.get(scope, set()))
    result = {scope: {} for scope in SCOPES}
    for (scope, source_day), (_, tickers) in daily.items():
        result[scope][source_day] = tickers
    return result


def _source_day(section: dict, fallback=None) -> date | None:
    state = section.get("refreshState", {})
    candidate = (
        section.get("sourceDate")
        or state.get("sourceCutoff")
        or section.get("_meta", {}).get("asOfDate")
        or fallback
    )
    if candidate:
        return _day(candidate)
    rows = section.get("rows", [])
    return _day(rows[0].get("priceDate") or rows[0].get("sourceDate")) if rows else None


def _history(ticker: str, rosters: dict[date, set[str]], current_day: date,
             trading_days: list[date]) -> dict:
    section_days = sorted(day for day in rosters if day <= current_day)
    appearances = [day for day in section_days if ticker in rosters[day]]
    prior = [day for day in appearances if day < current_day]
    common = {
        "firstDate": current_day.isoformat(), "previousDate": None,
        "recommendationDays": 1, "spanTradingDays": 1, "gapCalendarDays": None,
    }
    if not prior:
        return {**common, "kind": "new", "label": "신규 추천"}

    previous = prior[-1]
    gap = (current_day - previous).days
    if gap > 7:
        return {
            **common, "kind": "return", "label": f"{gap}일 만의 재추천",
            "previousDate": previous.isoformat(), "gapCalendarDays": gap,
        }

    current_index = section_days.index(current_day)
    consecutive = 0
    for day in reversed(section_days[:current_index + 1]):
        if ticker not in rosters[day]:
            break
        consecutive += 1
    if consecutive >= 2:
        start = section_days[current_index - consecutive + 1]
        return {
            "kind": "consecutive", "label": f"{consecutive}거래일 연속 추천",
            "firstDate": start.isoformat(), "previousDate": previous.isoformat(),
            "recommendationDays": consecutive, "spanTradingDays": consecutive,
            "gapCalendarDays": gap,
        }

    chain = [current_day]
    for day in reversed(prior):
        if (chain[-1] - day).days > 7:
            break
        chain.append(day)
    start = chain[-1]
    sessions = [day for day in trading_days if start <= day <= current_day]
    span = len(sessions) or len([day for day in section_days if start <= day <= current_day])
    return {
        "kind": "recent", "label": f"최근 {span}거래일 중 {len(chain)}일 추천",
        "firstDate": start.isoformat(), "previousDate": previous.isoformat(),
        "recommendationDays": len(chain), "spanTradingDays": span,
        "gapCalendarDays": gap,
    }


def _attach(rows: list[dict], scope: str, source_day: date | None,
            rosters: dict[str, dict[date, set[str]]], trading_days: list[date]) -> None:
    if source_day is None:
        return
    current = {str(row.get("ticker", "")).zfill(6) for row in rows if row.get("ticker")}
    rosters[scope][source_day] = current
    for row in rows:
        ticker = str(row.get("ticker", "")).zfill(6)
        if ticker in current:
            row["recommendationHistory"] = _history(ticker, rosters[scope], source_day, trading_days)


def attach_recommendation_history(board: dict, combined: dict, ledger: dict,
                                  trading_sessions=None) -> tuple[dict, dict]:
    """Return copies with per-project labels; holdings and commentary are untouched."""
    board, combined = deepcopy(board), deepcopy(combined)
    rosters = _published_rosters(ledger)
    sessions = [] if trading_sessions is None else trading_sessions
    trading_days = sorted({day for value in sessions if (day := _day(value)) is not None})

    for scope in ("p1", "p11", "p2"):
        section = board.get(scope, {})
        _attach(section.get("rows", []), scope, _source_day(section), rosters, trading_days)

    growth = board.get("growth", {})
    growth_day = _source_day(growth)
    _attach(growth.get("rows", []), "growth", growth_day, rosters, trading_days)
    sector_rows = [row for sector in growth.get("sectors", []) for row in sector.get("stocks", [])]
    _attach(sector_rows, "growth-sector", growth_day, rosters, trading_days)

    _attach(combined.get("rows", []), "combined", _source_day(combined), rosters, trading_days)
    policy = {
        "version": "recommendation-history-1.0", "gapCalendarDays": 7,
        "basis": "검증 후 공개된 프로젝트별 추천일; 순위 변동 미사용",
    }
    for scope in ("p1", "p11", "p2", "growth"):
        if scope in board:
            board[scope]["recommendationHistoryPolicy"] = policy
    combined["recommendationHistoryPolicy"] = policy
    return board, combined
