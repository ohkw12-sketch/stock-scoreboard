"""Independent, evidence-first scenario research. No legacy investment scores.

All prices are cut at the verified snapshot date. This is a current-snapshot
research tool, not a point-in-time historical backtest. Source dates and unknowns
survive all calculations; a missing forecast never becomes a zero estimate.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import json
import math
import re
import statistics

import numpy as np
import pandas as pd

VERSION = "scenario-research-1.6"
USABLE = {"유효 컨센서스", "참고 컨센서스", "개별 추정치", "외부 집계 컨센서스"}
RULES = {
    "minimumAnnualSales": 200_000_000_000, "minimumTurnover20": 1_000_000_000,
    "volumeMinimum": 1.5, "volumeStrong": 2.0, "maxSectors": 5, "maxPerSector": 3,
    "breakoutExtensionPct": 8, "supportDistancePct": 3,
    "upForwardMinOP": 10_000_000_000, "upForwardMinGrowthPct": 25,
    "upForwardMinDelta": 5_000_000_000, "upForwardMinSalesGrowthPct": 5,
    "upActualMinGrowthPct": 25, "upActualMinDelta": 3_000_000_000,
    "upActualMinSalesGrowthPct": 10, "upActualMinMarginPct": 10,
    "upFundamentalDisplay": 20,
    "description": "상승은 예상·확정 이익 성장의 동시 확인부터 선별하고 가격은 진입 상태에만 사용. 전망이 있으면 약한 전망을 단일 분기 호조로 대체하지 않음. 지지·저항은 보유한 전 기간의 거래대금 집중 가격대와 돌파 이력을 요구. 종합점수·수주 건수 가점 없음.",
}
SCENARIOS = {
    "up": {"title": "상승 시나리오", "subtitle": "예상·확정 이익 동반 성장 후 가격 진입 검토", "horizon": "기업 실적 6~12개월 · 진입 위치 20~60거래일",
           "action": "추세·과거 거래 가격대 돌파·거래량이 함께 확인되는지 검토", "readyLabel": "조건 충족"},
    "range": {"title": "박스권 시나리오", "subtitle": "반복 확인된 박스의 하단 접근", "horizon": "기업 실적 6~12개월 · 과거 거래 가격대 재확인",
              "action": "박스 하단 지지와 반등 확인 후 검토", "readyLabel": "조건 충족"},
    "down": {"title": "하락 시나리오", "subtitle": "보유 위험 점검과 상대적 방어력", "horizon": "보유 위험 점검 · 가격 흐름 20~60거래일",
             "action": "보유 위험을 먼저 확인하고 방어 후보를 관찰", "readyLabel": "방어 관찰"},
}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def pct(new, old):
    a, b = number(new), number(old)
    return (a / b - 1) * 100 if a is not None and b is not None and b > 0 else None


def ratio(a, b):
    a, b = number(a), number(b)
    return a / b if a is not None and b is not None and b > 0 else None


def dated(value, cutoff):
    try:
        parsed = date.fromisoformat(str(value)[:10])
        return parsed.isoformat() if parsed <= date.fromisoformat(cutoff) else None
    except (ValueError, TypeError):
        return None


def safe_url(value):
    return value if isinstance(value, str) and value.startswith("https://") else None


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (float, np.floating)):
        return round(float(value), 4) if math.isfinite(value) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    return value


def forecast_index(consensus, guidance, cutoff):
    """Choose newest *per metric*, keeping the selected metric's own date/URL.

    The existing collector's unweighted broker median is preserved. An older
    sales estimate cannot borrow the publication date of a later OP-only report.
    Future-dated aggregates are rejected, not reconstructed from mixed vintages.
    """
    points = defaultdict(dict)
    for row in consensus:
        period = str(row.get("period", ""))
        if not re.fullmatch(r"20\d{2}(FY|Q[1-4])", period) or row.get("status") not in USABLE:
            continue
        if not dated(row.get("latestReportDate"), cutoff):
            continue
        key = (str(row.get("ticker", "")).zfill(6), period)
        for metric, aggregate, field in (("sales", "salesMedianKrw100m", "salesKrw100m"),
                                         ("op", "operatingProfitMedianKrw100m", "operatingProfitKrw100m")):
            value = number(row.get(aggregate))
            if value is None:
                continue
            if any(number(x.get(field)) is not None and not dated(x.get("reportDate"), cutoff)
                   for x in row.get("estimates", [])):
                continue  # aggregate could already contain a future/undated value
            estimates = [x for x in row.get("estimates", []) if number(x.get(field)) is not None
                         and dated(x.get("reportDate"), cutoff) and safe_url(x.get("sourceUrl"))]
            if not estimates:
                continue  # no attributable source for this metric
            estimates.sort(key=lambda x: x["reportDate"], reverse=True)
            brokers = len({x.get("broker") for x in estimates})
            source_status = (row["status"] if row["status"] == "외부 집계 컨센서스" else
                             "개별 추정치" if brokers == 1 else "참고 컨센서스" if brokers == 2 else "유효 컨센서스")
            selected = {"value": value * 1e8, "date": estimates[0]["reportDate"],
                        "url": safe_url(estimates[0]["sourceUrl"]), "kind": "consensus",
                        "source": source_status, "brokerCount": brokers,
                        "sources": [{"date": x["reportDate"], "url": x["sourceUrl"], "name": x.get("broker") or row["status"],
                                     "value": number(x[field]) * 1e8} for x in estimates]}
            old = points[key].get(metric)
            if old is None or selected["date"] > old["date"]:
                points[key][metric] = selected
    for row in guidance:
        period = str(row.get("period", ""))
        if re.fullmatch(r"20\d{2}", period):
            period += "FY"
        publication = dated(row.get("published_at"), cutoff)
        if (not publication or not re.fullmatch(r"20\d{2}(FY|Q[1-4])", period)
                or row.get("basis") != "consolidated" or row.get("active") is not True
                or row.get("fetch_status") != "정상" or not safe_url(row.get("source_url"))):
            continue
        key = (str(row.get("ticker", "")).zfill(6), period)
        for metric, field in (("sales", "sales"), ("op", "operating_profit")):
            value = number((row.get(field) or {}).get("mid"))
            old = points[key].get(metric)
            if value is not None and (not old or publication >= old["date"]):
                points[key][metric] = {"value": value, "date": publication, "url": row["source_url"],
                    "kind": "guidance", "source": "회사 공식 가이던스", "brokerCount": None,
                    "sources": [{"date": publication, "url": row["source_url"], "name": "회사 공식 가이던스", "value": value}]}
    return points


def revision_index(observations, cutoff, year):
    """Only compare the same broker, metric and period; never cross-year levels."""
    groups = defaultdict(lambda: defaultdict(list))
    for row in observations:
        d = dated(row.get("report_date"), cutoff)
        value = number(row.get("operating_profit_krw_100m"))
        if (d and row.get("period") in (f"{year}FY", f"{year+1}FY") and value is not None
                and (number(row.get("confidence")) or 0) >= .8 and safe_url(row.get("source_url"))
                and row.get("broker") and not re.search(r"fnguide|공개.?집계|외부.?집계", str(row.get("broker")) + str(row.get("extraction_method")), re.I)):
            groups[(str(row["ticker"]).zfill(6), row["period"], row["broker"])][d].append(row)
    result = defaultdict(list)
    for (ticker, period, broker), days in groups.items():
        valid = []
        for day, rows in days.items():
            values = [float(x["operating_profit_krw_100m"]) for x in rows]
            # Conflicting same-day extractions are not silently averaged.
            if max(values) - min(values) > max(1, max(map(abs, values)) * .02):
                continue
            row = max(rows, key=lambda x: x["confidence"])
            valid.append((day, float(row["operating_profit_krw_100m"]), row["source_url"]))
        valid.sort()
        if len(valid) >= 2:
            a, b = valid[-2:]
            result[ticker].append({"period": period, "broker": broker, "beforeDate": a[0], "date": b[0],
                "before": a[1] * 1e8, "after": b[1] * 1e8, "changePct": pct(b[1], a[1]),
                "delta": (b[1] - a[1]) * 1e8, "url": b[2], "beforeUrl": a[2]})
    return result


def actuals(row, cutoff):
    if row.get("normalization_status") != "정상" or row.get("quarter_value_verified") not in (True, "True", "true"):
        return []
    receipt = str(row.get("receipt") or "")
    if not re.fullmatch(r"\d{14}", receipt) or not dated(f"{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}", cutoff):
        return []
    periods = str(row.get("normalization_periods") or "").split(",")
    try:
        sources = json.loads(row.get("normalization_sources") or "[]")
    except (TypeError, ValueError):
        sources = []
    result = []
    for period in sorted(set(periods)):
        if not re.fullmatch(r"20\d{2}Q[1-4]", period):
            continue
        q = period[-1]
        sales, op = number(row.get(f"normalized_sales_q{q}")), number(row.get(f"normalized_op_q{q}"))
        receipts = sorted({str(x.get("receipt", "")) for x in sources if x.get("quarter") == period
                           and re.fullmatch(r"\d{14}", str(x.get("receipt", "")))
                           and dated(f"{str(x['receipt'])[:4]}-{str(x['receipt'])[4:6]}-{str(x['receipt'])[6:8]}", cutoff)})
        if sales is not None and sales > 0 and op is not None and receipts:
            selected_receipt = receipts[-1]
            result.append({"period": period, "sales": sales, "op": op, "marginPct": op / sales * 100,
                "date": f"{selected_receipt[:4]}-{selected_receipt[4:6]}-{selected_receipt[6:8]}", "source": "OpenDART 확정 실적",
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={selected_receipt}",
                "sources": [f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r}" for r in receipts]})
    return result


def traded_zones(dates, highs, lows, volumes, values, *, exclude_recent=3):
    """Find meaningful traded-value nodes in all available prior daily bars.

    OHLCV cannot reveal actual execution by price. Daily value is spread across
    the day's high-low bins, so this is an explicit proxy, not an order book.
    """
    end = len(highs) - exclude_recent  # a breakout cannot manufacture its own supply zone
    if end < 25:
        return []
    valid = [(number(lows.iloc[i]), number(highs.iloc[i]), number(values.iloc[i]))
             for i in range(end) if (number(volumes.iloc[i]) or 0) > 0]
    valid = [(lo, hi, v) for lo, hi, v in valid if lo and hi and v and 0 < lo <= hi]
    if len(valid) < 20:
        return []
    floor = min(x[0] for x in valid)
    ceiling = max(x[1] for x in valid)
    step = max(math.log(1.02), math.log(ceiling / floor) / 160) if ceiling > floor else math.log(1.02)
    size = max(1, int(math.log(ceiling / floor) / step) + 2)
    profile = np.zeros(size)
    bar_bins = []
    for i in range(end):
        lo, hi, amount = number(lows.iloc[i]), number(highs.iloc[i]), number(values.iloc[i])
        if not lo or not hi or not amount or lo <= 0 or hi < lo or (number(volumes.iloc[i]) or 0) <= 0:
            bar_bins.append(None)
            continue
        left = max(0, min(size-1, int(math.log(lo / floor) / step)))
        right = max(left, min(size-1, int(math.log(hi / floor) / step)))
        if right - left > 20:  # bad daily price span must not smear across the profile
            bar_bins.append(None)
            continue
        profile[left:right+1] += amount / (right-left+1)
        bar_bins.append((left, right, amount))
    total = float(profile.sum())
    if total <= 0:
        return []
    smooth = np.convolve(profile, [.25, .5, .25], mode="same") if size >= 3 else profile
    largest = float(smooth.max())
    zones = []
    for peak in np.argsort(smooth)[::-1]:
        strength = float(smooth[peak])
        if strength < max(largest * .30, total * .005):
            break
        left = right = int(peak)
        while left > 0 and smooth[left-1] >= strength * .55:
            left -= 1
        while right < size-1 and smooth[right+1] >= strength * .55:
            right += 1
        zone_low, zone_high = floor * math.exp(left * step), floor * math.exp((right+1) * step)
        if pct(zone_high, zone_low) > 40 or any(not (zone_high < z["low"] or zone_low > z["high"]) for z in zones):
            continue
        contributions = []
        for i, bins in enumerate(bar_bins):
            if bins is None:
                continue
            a, b, amount = bins
            overlap = max(0, min(b, right) - max(a, left) + 1)
            if overlap:
                contributions.append((i, amount * overlap / (b-a+1)))
        traded = sum(amount for _, amount in contributions)
        active = len(contributions)
        spike = max((ratio(volumes.iloc[i], volumes.iloc[i-20:i].mean()) or 0
                     for i, _ in contributions if i >= 20), default=0)
        share = traded / total * 100
        if not ((active >= 10 and share >= 5) or (spike >= 2.5 and share >= 1.5)):
            continue
        running, formed = 0, contributions[-1][0]
        for i, amount in contributions:
            running += amount
            if running >= traded * .6:
                formed = i
                break
        zones.append({"low": zone_low, "high": zone_high, "kind": "과거 거래대금 집중 가격대(일봉 추정)",
                      "days": active, "lastDate": dates.iloc[contributions[-1][0]].strftime("%Y-%m-%d"),
                      "formedDate": dates.iloc[formed].strftime("%Y-%m-%d"), "formedIndex": formed,
                      "tradedValue": traded, "sharePct": share, "largestDailyVolumeRatio": spike})
    return sorted(zones, key=lambda z: z["high"])


def confirmed_box(dates, highs, lows, closes, support, break_index):
    """Require repeated lower/upper turns after a volume-backed support flip.

    The lower boundary is an established traded-value zone. The upper boundary
    is a repeated price rejection, not an arbitrary rolling-window high.
    """
    if support is None or len(closes) - break_index < 18:
        return None
    lower = [i for i in range(break_index + 1, len(closes))
             if number(lows.iloc[i]) is not None and number(closes.iloc[i]) is not None
             and lows.iloc[i] <= support * 1.025 and support * .985 <= closes.iloc[i] <= support * 1.04]
    if len(lower) < 2 or lower[-1] < len(closes) - 10:
        return None
    lower_starts = [lower[0]] + [i for previous, i in zip(lower, lower[1:]) if i - previous > 2]
    for start in lower_starts[-8:]:
        if len(closes) - start < 12:
            continue
        recent_highs = pd.to_numeric(highs.iloc[start:], errors="coerce").dropna()
        if len(recent_highs) < 12:
            continue
        ceiling = float(recent_highs.quantile(.9))
        width = pct(ceiling, support)
        if width is None or not 6 <= width <= 35:
            continue
        if pd.to_numeric(closes.iloc[start:], errors="coerce").max() > ceiling * 1.025:
            continue
        events = []
        for i in range(start, len(closes)):
            if i in lower:
                events.append((i, "low"))
            elif number(highs.iloc[i]) is not None and highs.iloc[i] >= ceiling * .98:
                events.append((i, "high"))
        turns = []
        for event in events:
            if turns and event[1] == turns[-1][1]:
                turns[-1] = event
            else:
                turns.append(event)
        if len(turns) < 4 or turns[-1][1] != "low" or sum(k == "high" for _, k in turns) < 2:
            continue
        return {"low": support, "high": ceiling, "widthPct": width,
                "positionPct": (closes.iloc[-1] - support) / (ceiling - support) * 100,
                "lowerTests": sum(k == "low" for _, k in turns),
                "upperTests": sum(k == "high" for _, k in turns),
                "firstTestDay": dates.iloc[turns[0][0]].strftime("%Y-%m-%d"),
                "lastLowerTestDay": dates.iloc[turns[-1][0]].strftime("%Y-%m-%d")}
    return None


def price_features(history, cutoff):
    h = history.sort_values("date").drop_duplicates("date", keep="last").copy()
    h = h[h["date"] <= pd.Timestamp(cutoff)]
    if h.empty:
        return None
    last = h.iloc[-1]
    c = pd.to_numeric(h["close"], errors="coerce")
    adjusted = pd.to_numeric(h.get("adjusted_close", c), errors="coerce").where(lambda x: x > 0, c)
    factor = adjusted / c
    comparable = adjusted / factor.iloc[-1]
    high = pd.to_numeric(h["high"], errors="coerce") * factor / factor.iloc[-1]
    low = pd.to_numeric(h["low"], errors="coerce") * factor / factor.iloc[-1]
    close, prev = number(c.iloc[-1]), number(comparable.iloc[-2]) if len(h) > 1 else None
    if close is None or close <= 0:
        return None
    volume = pd.to_numeric(h["volume"], errors="coerce")
    value = pd.to_numeric(h["value"], errors="coerce")
    local_highs = high[(high >= high.shift(1)) & (high >= high.shift(2)) & (high > high.shift(-1)) & (high > high.shift(-2))]
    local_lows = low[(low <= low.shift(1)) & (low <= low.shift(2)) & (low < low.shift(-1)) & (low < low.shift(-2))]
    window = h.index[-21:-1]
    peaks, troughs = local_highs[local_highs.index.isin(window)], local_lows[local_lows.index.isin(window)]
    swing_resistance = number(peaks.max() if not peaks.empty else high.iloc[-21:-1].max()) if len(h) >= 21 else None
    swing_support = number(troughs.min() if not troughs.empty else low.iloc[-21:-1].min()) if len(h) >= 21 else None
    amplitude = number(last["high"] - last["low"])
    position = (close - last["low"]) / amplitude if amplitude and amplitude > 0 else .5
    wick = (last["high"] - max(close, last["open"])) / amplitude if amplitude and amplitude > 0 else 0
    change = pct(close, prev)
    surge = pct(last["high"], prev)
    blowoff = (surge or 0) >= 10 and (position < .5 or wick >= .45)
    extended = (pct(close, number(comparable.tail(20).mean())) or 0) > 20 or (change or 0) >= 15
    factor_jump = (factor / factor.shift(1)).sub(1).abs() > .20
    basis_change = bool(factor_jump.tail(20).any())
    zones = traded_zones(h["date"], high, low, volume, value)
    crossed = []
    # Reconstruct each eligible crossing from the profile available *before*
    # that day's trade. This prevents a later rise from rewriting old supply.
    for j in range(25, len(h)):
        prior = number(volume.iloc[j-20:j].mean())
        day_range = number(high.iloc[j] - low.iloc[j])
        position_at_cross = (comparable.iloc[j] - low.iloc[j]) / day_range if day_range and day_range > 0 else 0
        if (not prior or (number(volume.iloc[j]) or 0) < prior * 1.5 or position_at_cross < .7
                or comparable.iloc[j] <= comparable.iloc[j-1]
                or abs(pct(comparable.iloc[j], close) or 0) > 12
                or factor_jump.iloc[max(1,j-20):j+1].any()):
            continue
        historic = traded_zones(h["date"].iloc[:j], high.iloc[:j], low.iloc[:j],
                               volume.iloc[:j], value.iloc[:j], exclude_recent=0)
        for z in historic:
            level = z["high"]
            if comparable.iloc[j-1] <= level * 1.005 < comparable.iloc[j]:
                crossed.append((z, number(volume.iloc[j] / prior), h["date"].iloc[j].strftime("%Y-%m-%d"), j))
    recent_crosses = [x for x in crossed if x[3] >= len(h)-3 and close > x[0]["high"] * 1.005]
    breakout_zone = max(recent_crosses, key=lambda x: (x[0]["high"], x[1])) if recent_crosses else None
    flipped = [x for x in crossed if x[3] < len(h)-2 and close >= x[0]["high"]
               and comparable.iloc[x[3]:].min() >= x[0]["low"] * .99]
    flipped_zone = max(flipped, key=lambda x: (x[0]["high"], x[3])) if flipped else None
    overhead = [z for z in zones if z["high"] > close]
    overhead_zone = min(overhead, key=lambda z: z["high"]) if overhead else None
    resistance_zone = breakout_zone[0] if breakout_zone else overhead_zone
    support_zone = flipped_zone[0] if flipped_zone else None
    boxes = [box for z, _, _, j in flipped
             if (box := confirmed_box(h["date"], high, low, comparable, z["high"], j)) and box["low"] <= close]
    box = max(boxes, key=lambda b: b["low"]) if boxes else None
    resistance = resistance_zone["high"] if resistance_zone else None
    support = support_zone["high"] if support_zone else None
    range_width = pct(support_zone["high"], support_zone["low"]) if support_zone else None
    breakout = bool(breakout_zone)
    return {"date": last["date"].strftime("%Y-%m-%d"), "close": close,
        "fetchedAt": str(last.get("fetched_at") or ""), "source": str(last.get("price_source") or "미확인"),
        "turnoverBasis": str(last.get("value_basis") or "미확인"), "bars": len(h),
        "r5": pct(comparable.iloc[-1], comparable.iloc[-6]) if len(h) >= 6 else None,
        "r20": pct(comparable.iloc[-1], comparable.iloc[-21]) if len(h) >= 21 else None,
        "r60": pct(comparable.iloc[-1], comparable.iloc[-61]) if len(h) >= 61 else None,
        "ma20": number(comparable.tail(20).mean()) if len(h) >= 20 else None,
        "ma60": number(comparable.tail(60).mean()) if len(h) >= 60 else None,
        "turnover20": number(value.tail(20).mean()) if len(h) >= 20 else None,
        "volume3": number(volume.tail(3).mean()) if len(h) >= 17 else None,
        "volumePrior14": number(volume.iloc[-17:-3].mean()) if len(h) >= 17 else None,
        "volumeRatio": ratio(volume.tail(3).mean(), volume.iloc[-17:-3].mean()) if len(h) >= 17 and not basis_change else None,
        "turnoverRatio": ratio(value.tail(3).mean(), value.iloc[-17:-3].mean()) if len(h) >= 17 else None,
        "resistance": resistance, "support": support,
        "resistanceBasis": resistance_zone["kind"] if resistance_zone else "거래 집중 가격대 미확인",
        "supportBasis": support_zone["kind"] if support_zone else "거래 집중 가격대 미확인",
        "resistanceZone": resistance_zone, "supportZone": support_zone,
        "box": box,
        "swingResistance": swing_resistance, "swingSupport": swing_support,
        "swingResistanceBasis": "20일 확인 스윙고점" if not peaks.empty else "20일 고점 대체",
        "breakout": breakout, "breakoutDayVolumeRatio": breakout_zone[1] if breakout_zone else None,
        "breakoutDay": breakout_zone[2] if breakout_zone else None,
        "supportFlipDay": flipped_zone[2] if flipped_zone else None,
        "distanceResistancePct": pct(close, resistance),
        "distanceSupportPct": pct(close, support), "rangeWidthPct": range_width,
        "drawdown60": pct(close, number(high.tail(60).max())), "closePosition": position, "upperWick": wick,
        "change1": change, "blowoff": bool(blowoff), "extended": bool(extended), "basisChange": basis_change,
        "marketCap": number(last.get("market_cap")), "trading": bool(number(last.get("volume")) and last["volume"] > 0),
        "chart": [{"date": x.strftime("%Y-%m-%d"), "close": float(y)} for x, y in zip(h["date"].tail(60), comparable.tail(60)) if pd.notna(y)],
        "name": str(last.get("name") or last["ticker"]), "sector": str(last.get("sector") or "미분류"), "market": str(last.get("market") or "미분류")}


def regime(rows):
    eligible = [p for p in rows if p.get("ma60") and p.get("r20") is not None and p.get("trading")]
    if not eligible:
        return {"label": "자료 부족", "count": 0, "above20": None, "above60": None, "return20Median": None, "return5Median": None}
    above20 = sum(p["close"] > p["ma20"] for p in eligible) / len(eligible) * 100
    above60 = sum(p["close"] > p["ma60"] for p in eligible) / len(eligible) * 100
    r20 = statistics.median(p["r20"] for p in eligible)
    r5 = statistics.median(p["r5"] for p in eligible if p["r5"] is not None)
    label = "혼조·전환"
    if len(eligible) < 3:
        label = "표본 부족"
    elif above20 >= 60 and above60 >= 55 and r20 > 0:
        label = "상승 우세"
    elif above20 <= 40 and above60 <= 45 and r20 < 0:
        label = "하락 우세"
    elif 40 <= above20 <= 60 and abs(r20) <= 5:
        label = "박스권 가능"
    return {"label": label, "count": len(eligible), "above20": above20, "above60": above60,
            "return20Median": r20, "return5Median": r5, "disagreement": bool((r5 > 0) != (r20 > 0))}


def evidence_index(events, cutoff):
    result, seen = defaultdict(list), set()
    for e in sorted(events, key=lambda x: str(x.get("publishedAt") or ""), reverse=True):
        d = dated(e.get("publishedAt"), cutoff)
        if (e.get("status") != "유효" or not d or not safe_url(e.get("url"))
                or (e.get("activeUntil") and str(e["activeUntil"]) < cutoff)
                or "컨센서스" in str(e.get("kind"))):
            continue
        # Same economic event appearing as news/disclosure receives one card.
        key = (e.get("ticker"), e.get("kind"), e.get("subject") or e.get("eventId") or e.get("receipt"),
               e.get("customer"), e.get("startAt") or e.get("firstPublished"))
        if key in seen:
            continue
        seen.add(key)
        result[str(e.get("ticker")).zfill(6)].append({"kind": e.get("kind"), "title": e.get("subject") or e.get("title"),
            "date": d, "source": e.get("source"), "url": e["url"], "fact": e.get("excerpt") or e.get("subject"),
            "amount": number(e.get("amount")), "salesRatioPct": number(e.get("revenueRatio")), "end": e.get("activeUntil"),
            "polarity": e.get("polarity"), "interpretation": "이익 기여액·인식 시점은 별도 확인. 사건 건수는 선정 가점으로 사용하지 않음."})
    return result


def stock_fundamentals(raw, points, revisions, cutoff, ticker):
    year = int(cutoff[:4])
    actual = actuals(raw, cutoff)
    forecasts = [{"period": period, **value} for (code, period), value in points.items()
                 if code == ticker and int(period[:4]) >= year]
    forecasts.sort(key=lambda x: x["period"])
    current, following = points.get((ticker, f"{year}FY"), {}), points.get((ticker, f"{year+1}FY"), {})
    s0, o0 = (current.get(k, {}).get("value") for k in ("sales", "op"))
    s1, o1 = (following.get(k, {}).get("value") for k in ("sales", "op"))
    margin0 = ratio(o0, s0)
    margin1 = ratio(o1, s1)
    q2 = next((x for x in actual if x["period"] == f"{year}Q2"), None)
    q1 = next((x for x in actual if x["period"] == f"{year}Q1"), None)
    latest = actual[-1] if actual else None
    quarter_as_of = str(raw.get("quarter_as_of") or "")
    try:
        quarter_day = date.fromisoformat(quarter_as_of[:10])
        matched_period = latest is not None and latest["period"] == f"{quarter_day.year}Q{(quarter_day.month-1)//3+1}"
    except ValueError:
        matched_period = False
    previous_op = number(raw.get("op_quarter_previous")) if matched_period else None
    previous_sales = number(raw.get("sales_quarter_previous")) if matched_period else None
    current_h1 = q1["op"] + q2["op"] if q1 and q2 else None
    implied_h2 = o0 - current_h1 if o0 is not None and current_h1 is not None else None
    q3_op = points.get((ticker, f"{year}Q3"), {}).get("op", {}).get("value")
    q4_op = points.get((ticker, f"{year}Q4"), {}).get("op", {}).get("value")
    quarter_h2 = q3_op + q4_op if q3_op is not None and q4_op is not None else None
    h2_bridge = bool(current_h1 is not None and current_h1 > 0 and implied_h2 is not None
        and (implied_h2 <= current_h1 * 2 or (quarter_h2 is not None and implied_h2 > 0
            and abs(quarter_h2 - implied_h2) <= implied_h2 * .2)))
    positive_forecast = bool(o0 is not None and o1 is not None and o1 > max(o0, 0) and s1 is not None and s0 is not None and s1 >= s0)
    actual_improving = bool(latest and previous_op is not None and previous_sales and latest["op"] > max(previous_op, 0) and latest["sales"] > previous_sales)
    latest_weak = bool(latest and (latest["op"] <= 0 or (pct(latest["op"], previous_op) is not None and pct(latest["op"], previous_op) < -20)))
    forecast_weak = bool(o0 is not None and o1 is not None and o1 < o0)
    op_forecast_pair = o0 is not None and o1 is not None
    forward_pair = op_forecast_pair and s0 is not None and s1 is not None
    latest_op_delta = latest["op"] - previous_op if latest and previous_op is not None else None
    latest_op_growth = pct(latest["op"], previous_op) if latest else None
    latest_sales_growth = pct(latest["sales"], previous_sales) if latest else None
    latest_margin = ratio(latest["op"], latest["sales"]) if latest else None
    strong_forward = bool(forward_pair and o0 >= RULES["upForwardMinOP"]
        and o1 - o0 >= RULES["upForwardMinDelta"]
        and (pct(o1, o0) or 0) >= RULES["upForwardMinGrowthPct"]
        and (pct(s1, s0) or 0) >= RULES["upForwardMinSalesGrowthPct"] and not latest_weak)
    strong_actual = bool(latest_op_delta is not None and latest_op_delta >= RULES["upActualMinDelta"]
        and (latest_op_growth or 0) >= RULES["upActualMinGrowthPct"]
        and (latest_sales_growth or 0) >= RULES["upActualMinSalesGrowthPct"]
        and (latest_margin or 0) * 100 >= RULES["upActualMinMarginPct"] and not latest_weak)
    growth_qualified = strong_forward if op_forecast_pair else strong_actual and not forecast_weak
    growth_path = ("전망·확정 실적 성장 확인" if strong_forward and strong_actual
                   else "전망 성장 확인" if strong_forward
                   else "확정 실적 성장 확인 · 내년 전망 미확인" if growth_qualified
                   else "전망 성장 강도·매출 근거 부족" if op_forecast_pair else "확정 실적 성장 강도 부족·전망 미확인")
    track = ("실적·전망 동반 개선" if positive_forecast and actual_improving else "전망 개선 확인" if positive_forecast
             else "실적 회복 확인" if actual_improving and not forecast_weak else "개선 근거 대기")
    warnings = []
    if not current:
        warnings.append("올해 외부 전망 미확인 · 실적자료로 별도 관찰")
    elif s0 is None or o0 is None:
        warnings.append("올해 매출·영업이익 전망 중 일부 미확인")
    if not following:
        warnings.append("내년 외부 전망 미확인")
    if latest_weak:
        warnings.append("최근 분기 적자 또는 전년 대비 영업이익 20% 이상 감소")
    if forecast_weak:
        warnings.append("내년 영업이익 감소 전망")
    if margin0 is not None and margin0 <= .02:
        warnings.append("낮은 이익 기저 · 성장률보다 이익 증가액·마진 개선 확인")
    if not revisions:
        warnings.append("동일 증권사·동일 기간의 전망 변경 비교자료 부족")
    stale = [p[k]["date"] for p in forecasts for k in ("sales", "op") if k in p
             and (date.fromisoformat(cutoff) - date.fromisoformat(p[k]["date"])).days > 60]
    if stale:
        warnings.append("60일 초과 전망 포함 · 최신 보유값을 유지하며 날짜 표시")
    return {"actuals": actual, "forecasts": forecasts, "revisions": sorted(revisions, key=lambda x: x["date"], reverse=True),
        "track": track, "positiveForecast": positive_forecast, "actualImproving": actual_improving,
        "forwardPair": forward_pair, "opForecastPair": op_forecast_pair,
        "strongForward": strong_forward, "strongActual": strong_actual,
        "growthQualified": growth_qualified, "growthPath": growth_path,
        "h2BridgeVerified": h2_bridge, "quarterH2OP": quarter_h2,
        "latestWeak": latest_weak, "forecastWeak": forecast_weak, "annualSales": s0, "annualOP": o0, "nextOP": o1,
        "annualMarginPct": margin0 * 100 if margin0 is not None else None,
        "nextMarginPct": margin1 * 100 if margin1 is not None else None,
        "nextOPGrowthPct": pct(o1, o0), "nextOPDelta": o1 - o0 if o1 is not None and o0 is not None else None,
        "nextSalesGrowthPct": pct(s1, s0), "latestActualPeriod": latest["period"] if latest else None,
        "latestOPGrowthPct": latest_op_growth, "latestOPDelta": latest_op_delta,
        "latestSalesGrowthPct": latest_sales_growth,
        "latestMarginPct": latest_margin * 100 if latest_margin is not None else None,
        "h2ImpliedOP": implied_h2, "h2RequiredVsH1Pct": pct(implied_h2, current_h1),
        "warnings": warnings, "forecastDates": sorted({p[k]["date"] for p in forecasts for k in ("sales", "op") if k in p}),
        "reportedSalesTTM": sum(x["sales"] for x in actual) if len(actual) == 4 else None,
        "durability": "복수 분기 전망 확인" if sum("op" in p for p in forecasts if "Q" in p["period"]) >= 2 else "연간 전망·단기 실적만으로 지속성 확정 불가"}


def assess(stock):
    p, f = stock["price"], stock["fundamentals"]
    cutoff = stock["asOf"]
    blockers = []
    if re.search(r"건설|건축|바이오|제약|의약|biotech|construction|pharma", stock["sector"], re.I):
        blockers.append("기존 제외 업종")
    if p["date"] != cutoff:
        blockers.append("기준일 가격 누락")
    if p["bars"] < 61:
        blockers.append("60거래일 비교 이력 부족")
    if not p["trading"]:
        blockers.append("기준일 거래 없음")
    if p["basisChange"]:
        blockers.append("기업행사·가격기준 변동 확인 필요")
    if p["turnover20"] is None or p["turnover20"] < RULES["minimumTurnover20"]:
        blockers.append("20일 평균 거래대금 10억원 미달·미확인")
    annual = f["annualSales"] if f["annualSales"] is not None else f["reportedSalesTTM"]
    if annual is None or annual < RULES["minimumAnnualSales"]:
        blockers.append("연간 매출 2,000억원 미달·미확인")
    if not (f["positiveForecast"] or (f["actualImproving"] and not f["forecastWeak"])):
        blockers.append("실적·전망 개선 근거 대기")
    if any(x.get("polarity") == "negative" for x in stock["evidence"]):
        blockers.append("유효한 부정 공시 영향 확인 필요")
    if p["blowoff"]:
        blockers.append("당일 급등 후 종가 밀림")
    extension = p["distanceResistancePct"]
    heat = p["extended"] or (extension is not None and extension > RULES["breakoutExtensionPct"])
    heat_exception = bool(not blockers and heat and not p["blowoff"] and f["positiveForecast"] and f["actualImproving"]
                          and (p["volumeRatio"] or 0) >= 2 and p["breakout"] and p["closePosition"] >= .8)
    if heat:
        blockers.append("과열 관찰" if heat_exception else "추격 이격 과다")
    base = not blockers
    check = lambda name, ok, reason: {"name": name, "met": bool(ok), "waiting": reason}
    financial_sector = bool(re.search(r"금융|은행|증권|보험", stock["sector"]))
    f["upCore"] = bool(f["strongForward"] and f["strongActual"] and f["h2BridgeVerified"]
                        and not financial_sector)
    core_waiting = ("내년 매출·OP 전망의 충분한 증가 확인" if not f["strongForward"] else
                    "최근 확정 분기 매출·OP 증가와 이익률 확인" if not f["strongActual"] else
                    "올해 하반기 예상 OP가 상반기의 2배를 넘는 경우 분기별 전망 근거 확인" if not f["h2BridgeVerified"] else
                    "금융업은 동일 영업이익 기준의 성장 순위에서 제외" if financial_sector else "")
    f["upCoreReason"] = core_waiting or "예상·확정 실적 동반 확인"
    up = [check("예상·확정 이익 동반 성장", f["upCore"], core_waiting),
          check("중기 추세", p["ma60"] and p["ma20"] and p["close"] > p["ma20"] > p["ma60"], "종가 > 20일선 > 60일선 확인"),
          check("시장 대비 강세", (p.get("rs20") or 0) > 0, "20일 시장 대비 수익률 양수 확인"),
          check("큰 매물대 종가 돌파", p["breakout"], "과거 거래대금 집중 가격대를 거래량 동반 고가권 종가로 돌파 확인"),
          check("거래량 확인", (p["volumeRatio"] or 0) >= 1.5, "최근 3일 / 직전 14일 거래량 1.5배 이상 확인")]
    box = p.get("box")
    box_distance = pct(p["close"], box["low"]) if box else None
    ranged = [check("반복된 박스 상·하단 확인", bool(box), "지지 전환 매물대와 두 차례 이상 반복된 상단·하단 전환 확인"),
              check("현재 박스 하단", box is not None and box_distance is not None
                    and 0 <= box_distance <= RULES["supportDistancePct"]
                    and 0 <= box["positionPct"] <= 25,
                    "종가가 박스 하단 위 3% 이내이면서 전체 박스 폭의 하위 25%인지 확인"),
              check("저항 돌파 후 지지 유지", bool(p["supportFlipDay"]), "과거 큰 매물대의 거래량 동반 돌파와 현재 지지 유지 확인"),
              check("하락 추세 진정", p["r20"] is not None and p["r20"] >= -5 and p["ma60"] and p["close"] >= p["ma60"] * .95, "20일 수익률 -5% 이상·60일선 대비 -5% 이상 확인"),
              check("종가 회복", p["closePosition"] >= .6 and (p["change1"] or 0) >= 0, "당일 상승·일중 범위 상위 40% 종가 확인")]
    down = [check("상대 방어", (p.get("rs20") or 0) >= 0, "20일 시장 대비 성과가 음수인지 확인"),
            check("중기 가격 유지", p["ma60"] and p["close"] >= p["ma60"], "60일선 유지 여부 확인"),
            check("낙폭 제한", p["drawdown60"] is not None and p["drawdown60"] >= -15, "60일 고점 대비 낙폭 15% 이내 확인"),
            check("이익 훼손 점검", bool(f["actuals"]) and not f["latestWeak"] and not f["forecastWeak"], "확정 실적·전망의 감소 위험 확인")]
    stock["blockers"] = blockers
    stock["heatObservation"] = heat_exception
    stock["eligible"] = base
    stock["scenarios"] = {}
    for key, checks in (("up", up), ("range", ranged), ("down", down)):
        stock["scenarios"][key] = {"state": SCENARIOS[key]["readyLabel"] if base and all(c["met"] for c in checks) else "조건 대기",
            "ready": bool(base and all(c["met"] for c in checks)), "checks": checks,
            "waiting": blockers + [c["waiting"] for c in checks if not c["met"]]}
    stock["nextChecks"] = ["다음 실적 발표에서 매출·영업이익과 기대치 차이 확인 (발표일 미수집)",
        "동일 기간 전망이 새로 발표되면 기존 수치와 비교", "가격 조건은 다음 완료 거래일에 다시 확인"]
    stock["invalidation"] = ["동일 기간 영업이익 전망 하향 시 개선 근거 재평가", "확정 실적이 개선 전망을 뒷받침하지 못하면 보류",
        f"지지 전환 가격대 {p['support']:,.0f}원 종가 이탈 시 가격 조건 재평가" if p["support"] else "지지 전환 매물대 미확인 · 가격 방어 가정 보류"]
    return stock


def choose(stocks, key):
    # Evidence count, legacy scores and small-base percentage growth never rank.
    candidates = [s for s in stocks if (s["eligible"] or s["heatObservation"])
                  and (key != "up" or s["fundamentals"]["upCore"])]
    def order(s):
        f, p, state = s["fundamentals"], s["price"], s["scenarios"][key]
        if key == "up":
            return (-int(state["ready"]), int(s["heatObservation"]),
                    -(f["nextOPDelta"] or 0), -(f["latestOPDelta"] or 0),
                    -((f["nextMarginPct"] or 0) - (f["annualMarginPct"] or 0)), s["ticker"])
        improved_margin = (f["nextMarginPct"] or 0) - (f["annualMarginPct"] or 0)
        return (-int(state["ready"]), int(s["heatObservation"]), -int(f["actualImproving"] and f["positiveForecast"]),
                -sum(c["met"] for c in state["checks"]), -max(-20, min(20, improved_margin)), -(p.get("rs20") or 0), s["ticker"])
    candidates.sort(key=order)
    counts, selected = Counter(), []
    for s in candidates:
        if key in ("up", "range") and not s["scenarios"][key]["ready"]:
            continue
        sector = s["sector"]
        if counts[sector] >= RULES["maxPerSector"] or (sector not in counts and len(counts) >= RULES["maxSectors"]):
            continue
        counts[sector] += 1
        selected.append(s["ticker"])
    watch = []
    watch_count = 0
    if key == "up":
        watch_candidates = [s for s in candidates if s["eligible"] and not s["scenarios"]["up"]["ready"]]
        watch_count = len(watch_candidates)
        # Price proximity does not select or order companies. It only describes
        # their later entry state. The ordered shortlist is fundamentally led.
        watch_candidates.sort(key=lambda s: (-(s["fundamentals"]["nextOPDelta"] or 0),
                            -(s["fundamentals"]["latestOPDelta"] or 0),
                            -((s["fundamentals"]["nextMarginPct"] or 0)
                              - (s["fundamentals"]["annualMarginPct"] or 0)), s["ticker"]))
        counts = Counter()
        for s in watch_candidates:
            sector = s["sector"]
            if counts[sector] >= RULES["maxPerSector"]:
                continue
            counts[sector] += 1
            watch.append(s["ticker"])
            if len(watch) >= RULES["upFundamentalDisplay"]:
                break
    return {**SCENARIOS[key], "tickers": selected, "watchTickers": watch, "watchCount": watch_count,
            "candidateCount": len(candidates),
            "readyCount": sum(s["scenarios"][key]["ready"] for s in candidates),
            "shownReadyCount": sum(s["scenarios"][key]["ready"] for s in candidates if s["ticker"] in selected)}


def build(prices, fundamentals, report, consensus, guidance, observations, evidence, old_board, *, generated_at, snapshot_id, previous=None):
    cutoff = str(report["latestPriceDate"])[:10]
    year = int(cutoff[:4])
    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    prices["ticker"] = prices["ticker"].astype(str).str.zfill(6)
    prices = prices[prices["date"] <= pd.Timestamp(cutoff)]
    features = {str(t): price_features(h, cutoff) for t, h in prices.groupby("ticker", sort=False)}
    features = {t: p for t, p in features.items() if p}
    market_prices = [p for p in features.values() if p["date"] == cutoff and p["market"] in ("KOSPI", "KOSDAQ")]
    market = regime(market_prices)
    market["basis"] = "KOSPI·KOSDAQ 개별 종목 분포 · 지수 수익률 아님 · 보조 해석"
    market["date"] = cutoff
    market["markets"] = [{"name": m, **regime([p for p in market_prices if p["market"] == m])} for m in ("KOSPI", "KOSDAQ")]
    points = forecast_index(consensus, guidance, cutoff)
    revisions = revision_index(observations, cutoff, year)
    contexts = evidence_index(evidence.get("events", []), cutoff)
    fundmap = {str(r["ticker"]).zfill(6): r for r in fundamentals.to_dict("records")}
    stocks = []
    for ticker, p in features.items():
        p["rs20"] = p["r20"] - market["return20Median"] if p["r20"] is not None and market["return20Median"] is not None else None
        f = stock_fundamentals(fundmap.get(ticker, {}), points, revisions.get(ticker, []), cutoff, ticker)
        p["popCurrent"] = ratio(p["marketCap"], f["annualOP"])
        p["popNext"] = ratio(p["marketCap"], f["nextOP"])
        stock = {"ticker": ticker, "name": p["name"], "sector": p["sector"], "asOf": cutoff,
                 "price": p, "fundamentals": f, "evidence": sorted(contexts.get(ticker, []), key=lambda x: x.get("polarity") != "negative")[:6]}
        stocks.append(assess(stock))
    stockmap = {s["ticker"]: s for s in stocks}
    sectors = []
    for sector in sorted({s["sector"] for s in stocks}):
        members = [s for s in stocks if s["sector"] == sector]
        live = [s for s in members if s["price"]["date"] == cutoff]
        sec = regime([s["price"] for s in live])
        forecast_members = [s for s in live if s["fundamentals"]["nextOPGrowthPct"] is not None]
        sec.update(name=sector, totalCount=len(members), forecastCount=len(forecast_members),
                   improvedForecastCount=sum(s["fundamentals"]["positiveForecast"] for s in forecast_members),
                   relative20=sec["return20Median"] - market["return20Median"] if sec["return20Median"] is not None and market["return20Median"] is not None else None,
                   eligibleCount=sum(s["eligible"] for s in members), tickers=[s["ticker"] for s in members],
                   industryFacts=[{k: e.get(k) for k in ("kind", "period", "growthRate", "source", "url", "publishedAt")} for e in evidence.get("sectors", [])
                                  if e.get("sector") == sector and e.get("status") == "유효" and dated(e.get("publishedAt"), cutoff) and safe_url(e.get("url"))])
        sectors.append(sec)
    sectors.sort(key=lambda x: (-(x["relative20"] if x["relative20"] is not None else -999), x["name"]))
    scenarios = {key: choose(stocks, key) for key in SCENARIOS}
    holdings, assessment = [], {x.get("ticker"): x for x in old_board.get("p3", {}).get("assessments", [])}
    for row in old_board.get("p3", {}).get("rows", []):
        ticker = str(row.get("ticker", "")).zfill(6)
        s = stockmap.get(ticker)
        item = {k: row.get(k) for k in ("ticker", "name", "qty", "avg")}
        note = assessment.get(ticker)
        item["assessment"] = note if note and dated(note.get("checkedAt"), cutoff) else None
        item["returnPct"] = pct(s["price"]["close"], row.get("avg")) if s else None
        item["marketValue"] = s["price"]["close"] * row["qty"] if s and number(row.get("qty")) is not None else None
        risk = []
        if s:
            p, f = s["price"], s["fundamentals"]
            if p["ma60"] and p["close"] < p["ma60"]:
                risk.append("60일선 아래")
            if p["drawdown60"] is not None and p["drawdown60"] < -20:
                risk.append("60일 고점 대비 20% 이상 하락")
            if f["latestWeak"] or f["forecastWeak"]:
                risk.append("실적·전망 감소 확인")
            if not f["actuals"] or not f["forecasts"]:
                risk.append("실적 또는 전망 자료 보완 필요")
        else:
            risk.append("종목 원자료 미확인")
        item["risks"] = risk
        item["state"] = "우선 점검" if risk else "정기 점검"
        holdings.append(item)
    total_value = sum(h["marketValue"] or 0 for h in holdings)
    for h in holdings:
        h["weightPct"] = h["marketValue"] / total_value * 100 if h["marketValue"] is not None and total_value else None
    selected = set(t for scenario in scenarios.values() for t in scenario["tickers"])
    old_p2 = old_board.get("p2", {})
    old_rows = old_p2.get("rows", []) + old_p2.get("interestGrowth", {}).get("rows", [])
    old_ids = {str(x.get("ticker", "")).zfill(6) for x in old_rows}
    old_context = old_board.get("meta", {}).get("refreshState", {})
    same = old_context.get("snapshotId") == snapshot_id and old_context.get("sourceCutoff") == cutoff
    cases = ["222800", "007810", "064350", "403870", "003350", "101490", "088800"]
    comparison = {"sameSnapshot": same, "oldPriceDate": old_context.get("sourceCutoff"), "oldSnapshotId": old_context.get("snapshotId"),
        "oldCount": len(old_ids), "newShownCount": len(selected), "overlap": sorted(old_ids & selected),
        "added": sorted(selected - old_ids) if same else [], "removed": sorted(old_ids - selected) if same else [],
        "oldSectorCounts": dict(Counter(stockmap[t]["sector"] for t in old_ids if t in stockmap)),
        "newSectorCounts": dict(Counter(stockmap[t]["sector"] for t in selected)),
        "cases": [{"ticker": t, "inOld": t in old_ids, "inNew": t in selected,
                   "blockers": stockmap[t]["blockers"] if t in stockmap else ["원자료 없음"]} for t in cases],
        "performanceStatus": "현재 동일 자료의 선정 차이 점검. 수익률 개선·성공형 누락 해결 검증 아님.",
        "pointInTime": "실제 수집 시점 이전으로 소급한 투자 성과를 계산하지 않음. 오늘부터 저장되는 관찰 이력으로 후속 검증."}
    previous_map = (previous or {}).get("stocks", {})
    changes = []
    history_status = "첫 기준 자료 · 이후 날짜부터 변화 비교"
    if previous and previous.get("meta", {}).get("priceDate", "") < cutoff:
        history_status = "변화 비교 가능"
        for s in stocks:
            old = previous_map.get(s["ticker"])
            if old:
                for metric in ("annualOP", "nextOP"):
                    a, b = old["fundamentals"].get(metric), s["fundamentals"].get(metric)
                    if a is not None and b is not None and a != b:
                        changes.append({"ticker": s["ticker"], "metric": metric, "before": a, "after": b,
                                        "beforePriceDate": previous["meta"]["priceDate"], "priceDate": cutoff})
    elif previous and previous.get("meta", {}).get("priceDate") == cutoff:
        changes = previous.get("changes", [])
        history_status = previous.get("meta", {}).get("historyStatus", history_status)
    coverage = {"requested": report.get("requestedUniverseCount"), "priceRows": len(stocks),
        "currentPrice": sum(s["price"]["date"] == cutoff for s in stocks), "actualFourQuarters": sum(len(s["fundamentals"]["actuals"]) == 4 for s in stocks),
        "anyForecast": sum(bool(s["fundamentals"]["forecasts"]) for s in stocks),
        "annualPair": sum(s["fundamentals"]["annualOP"] is not None and s["fundamentals"]["nextOP"] is not None for s in stocks),
        "quarterForecast": sum(any("Q" in p["period"] and "op" in p for p in s["fundamentals"]["forecasts"]) for s in stocks),
        "matchedRevisions": sum(bool(s["fundamentals"]["revisions"]) for s in stocks),
        "eligible": sum(s["eligible"] for s in stocks), "blockerCounts": dict(Counter(reason for s in stocks for reason in s["blockers"])),
        "missingStocks": report.get("requestedMissingTickers", report.get("missingTickers", [])),
        "collectionStatus": report.get("qualityStatus", "미확인"), "sourceAttempts": report.get("attempts", []),
        "limitations": ["저장된 전체시장 가격·실적·전망을 재계산. 새 자료를 수집했다는 의미가 아님.",
            "거래대금은 대부분 종가×거래량 대용값. 실제 체결대금과 차이가 날 수 있음.",
            "수급 주체별 매매·제품 가격·가동률·재고의 전 종목 연속 자료는 미연결.",
            "업종 분류가 넓거나 미분류인 회사가 있어 동일 사업 비교는 원문 확인 필요.",
            "가격이 안 올랐다는 사실만으로 저평가·상승 여력을 판정하지 않음.",
            "실적 발표 전 시장 기대치와 실제치의 비교는 별도 시점 자료가 필요. 사후 전망으로 서프라이즈를 만들지 않음."]}
    return clean({"meta": {"version": VERSION, "priceDate": cutoff, "generatedAt": generated_at, "snapshotId": snapshot_id,
        "dataMode": "저장자료 재계산", "forecastPublicationDateRange": sorted({d for s in stocks for d in s["fundamentals"]["forecastDates"]}),
        "backtest": False, "historyStatus": history_status,
        "sourcePolicy": "같은 기간·항목에서 컨센서스와 공식 가이던스 중 최신 발표값. 같은 날 가이던스 우선. 날짜·자료 개수에 점수 가중치 없음."},
        "rules": RULES, "market": market, "sectors": sectors, "scenarios": scenarios, "stocks": stockmap,
        "holdings": holdings, "comparison": comparison, "coverage": coverage, "changes": changes})
