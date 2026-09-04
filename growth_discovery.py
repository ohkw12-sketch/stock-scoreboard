"""Evidence-led growth discovery. No generated future profits or fair prices.

All scores are screening heuristics, not calibrated probabilities. The event ledger
retains old evidence, while corrections and validity are checked on each live run.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

from dart_fundamentals import _api_key, _request_bytes, _request_json

KST = timezone(timedelta(hours=9))
EVENT_NAMES = re.compile(r'단일판매|공급계약|신규시설투자|영업.*전망|장래사업|기업설명회|투자판단')


def number(value):
    try:
        n = float(str(value).replace(',', '').replace('%', '').strip())
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def day(value):
    match = re.search(r'(20\d{2})[.\-/년 ]*([01]?\d)[.\-/월 ]*([0-3]?\d)', str(value or ''))
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups())).date().isoformat()
    except ValueError:
        return None


def fingerprint(*parts):
    text = '|'.join(re.sub(r'\s+', '', str(p or '')).lower() for p in parts)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]


def json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def document_fields(markup):
    soup = BeautifulSoup(markup, 'html.parser')
    for element in soup(['script', 'style']):
        element.decompose()
    rows = []
    for tr in soup.find_all('tr'):
        cells = [re.sub(r'\s+', ' ', c.get_text(' ', strip=True)) for c in tr.find_all(['td', 'th'], recursive=False)]
        if len(cells) >= 2:
            rows.append(cells)
    return rows, soup.get_text(' ', strip=True)


def parse_disclosure(item, markup, checked_at):
    """Structured primary-source contracts; titles/IR schedules alone are not evidence."""
    rows, text = document_fields(markup)
    def field(pattern):
        matches = [r[-1] for r in rows if any(re.search(pattern, c) for c in r[:-1])]
        return matches[-1] if matches else None
    title = item['report_nm'].strip()
    receipt = item['rcept_no']
    published = day(item['rcept_dt'])
    correction = '정정' in title
    original = day(field(r'정정관련.*제출일|정정대상.*제출일|최초.*공시일'))
    event = {
        'ticker': str(item['stock_code']).zfill(6), 'title': title,
        'source': 'DART', 'sourceType': '공시', 'receipt': receipt,
        'url': f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}',
        'firstPublished': original or published, 'publishedAt': published,
        'lastVerified': checked_at, 'fetchedAt': checked_at, 'correction': correction,
        'status': '검토필요', 'polarity': 'neutral', 'factType': '공시사실',
        'materiality': 0.0, 'kind': '기타', 'activeUntil': None,
    }
    if '단일판매' in title or '공급계약' in title:
        subject = field(r'판매.*공급계약\s*내용|체결계약명')
        customer = field(r'^\s*\d*\.?\s*계약상대방$')
        signed = day(field(r'계약\(수주\)일자|계약.*체결일'))
        start, end = day(field(r'^시작일$')), day(field(r'^종료일$'))
        amount = number(field(r'계약금액.*원|계약금액.*총액|확정\s*계약금액'))
        ratio = number(field(r'매출액\s*대비'))
        if amount is None:
            amount = number(field(r'^계약금액$|^계약금액\(원\)$'))
        revenue = number(field(r'^최근\s*매출액'))
        if ratio is None and amount is not None and revenue and revenue > 0:
            ratio = amount / revenue * 100
        event.update(kind='수주', subject=subject, customer=customer, signedAt=signed,
                     startAt=start, activeUntil=end, amount=amount, revenueRatio=ratio,
                     region=field(r'판매.*공급지역'),
                     recurring='해당' == field(r'동종계약\s*이행여부'))
        # Amendments share the first-publication date; transaction date and subject
        # separate multiple contracts on the same day without counting republications.
        event['eventId'] = fingerprint(event['ticker'], '수주', signed or original or published, subject)
        if '해지' in title or '취소' in title:
            event.update(status='무효', polarity='negative')
        elif subject and signed and end and amount is not None and amount > 0:
            event.update(status='유효' if end >= checked_at[:10] else '상태확인필요', polarity='positive')
            years = max(1.0, (pd.Timestamp(end) - pd.Timestamp(start or signed)).days / 365.25)
            annual_size = (ratio or 0) / years
            event['materiality'] = round(min(100, 25 + 18 * math.log1p(max(0, annual_size))), 2)
            event['sizePerYearProxy'] = round(annual_size, 2)  # scale proxy, NOT a revenue forecast
        event['excerpt'] = ' · '.join(str(x) for x in (subject, customer, end) if x)[:500]
    elif '신규시설투자' in title:
        end = day(field(r'^종료일$'))
        purpose = field(r'투자목적')
        amount = number(field(r'투자금액.*원|^투자금액$'))
        ratio = number(field(r'자기자본.*대비'))
        event.update(kind='증설', subject=purpose, activeUntil=end, amount=amount,
                     polarity='neutral', excerpt=(purpose or '')[:500])
        # Capex alone does not prove demand; keep as a corroboration hint only.
        event['eventId'] = fingerprint(event['ticker'], '증설', original or published, purpose)
    else:
        event['eventId'] = fingerprint(event['ticker'], title, original or published)
        event['kind'] = 'IR' if '기업설명회' in title else '전망·사업'
        event['excerpt'] = text[:500]
    return event


def merge_events(events):
    groups = {}
    for event in sorted(events, key=lambda e: (e.get('publishedAt', ''), e.get('receipt', ''))):
        key = event['eventId']
        prior = groups.get(key)
        if prior:
            first = min(prior['firstPublished'], event['firstPublished'])
            refs = sorted({r for r in prior.get('receipts', [prior.get('receipt')]) + [event.get('receipt')] if r})
            groups[key] = dict(event, firstPublished=first, receipts=refs)
        else:
            groups[key] = dict(event, receipts=[event['receipt']] if event.get('receipt') else [])
    # If a correction/cancellation cannot be matched confidently, quarantine
    # potentially related contracts instead of continuing to count stale positives.
    for event in list(groups.values()):
        if not event.get('correction') and event.get('polarity') != 'negative':
            continue
        for prior in groups.values():
            if prior is event or prior['ticker'] != event['ticker'] or prior['kind'] != event['kind']:
                continue
            same_subject = prior.get('subject') and prior.get('subject') == event.get('subject')
            if same_subject and prior['publishedAt'] <= event['publishedAt']:
                if prior.get('signedAt') == event.get('signedAt') or not event.get('signedAt'):
                    prior['status'] = '정정관계확인필요'
    return list(groups.values())


def collect_disclosures(config, universe, now=None):
    now = now or datetime.now(KST)
    checked = now.isoformat(timespec='seconds')
    cache = Path(config['cache_dir']) / 'growth'
    cache.mkdir(parents=True, exist_ok=True)
    ledger_path = cache / 'event_ledger.json'
    prior = json.loads(ledger_path.read_text('utf-8')) if ledger_path.exists() else []
    status = {'source': 'OpenDART 공시 원문', 'checkedAt': checked, 'status': '정상',
              'requestedTickers': len(universe), 'failures': [], 'documentsFailed': [], 'reviewOnlyCount': 0}
    key = _api_key()
    if not key:
        status.update(status='설정필요', problem='DART_API_KEY 없음')
        return prior, status
    list_cache = cache / 'disclosure_index.json'
    previous_index = json.loads(list_cache.read_text('utf-8')) if list_cache.exists() else {'items': [], 'scannedThrough': None}
    start = now.date() - timedelta(days=int(config.get('growth_history_days', 400)))
    if previous_index.get('scannedThrough'):
        start = max(start, pd.Timestamp(previous_index['scannedThrough']).date() - timedelta(days=7))
    cursor, items = start, list(previous_index['items'])
    while cursor <= now.date():
        end = min(now.date(), cursor + timedelta(days=75))
        for cls in ('Y', 'K'):
            page = 1
            while True:
                params = {'crtfc_key': key, 'bgn_de': cursor.strftime('%Y%m%d'),
                          'end_de': end.strftime('%Y%m%d'), 'pblntf_ty': 'I', 'corp_cls': cls,
                          'page_count': '100', 'page_no': str(page), 'last_reprt_at': 'N'}
                try:
                    payload = _request_json('list.json', params, 3, 0.18)
                    if payload.get('status') == '013':
                        break
                    if payload.get('status') != '000':
                        raise RuntimeError('DART status ' + str(payload.get('status')))
                    items.extend(r for r in payload.get('list', []) if r.get('stock_code') in universe and EVENT_NAMES.search(r.get('report_nm', '')))
                    if page >= int(payload.get('total_page', 1)):
                        break
                    page += 1
                except Exception as exc:
                    status['failures'].append({'period': [str(cursor), str(end)], 'market': cls, 'page': page, 'error': type(exc).__name__})
                    break
        print(f'Growth disclosure scan: {end}', flush=True)
        cursor = end + timedelta(days=1)
    items = list({r['rcept_no']: r for r in items}.values())
    json_write(list_cache, {'items': items, 'scannedThrough': str(now.date()) if not status['failures'] else previous_index.get('scannedThrough')})
    # Review-only IR schedules are indexed, but never counted as positive facts.
    selected = [r for r in items if re.search('단일판매|공급계약|신규시설투자', r['report_nm'])]
    status['reviewOnlyCount'] = len(items) - len(selected)
    docs = cache / 'documents'
    docs.mkdir(exist_ok=True)
    def fetch(item):
        path = docs / (item['rcept_no'] + '.html')
        if path.exists():
            markup = path.read_text('utf-8')
        else:
            last = None
            for attempt in range(3):
                try:
                    payload = _request_bytes('document.xml', {'crtfc_key': key, 'rcept_no': item['rcept_no']})
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        markup = '\n'.join(archive.read(n).decode('utf-8', errors='replace') for n in archive.namelist() if n.lower().endswith(('.xml', '.html')))
                    path.write_text(markup, 'utf-8')
                    break
                except Exception as exc:
                    last = exc
                    time.sleep(0.4 * (attempt + 1))
            else:
                raise RuntimeError(type(last).__name__)
            time.sleep(0.15)
        return parse_disclosure(item, markup, checked)
    parsed = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(fetch, r): r for r in selected}
        for idx, future in enumerate(as_completed(tasks), 1):
            item = tasks[future]
            try:
                parsed.append(future.result())
            except Exception as exc:
                status['documentsFailed'].append({'ticker': item['stock_code'], 'receipt': item['rcept_no'], 'error': type(exc).__name__})
            if idx % 200 == 0:
                print(f'Growth documents: {idx}/{len(selected)}', flush=True)
    # Failed correction retrieval makes the related company's old evidence unsafe.
    failed_tickers = {x['ticker'] for x in status['documentsFailed']}
    events = merge_events(prior + parsed)
    for e in events:
        if e['ticker'] in failed_tickers or status['failures']:
            e['status'] = '상태확인필요'
        elif e.get('activeUntil') and e['activeUntil'] < str(now.date()) and e['status'] == '유효':
            e['status'] = '상태확인필요'
        elif e['status'] == '유효':
            e['lastVerified'] = checked
    status.update(indexedReports=len(items), parsedDocuments=len(parsed), ledgerCount=len(events),
                  evidenceTickers=len({e['ticker'] for e in events if e['status'] == '유효' and e['polarity'] == 'positive'}),
                  historyStart=str(start), scannedThrough=str(now.date()))
    if status['failures'] or status['documentsFailed']:
        status['status'] = '부분수집'
    json_write(ledger_path, events)
    json_write(cache / 'collection_status.json', status)
    return events, status


def credential(name):
    return os.environ.get(name) or (os.name == 'nt' and _windows_credential(name)) or None


def _windows_credential(name):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except OSError:
        return None


def collect_news_hints(config, names):
    """Discovery queue only. Search snippets are not verified business evidence."""
    key, secret = credential('NAVER_CLIENT_ID'), credential('NAVER_CLIENT_SECRET')
    if not key or not secret:
        return [], {'status': '설정필요', 'source': 'NAVER 뉴스', 'problem': '뉴스 검색 키 미설정; 뉴스 근거를 생성하지 않음'}
    hints, failures = [], []
    for name in names:
        query = urllib.parse.urlencode({'query': f'{name} 수주 성장 해외', 'sort': 'date', 'display': 10})
        request = urllib.request.Request('https://openapi.naver.com/v1/search/news.json?' + query,
                                        headers={'X-Naver-Client-Id': key, 'X-Naver-Client-Secret': secret})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
            hints.extend(dict(row, entity=name, status='원문검증대기') for row in result.get('items', []))
        except Exception as exc:
            failures.append({'name': name, 'error': type(exc).__name__})
    json_write(Path(config['cache_dir']) / 'growth/news_queue.json', hints)
    return hints, {'status': '부분수집' if failures else '정상', 'source': 'NAVER 뉴스', 'hints': len(hints), 'failures': failures}


def confidence(events):
    positives = {e['eventId']: e for e in events if e['status'] == '유효' and e['polarity'] == 'positive'}
    negatives = {e['eventId'] for e in events if e['polarity'] == 'negative'}
    n = len(positives)
    sources = len({e.get('sourceType') for e in positives.values()})
    score = max(0, min(100, 100 * n / (n + 3) + max(0, sources - 1) * 3 - len(negatives) * 12))
    return {'evidenceCount': n, 'counterEvidenceCount': len(negatives), 'sourceTypeCount': sources,
            'score': round(score, 1), 'label': '높음' if score >= 65 else '보통' if score >= 40 else '초기'}


class PriceResponse:
    def __init__(self, prices):
        frame = prices.copy().sort_values(['ticker', 'date'])
        if 'Adj Close' in frame:
            adjusted = pd.to_numeric(frame['Adj Close'], errors='coerce')
            frame['close'] = adjusted.where(adjusted.gt(0), frame['close'])
            frame['_adjusted'] = adjusted.gt(0)
            self.adjusted = True
        else:
            self.adjusted = bool(frame.get('adjusted', pd.Series(False)).fillna(False).all())
            frame['_adjusted'] = frame.get('adjusted', False)
        frame['date'] = pd.to_datetime(frame['date']).dt.normalize()
        frame['ret'] = frame.groupby('ticker')['close'].pct_change(fill_method=None)
        self.groups = {k: g.set_index('date') for k, g in frame.groupby('ticker')}
        self.market = frame.groupby('date')['ret'].median().fillna(0)
        self.sectors = frame.groupby(['sector', 'date'])['ret'].median()
        self.latest = frame['date'].max()

    def evaluate(self, ticker, sector, event, adverse=False):
        g = self.groups.get(ticker)
        result = {'label': '판정 불가', 'eventId': event['eventId'], 'basis': '수정가격 기반 상대반응' if self.adjusted else '수정 여부 미확인 가격', 'sourceDate': str(self.latest.date())}
        if g is None or g.index.max() != self.latest:
            return result
        published = pd.Timestamp(event['firstPublished'])
        before = g[g.index < published]
        after = g[g.index >= published]
        if len(before) < 21 or len(after) < 2:
            return result
        window = g.loc[before.index[-21]:]
        if not window['_adjusted'].fillna(False).all():
            result['basis'] = '관찰 구간 수정주가 누락'
            return result
        if window['ret'].abs().gt(.35).any():
            result['basis'] = '기업행동 또는 비정상 가격변동 확인 필요'
            return result
        base = float(before.close.iloc[-1])
        own = (float(after.close.iloc[-1]) / base - 1) * 100
        pre = (base / float(before.close.iloc[-21]) - 1) * 100
        market = (np.prod(1 + self.market.reindex(after.index).fillna(0)) - 1) * 100
        sr = self.sectors.loc[sector] if sector in self.sectors.index.get_level_values(0) else self.market
        sector_ret = (np.prod(1 + sr.reindex(after.index).fillna(0)) - 1) * 100
        peak = (float(after.close.max()) / base - 1) * 100
        residual = own - sector_ret
        result.update(returnPct=round(own, 2), pre20Pct=round(pre, 2), marketExcessPct=round(own-market, 2),
                      sectorExcessPct=round(residual, 2), peakReturnPct=round(peak, 2), observationDays=len(after))
        # Price thresholds are disclosed heuristics. Never infer an intrinsic-value %.
        if not self.adjusted or adverse or (peak >= 25 and own < peak / 2):
            return result
        if pre >= 25 or own >= 35 or residual >= 25:
            result['label'] = '큰 가격 반응'
        elif own <= 10 and residual <= 5 and pre < 15:
            result['label'] = '미반영 가능'
        else:
            result['label'] = '일부 가격 반응'
        return result


def fundamental_profile(row):
    sales, op = number(row.get('sales_current')), number(row.get('op_current'))
    previous = number(row.get('sales_previous'))
    growth = (sales / previous - 1) * 100 if sales is not None and previous and previous > 0 else None
    margin = op / sales * 100 if op is not None and sales and sales > 0 else None
    qs = [number(row.get('normalized_op_q' + str(q))) for q in (3, 4, 1, 2)]
    available = [v for v in qs if v is not None]
    # Profitability and realized earnings persistence, not uncollected debt/CF data.
    quality = None if margin is None else max(0, min(100, 50 + margin * 2))
    if quality is not None and available:
        quality = .6 * quality + .4 * 100 * sum(v > 0 for v in available) / len(available)
    score = (max(-50, min(150, growth)) + 50) / 2 if growth is not None else 0
    return {'growthRate': round(growth, 1) if growth is not None else None,
            'growthBasis': f"공시 누적 매출 전년동기 · {str(row.get('as_of', ''))[:10]}",
            'fundamentalScore': round(quality, 1) if quality is not None else None,
            'fundamentalBasis': '실제 영업이익률·흑자분기 비율; 재무건전성 종합점수 아님',
            '_growthScore': score}


def build_growth_board(prices, fundamentals, events, source_status, now=None,
                       sector_events=None, sector_links=None):
    now = now or datetime.now(KST)
    today = now.date()
    latest = prices.sort_values('date').groupby('ticker').tail(1).set_index('ticker')
    financials = {str(r['ticker']).zfill(6): r for r in fundamentals.to_dict('records')}
    response = PriceResponse(prices)
    grouped = {}
    for event in merge_events(events):
        if pd.Timestamp(event['firstPublished']).date() > today:
            continue
        if event.get('activeUntil') and event['activeUntil'] < str(today) and event['status'] == '유효':
            event = dict(event, status='상태확인필요')
        grouped.setdefault(event['ticker'], []).append(event)
    candidates, audited = [], []
    for ticker, evidence in grouped.items():
        if ticker not in latest.index:
            continue
        stock = latest.loc[ticker]
        positives = [e for e in evidence if e['polarity'] == 'positive' and e['status'] == '유효']
        eligible, reactions = [], []
        recent_negative = any(e['polarity'] == 'negative' and (today - pd.Timestamp(e['publishedAt']).date()).days <= 90 for e in evidence)
        for event in positives:
            reaction = response.evaluate(ticker, stock['sector'], event, recent_negative)
            reactions.append(reaction)
            age = (today - pd.Timestamp(event['firstPublished']).date()).days
            if age <= 90 or reaction['label'] in {'미반영 가능', '일부 가격 반응'}:
                eligible.append(event)
        if not eligible:
            continue
        conf = confidence(eligible + [e for e in evidence if e['polarity'] == 'negative' and (today - pd.Timestamp(e['publishedAt']).date()).days <= 90])
        profile = fundamental_profile(financials.get(ticker, {}))
        anchor = max(eligible, key=lambda e: e.get('materiality', 0))
        reaction = next(r for r in reactions if r['eventId'] == anchor['eventId'])
        materiality = max(e.get('materiality', 0) for e in eligible)
        total = .35 * materiality + .30 * profile.pop('_growthScore') + .25 * (profile['fundamentalScore'] or 0) + .10 * conf['score']
        result = dict(ticker=ticker, name=stock['name'], sector=stock['sector'], **profile,
                      confidence=conf['label'], evidenceCount=conf['evidenceCount'], confidenceScore=conf['score'],
                      priceReflection=reaction['label'], score=round(total, 2),
                      stage='초기 포착' if conf['evidenceCount'] == 1 else '근거 확대',
                      firstPublished=min(e['firstPublished'] for e in eligible), lastVerified=max(e['lastVerified'] for e in eligible),
                      sourceDate=str(response.latest.date()), oldEvidenceCount=sum((today-pd.Timestamp(e['firstPublished']).date()).days > 90 for e in eligible))
        candidates.append(result)
        audited.append(dict(result, events=eligible, priceResponses=reactions, counterEvidence=[e for e in evidence if e['polarity']=='negative']))
    candidates.sort(key=lambda r: (-r['score'], r['ticker']))
    sectors = []
    for name in sorted({r['sector'] for r in candidates}):
        members = [r for r in candidates if r['sector'] == name]
        # Multiple independently contracted firms = common demand signal, not
        # proof that every sector constituent is growing.
        if len(members) < 3 or name == '미분류':
            continue
        selected = members[:3]
        member_ids = {r['ticker'] for r in members}
        sector_conf = confidence([e for a in audited if a['ticker'] in member_ids for e in a['events'] + a['counterEvidence']])
        count = sector_conf['evidenceCount']
        sectors.append({'sector': name, 'confidence': sector_conf['label'], 'confidenceScore': sector_conf['score'],
                        'evidenceCount': count, 'growthCompanyCount': len(members),
                        'score': round(sum(r['score'] for r in selected)/len(selected), 2), 'stocks': selected,
                        'basis': '동일 섹터 복수 기업의 유효 수주 근거; 수출통계 검증과 구별'})
    # Independent sector path: an official industry statistic plus verified
    # product exposure and realized growth in multiple constituent companies.
    # It does not require those companies to have a large-contract disclosure.
    for evidence in sector_events or []:
        if evidence['status'] != '유효' or evidence['publishedAt'] > str(today):
            continue
        name = evidence['sector']
        existing = next((s for s in sectors if s['sector']==name), None)
        if evidence['polarity']=='negative':
            if existing:
                existing['score'] = round(existing['score'] * .75, 2)
                existing['confidenceScore'] = max(0, existing['confidenceScore']-12)
                existing['confidence'] = '높음' if existing['confidenceScore']>=65 else '보통' if existing['confidenceScore']>=40 else '초기'
            continue
        if (today-pd.Timestamp(evidence['firstPublished']).date()).days>90:
            # No fabricated price-underreaction test for unavailable sector history.
            continue
        members = []
        for ticker in (sector_links or {}).get(evidence['eventId'], set()):
            if ticker not in latest.index or latest.loc[ticker]['sector']!=name:
                continue
            stock = latest.loc[ticker]
            if pd.Timestamp(stock['date']) != response.latest:
                continue
            profile = fundamental_profile(financials.get(ticker, {}))
            if profile['growthRate'] is None or profile['growthRate']<=0:
                continue
            quality = profile['fundamentalScore'] or 0
            member_score = .55*profile.pop('_growthScore') + .45*quality
            members.append(dict(ticker=ticker,name=stock['name'],sector=name,**profile,score=round(member_score,2),
                                exposureBasis='상장사 제품 정보 일치 + 실제 매출 성장; 기업별 수출액 미확인'))
        if len(members)<3:
            continue
        members.sort(key=lambda r:(-r['score'],r['ticker']))
        selected = members[:3]
        # Statistic = one independent event, even when it links to many stocks.
        linked_events = [e for a in audited if a['ticker'] in {r['ticker'] for r in members}
                         for e in a['events']]
        conf = confidence([evidence] + linked_events)
        score = .55*evidence['materiality'] + .45*sum(r['score'] for r in selected)/3
        row = dict(sector=name, confidence=conf['label'],confidenceScore=conf['score'],
                   evidenceCount=conf['evidenceCount'],growthCompanyCount=len(members),score=round(score,2),
                   stocks=selected,basis='공식 품목 수출 증가 + 제품 연결 + 복수 기업 실제 매출 성장',
                   exportGrowth=evidence['growthRate'],exportPeriod=evidence['period'],sourceUrl=evidence['url'])
        if existing:
            row['score'] = max(row['score'], existing['score'])
            sectors.remove(existing)
        sectors.append(row)
    sectors.sort(key=lambda r: (-r['score'], r['sector']))
    sector_rows = [dict(r, rank=i+1) for i, r in enumerate(sectors[:5])]
    stock_rows = [dict(r, rank=i+1) for i, r in enumerate(candidates[:10])]
    status = f"성장 섹터 {len(sector_rows)}개 · 개별 성장 {len(stock_rows)}개 · 전체 {len(latest):,}종목 평가 · 가격 {response.latest:%Y-%m-%d}"
    return {'status': status, 'sectors': sector_rows, 'rows': stock_rows,
            'dataStatus': dict(source_status, candidateCount=len(candidates), requestedTickers=len(latest),
                               oldEvidenceUsed=sum(r['oldEvidenceCount'] for r in candidates)),
            'methodVersion': 'growth-evidence-v1', 'updatedKST': now.strftime('%Y-%m-%d %H:%M'),
            'notice': '신뢰도는 독립 근거 충실도이며 성공확률이 아닙니다. 수주·외부 컨센서스·공식 수출통계 사용 범위는 아래에 표시합니다.',
            '_audit': audited}
