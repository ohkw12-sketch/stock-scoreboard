"""Forward-only recommendation ledger and date-aligned, missing-data-aware evaluation."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean, median

import pandas as pd

from refresh_store import digest, json_write, read_json

KST = timezone(timedelta(hours=9))
VERSION = 'performance-1.0'
HORIZONS = (5, 20, 60, 120)


def timestamp(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError('기록시각에는 시간대가 필요합니다.')
    return stamp.tz_convert('Asia/Seoul')


def empty_ledger():
    return {'schemaVersion': 1, 'cohorts': [], 'universes': {}}


def record_publication(ledger, board, combined, universe, *, observed_at, engine_version):
    """Call only AFTER verifying the exact generation on the deployed site. Idempotent by section generation."""
    import copy
    ledger = copy.deepcopy(ledger)
    timestamp(observed_at)
    members = sorted(set(str(t).zfill(6) for t in universe))
    universe_id = digest(members)
    ledger['universes'].setdefault(universe_id, members)
    sections = [(key, board.get(key, {})) for key in ('p1', 'p11', 'p2', 'growth')]
    sections.append(('combined', combined))
    for key, section in sections:
        state = section.get('refreshState', {})
        current_run = board.get('meta', {}).get('runId')
        if current_run and state.get('runId') != current_run:
            continue
        generated = section.get('generatedAt') or state.get('generatedAt')
        if not generated or state.get('status') == '실패·이전유지':
            continue
        if timestamp(generated) > timestamp(observed_at):
            raise ValueError('공개 확인시각이 산출시각보다 빠릅니다.')
        rows = list(section.get('rows', []))
        if key == 'growth':
            for sector in section.get('sectors', []):
                rows += [{**r, '_sectorPick': True} for r in sector.get('stocks', [])]
        records = []
        seen = set()
        for row in rows:
            if not row.get('ticker'):
                continue
            group = {'p1': '진입', 'p11': '순환', 'p2': '가치', 'growth': '성장', 'combined': '종합' }[key]
            group = ('성장섹터' if row.get('_sectorPick') else group)
            if key == 'p1':
                group += ' · ' + row.get('entryState', '미확인')
            if key == 'combined':
                group += ' · ' + row['condition'] + ' · ' + row['entryState']
            identity = (row['ticker'], group)
            if identity in seen:
                continue
            seen.add(identity)
            records.append({'ticker': row['ticker'], 'name': row['name'], 'group': group,
                            'rank': row.get('rank', row.get('typeRank')), 'sector': row.get('sector'),
                            'signal': row.get('entryState') or row.get('signal'),
                            'sourceRow': row})
        # Retries of the same generation cannot change the first observed time or duplicate records.
        rule_version = section.get('ruleVersion', section.get('methodVersion', key + '-existing'))
        recorded_version = state.get('engineVersion') or section.get('engineVersion') or engine_version
        cohort_id = digest({'section': key, 'generatedAt': generated, 'engineVersion': recorded_version,
                            'ruleVersion': rule_version,
                            'snapshot': section.get('snapshotId') or state.get('snapshotId'), 'rows': records})
        if any(c['id'] == cohort_id for c in ledger['cohorts']):
            continue
        ledger['cohorts'].append({'id': cohort_id, 'section': key, 'generatedAt': generated,
                                  'observedPublishedAt': observed_at, 'universeId': universe_id,
                                  'snapshotId': section.get('snapshotId') or state.get('snapshotId'),
                                  'evidenceSnapshotId': section.get('evidenceSnapshotId') or state.get('evidenceSnapshotId'),
                                  'runId': state.get('runId'),
                                  'engineVersion': recorded_version, 'ruleVersion': rule_version,
                                  'sourceDate': section.get('sourceDate') or state.get('sourceCutoff'),
                                  'records': records})
    return ledger


class PriceBook:
    def __init__(self, prices):
        frame = prices.copy()
        if frame.empty:
            frame = pd.DataFrame(columns=['ticker', 'date'])
        frame['ticker'] = frame.ticker.astype(str).str.zfill(6)
        frame['date'] = pd.to_datetime(frame.date).dt.normalize()
        frame = frame.sort_values('date').drop_duplicates(['ticker', 'date'], keep='last')
        self.sessions = sorted(frame.date.unique())
        self.frames = {t: g.set_index('date') for t, g in frame.groupby('ticker')}
        self.latest = pd.Timestamp(self.sessions[-1]) if self.sessions else None
        self.benchmark_cache = {}

    def quote(self, ticker, date, field):
        frame = self.frames.get(ticker)
        if frame is None or date not in frame.index:
            return None, '가격 누락·거래정지·상장폐지 확인 필요'
        row = frame.loc[date]
        if pd.isna(row.get('price_date_verified')) or row.get('price_date_verified') != True:
            return None, '가격 날짜 검증 대기'
        if row.get('adjusted_basis') != 'provider_adjusted_close' and (
                pd.isna(row.get('adjusted')) or row.get('adjusted') != True):
            return None, '수정주가 확인 대기'
        close, value = row.get('close'), row.get(field)
        if pd.isna(value) or value <= 0 or pd.isna(close) or close <= 0:
            return None, '유효 가격 없음'
        if pd.isna(row.get('volume')) or row.get('volume') <= 0:
            return None, '거래정지·무거래 확인 필요'
        adjusted_close = row.get('adjusted_close')
        if pd.isna(adjusted_close):
            adjusted_close = row.get('Adj Close')
        if row.get('adjusted_basis') == 'provider_adjusted_close' and (pd.isna(adjusted_close) or adjusted_close <= 0):
            return None, '수정주가 값 누락'
        factor = float(adjusted_close / close) if pd.notna(adjusted_close) and adjusted_close > 0 else 1
        return float(value * factor), None

    def benchmark(self, universe_id, members, entry_date, exit_date, cost_bps):
        key = (universe_id, entry_date, exit_date, cost_bps)
        if key in self.benchmark_cache:
            return self.benchmark_cache[key]
        returns = []
        for ticker in members:
            start, _ = self.quote(ticker, entry_date, 'open')
            end, _ = self.quote(ticker, exit_date, 'close')
            if start is not None and end is not None:
                returns.append((end/start-1)*100 - cost_bps/100)
        # No silent survivor-only average. Require the complete frozen universe.
        result = {'returnPct': mean(returns) if returns and len(returns) == len(members) else None,
                  'coverage': len(returns), 'requested': len(members),
                  'name': '추천 당시 전체시장 동일비중', 'status': '완전' if len(returns) == len(members) else '누락으로 비교 보류'}
        self.benchmark_cache[key] = result
        return result


def evaluate_legacy(ledger, prices, *, generated_at=None, cost_bps=0, trading_sessions=None):
    if cost_bps < 0:
        raise ValueError('비용은 음수일 수 없습니다.')
    generated_at = generated_at or datetime.now(KST).isoformat(timespec='seconds')
    as_of = timestamp(generated_at)
    eligible_cohorts = [c for c in ledger.get('cohorts', []) if timestamp(c['observedPublishedAt']) <= as_of]
    daily_final = {}
    for cohort in eligible_cohorts:
        key = (cohort['section'], cohort['generatedAt'][:10], cohort['engineVersion'], cohort['ruleVersion'])
        previous = daily_final.get(key)
        if previous is None or timestamp(cohort['observedPublishedAt']) > timestamp(previous['observedPublishedAt']):
            daily_final[key] = cohort
    cohorts = list(daily_final.values())
    cutoff = as_of.tz_localize(None).normalize()
    if (as_of.hour, as_of.minute) < (15, 40):
        cutoff -= pd.Timedelta(days=1)
    usable_prices = prices[pd.to_datetime(prices.date).dt.normalize() <= cutoff] if not prices.empty else prices
    book = PriceBook(usable_prices)
    calendar_status = '공개 확인 추천 없음 · 거래일 평가 전'
    confirmed_sessions = None
    if trading_sessions is not None:
        calendar_status = '주입된 거래소 거래일'
        confirmed_sessions = sorted({pd.Timestamp(d).tz_localize(None).normalize() for d in trading_sessions})
    elif cohorts:
        first = min(timestamp(c['observedPublishedAt']).tz_localize(None).normalize() for c in cohorts)
        if first >= cutoff:
            confirmed_sessions = []
            calendar_status = '공개일 이후 가격 없음'
        else:
            try:
                import exchange_calendars as xcals
                calendar = xcals.get_calendar('XKRX')
                confirmed_sessions = [pd.Timestamp(d).tz_localize(None) for d in calendar.sessions_in_range(first, cutoff)]
                calendar_status = 'XKRX 거래 캘린더'
            except Exception:
                calendar_status = '거래소 거래일 확인 대기'
    rows = []
    for cohort in cohorts:
        public_day = timestamp(cohort['observedPublishedAt']).tz_localize(None).normalize()
        sessions = [d for d in (confirmed_sessions or []) if public_day < d <= cutoff]
        members = ledger.get('universes', {}).get(cohort['universeId'], [])
        for record in cohort['records']:
            row = {k: record.get(k) for k in ('ticker', 'name', 'group', 'rank', 'sector')}
            row.update(cohortId=cohort['id'], recommendationDate=cohort['generatedAt'][:10],
                       publishedAt=cohort['observedPublishedAt'], engineVersion=cohort['engineVersion'],
                       ruleVersion=cohort['ruleVersion'], entryDate=None, entryPrice=None,
                       latestDate=str(sessions[-1].date()) if sessions else None,
                       currentReturnPct=None, maxDrawdownPct=None, horizons={})
            if not sessions:
                row['status'] = '거래소 거래일 확인 대기' if confirmed_sessions is None else '다음 거래일 대기'
                rows.append(row)
                continue
            entry_date = sessions[0]
            start, problem = book.quote(record['ticker'], entry_date, 'open')
            row['entryDate'] = str(entry_date.date())
            row['entryPrice'] = start
            if problem:
                row['status'] = '진입 기준 미확정 · ' + problem
                rows.append(row)
                continue
            row['status'] = '추적 중'
            latest, problem = book.quote(record['ticker'], sessions[-1], 'close')
            if latest is not None:
                row['currentReturnPct'] = round((latest/start-1)*100-cost_bps/100, 4)
            else:
                row['status'] = problem
            path, complete_path = [start], True
            for day in sessions:
                close, _ = book.quote(record['ticker'], day, 'close')
                if close is None:
                    complete_path = False
                    break
                path.append(close)
            if complete_path:
                peak, worst = path[0], 0
                for value in path:
                    peak = max(peak, value)
                    worst = min(worst, (value/peak-1)*100)
                row['maxDrawdownPct'] = round(worst, 4)
            for horizon in HORIZONS:
                result = {'status': '기간 미도래', 'returnPct': None, 'excessPct': None, 'exitDate': None}
                if len(sessions) >= horizon:
                    exit_date = sessions[horizon-1]  # Entry session counts as trading day 1.
                    close, problem = book.quote(record['ticker'], exit_date, 'close')
                    result.update(exitDate=str(exit_date.date()), status=problem or '완료')
                    if close is not None:
                        ret = (close/start-1)*100-cost_bps/100
                        result['returnPct'] = round(ret, 4)
                        benchmark = book.benchmark(cohort['universeId'], members, entry_date, exit_date, cost_bps)
                        result['benchmark'] = benchmark
                        if benchmark['returnPct'] is not None:
                            result['excessPct'] = round(ret-benchmark['returnPct'], 4)
                row['horizons'][str(horizon)] = result
            rows.append(row)
    summaries = []
    groups = defaultdict(list)
    for row in rows:
        groups[(row['group'], row['engineVersion'], row['ruleVersion'])].append(row)
    for (group, engine, rule), items in sorted(groups.items()):
        for horizon in HORIZONS:
            ready = [r for r in items if r['horizons'].get(str(horizon), {}).get('returnPct') is not None]
            values = [r['horizons'][str(horizon)]['returnPct'] for r in ready]
            excess = [r['horizons'][str(horizon)]['excessPct'] for r in ready
                      if r['horizons'][str(horizon)]['excessPct'] is not None]
            summaries.append({'group': group, 'engineVersion': engine, 'ruleVersion': rule,
                              'horizon': horizon, 'total': len(items), 'evaluated': len(ready),
                              'waitingOrMissing': len(items)-len(ready),
                              'uniqueStocks': len({r['ticker'] for r in ready}),
                              'recommendationDays': len({r['recommendationDate'] for r in ready}),
                              'meanReturnPct': round(mean(values), 4) if values else None,
                              'medianReturnPct': round(median(values), 4) if values else None,
                              'winRatePct': round(sum(v > 0 for v in values)/len(values)*100, 2) if values else None,
                              'meanExcessPct': round(mean(excess), 4) if excess else None,
                              'excessSampleCount': len(excess)})
    return {'schemaVersion': 1, 'evaluationVersion': VERSION,
            'generatedAt': generated_at,
            'priceDate': str(book.latest.date()) if book.latest is not None else None,
            'status': '공개 확인 추천을 추적 중' if rows else '공개 확인된 추천 기록 대기 · 과거 성과를 소급 생성하지 않음',
            'recordCount': len(rows), 'cohortCount': len(cohorts),
            'archivedCohortCount': len(eligible_cohorts), 'cohortPolicy': '평가창·버전·추천일별 최종 공개 확인본',
            'costBps': cost_bps, 'entryRule': '공개 확인일 다음 실제 거래일 시가; 진입일을 1거래일로 계산',
            'calendarStatus': calendar_status,
            'returnBasis': '검증된 공급자 수정주가 기준 · 배당 포함 여부는 공급자 조정 방식에 따름 · 실현손익 아님',
            'benchmarkRule': '추천 당시 전체시장 동일비중; 구성종목 가격 누락 시 초과수익 미표시',
            'notice': '모든 공개회차를 보존하되 같은 평가창·버전·추천일은 최종 공개 확인본만 집계합니다. 미도래·누락은 성공률 분모에서 제외하고 별도 표시합니다. 같은 종목의 반복 추천은 독립 표본이 아닙니다. 비용은 왕복 합산이며 기본 0bp입니다.',
            'horizons': list(HORIZONS), 'summaries': summaries, 'rows': rows}


def evaluate(ledger, prices, **kwargs):
    from performance_feedback import evaluate_first_recommendations
    return evaluate_first_recommendations(ledger, prices, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ledger', type=Path, default=Path('recommendation-history.json'))
    parser.add_argument('--prices', type=Path, default=Path('test_output/verified_prices.pkl'))
    parser.add_argument('--output', type=Path, default=Path('test_output/recommendation-performance.test.json'))
    parser.add_argument('--cost-bps', type=float, default=0)
    args = parser.parse_args()
    ledger = read_json(args.ledger, empty_ledger())
    if args.prices == Path('test_output/verified_prices.pkl') and Path('test_output/verified_snapshot.json').exists():
        from refresh_store import load_verified_frames
        prices, _, _ = load_verified_frames(Path('test_output'), Path('cache'))
        from performance_prices import collect_performance_prices
        prices, _ = collect_performance_prices(ledger, prices, {'cache_dir': Path('cache')}, reuse=True)
    else:
        prices = pd.read_pickle(args.prices)
    json_write(args.output, evaluate(ledger, prices, cost_bps=args.cost_bps))


if __name__ == '__main__':
    main()
