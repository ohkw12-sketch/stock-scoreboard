"""First-recommendation close returns and auditable, lagged pattern ranking."""
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from statistics import mean, median
import math
import re
import pandas as pd

LABELS = {'p1': '진입', 'p11': '순환', 'p2': '가치', 'growth': '성장', 'combined': '종합'}
HORIZONS = (1, 5, 20, 60, 120)


def observed(c):
    return c.get('recordedAt') or c['observedPublishedAt']


def features(section_rows):
    tokens = {LABELS[k] for k in section_rows if k in LABELS and k != 'combined'}
    for key, row in section_rows.items():
        if key == 'combined':
            continue
        if key == 'p1' and row.get('entryState'):
            tokens.add('진입:' + row['entryState'])
        if key == 'p11':
            stage = re.search(r'[①②③④⑤⑥][가-힣]+|X조기이탈|X종료', str(row.get('marketState', row.get('stage', ''))))
            if stage:
                tokens.add('순환:' + stage.group())
            if row.get('relation'):
                tokens.add('관계:' + row['relation'])
        if key == 'p2':
            pop = row.get('normalizedPOP')
            if isinstance(pop, (int, float)) and math.isfinite(pop):
                tokens.add('가치:' + ('1배미만' if pop < 1 else '1~5배' if pop <= 5 else '5~10배' if pop <= 10 else '10배초과'))
            discount = row.get('normalizedPremiumPct')
            if isinstance(discount, (int, float)) and math.isfinite(discount):
                tokens.add('가치:섹터할인' if discount < 0 else '가치:섹터프리미엄')
        if key == 'growth' and row.get('priceReflection'):
            tokens.add('성장:' + row['priceReflection'])
    return sorted(tokens)


def evaluate_first_recommendations(ledger, prices, *, generated_at=None, cost_bps=0, trading_sessions=None):
    from recommendation_performance import KST, PriceBook, timestamp
    if cost_bps < 0:
        raise ValueError('비용은 음수일 수 없습니다.')
    generated_at = generated_at or datetime.now(KST).isoformat(timespec='seconds')
    asof = timestamp(generated_at)
    cutoff = asof.tz_localize(None).normalize()
    if (asof.hour, asof.minute) < (15, 40):
        cutoff -= pd.Timedelta(days=1)
    cohorts = sorted([c for c in ledger.get('cohorts', []) if timestamp(observed(c)) <= asof], key=lambda c: timestamp(observed(c)))
    usable = prices[pd.to_datetime(prices.date).dt.normalize() <= cutoff] if not prices.empty else prices
    book = PriceBook(usable)
    sessions = None
    calendar_status = '거래소 거래일 확인 대기'
    if trading_sessions is not None:
        sessions = sorted({pd.Timestamp(d).tz_localize(None).normalize() for d in trading_sessions if pd.Timestamp(d).tz_localize(None).normalize() <= cutoff})
        calendar_status = '주입된 거래소 거래일'
    elif cohorts:
        try:
            import exchange_calendars as xcals
            start = min(timestamp(observed(c)).tz_localize(None).normalize() for c in cohorts) - pd.Timedelta(days=10)
            sessions = [pd.Timestamp(d).tz_localize(None) for d in xcals.get_calendar('XKRX').sessions_in_range(start, cutoff)]
            calendar_status = 'XKRX 거래 캘린더'
        except Exception:
            pass
    # Freeze the first occurrence in each project, regardless of later rank/version changes.
    first, latest_sections = {}, {}
    for c in cohorts:
        latest_sections[c['section']] = c
        for r in c['records']:
            key = (c['section'], r['ticker'])
            if key in first:
                continue
            # Only contemporaneously saved recommendations can define an overlap.
            related = {}
            for section, prior in latest_sections.items():
                if prior.get('sourceDate') != c.get('sourceDate'):
                    continue
                hit = next((x for x in prior['records'] if x['ticker'] == r['ticker']), None)
                if hit:
                    related[section] = hit.get('sourceRow', {})
            first[key] = (c, r, related)
    # Same-timestamp section batches are simultaneous, independent of serialization order.
    for key, (c, r, related) in first.items():
        for other in cohorts:
            if observed(other) == observed(c) and other.get('sourceDate') == c.get('sourceDate'):
                hit = next((x for x in other['records'] if x['ticker'] == r['ticker']), None)
                if hit:
                    related[other['section']] = hit.get('sourceRow', {})
    rows = []
    for (section, ticker), (c, r, related) in first.items():
        day = timestamp(observed(c)).tz_localize(None).normalize()
        # Weekend recommendations use the next session close, explicitly shown in entryDate.
        future = [d for d in (sessions or []) if d >= day]
        entry = future[0] if future else None
        row = dict(ticker=ticker, name=r['name'], group=LABELS[section], section=section,
                   rank=r.get('rank') or r.get('sourceRow',{}).get('typeRank'), sector=r.get('sector'), cohortId=c['id'],
                   recommendationDate=str(day.date()), publishedAt=c.get('observedPublishedAt'),
                   recordBasis=c.get('recordBasis', '사이트 공개 확인'), sourceCommit=c.get('sourceCommit'),
                   engineVersion=c['engineVersion'], ruleVersion=c['ruleVersion'],
                   features=features(related), sourceRow=r.get('sourceRow', {}),
                   entryDate=str(entry.date()) if entry is not None else None, entryPrice=None,
                   latestDate=str(sessions[-1].date()) if sessions else None,
                   currentReturnPct=None, maxDrawdownPct=None, horizons={}, status='추천일 종가 대기')
        start, problem = book.quote(ticker, entry, 'close') if entry is not None else (None, calendar_status)
        row['entryPrice'] = start
        if start is None:
            row['status'] = problem
            rows.append(row)
            continue
        row['status'] = '추적 중'
        row['elapsedSessions'] = len(future)-1
        end, problem = book.quote(ticker, future[-1], 'close')
        if end is not None:
            row['currentReturnPct'] = round((end/start-1)*100-cost_bps/100, 4)
        else:
            row['status'] = problem
        path = [book.quote(ticker, d, 'close')[0] for d in future]
        if all(v is not None for v in path):
            peak, worst = start, 0
            for value in path:
                peak = max(peak, value)
                worst = min(worst, (value/peak-1)*100)
            row['maxDrawdownPct'] = round(worst, 4)
        for h in HORIZONS:
            result = dict(status='기간 미도래', returnPct=None, excessPct=None, exitDate=None)
            if len(future) > h:
                date = future[h]
                close, problem = book.quote(ticker, date, 'close')
                result.update(exitDate=str(date.date()), status=problem or '완료')
                if close is not None:
                    result['returnPct'] = round((close/start-1)*100-cost_bps/100, 4)
            row['horizons'][str(h)] = result
        rows.append(row)
    rows.sort(key=lambda r: (-(r['currentReturnPct'] if r['currentReturnPct'] is not None else -1e9), r['ticker'], r['section']))
    summaries = []
    for group in LABELS.values():
        subset = [r for r in rows if r['group'] == group]
        if not subset:
            continue
        for h in HORIZONS:
            ready = [r for r in subset if r['horizons'].get(str(h), {}).get('returnPct') is not None]
            vals = [r['horizons'][str(h)]['returnPct'] for r in ready]
            summaries.append(dict(group=group, horizon=h, total=len(subset), evaluated=len(vals),
                uniqueStocks=len(ready), waitingOrMissing=len(subset)-len(vals),
                meanReturnPct=round(mean(vals),4) if vals else None, medianReturnPct=round(median(vals),4) if vals else None,
                winRatePct=round(sum(v>0 for v in vals)/len(vals)*100,2) if vals else None))
    return dict(schemaVersion=1, evaluationVersion='performance-2.0', generatedAt=generated_at,
        priceDate=str(book.latest.date()) if book.latest is not None else None, recordCount=len(rows),
        cohortCount=len(cohorts), rows=rows, summaries=summaries, horizons=list(HORIZONS), costBps=cost_bps,
        status='프로젝트별 최초 추천일부터 추적', calendarStatus=calendar_status,
        entryRule='프로젝트별 최초 추천일 종가 고정 · 휴장일 추천은 다음 거래일 종가 · N거래일 후 종가 비교',
        returnBasis='공급자 수정종가 기준 · 실제 매매 수익률 아님',
        benchmarkRule='과거 전체시장 구성 검증 전 시장 대비 성과는 미표시',
        notice='과거 저장본은 Git 저장일 기준 복원이며 실제 사이트 공개시각은 미확인입니다. 최초 기록과 당시 조건을 고정하고 반복 추천으로 시작일을 바꾸지 않습니다.')


def learn_patterns(performance, *, cutoff_date):
    # Outcomes must have ended strictly before the current recommendation price date.
    eligible = [r for r in performance.get('rows', []) if r.get('section') != 'combined']
    chosen, samples = None, []
    for horizon in (20, 5, 1):
        unique = {}
        for r in eligible:
            result = r.get('horizons', {}).get(str(horizon), {})
            if result.get('returnPct') is None or not result.get('exitDate') or result['exitDate'] >= cutoff_date:
                continue
            key = (r['ticker'], r['recommendationDate'])
            # A multi-project recommendation is one observation, never multiple wins.
            if key not in unique:
                unique[key] = {**r, 'features': list(r.get('features', [])), 'outcome': result['returnPct']}
            else:
                unique[key]['features'] = sorted(set(unique[key]['features']) | set(r.get('features', [])))
        if len(unique) >= 3:
            chosen, samples = horizon, list(unique.values())
            break
    buckets = defaultdict(list)
    for row in samples:
        tokens = row['features']
        for size in (1, 2, 3):
            for pattern in combinations(sorted(tokens), size):
                if any(token.split(':')[0] in pattern for token in pattern if ':' in token):
                    continue
                buckets[pattern].append(row)
    patterns = []
    for pattern, items in buckets.items():
        # Equal weight per ticker, including when first recommendation dates differ across projects.
        by_ticker = defaultdict(list)
        for row in items:
            by_ticker[row['ticker']].append(row['outcome'])
        values = [mean(v) for v in by_ticker.values()]
        if len(values) < 3:
            continue
        days = len({r['recommendationDate'] for r in items})
        score = median(values) * len(values)/(len(values)+10)
        patterns.append(dict(pattern=list(pattern), label=' + '.join(t.replace(':',' ') for t in pattern), horizon=chosen,
            sampleCount=len(values), recommendationDays=days, meanReturnPct=round(mean(values),3),
            medianReturnPct=round(median(values),3), winRatePct=round(sum(v>0 for v in values)/len(values)*100,2),
            score=round(score,4), status='초기 관찰' if len(values)<10 or days<3 else '누적 관찰',
            archiveSampleCount=len({r['ticker'] for r in items if r.get('recordBasis') != '사이트 공개 확인'})))
    patterns.sort(key=lambda p: (-p['score'], -p['sampleCount'], p['label']))
    for i, p in enumerate(patterns, 1):
        p['rank'] = i
    return dict(version='feedback-1.0', cutoffDate=cutoff_date, horizon=chosen, sampleCount=len(samples),
        patterns=patterns, status='과거 성과 기반 초기 순위' if patterns else '완료된 비교 표본 부족',
        notice='동일 기간 완료 성과만 사용 · 현재 추천 가격일 이전 결과만 반영 · 종목 중복 가중 방지 · 소표본 중앙값 축소 · 과거 규칙·저장본 포함, 별도 미래 검증 전 · 상승확률/예상수익률 아님')


def rank_recent(board, performance, *, source_date, generated_at, snapshot_id):
    model = learn_patterns(performance, cutoff_date=source_date)
    members = defaultdict(dict)
    for key in ('p1','p11','p2','growth'):
        section = board.get(key,{})
        if section.get('refreshState',{}).get('status') == '실패·이전유지':
            continue
        date = section.get('refreshState',{}).get('sourceCutoff') or section.get('sourceDate')
        if date and date != source_date:
            continue
        rows = list(section.get('rows',[]))
        if key == 'growth':
            rows += [r for s in section.get('sectors',[]) for r in s.get('stocks',[])]
        for r in rows:
            if r.get('ticker'):
                members[r['ticker']][key] = r
    candidates = []
    for ticker, related in members.items():
        tokens = features(related)
        matches = [p for p in model['patterns'] if p['score'] > 0 and p['meanReturnPct'] > 0 and set(p['pattern']).issubset(tokens)]
        # One best supported pattern per candidate: overlapping patterns cannot stack points.
        best = matches[0] if matches else None
        row = next(iter(related.values()))
        entry = related.get('p1',{})
        candidates.append(dict(ticker=ticker, name=row['name'], sector=row.get('sector'),
            currentProjectRank=mean([float(r.get('rank') or r.get('typeRank') or 999) for r in related.values()]),
            conditions=[LABELS[k] for k in related], condition=' + '.join(LABELS[k] for k in related),
            entryState=entry.get('entryState','진입 미충족'),
            currentPrice=entry.get('currentPrice') or row.get('currentPrice'),
            combinedScore=best['score'] if best else None, matchedPattern=best,
            features=tokens, sourceDate=source_date, valueScore=related.get('p2',{}).get('valueScore'),
            growthScore=related.get('growth',{}).get('score'),
            sectorRelation=related.get('p11',{}).get('relation','미확인')))
    candidates.sort(key=lambda r: (r['combinedScore'] is None, -(r['combinedScore'] or 0), -len(r['conditions']), r['currentProjectRank'], r['ticker']))
    ranked = [r for r in candidates if r['combinedScore'] is not None]
    for i,r in enumerate(ranked,1):
        r['rank']=i
    return dict(schemaVersion=1, ruleVersion='combined-feedback-2.0', snapshotId=snapshot_id,
        generatedAt=generated_at, sourceDate=source_date, status=model['status'], publicationState='preview',
        candidateCount=len(candidates), matchedCandidateCount=len(ranked), rows=ranked[:10], feedback=model,
        notice='과거 평균·중앙 성과가 모두 양수인 공통조건만 추천 · 동점은 프로젝트 중복 수와 현재 순위 · 진입은 필수가 아니며 진입구분 별도 표시 · '+model['notice'])
