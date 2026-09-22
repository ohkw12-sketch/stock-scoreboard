"""Build the value-board fundamental basis without using prior-year levels.

Current-year consensus or official guidance is preferred.  When neither source
has a usable current-year sales and operating-profit pair, the prior year's
quarter pattern is used only as a seasonality ratio to extend the current
year's verified Q1 and Q2 results into Q3 and Q4.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

import numpy as np
import pandas as pd


KST = timezone(timedelta(hours=9))
KRW_100M = 100_000_000.0


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _current_quarters(row, year: int):
    periods = str(row.get("normalization_periods") or "")
    if periods and not all(f"{year}Q{quarter}" in periods for quarter in (1, 2)):
        return (None,) * 4
    return tuple(_number(row.get(f"normalized_{kind}_q{quarter}"))
                 for kind, quarter in (("sales", 1), ("sales", 2), ("op", 1), ("op", 2)))


def _prior_seasonality(row, year: int):
    """Return prior-year Q1..Q4 amounts used only to calculate ratios."""
    periods = str(row.get("normalization_periods") or "")
    prior = year - 1
    if periods and not all(f"{prior}Q{quarter}" in periods for quarter in (3, 4)):
        return (None,) * 8
    prior_h1_sales = _number(row.get("sales_previous"))
    prior_q2_sales = _number(row.get("sales_quarter_previous"))
    prior_h1_op = _number(row.get("op_previous"))
    prior_q2_op = _number(row.get("op_quarter_previous"))
    prior_q1_sales = (prior_h1_sales - prior_q2_sales
                      if prior_h1_sales is not None and prior_q2_sales is not None else None)
    prior_q1_op = (prior_h1_op - prior_q2_op
                   if prior_h1_op is not None and prior_q2_op is not None else None)
    return (
        prior_q1_sales, prior_q2_sales,
        _number(row.get("normalized_sales_q3")), _number(row.get("normalized_sales_q4")),
        prior_q1_op, prior_q2_op,
        _number(row.get("normalized_op_q3")), _number(row.get("normalized_op_q4")),
    )


def _source_label(row, year: int) -> str:
    source = str(row.get(f"forecast_source_{year}") or row.get("consensus_source") or "").strip()
    guidance = _truthy(row.get(f"forecast_guidance_used_{year}") or row.get("guidance_used"))
    consensus = _truthy(row.get(f"forecast_consensus_used_{year}"))
    if guidance and consensus:
        return "회사 공식 가이던스 + 외부 컨센서스"
    if guidance:
        return "회사 공식 가이던스"
    return source or "외부 컨센서스"


def _actual_quality(q1_sales, q2_sales, q1_op, q2_op):
    values = (q1_sales, q2_sales, q1_op, q2_op)
    completeness = sum(value is not None for value in values) / 4
    profits = [value for value in (q1_op, q2_op) if value is not None]
    positive = sum(value > 0 for value in profits) / len(profits) if profits else 0
    margins = [profit / sales * 100 for profit, sales in ((q1_op, q1_sales), (q2_op, q2_sales))
               if profit is not None and sales is not None and sales > 0]
    if len(margins) == 2:
        mean = (abs(margins[0]) + abs(margins[1])) / 2
        consistency = 100 / (1 + abs(margins[1] - margins[0]) / max(mean, 1))
    else:
        consistency = 0
    return max(0.0, min(100.0, completeness * 40 + positive * 30 + consistency * .30))


def attach_future_fundamentals(frame: pd.DataFrame, *, current_year: int | None = None) -> pd.DataFrame:
    """Attach the annual basis used by the value-growth board.

    Forecast values are stored by the forecast integration in KRW 100m and are
    converted to won here.  Seasonal fallback never feeds a prior-year amount
    directly into valuation; prior-year quarters only determine scaling ratios.
    """
    year = int(current_year or datetime.now(KST).year)
    data = frame.copy()
    if data.empty:
        return data

    records = []
    for raw in data.to_dict("records"):
        q1_sales, q2_sales, q1_op, q2_op = _current_quarters(raw, year)
        forecast_sales = _number(raw.get(f"consensus_sales_{year}"))
        forecast_op = _number(raw.get(f"consensus_op_{year}"))
        forecast_complete = forecast_sales is not None and forecast_sales > 0 and forecast_op is not None

        annual_sales = forecast_sales * KRW_100M if forecast_complete else None
        annual_op = forecast_op * KRW_100M if forecast_complete else None
        q3_sales = q4_sales = q3_op = q4_op = None
        seasonality_used = False
        source = _source_label(raw, year) if forecast_complete else None
        source_date = (raw.get(f"forecast_date_{year}") or raw.get("consensus_as_of")
                       if forecast_complete else raw.get("as_of"))
        source_url = (raw.get(f"forecast_source_url_{year}") or raw.get("consensus_source_url")
                      if forecast_complete else None)

        if not forecast_complete:
            (p_q1_sales, p_q2_sales, p_q3_sales, p_q4_sales,
             p_q1_op, p_q2_op, p_q3_op, p_q4_op) = _prior_seasonality(raw, year)
            current_h1_sales = (q1_sales + q2_sales
                                if q1_sales is not None and q2_sales is not None else None)
            prior_h1_sales = (p_q1_sales + p_q2_sales
                              if p_q1_sales is not None and p_q2_sales is not None else None)
            valid_sales_pattern = (
                current_h1_sales is not None and current_h1_sales > 0
                and prior_h1_sales is not None and prior_h1_sales > 0
                and p_q3_sales is not None and p_q3_sales > 0
                and p_q4_sales is not None and p_q4_sales > 0
            )
            if valid_sales_pattern and q1_op is not None and q2_op is not None:
                sales_scale = current_h1_sales / prior_h1_sales
                q3_sales, q4_sales = p_q3_sales * sales_scale, p_q4_sales * sales_scale
                current_h1_op = q1_op + q2_op
                prior_h1_op = (p_q1_op + p_q2_op
                               if p_q1_op is not None and p_q2_op is not None else None)
                if (prior_h1_op is not None and prior_h1_op > 0
                        and p_q3_op is not None and p_q4_op is not None):
                    op_scale = current_h1_op / prior_h1_op
                    q3_op, q4_op = p_q3_op * op_scale, p_q4_op * op_scale
                else:
                    h1_margin = current_h1_op / current_h1_sales
                    q3_op, q4_op = q3_sales * h1_margin, q4_sales * h1_margin
                annual_sales = current_h1_sales + q3_sales + q4_sales
                annual_op = current_h1_op + q3_op + q4_op
                seasonality_used = True
                source = f"{year}년 1·2분기 실제 + {year - 1}년 계절성"
                source_url = raw.get("source_url")

        complete = annual_sales is not None and annual_sales > 0 and annual_op is not None
        opm = annual_op / annual_sales * 100 if complete else None
        next_sales = _number(raw.get(f"consensus_sales_{year + 1}"))
        next_op = _number(raw.get(f"consensus_op_{year + 1}"))
        next_sales_growth = ((next_sales / (annual_sales / KRW_100M) - 1) * 100
                             if complete and next_sales is not None and next_sales > 0 else None)
        next_op_growth = ((next_op / (annual_op / KRW_100M) - 1) * 100
                          if complete and annual_op and annual_op > 0 and next_op is not None else None)
        adjustment = ((annual_op / ((q1_op + q2_op) * 2) - 1) * 100
                      if complete and q1_op is not None and q2_op is not None and q1_op + q2_op > 0 else None)

        raw.update({
            "value_fundamental_year": year,
            "value_fundamental_complete": bool(complete),
            "value_fundamental_sales": annual_sales,
            "value_fundamental_op": annual_op,
            "value_fundamental_opm_pct": opm,
            "value_fundamental_average_quarterly_sales": annual_sales / 4 if complete else None,
            "value_fundamental_source": source,
            "value_fundamental_source_date": source_date,
            "value_fundamental_source_url": source_url,
            "value_fundamental_forecast_available": bool(forecast_complete),
            "value_fundamental_seasonality_used": seasonality_used,
            "value_fundamental_prior_year_role": "계절성 비율만" if seasonality_used else "미사용",
            "value_fundamental_q1_sales": q1_sales,
            "value_fundamental_q2_sales": q2_sales,
            "value_fundamental_q3_sales_estimate": q3_sales,
            "value_fundamental_q4_sales_estimate": q4_sales,
            "value_fundamental_q1_op": q1_op,
            "value_fundamental_q2_op": q2_op,
            "value_fundamental_q3_op_estimate": q3_op,
            "value_fundamental_q4_op_estimate": q4_op,
            "value_fundamental_next_sales_growth_pct": next_sales_growth,
            "value_fundamental_next_op_growth_pct": next_op_growth,
            "value_fundamental_quality": _actual_quality(q1_sales, q2_sales, q1_op, q2_op),
            "value_fundamental_adjustment_pct": adjustment,
        })
        records.append(raw)
    return pd.DataFrame(records, index=data.index)
