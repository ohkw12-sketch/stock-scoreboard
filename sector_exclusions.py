"""Shared listing classifications and public-project sector exclusions."""
from __future__ import annotations

from pathlib import Path
import re

import pandas as pd


PUBLIC_SECTOR_PATTERN = re.compile(
    r"건설|건축|토목|주택건설|종합건설|바이오|제약|의약|생명공학|"
    r"biotech|construction|pharma",
    re.IGNORECASE,
)
FINANCE_PATTERN = re.compile(
    r"금융|은행|보험|증권|신탁|카드|캐피탈|financial|bank|insurance",
    re.IGNORECASE,
)
HOLDING_PATTERN = re.compile(
    r"홀딩스|지주|회사 본부|경영 컨설팅|holding",
    re.IGNORECASE,
)


def attach_listing_metadata(frame: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    """Attach the official KRX industry without replacing the display theme."""
    result = frame.copy()
    result["ticker"] = result["ticker"].astype(str).str.zfill(6)
    for column in ("listing_industry", "listing_products"):
        if column not in result:
            result[column] = ""
        result[column] = result[column].fillna("").astype(str)

    # Library callers and unit tests often use synthetic six-digit tickers.
    # Read the live KRX cache only when the application supplied its location.
    if not config or "cache_dir" not in config:
        return result
    cache_dir = Path(config["cache_dir"])
    path = cache_dir / "krx_listing_desc.csv"
    if not path.exists():
        return result
    try:
        listing = pd.read_csv(path, dtype={"Code": str})
    except (OSError, ValueError, UnicodeError):
        return result
    if not {"Code", "Industry"}.issubset(listing.columns):
        return result
    listing["ticker"] = listing["Code"].astype(str).str.zfill(6)
    listing = listing.drop_duplicates("ticker", keep="last").set_index("ticker")
    industry = result["ticker"].map(listing["Industry"].fillna("").astype(str))
    result["listing_industry"] = result["listing_industry"].where(
        result["listing_industry"].str.strip().ne(""), industry.fillna(""),
    )
    if "Products" in listing:
        products = result["ticker"].map(listing["Products"].fillna("").astype(str))
        result["listing_products"] = result["listing_products"].where(
            result["listing_products"].str.strip().ne(""), products.fillna(""),
        )
    return result


def _classification_text(frame: pd.DataFrame) -> pd.Series:
    sector = frame.get("sector", pd.Series("", index=frame.index)).fillna("").astype(str)
    industry = frame.get("listing_industry", pd.Series("", index=frame.index)).fillna("").astype(str)
    return sector + " " + industry


def public_sector_exclusion_mask(frame: pd.DataFrame) -> pd.Series:
    """Return construction and bio/pharma exclusions for public projects."""
    return _classification_text(frame).str.contains(PUBLIC_SECTOR_PATTERN, na=False)


def finance_holding_masks(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Detect finance and holding structures using theme and official industry."""
    classification = _classification_text(frame)
    names = frame.get("name", pd.Series("", index=frame.index)).fillna("").astype(str)
    finance = classification.str.contains(FINANCE_PATTERN, na=False)
    holding = (
        names.str.contains(HOLDING_PATTERN, na=False)
        | classification.str.contains(HOLDING_PATTERN, na=False)
    )
    return finance, holding
