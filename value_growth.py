"""Value-growth ranking built from verified value and growth candidates."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

import pandas as pd


KST = timezone(timedelta(hours=9))
RULE_VERSION = "value-growth-2.1"
DISPLAY_LIMIT = 20


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _day(value):
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def _recent_valid_negatives(row: dict, today) -> list[dict]:
    result = []
    for event in row.get("counterEvidence", []):
        published = _day(event.get("publishedAt") or event.get("firstPublished"))
        if (event.get("polarity") == "negative" and event.get("status") == "유효"
                and published is not None and 0 <= (today - published).days <= 90):
            result.append(event)
    return result


def _consensus_only(row: dict) -> bool:
    events = [event for event in row.get("events", [])
              if event.get("status") == "유효" and event.get("polarity") == "positive"]
    return bool(events) and all(
        event.get("kind") == "컨센서스" or event.get("sourceType") == "컨센서스"
        for event in events
    )


def _risk(fundamental: dict, price: dict, value_row: dict, growth_row: dict) -> tuple[int, list[str], dict]:
    op = [_number(fundamental.get(f"value_fundamental_q{quarter}_op")) for quarter in (1, 2)]
    sales = [_number(fundamental.get(f"value_fundamental_q{quarter}_sales")) for quarter in (1, 2)]
    warnings: list[str] = []
    penalty = 0

    forward_op_growth = _number(fundamental.get("value_fundamental_next_op_growth_pct"))
    q2_decline = op[0] is not None and op[1] is not None and op[1] < op[0]
    if forward_op_growth is not None and forward_op_growth <= -20:
        penalty += 15
        warnings.append("내년 예상 영업이익 20% 이상 감소 -15")
    elif forward_op_growth is not None and forward_op_growth < 0:
        penalty += 7
        warnings.append("내년 예상 영업이익 감소 -7")
    elif forward_op_growth is None and q2_decline:
        penalty += 7
        warnings.append("2026년 2분기 영업이익이 1분기보다 감소 -7")

    margins = [profit / revenue * 100 if profit is not None and revenue and revenue > 0 else None
                for profit, revenue in zip(op, sales)]
    available_margins = [value for value in margins if value is not None]
    average_margin = sum(available_margins) / len(available_margins) if len(available_margins) == 2 else None
    latest_margin = margins[-1]
    if (margins[0] is not None and latest_margin is not None
            and latest_margin < margins[0] * .70):
        penalty += 5
        warnings.append("2026년 2분기 영업이익률이 1분기의 70% 미만 -5")

    current_annualized_pop = _number(value_row.get("normalizedPOP"))
    sector_pop = _number(value_row.get("sectorNormalizedPOP"))
    if current_annualized_pop is not None and sector_pop is not None and current_annualized_pop > sector_pop:
        penalty += 5
        warnings.append("당해연도 P/OP가 섹터 중앙보다 높음 -5")

    if bool(value_row.get("seasonalityFallback")):
        penalty += 5
        warnings.append("컨센서스·가이던스 없음·계절성 추정 -5")

    if _consensus_only(growth_row):
        penalty += 5
        warnings.append("성장 근거가 컨센서스뿐임 -5")

    applied = min(25, penalty)
    return applied, warnings, {
        "operatingProfitDeclineStreak": 1 if q2_decline else 0,
        "nextYearOperatingProfitGrowthPct": (
            round(forward_op_growth, 1) if forward_op_growth is not None else None
        ),
        "latestQuarterOperatingMarginPct": round(latest_margin, 1) if latest_margin is not None else None,
        "fourQuarterAverageOperatingMarginPct": round(average_margin, 1) if average_margin is not None else None,
        "currentHalfAverageOperatingMarginPct": round(average_margin, 1) if average_margin is not None else None,
        "currentQuarterAnnualizedPOP": round(current_annualized_pop, 1) if current_annualized_pop is not None else None,
        "sectorNormalizedPOP": round(sector_pop, 1) if sector_pop is not None else None,
        "consensusOnlyEvidence": _consensus_only(growth_row),
        "seasonalityEstimateOnly": bool(value_row.get("seasonalityFallback")),
    }


def build_value_growth_board(value_board: dict, growth_board: dict, growth_audit: list[dict],
                             fundamentals: pd.DataFrame, prices: pd.DataFrame,
                             *, now: datetime | None = None, display_limit: int = DISPLAY_LIMIT) -> dict:
    """Intersect both verified candidate pools and rank 50/50 less capped risk penalties."""
    now = now or datetime.now(KST)
    today = now.date()
    values = {str(row.get("ticker", "")).zfill(6): row for row in value_board.get("_allRows", [])}
    growth = {str(row.get("ticker", "")).zfill(6): row for row in growth_audit}
    financials = {str(row.get("ticker", "")).zfill(6): row for row in fundamentals.to_dict("records")}
    latest_prices = prices.sort_values("date").groupby("ticker").tail(1)
    latest = {str(row.get("ticker", "")).zfill(6): row for row in latest_prices.to_dict("records")}
    common = sorted(set(values) & set(growth))
    rows, exclusions = [], []
    for ticker in common:
        value_row, growth_row = values[ticker], growth[ticker]
        negatives = _recent_valid_negatives(growth_row, today)
        if negatives:
            exclusions.append({
                "ticker": ticker, "name": growth_row.get("name") or value_row.get("name"),
                "reason": "최근 90일 유효 부정 근거", "evidenceCount": len(negatives),
            })
            continue
        value_score, growth_score = _number(value_row.get("valueScore")), _number(growth_row.get("score"))
        if value_score is None or growth_score is None:
            continue
        risk_penalty, risk_warnings, risk_metrics = _risk(
            financials.get(ticker, {}), latest.get(ticker, {}), value_row, growth_row,
        )
        score = round(.50 * value_score + .50 * growth_score - risk_penalty, 2)
        row = deepcopy(growth_row)
        row.pop("events", None)
        row.pop("priceResponses", None)
        row.pop("counterEvidence", None)
        row.update({
            "ticker": ticker,
            "valueGrowthScore": score,
            "score": score,
            "valueScore": round(value_score, 1),
            "growthScore": round(growth_score, 2),
            "normalizedPOP": value_row.get("normalizedPOP"),
            "normalizedPremiumPct": value_row.get("normalizedPremiumPct"),
            "valueConfidence": value_row.get("confidence"),
            "riskPenalty": risk_penalty,
            "riskWarnings": risk_warnings,
            "valueBasis": (
                f"{value_row.get('fundamentalYear', '당해연도')}E P/OP {value_row.get('normalizedPOP', '—')}배 · "
                f"섹터 대비 {abs(value_row.get('normalizedPremiumPct') or 0):.1f}% "
                f"{'할인' if (value_row.get('normalizedPremiumPct') or 0) < 0 else '프리미엄'}"
            ),
            "fundamentalSource": value_row.get("fundamentalSource"),
            "fundamentalSourceDate": value_row.get("fundamentalSourceDate"),
            "seasonalityFallback": value_row.get("seasonalityFallback", False),
            **risk_metrics,
        })
        rows.append(row)
    rows.sort(key=lambda row: (-row["valueGrowthScore"], row["ticker"]))
    all_rows = [dict(row, rank=index) for index, row in enumerate(rows, 1)]
    displayed = all_rows[:max(0, int(display_limit))]
    value_status = value_board.get("dataStatus", {})
    growth_status = growth_board.get("dataStatus", {})
    source_date = next((row.get("sourceDate") for row in all_rows if row.get("sourceDate")),
                       value_board.get("_meta", {}).get("asOfDate"))
    return {
        "status": (
            f"가치 후보 {len(values)}개와 성장 근거 후보 {len(growth)}개의 교집합 {len(common)}개 · "
            f"부정 근거 {len(exclusions)}개 제외 · 최종 {len(all_rows)}개 중 {len(displayed)}개 표시"
        ),
        "method": (
            "당해연도 미래 펀더멘털 가치점수 50% + 성장조기포착 점수 50% - 위험감점(최대 25점) · "
            "최근 90일 유효 부정 근거는 제외 · 최종 순위는 최대 20위까지만 표시"
        ),
        "rows": displayed,
        "_allRows": all_rows,
        "_eligibility": [
            {"ticker": ticker, "name": (growth.get(ticker) or values.get(ticker) or {}).get("name"),
             "eligible": any(row["ticker"] == ticker for row in all_rows),
             "score": next((row["valueGrowthScore"] for row in all_rows if row["ticker"] == ticker), None),
             "reason": "가치·성장 교집합 통과" if any(row["ticker"] == ticker for row in all_rows)
                       else "최근 90일 유효 부정 근거"}
            for ticker in common
        ],
        "excludedRows": exclusions,
        "dataStatus": {
            "status": "정상",
            "valueCandidateCount": len(values),
            "growthCandidateCount": len(growth),
            "intersectionCount": len(common),
            "recentNegativeExcludedCount": len(exclusions),
            "finalCandidateCount": len(all_rows),
            "displayLimit": int(display_limit),
            "valuePrerequisites": {
                "minimumAverageQuarterlySales": value_status.get("minimumAverageQuarterlySales"),
                "minimumAverageQuarterlyOperatingMarginPct": value_status.get("minimumAverageQuarterlyOperatingMarginPct"),
            },
            "growthPrerequisites": {
                "minimumAverageQuarterlySales": growth_status.get("minimumAverageQuarterlySales"),
                "minimumAverageQuarterlyOperatingMarginPct": growth_status.get("minimumAverageQuarterlyOperatingMarginPct"),
                "minimumAverageTurnover": growth_status.get("minimumAverageTurnover"),
            },
            "evidence": growth_status,
        },
        "ruleVersion": RULE_VERSION,
        "methodVersion": RULE_VERSION,
        "projectType": "value-growth",
        "sourceDate": source_date,
        "updatedKST": now.strftime("%Y-%m-%d %H:%M"),
        "notice": (
            "가이던스·컨센서스가 있으면 최신 전망을 사용하고, 없으면 1·2분기 실제와 전년도 계절성 비율로 "
            "3·4분기를 추정하고 신뢰도 5점을 감점합니다. 전년도 절대 실적은 평가하지 않으며, "
            "당해연도 P/OP가 섹터 중앙보다 높으면 5점을 감점합니다."
        ),
        "_meta": {"asOfDate": source_date, "engineVersion": RULE_VERSION,
                  "projectType": "value-growth", "displayLimit": int(display_limit)},
    }
