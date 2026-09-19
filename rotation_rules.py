"""Point-in-time rules for the p11 rotation board only.

No future bars, undated evidence, or synthetic fundamentals are admitted.
Thresholds are explicit heuristics, not fitted return predictions.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def macd_weight(age, reignited=False):
    if reignited:
        return 1.0
    if not math.isfinite(number(age, float('nan'))):
        return .30  # A crossover outside the available history is not a new GC.
    return next(weight for end, weight in [(3, 1), (7, .9), (12, .75), (20, .55), (float('inf'), .3)] if age <= end)


def features(prices):
    parts = []
    for _, group in prices.groupby('ticker', sort=False):
        g = group.sort_values('date').copy()
        if g.date.duplicated().any():
            raise ValueError('duplicate ticker/session in rotation prices')
        close = pd.to_numeric(g.close)
        volume = pd.to_numeric(g.volume)
        g['volume3'] = volume.rolling(3).mean()
        g['volume14_before3'] = volume.shift(3).rolling(14).mean()
        g['rotation_volume_ratio'] = g.volume3 / g.volume14_before3.replace(0, np.nan)
        g['turnover3'] = g.value.rolling(3).mean()
        # Prior 20 completed sessions; today's high never changes today's resistance.
        g['resistance20'] = g.high.shift(1).rolling(20).max()
        g['resistance_distance'] = close / g.resistance20 - 1
        g['rotation_ma20'] = close.rolling(20).mean()
        macd = close.ewm(span=12, adjust=False, min_periods=26).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
        signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
        above = macd > signal
        cross = above & (macd.shift(1) <= signal.shift(1))
        positions = pd.Series(np.arange(len(g)), index=g.index)
        g['macd_age'] = positions - positions.where(cross).ffill()
        g['macd_ok'] = above & signal.notna()
        g['close_breakout'] = close > g.resistance20
        g['reignited'] = (g.rotation_volume_ratio >= 2) & g.close_breakout & ~g.close_breakout.shift(1, fill_value=False)
        g['macd_weight'] = [macd_weight(age, bool(re)) for age, re in zip(g.macd_age, g.reignited)]
        span = (g.high - g.low).replace(0, np.nan)
        g['close_location'] = ((close - g.low) / span).fillna(1.0)
        g['upper_wick'] = ((g.high - pd.concat([g.open, close], axis=1).max(axis=1)) / span).fillna(0.0)
        g['high_fade'] = 1 - close / g.high
        for days in (1, 3, 5):
            g[f'rotation_ret{days}'] = close.pct_change(days, fill_method=None)
        g['intraday_rise'] = g.high / close.shift(1) - 1
        g['valid_bars'] = ((g.volume > 0) & (g.value > 0) & (g.low > 0) &
                           (g.high >= g[['open', 'close']].max(axis=1)) &
                           (g.low <= g[['open', 'close']].min(axis=1))).rolling(20).min().eq(1)
        parts.append(g)
    return pd.concat(parts, ignore_index=True) if parts else prices.copy()


def dated_evidence(events, as_of):
    """Use the version's publication date, never the original date of a correction."""
    valid = []
    for event in events:
        published = pd.to_datetime(event.get('publishedAt'), errors='coerce', utc=True)
        cutoff = pd.Timestamp(as_of).normalize().tz_localize('Asia/Seoul') + pd.Timedelta(hours=15, minutes=30)
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(event.get('publishedAt', ''))):
            # Unknown release time: do not assume a same-day filing preceded the close.
            available = pd.Timestamp(event['publishedAt']).tz_localize('Asia/Seoul') + pd.Timedelta(days=1)
            if available > cutoff:
                continue
        if pd.isna(published) or published > cutoff:
            continue
        age = (pd.Timestamp(as_of).date() - published.date()).days
        if age > int(event.get('maxAgeDays', 30)) or any(
                not isinstance(event.get(key), str) or not event[key].strip() for key in ('source','eventId')):
            continue
        if event.get('status') != 'verified':
            continue
        valid.append(event)
    return valid


def evidence_from_sources(fundamentals, config):
    """Conservative adapters: unverified/missing release dates earn no points."""
    events = []
    if fundamentals is not None:
        for row in fundamentals.to_dict('records'):
            ticker = str(row['ticker']).zfill(6)
            receipt = str(row.get('receipt', '')).split('.')[0]
            # Receipt date is publication; fiscal period end is never publication.
            if re.fullmatch(r'20\d{12}', receipt) and row.get('quarter_value_verified') == True:
                growth = number(row.get('op_1y_growth'))
                if growth > 0 and number(row.get('op_current')) > 0:
                    events.append(dict(ticker=ticker, eventId='dart:'+receipt, source='DART',
                        publishedAt=pd.to_datetime(receipt[:8]).strftime('%Y-%m-%d'),
                        status='verified', kind='earnings', points=min(20, growth / 2),
                        strong=growth >= 30, maxAgeDays=90))
            observed = row.get('consensus_fetched_at')
            if (isinstance(observed, str) and row.get('consensus_as_of_precision') == 'day'
                    and number(row.get('consensus_change_20d')) > 0):
                events.append(dict(ticker=ticker, eventId=f'consensus:{ticker}:{observed}',
                    source=row.get('consensus_source', ''), publishedAt=observed, status='verified',
                    kind='consensus', points=min(25, number(row['consensus_change_20d']) * 2.5),
                    strong=number(row['consensus_change_20d']) >= 10))
    ledger = Path(config.get('cache_dir', 'cache')) / 'growth' / 'event_ledger.json'
    if ledger.exists():
        for row in json.loads(ledger.read_text('utf-8')):
            if row.get('status') != '유효' or row.get('polarity') != 'positive' or row.get('correction'):
                continue
            events.append(dict(ticker=str(row['ticker']).zfill(6), eventId='dart:'+str(row['receipt']) if row.get('receipt') else row['eventId'],
                source=row.get('url') or row.get('source'), publishedAt=row.get('publishedAt'),
                status='verified', kind='catalyst', points=min(25, number(row.get('materiality')) / 2),
                strong=number(row.get('revenueRatio')) >= 10))
    extra = config.get('rotation_evidence_file')
    if extra:
        events.extend(json.loads(Path(extra).read_text('utf-8')))
    return events


def score_candidate(item, events, minimum_turnover):
    r = item
    reasons = []
    ratio = number(r.get('rotation_volume_ratio'))
    distance = number(r.get('resistance_distance'), -1)
    if re.search(r'건설|바이오|제약|의약|biotech|construction|pharma', str(r.get('sector', '')), re.I):
        reasons.append('기존 제외 업종')
    if not r.get('valid_bars', False) or not r.get('macd_ok', False):
        reasons.append('가격 이력 또는 MACD 미충족')
    if number(r.get('close')) < number(r.get('rotation_ma20'), float('inf')):
        reasons.append('20일선 미회복')
    if ratio < 1.5:
        reasons.append('거래량 1.5배 미달')
    if min(number(r.get('value')), number(r.get('turnover3'))) < minimum_turnover:
        reasons.append('당일/3일 평균 거래대금 미달')
    if distance < -.03 - 1e-10:
        reasons.append('20일 저항 -3% 밖')
    if r.get('sector_stage') in ('X조기이탈', 'X종료'):
        reasons.append('순환 종료/조기이탈')
    # One underlying event earns its strongest contribution once across domains.
    unique = {}
    for event in dated_evidence(events, r['date']):
        key = event['eventId']
        if key not in unique or number(event.get('points')) > number(unique[key].get('points')):
            unique[key] = event
    financial = min(35, sum(max(0, number(e.get('points'))) for e in unique.values() if e.get('kind') in ('earnings', 'consensus')))
    catalyst = min(25, sum(max(0, number(e.get('points'))) for e in unique.values() if e.get('kind') in ('catalyst', 'industry')))
    strong_fundamental = any(e.get('strong') is True and e.get('kind') in ('earnings', 'consensus', 'catalyst')
        and (pd.Timestamp(r['date']).date() - pd.to_datetime(e['publishedAt'], utc=True).date()).days <= 30
        for e in unique.values())
    breakout = bool(r.get('close_breakout', False))
    hot = number(r.get('rotation_ret1')) >= .10 or number(r.get('rotation_ret3')) > .15 or number(r.get('rotation_ret5')) > .25
    distribution = ((number(r.get('intraday_rise')) >= .10 and number(r.get('high_fade')) >= .05) or
                    (number(r.get('upper_wick')) >= .45 and number(r.get('close_location')) < .6))
    exception = hot and strong_fundamental and ratio >= 2 and breakout and number(r.get('close_location')) >= .8 and number(r.get('high_fade')) <= .03 and not distribution
    if hot and not exception:
        reasons.append('과열 예외 4조건 미충족')
    # Correlated tape signals form one capped block, not four independent bonuses.
    chart = min(40, (18 if ratio >= 2 else 10 if ratio >= 1.5 else 0) +
                (16 if breakout else 3 if distance >= -.03 else 0) +
                6 * number(r.get('macd_weight')))
    # Distribution always blocks entry. Non-overheated names satisfying every
    # minimum gate can remain low-ranked observations, with a 20-point penalty.
    penalty = (20 if distribution else 0) + (15 if distance > .10 else 0) + (15 if exception else 0) + (15 if r.get('sector_stage') == '⑥후반' else 0)
    watch = distribution or exception or not breakout or distance > .10 or r.get('sector_stage') == '⑥후반'
    return dict(eligible=not reasons, exclusionReasons=reasons, score=round(max(0, chart + financial + catalyst - penalty), 2),
        scoreBlocks={'수급·차트': chart, '실적·컨센서스': financial, '신규촉매·업황': catalyst},
        penalty=penalty, watchOnly=watch, overheated=hot, heatException=exception,
        distributionWarning=distribution,
        volumeRatio=round(ratio, 4), volumeGrade='A' if ratio >= 2 else 'B',
        resistanceDistancePct=round(distance * 100, 4), closeBreakout=breakout,
        macdWeight=number(r.get('macd_weight')), evidenceIds=sorted(unique),
        evidenceStatus='확인' if unique else '시점 확인 근거 없음')


def select_top(candidates, limit=15):
    """At most five sectors, three stocks each; no relaxation to fill slots."""
    ranked = sorted(candidates, key=lambda r: (-r['stockEntryScore'], -r['volumeRatio'], r['ticker']))
    selected, sectors, themes = [], set(), set()
    limit = max(0, min(15, limit))
    sector_limit = min(5, limit)
    for row in ranked:
        if len(selected) >= min(3, limit):
            break
        if row['watchOnly'] or row['sector'] in sectors or row['detailTheme'] in themes:
            continue
        selected.append(dict(row, rank=len(selected)+1, signal='진입 검토', entryFit='진입 검토'))
        sectors.add(row['sector'])
        themes.add(row['detailTheme'])
    for row in ranked:
        if len(selected) >= sector_limit:
            break
        if row['sector'] in sectors:
            continue
        selected.append(dict(row, rank=len(selected)+1, entryFit='관찰',
                             signal='과열 관찰' if row['heatException'] else '관찰'))
        sectors.add(row['sector'])
    entry_tickers = {row['ticker'] for row in selected if row['entryFit']=='진입 검토'}
    grouped = []
    for sector_rank, representative in enumerate(selected, 1):
        members = [representative] + [r for r in ranked if r['sector']==representative['sector'] and r['ticker']!=representative['ticker']]
        for stock_rank, row in enumerate(members[:3], 1):
            if len(grouped) >= limit:
                break
            entry = row['ticker'] in entry_tickers
            grouped.append(dict(row, rank=len(grouped)+1, sectorRank=sector_rank, rankInSector=stock_rank,
                entryFit='진입 검토' if entry else '관찰',
                signal='진입 검토' if entry else '과열 관찰' if row['heatException'] else '관찰'))
    return grouped


def improving_financial_watch(fundamentals, profiles, as_of):
    """Only complete, published financials with sales AND profit improvement.

    Accept verified standalone-quarter YoY or consecutive-quarter improvement.
    Loss-making latest quarters, unknown dates and future corrections cannot pass.
    """
    watches, events = {}, []
    if fundamentals is None:
        return watches, events
    for r in fundamentals.to_dict('records'):
        ticker = str(r['ticker']).zfill(6)
        profile = profiles.get(ticker, {})
        if profile.get('eligible') or not profile.get('complete'):
            continue
        try:
            sources = json.loads(r.get('normalization_sources', '[]'))
            periods = sorted(pd.Period(p.strip(), freq='Q') for p in r['normalization_periods'].split(','))
            receipts = [str(s['receipt']) for s in sources]
            if len(periods) != 4 or any(periods[i]+1 != periods[i+1] for i in range(3)) or not receipts:
                continue
            if {str(p) for p in periods} - {s.get('quarter') for s in sources}:
                continue
            if any(not re.fullmatch(r'20\d{12}', v) or
                   not pd.to_datetime(v[:8], errors='coerce') < pd.Timestamp(as_of).normalize() for v in receipts):
                continue
            if not 0 <= (pd.Timestamp(as_of) - periods[-1].end_time.normalize()).days <= 180:
                continue
            q, previous = periods[-1].quarter, periods[-2].quarter
            sales, profit = number(r.get(f'normalized_sales_q{q}')), number(r.get(f'normalized_op_q{q}'))
            prev_sales, prev_profit = number(r.get(f'normalized_sales_q{previous}')), number(r.get(f'normalized_op_q{previous}'))
            qoq = sales > prev_sales > 0 and profit > prev_profit and profit > 0
            yoy = (r.get('quarter_value_verified') == True and
                   str(r.get('quarter_as_of')) == str(periods[-1].end_time.date()) and
                   sales == number(r.get('sales_quarter_current')) and profit == number(r.get('op_quarter_current')) and
                   sales > number(r.get('sales_quarter_previous')) > 0 and
                   profit > number(r.get('op_quarter_previous')) and profit > 0)
            if not (qoq or yoy):
                continue
            misses = []
            if profile['averageQuarterlySales'] < 50e9:
                misses.append(f"평균 분기매출 {profile['averageQuarterlySales']/1e8:.1f}억원(500억원 미달)")
            if profile['averageQuarterlyOperatingMarginPct'] < 15:
                misses.append(f"평균 영업이익률 {profile['averageQuarterlyOperatingMarginPct']:.1f}%(15% 미달)")
            basis = '전년 동분기' if yoy else '직전 분기'
            watches[ticker] = dict(reason=' · '.join(misses)+f' · {basis} 대비 매출·영업이익 증가, 최근 분기 흑자',
                                   basis=basis, financial=profile)
            # Same filing ID as the existing earnings adapter: deduplicated,
            # never a second independent catalyst or an automatic heat exception.
            receipt = max(receipts)
            events.append(dict(ticker=ticker, eventId='dart:'+receipt, source='DART',
                publishedAt=pd.to_datetime(receipt[:8]).strftime('%Y-%m-%d'), status='verified',
                kind='earnings', points=10, strong=False, maxAgeDays=180))
        except (ValueError, TypeError, KeyError):
            continue
    return watches, events


def build_rotation(prices, sectors, templates, config, evidence, financial_watches=None):
    frame = features(prices)
    latest = frame[frame.date.eq(frame.date.max())]
    sector_map = {r['name']: r for r in sectors}
    template_map = {r['ticker']: r for r in templates}
    evidence_map = {}
    for event in evidence:
        evidence_map.setdefault(str(event.get('ticker', '')).zfill(6), []).append(event)
    candidates, audit = [], []
    theme_map = config.get('rotation_theme_map', {})
    for row in latest.to_dict('records'):
        sector = sector_map.get(row['sector'], {})
        row['sector_stage'] = sector.get('stage')
        result = score_candidate(row, evidence_map.get(row['ticker'], []), config['minimum_daily_turnover'])
        financial_watch = (financial_watches or {}).get(row['ticker'])
        if financial_watch:
            result['watchOnly'] = True
            result['financialWatch'] = True
            result['financialWatchReason'] = financial_watch['reason']
        if row['ticker'] not in template_map:
            result['eligible'] = False
            result['exclusionReasons'].append('기존 섹터/유동성 조건 미충족')
        audit.append(dict(ticker=row['ticker'], name=row['name'], sector=row['sector'],
            reason=' / '.join(result['exclusionReasons']) or ('강감점 관찰' if result['watchOnly'] else '순환 후보 조건 통과'),
            priceDate=str(row['date'].date()), **result))
        if not result['eligible']:
            continue
        public = dict(template_map[row['ticker']], **result)
        theme = theme_map.get(row['ticker'], row.get('detail_theme'))
        theme = theme.strip() if isinstance(theme, str) and theme.strip() else row['sector']
        public.update(stockEntryScore=result['score'], detailTheme=theme,
            entryFit='관찰' if result['watchOnly'] else '진입 검토',
            reason=f"거래량 {result['volumeRatio']:.2f}배 · 종가 저항 {result['resistanceDistancePct']:+.2f}% · "
                   f"수급/실적/촉매 {result['scoreBlocks']['수급·차트']:.1f}/{result['scoreBlocks']['실적·컨센서스']:.1f}/{result['scoreBlocks']['신규촉매·업황']:.1f} · 감점 {result['penalty']} · {result['evidenceStatus']}")
        if financial_watch:
            public['reason'] = '조기 관찰 · '+financial_watch['reason']+' · '+public['reason']
        candidates.append(public)
    return select_top(candidates, min(15, config['top_stock_count'])), candidates, audit
