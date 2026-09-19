"""Shared filters derived only from four confirmed reported quarters."""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


QUARTER_ORDER = (3, 4, 1, 2)
SALES_COLUMNS = tuple(f"normalized_sales_q{quarter}" for quarter in QUARTER_ORDER)
OP_COLUMNS = tuple(f"normalized_op_q{quarter}" for quarter in QUARTER_ORDER)


def add_four_quarter_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    """Add complete-history, average-sales and mean-quarterly-margin columns.

    The margin is the simple mean of the four individual quarterly margins.
    It deliberately differs from TTM operating profit divided by TTM sales.
    """
    data = frame.copy()
    for column in SALES_COLUMNS + OP_COLUMNS:
        if column not in data:
            data[column] = np.nan
        data[column] = pd.to_numeric(data[column], errors="coerce")
    sales = data[list(SALES_COLUMNS)]
    operating_profit = data[list(OP_COLUMNS)].copy()
    operating_profit.columns = sales.columns
    complete = sales.notna().all(axis=1) & operating_profit.notna().all(axis=1) & sales.gt(0).all(axis=1)
    if "normalized_quarter_count" in data:
        reported_count = pd.to_numeric(data["normalized_quarter_count"], errors="coerce")
        complete &= reported_count.eq(4)
    data["reported_four_quarter_complete"] = complete
    data["average_quarterly_sales"] = sales.mean(axis=1).where(complete)
    quarterly_margins = operating_profit.div(sales).mul(100)
    data["average_quarterly_op_margin_pct"] = quarterly_margins.mean(axis=1).where(complete)
    return data


def four_quarter_metrics(row: Mapping) -> dict:
    """Return the same four-quarter metrics for a dict-like record."""
    data = add_four_quarter_metrics(pd.DataFrame([dict(row)]))
    item = data.iloc[0]
    complete = bool(item["reported_four_quarter_complete"])
    return {
        "complete": complete,
        "averageQuarterlySales": (
            float(item["average_quarterly_sales"])
            if complete and pd.notna(item["average_quarterly_sales"]) else None
        ),
        "averageQuarterlyOperatingMarginPct": (
            float(item["average_quarterly_op_margin_pct"])
            if complete and pd.notna(item["average_quarterly_op_margin_pct"]) else None
        ),
    }
