"""Offline, receipt-date-gated daily replay. Never publishes or tunes thresholds.

Uses the recorded present-day universe and cached versions of filings, so this is
a retrospective reconstruction, not a survivorship-free point-in-time backtest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import dart_fundamentals as dart
import rotation_screener as engine
import rotation_rules as rules
from refresh_store import load_verified_frames
from rotation_replay import CASES
from rotation_rules import features, score_candidate
from rotation_screener import load_config, run_engine, reported_financial_prerequisites


def published_before(receipt, day):
    receipt = str(receipt or '')
    if not re.fullmatch(r'20\d{12}', receipt):
        return False
    date = pd.to_datetime(receipt[:8], format='%Y%m%d', errors='coerce')
    return bool(pd.notna(date) and date < pd.Timestamp(day).normalize())


def load_periods(cache, universe, recovered_filings=None):
    periods = {}
    for path in sorted((Path(cache) / 'dart_periods').glob('*.json')):
        year, code = path.stem.split('_')
        entries = json.loads(path.read_text(encoding='utf-8')).get('companies', {})
        raw = [row for entry in entries.values() for row in entry.get('rows', [])]
        frame = dart._parse_multi_account_rows(raw, universe, int(year), code)
        if not frame.empty:
            periods[(int(year), code)] = frame
    for row in recovered_filings or []:
        if not str(row.get('source', '')).startswith('https://dart.fss.or.kr/'):
            raise ValueError('Recovered filings require their original DART source URL')
        key = (int(row['report_year']), str(row['report_code']))
        periods[key] = pd.concat([periods.get(key, pd.DataFrame()),pd.DataFrame([row])],ignore_index=True)
    return periods


def historical_financials(periods, universe, day):
    allowed = {key: frame[frame.receipt.map(lambda r: published_before(r, day))]
               .sort_values('receipt').drop_duplicates('ticker',keep='last').copy()
               for key, frame in periods.items()}
    nonempty = [frame for frame in allowed.values() if not frame.empty]
    if not nonempty:
        return pd.DataFrame(columns=['ticker']), []
    combined = pd.concat(nonempty, ignore_index=True).sort_values(['as_of', 'receipt'])
    combined = combined.drop_duplicates('ticker', keep='last').reset_index(drop=True)

    def offline_loader(requested, key, config, year, code):
        frame = allowed.get((year, code), pd.DataFrame(columns=['ticker']))
        return frame[frame.ticker.isin(requested.ticker)].copy(), []

    # Reuse the production quarter reconstruction, with network collection
    # replaced solely by receipt-gated local rows; no later restatement fallback.
    with patch.object(dart, '_collect_bulk_period_values', offline_loader):
        result, failures = dart._attach_normalized_ttm(combined, universe, '', {})
    return result, failures


def forward_outcome(prices, ticker, day, horizon, cost_bps=30):
    """Signal after close; enter next market-session open, exit Nth close."""
    sessions = sorted(pd.to_datetime(prices.date.unique()))
    position = sessions.index(pd.Timestamp(day))
    if position + horizon >= len(sessions):
        return {'status': 'pending', 'netPct': None}
    entry_day, exit_day = sessions[position+1], sessions[position+horizon]
    bars = prices[prices.ticker.eq(ticker)].set_index('date')
    if entry_day not in bars.index or exit_day not in bars.index:
        return {'status': 'missing_session', 'netPct': None}
    entry, exit_bar = bars.loc[entry_day], bars.loc[exit_day]
    if entry.open <= 0 or entry.volume <= 0 or exit_bar.close <= 0 or exit_bar.volume <= 0:
        return {'status': 'untradable', 'netPct': None}
    gross = (float(exit_bar.close) / float(entry.open) - 1) * 100
    return {'status': 'measured', 'entryDate': str(entry_day.date()),
            'exitDate': str(exit_day.date()), 'grossPct': round(gross, 4),
            'netPct': round(gross-cost_bps/100, 4)}


def summarize(days, key, horizon):
    daily, trades = [], []
    for day in days:
        rows = day[key]
        if not rows:
            if day.get('horizonMatured', {}).get(str(horizon), False):
                daily.append(0.0)
            continue
        measured = [r[f'forward{horizon}']['netPct'] for r in rows
                    if r[f'forward{horizon}']['status'] == 'measured']
        trades.extend(measured)
        if len(measured) == len(rows):
            # Fixed five equal slots, absent candidates remain in cash.
            daily.append(sum(measured)/5)
    return {'measuredSignals': len(trades), 'completeSignalDays': len(daily),
            'meanSignalNetPct': float(np.mean(trades)) if trades else None,
            'positiveSignalPct': float(np.mean(np.array(trades)>0)*100) if trades else None,
            'meanFiveSlotCohortNetPct': float(np.mean(daily)) if daily else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--start', default='2026-07-31')
    parser.add_argument('--end', default='2026-09-18')
    parser.add_argument('--recovered-filings', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    prices, _, source = load_verified_frames(args.state/'test_output', args.state/'cache')
    prices = prices.sort_values(['ticker', 'date']).reset_index(drop=True)
    universe = prices.groupby('ticker', sort=False).tail(1)[['ticker', 'name', 'sector']]
    recovered = json.loads(args.recovered_filings.read_text(encoding='utf-8')) if args.recovered_filings else []
    periods = load_periods(args.state/'cache', universe, recovered)
    config = load_config(None, None)
    config['cache_dir'] = args.state/'cache'
    signature = hashlib.sha256()
    inputs = [Path(__file__), Path(engine.__file__), Path(rules.__file__), Path(dart.__file__),
              Path(__file__).with_name('reported_financials.py'),
              args.state/'cache'/'growth'/'event_ledger.json',
              args.state/'test_output'/'verified_snapshot.json', Path(config['sector_overrides_file'])]
    inputs += sorted((args.state/'cache'/'dart_periods').glob('*.json'))
    if args.recovered_filings:
        inputs.append(args.recovered_filings)
    for path in inputs:
        signature.update(str(path.resolve()).encode())
        signature.update(path.read_bytes() if path.exists() else b'missing')
    signature.update(json.dumps(config, sort_keys=True, default=str).encode())
    signature.update(f'{args.start}:{args.end}'.encode())
    identity = signature.hexdigest()
    manifest = args.out/'run-manifest.json'
    if args.resume and (not manifest.exists() or json.loads(manifest.read_text())['fingerprint'] != identity):
        raise ValueError('Cannot resume: replay code, source, dates or configuration changed')
    manifest.write_text(json.dumps(dict(fingerprint=identity),indent=2),encoding='utf-8')
    overrides = engine.read_overrides(config['sector_overrides_file'])
    prices['sector'] = prices.ticker.map(overrides).fillna(prices.sector)
    # All these transforms are causal rolling/expanding transforms. Slice their
    # results before sector-state inference, scoring, or cross-sectional selection.
    print('Computing causal price/sector features once', flush=True)
    stock_features, sector_features = engine.build_daily_features(prices)
    rotation_features = features(prices)
    dates = sorted(prices.loc[prices.date.between(args.start, args.end), 'date'].unique())
    market_dates = sorted(prices.date.unique())
    days = []
    for date in dates:
        day = str(pd.Timestamp(date).date())
        checkpoint = args.out/f'{day}.json'
        if args.resume and checkpoint.exists():
            days.append(json.loads(checkpoint.read_text(encoding='utf-8')))
            continue
        prefix = prices[prices.date.le(date)].copy()
        fund, failures = historical_financials(periods, universe, day)
        with patch.object(engine, 'read_overrides', return_value={}), \
             patch.object(engine, 'build_daily_features', return_value=(
                 stock_features[stock_features.date.le(date)].copy(),
                 sector_features[sector_features.date.le(date)].copy())), \
             patch.object(rules, 'features', return_value=rotation_features[rotation_features.date.eq(date)].copy()):
            result = run_engine(prefix, config, 'receipt-gated retrospective reconstruction', fund)
        profiles = reported_financial_prerequisites(fund, 50e9, 15)
        def outcomes(rows):
            return [dict(row, **{f'forward{n}': forward_outcome(prices, row['ticker'], day, n)
                                for n in (5, 10, 20)}) for row in rows]
        # Old implementation's existing sector cap was 4. Compare equal Top5
        # capacity, same reconstructed financial prerequisites and same prices.
        legacy, counts = [], {}
        for row in result['_legacyEntryRows']:
            if counts.get(row['sector'], 0) >= 4:
                continue
            legacy.append(row)
            counts[row['sector']] = counts.get(row['sector'], 0)+1
            if len(legacy) == 5:
                break
        named = []
        for ticker, name, sector, signal_day, label in CASES:
            bars = prefix[prefix.ticker.eq(ticker)]
            if bars.empty or bars.date.max() != pd.Timestamp(day):
                named.append(dict(ticker=ticker, name=name, status='price missing'))
                continue
            row = rotation_features[rotation_features.ticker.eq(ticker) & rotation_features.date.eq(date)].iloc[-1].to_dict()
            chart = score_candidate(row, [], config['minimum_daily_turnover'])
            audit = next((r for r in result['_eligibility'] if r['ticker']==ticker), {})
            shown = next((r for r in result['rows'] if r['ticker']==ticker), None)
            named.append(dict(ticker=ticker, name=name, originalSignalDate=signal_day,
                chartOnlyEligible=chart['eligible'], chartReasons=chart['exclusionReasons'],
                financial=profiles.get(ticker), fullCandidate=audit.get('eligible', False),
                fullReason=audit.get('reason'), displayed=shown is not None,
                rank=shown.get('rank') if shown else None))
        latest = prefix[prefix.date.eq(date)]
        record = dict(date=day, universeCount=len(latest),
            horizonMatured={str(n):market_dates.index(date)+n<len(market_dates) for n in (5,10,20)},
            financiallyComplete=sum(p['complete'] for p in profiles.values()),
            financiallyEligible=sum(p['eligible'] for p in profiles.values()),
            unavailableFinancialTickers=sorted(set(latest.ticker)-{t for t,p in profiles.items() if p['complete']}),
            financialReconstructionFailures=failures,
            oldTop5=outcomes(legacy), newTop5=outcomes(result['rows']),
            newEntries=outcomes([r for r in result['rows'] if r['rank']<=3]),
            newObservations=outcomes([r for r in result['rows'] if r['rank']>=4]),
            cases=named)
        checkpoint.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        days.append(record)
        print(day, 'universe',len(latest),'financial',record['financiallyEligible'],
              'new', len(result['rows']), flush=True)
    summary = {key:{str(n):summarize(days,key,n) for n in (5,10,20)}
               for key in ('oldTop5','newTop5','newEntries','newObservations')}
    report = dict(start=args.start, end=args.end, sessions=len(days),
        comparison='old and new Top5 under identical receipt-gated financial prerequisites',
        assumedRoundTripCostBps=30, summary=summary, days=days,
        limitations=['Current snapshot universe: delisted and historical membership not reconstructed.',
          'Only cached filing versions with receipt dates before the signal are used; superseded versions may be unavailable.',
          'Current adjusted price history and turnover provenance retained; adjustment and survivorship biases remain.',
          'Unavailable historical consensus has zero points; no forward consensus values are backfilled.',
          'Watch rows are hypothetical observations, not entry signals. Results reported separately.',
          'Overlapping daily cohorts are not an independent sample or a tradable portfolio equity curve.',
          '30 bps is an illustrative round-trip cost, not a verified tax or slippage schedule.',
          'No thresholds were optimized on these returns. Unmatured or untradable returns stay null.'])
    (args.out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


if __name__ == '__main__':
    main()
