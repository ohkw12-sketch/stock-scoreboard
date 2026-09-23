"""Value-growth ranking built from verified value and growth candidates."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

import pandas as pd


KST = timezone(timedelta(hours=9))
RULE_VERSION = "value-growth-split-4.0"
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


def _price_metrics(prices: pd.DataFrame) -> dict[str, dict]:
    """Return medium-term, non-chasing price checks for combined selection."""
    metrics: dict[str, dict] = {}
    if prices.empty or "ticker" not in prices or "close" not in prices:
        return metrics
    for ticker, group in prices.sort_values("date").groupby("ticker"):
        closes = pd.to_numeric(group["close"], errors="coerce").dropna()
        if closes.empty:
            continue
        current = float(closes.iloc[-1])
        ret5 = (
            (current / float(closes.iloc[-6]) - 1) * 100
            if len(closes) >= 6 and closes.iloc[-6] > 0 else None
        )
        ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else None
        previous_ma20 = float(closes.iloc[-21:-1].mean()) if len(closes) >= 21 else None
        distance = (
            (current / ma20 - 1) * 100 if ma20 is not None and ma20 > 0 else None
        )
        slope = (
            (ma20 / previous_ma20 - 1) * 100
            if ma20 is not None and previous_ma20 is not None and previous_ma20 > 0
            else None
        )
        non_overheated = bool(
            ret5 is not None
            and distance is not None
            and slope is not None
            and ret5 <= 10
            and distance <= 7
            and distance >= -3
            and slope > 0
        )
        metrics[str(ticker).zfill(6)] = {
            "currentPrice": round(current, 2),
            "fiveDayReturnPct": round(ret5, 2) if ret5 is not None else None,
            "ma20DistancePct": round(distance, 2) if distance is not None else None,
            "ma20SlopePct": round(slope, 2) if slope is not None else None,
            "nonOverheated": non_overheated,
        }
    return metrics


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
        penalty += 10
        warnings.append("컨센서스·가이던스 없음·계절성 추정 -10")

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
                             rotation_board: dict | None = None, *, now: datetime | None = None,
                             display_limit: int = DISPLAY_LIMIT) -> dict:
    """Build separate market-interest and absolute-value growth rankings."""
    now = now or datetime.now(KST)
    today = now.date()
    absolute_source = value_board.get("_absoluteRows") or value_board.get("_allRows", [])
    values = {str(row.get("ticker", "")).zfill(6): row for row in absolute_source}
    growth = {str(row.get("ticker", "")).zfill(6): row for row in growth_audit}
    financials = {str(row.get("ticker", "")).zfill(6): row for row in fundamentals.to_dict("records")}
    latest_prices = prices.sort_values("date").groupby("ticker").tail(1)
    latest = {str(row.get("ticker", "")).zfill(6): row for row in latest_prices.to_dict("records")}
    price_checks = _price_metrics(prices)
    common = sorted(set(values) & set(growth))
    absolute_rows, exclusions = [], []
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
            "absoluteValueGrowthScore": score,
            "score": score,
            "valueScore": round(value_score, 1),
            "growthScore": round(growth_score, 2),
            "normalizedPOP": value_row.get("normalizedPOP"),
            "normalizedPremiumPct": None,
            "valueConfidence": value_row.get("confidence"),
            "riskPenalty": risk_penalty,
            "riskWarnings": risk_warnings,
            "valueBasis": (
                f"{value_row.get('fundamentalYear', '당해연도')}E 절대 P/OP "
                f"{value_row.get('normalizedPOP', '—')}배 · 섹터 가치비교 제외"
            ),
            "fundamentalSource": value_row.get("fundamentalSource"),
            "fundamentalSourceDate": value_row.get("fundamentalSourceDate"),
            "fundamentalYear": value_row.get("fundamentalYear"),
            "seasonalityFallback": value_row.get("seasonalityFallback", False),
            "sectorAttentionUsed": False,
            **price_checks.get(ticker, {}),
            **risk_metrics,
        })
        absolute_rows.append(row)
    absolute_rows.sort(key=lambda row: (-row["valueGrowthScore"], row["ticker"]))
    all_rows = [dict(row, rank=index) for index, row in enumerate(absolute_rows, 1)]
    displayed = all_rows[:max(0, int(display_limit))]

    sector_rows = (rotation_board or {}).get("_allSectors") or (rotation_board or {}).get("sectors", [])
    sectors = {str(row.get("name", "")): row for row in sector_rows}
    interest_exclusions, interest_rows = [], []
    for ticker, growth_row in growth.items():
        negatives = _recent_valid_negatives(growth_row, today)
        if negatives:
            interest_exclusions.append({
                "ticker": ticker, "name": growth_row.get("name"),
                "reason": "최근 90일 유효 부정 근거", "evidenceCount": len(negatives),
            })
            continue
        sector_row = sectors.get(str(growth_row.get("sector", "")))
        growth_score = _number(growth_row.get("score"))
        sector_score = _number(
            (sector_row or {}).get("trendScore", (sector_row or {}).get("score"))
        )
        if growth_score is None or sector_score is None:
            continue
        # Sector interest is deliberately capped at 20% because the rotation tab
        # already exposes the same signal.  Company growth remains the main rank.
        interest_score = round(.80 * growth_score + .20 * sector_score, 2)
        stage = str(sector_row.get("stage") or "")
        trend_state = str(sector_row.get("trendState") or "관찰")
        entry_fit = str(sector_row.get("entryFit") or "관찰")
        risk_gauge = _number(sector_row.get("riskGauge")) or 0
        if stage == "⑥후반":
            entry_state = "추격주의"
        elif risk_gauge >= 70:
            entry_state = "과열주의"
        elif trend_state in {"추세확인", "추세유지"}:
            entry_state = trend_state
        elif trend_state == "신규포착":
            entry_state = "확인대기"
        else:
            entry_state = trend_state
        row = deepcopy(growth_row)
        row.pop("events", None)
        row.pop("priceResponses", None)
        row.pop("counterEvidence", None)
        row.update({
            "ticker": ticker,
            "interestGrowthScore": interest_score,
            "score": interest_score,
            "growthScore": round(growth_score, 2),
            "sectorAttentionScore": round(sector_score, 1),
            "sectorTrendScore": round(sector_score, 1),
            "sectorTodayScore": sector_row.get("todayScore"),
            "sectorTop20Days10": sector_row.get("top20Days10"),
            "sectorTrendState": trend_state,
            "sectorRank": sector_row.get("rank"),
            "sectorStage": stage,
            "sectorEntryFit": entry_fit,
            "sectorRiskGauge": round(risk_gauge, 1),
            "entryState": entry_state,
            "valuationMetricsUsed": False,
            **price_checks.get(ticker, {}),
        })
        interest_rows.append(row)
    interest_rows.sort(key=lambda row: (-row["interestGrowthScore"], row["ticker"]))
    all_interest_rows = [dict(row, rank=index) for index, row in enumerate(interest_rows, 1)]
    displayed_interest = all_interest_rows[:max(0, int(display_limit))]
    common_top = {row["ticker"] for row in displayed} & {
        row["ticker"] for row in displayed_interest
    }
    for row in all_rows:
        row["alsoInterestTop20"] = row["ticker"] in common_top
    for row in all_interest_rows:
        row["alsoAbsoluteTop20"] = row["ticker"] in common_top

    value_status = value_board.get("dataStatus", {})
    growth_status = growth_board.get("dataStatus", {})
    source_date = next((row.get("sourceDate") for row in all_interest_rows if row.get("sourceDate")),
                       value_board.get("_meta", {}).get("asOfDate"))
    interest_method = (
        "기업별 성장근거 80% + 해당 섹터 5·10·20일 추세 20% · 가치배수 제외 · "
        "섹터 신호 중복을 줄이고 오늘 강도와 추세상태를 별도 표시"
    )
    absolute_method = (
        "P/OP 15배 이하·절대가치점수 60점 이상만 대상 · 금융·지주 제외 · "
        "절대가치 50% + 기업별 성장근거 50% - 위험감점(최대 25점)"
    )
    return {
        "status": (
            f"시장관심·성장 {len(all_interest_rows)}개 중 {len(displayed_interest)}개 · "
            f"절대 저평가·성장 {len(all_rows)}개 중 {len(displayed)}개 표시"
        ),
        "method": "서로 다른 목적을 한 점수에 섞지 않고 시장 관심과 절대 저평가를 위·아래 두 표로 분리",
        "rows": displayed,
        "_allRows": all_rows,
        "interestGrowth": {
            "status": f"시장관심·성장 상위 {len(displayed_interest)}개",
            "method": interest_method,
            "rows": displayed_interest,
        },
        "absoluteValueGrowth": {
            "status": f"절대 저평가·성장 상위 {len(displayed)}개",
            "method": absolute_method,
            "rows": displayed,
        },
        "_allInterestRows": all_interest_rows,
        "_eligibility": [
            {"ticker": ticker, "name": (growth.get(ticker) or values.get(ticker) or {}).get("name"),
             "eligible": any(row["ticker"] == ticker for row in all_rows),
             "score": next((row["valueGrowthScore"] for row in all_rows if row["ticker"] == ticker), None),
             "reason": "가치·성장 교집합 통과" if any(row["ticker"] == ticker for row in all_rows)
                       else "최근 90일 유효 부정 근거"}
            for ticker in common
        ],
        "excludedRows": exclusions,
        "interestExcludedRows": interest_exclusions,
        "dataStatus": {
            "status": "정상",
            "valueCandidateCount": len(values),
            "growthCandidateCount": len(growth),
            "intersectionCount": len(common),
            "recentNegativeExcludedCount": len(exclusions),
            "finalCandidateCount": len(all_rows),
            "interestCandidateCount": len(all_interest_rows),
            "interestRecentNegativeExcludedCount": len(interest_exclusions),
            "commonTop20Count": len(common_top),
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
            "시장관심표는 성장용 완화 선조건과 동일 기간 성장률만 사용합니다. 절대 저평가표는 "
            "엄격한 가치 선조건을 적용하며 계절성 추정만 사용하면 10점을 감점합니다."
        ),
        "_meta": {"asOfDate": source_date, "engineVersion": RULE_VERSION,
                  "projectType": "value-growth", "displayLimit": int(display_limit)},
    }
