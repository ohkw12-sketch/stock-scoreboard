"""Evidence-gated issue diffusion. Reviewed facts enter through cache/issue-input.json.

YouTube is discovery only. Scores are deterministic research heuristics, not
probabilities. No source board, position, or valuation score is modified.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import argparse
import math
import re
from urllib.parse import urlsplit

from refresh_store import read_json, json_write, run_lock

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
SOURCES = ['DART', 'KIND', 'SEC', '기업IR', 'Reuters', '연합뉴스', '증권사 리포트']
WEIGHTS = dict(issue=20, leader=20, spread=20, fundamental=30, entry=10)


def trading_day(now):
    import exchange_calendars as xc
    # An unknown/out-of-range calendar raises: never assume the exchange is open.
    return bool(xc.get_calendar('XKRX').is_session(now.astimezone(KST).date().isoformat()))


def stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def num(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and not isinstance(value, bool) else None
    except (ValueError, TypeError):
        return None


def verified(fact, now):
    published, checked = stamp(fact.get('publishedAt')), stamp(fact.get('verifiedAt'))
    return (fact.get('source') in SOURCES and fact.get('status') == 'verified'
            and fact.get('reviewer') and fact.get('summary')
            and urlsplit(str(fact.get('url', ''))).scheme == 'https'
            and urlsplit(str(fact.get('url', ''))).netloc
            and published and checked and published <= checked <= now
            and now - checked <= timedelta(days=7))


def quote(row, now, overseas=False):
    q = row.get('quote') or {}
    at = stamp(q.get('asOf'))
    age = timedelta(hours=18) if overseas else timedelta(minutes=30)
    valid = (at and timedelta(0) <= now-at <= age and q.get('status') == 'verified'
             and q.get('source') and q.get('url') and q.get('adjustmentChecked') is True
             and all(num(q.get(k)) is not None for k in ('changePct', 'return5dPct', 'turnoverRatio', 'distance20Pct'))
             and q['turnoverRatio'] > 0)
    if not overseas:
        valid = valid and at.astimezone(KST).date() == now.astimezone(KST).date()
    return q if valid else {}


def matching_boards(board, ticker, date):
    matches = []
    for key, label in [('p1', '진입'), ('p11', '순환'), ('p2', '가치')]:
        section = board.get(key, {})
        if section.get('refreshState', {}).get('status') == '실패·이전유지':
            continue
        for r in section.get('rows', []):
            if str(r.get('ticker')) != ticker or r.get('priceDate') != date:
                continue
            passed = (key == 'p1' and r.get('entryState') == '진입가능'
                      or key == 'p11' and r.get('entryFit') == '진입적합' and not r.get('overheated')
                      or key == 'p2' and num(r.get('normalizedPOP')) is not None
                      and 1 <= r['normalizedPOP'] <= 10 and num(r.get('normalizedPremiumPct')) is not None
                      and r['normalizedPremiumPct'] < 0 and r.get('confidence') in ('A', 'B')
                      and (r.get('normalizedQuarterCount') or 0) >= 4)
            if passed:
                matches.append(label)
                break
    return matches


def build(payload, board, now, slot):
    if slot not in ('08:00', '10:30', '15:00'):
        raise ValueError('Unsupported scan slot')
    output = dict(schemaVersion=1, generatedAt=now.isoformat(), sourceDate=str(now.date()), slot=slot,
                  weights=WEIGHTS, issues=[], status='검증완료', missing=payload.get('missing', []))
    captured = stamp(payload.get('asOf'))
    if not captured or not timedelta(0) <= now-captured <= timedelta(minutes=60):
        output.update(status='수집실패', missing=['이번 실행의 이슈 원자료 없음 또는 만료'])
        return output
    seen = set()
    for issue in payload.get('issues', []):
        if not issue.get('id') or issue['id'] in seen:
            continue
        seen.add(issue['id'])
        facts = {f['id']: f for f in issue.get('evidence', []) if f.get('id') and verified(f, now)}
        positive = {k: f for k, f in facts.items() if f.get('polarity') == 'positive'}
        strength = min(20, sum(5 for kind in {'contract', 'policy', 'customer', 'guidance'}
                              if any(f.get('kind') == kind for f in positive.values())))
        leaders = []
        leader_ok = False
        for leader in issue.get('leaders', []):
            q = quote(leader, now, leader.get('market') != 'KR')
            confirmed = bool(q and q['changePct'] >= 2 and q['turnoverRatio'] >= 1.5)
            leader_ok |= confirmed
            hot = bool(q and (q['changePct'] >= 10 or q['return5dPct'] >= 25 or q['distance20Pct'] >= 20))
            leaders.append(dict(name=leader.get('name'), market=leader.get('market'), quote=q,
                                tags=['주도주' if confirmed else '반응 미확인'] + (['급등', '추격주의'] if hot else [])))
        candidates = []
        used = set()
        for row in issue.get('candidates', []):
            ticker = str(row.get('ticker', ''))
            if not re.fullmatch(r'\d{6}', ticker) or ticker in used:
                continue
            used.add(ticker)
            sector = str(row.get('sector', ''))
            if not sector or re.search('건설|바이오|제약|의약|biotech|construction|pharma', sector, re.I):
                continue
            refs = [positive[k] for k in row.get('evidenceIds', []) if k in positive and positive[k].get('ticker') == ticker]
            direct = any(f.get('kind') in ('product', 'customer', 'contract') for f in refs)
            earnings = [f for f in refs if f.get('kind') in ('earnings', 'consensus', 'contract', 'capex')
                        and f.get('period') in ('2026Q3', '2026Q4', '2027Q1', '2027Q2')]
            fundamental = min(30, 10 * len({f['kind'] for f in earnings}))
            q = quote(row, now) if slot != '08:00' else {}
            confirmed = bool(q and q['changePct'] > 0 and q['turnoverRatio'] >= 1.5)
            hot = bool(q and (q['changePct'] >= 10 or q['return5dPct'] >= 25 or q['distance20Pct'] >= 20))
            candidates.append(dict(ticker=ticker, name=row.get('name', ticker), sector=sector,
                direct=direct, fundamental=fundamental, quote=q, confirmed=confirmed, hot=hot,
                evidence=refs, earningsPeriods=sorted({f['period'] for f in earnings})))
        breadth = sum(r['confirmed'] and r['direct'] for r in candidates)
        spread = min(20, breadth * 5) if breadth >= 2 else 0
        for r in candidates:
            entry = (0 if r['hot'] else 10 if r['confirmed'] and r['quote']['changePct'] <= 5 else 5) if r['quote'] else 0
            scores = dict(issue=strength, leader=20 if leader_ok else 0, spread=spread,
                          fundamental=r['fundamental'], entry=entry)
            score = sum(scores.values())
            stage = '탐색'
            if slot != '08:00' and leader_ok and r['confirmed']:
                stage = '확인'
                if breadth >= 2:
                    stage = '확산'
                    if slot == '15:00' and r['direct'] and r['fundamental'] >= 20 and score >= 75 and entry == 10:
                        stage = '최종선정'
            matches = matching_boards(board, r['ticker'], str(now.date())) if stage == '최종선정' else []
            tags = (['CORE'] if len(matches) >= 2 else ['DOUBLE'] if matches else [])
            if r['hot']:
                tags += ['WATCH', '추격주의']
            elif stage != '최종선정':
                tags += ['WATCH']
            r.update(scores=scores, score=score, stage=stage, tags=tags, matchedBoards=matches,
                     priceStatus='확인' if r['confirmed'] else '미확인',
                     overheat='높음' if r['hot'] else '보통' if r['quote'] else '미확인')
        candidates.sort(key=lambda r: (r['stage'] != '최종선정', r['hot'], -r['score'], r['ticker']))
        output['issues'].append(dict(id=issue['id'], name=issue.get('name', issue['id']), strength=strength*5,
            marketGrade='A' if breadth >= 3 else 'B' if breadth >= 2 else '미확인',
            fundamentalGrade='A' if any(r['fundamental'] >= 20 for r in candidates) else '미확인',
            spreadType='섹터확산' if breadth >= 2 else '개별반응·미확인', leaders=leaders, candidates=candidates,
            evidence=sorted(facts.values(), key=lambda f: SOURCES.index(f['source'])),
            detection=issue.get('detection', [])))
    output['issues'].sort(key=lambda i: (-i['strength'], i['id']))
    output['issues'] = output['issues'][:3]
    if not output['issues']:
        output['status'] = '확인된 이슈 없음'
    return output


def refresh(board=None, now=None, slot=None, root=ROOT):
    now = (now or datetime.now(KST)).astimezone(KST)
    if not trading_day(now):
        return None  # No reads of market inputs, writes, analyses, or notifications.
    slot = slot or ('08:00' if now.hour < 10 else '10:30' if now.hour < 15 else '15:00')
    try:
        result = build(read_json(root/'cache/issue-input.json', {}), board or read_json(root/'data.json', {}), now, slot)
    except (ValueError, TypeError, KeyError, AttributeError):
        result = dict(status='수집실패', issues=[], missing=['원자료 형식 검증 실패'], generatedAt=now.isoformat())
    previous = read_json(root/'issue-spread.json', {})
    if result['status'] == '수집실패' and previous.get('issues'):
        result = {**previous, 'status': '갱신 실패·이전 자료 유지', 'attemptedAt': now.isoformat(), 'missing': result['missing']}
    json_write(root/'test_output/issue-spread.test.json', result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--slot', choices=['08:00', '10:30', '15:00'])
    parser.add_argument('--promote', action='store_true')
    args = parser.parse_args()
    if not trading_day(datetime.now(KST)):
        return
    with run_lock(ROOT/'cache'):
        result = refresh(slot=args.slot)
        if result is not None and args.promote:
            json_write(ROOT/'issue-spread.json', result)


if __name__ == '__main__':
    main()
