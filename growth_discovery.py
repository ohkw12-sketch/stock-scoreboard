"""Evidence-led growth discovery. No generated future profits or fair prices.

All scores are screening heuristics, not calibrated probabilities. The event ledger
retains old evidence, while corrections and validity are checked on each live run.
"""
from __future__ import annotations

import copy
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
DISCLOSURE_PARSER_VERSION = 'dart-growth-v3'


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


def read_json(path, default):
    path = Path(path)
    return json.loads(path.read_text('utf-8-sig')) if path.exists() else copy.deepcopy(default)


def cached_source_status(previous, now, available, reason='명시적 원자료 재사용'):
    """Reading a cache is not a new verification of the source."""
    result = dict(previous, cacheReadAt=now.isoformat(timespec='seconds'), cacheReason=reason,
                  networkRequests=0, reused=True)
    result['sourceStatus'] = previous.get('status', '미수집')
    if not available:
        result.update(status='미수집', problem='재사용할 검증 원자료 없음')
    elif previous.get('status') in {'정상', '캐시유지'}:
        result['status'] = '캐시유지'
    return result


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
        'firstPublished': original or published, 'publishedAt': published, 'originalPublished': original,
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
        event['eventId'] = fingerprint(event['ticker'], '수주', signed or original or published, subject, customer)
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
            same_customer = (not prior.get('customer') or not event.get('customer')
                             or prior.get('customer') == event.get('customer'))
            original_link = event.get('originalPublished') == prior.get('firstPublished')
            if same_subject and prior['publishedAt'] <= event['publishedAt']:
                if original_link or (same_customer and (prior.get('signedAt') == event.get('signedAt') or not event.get('signedAt'))):
                    prior['status'] = '정정관계확인필요'
    # A supplied original-source link can identify the same business event across
    # a filing, IR document and news article. Cancellation of that event wins over
    # an older positive republication, without treating other contracts as void.
    first_dates = {}
    for event in groups.values():
        key = event.get('independentEventId', event['eventId'])
        first_dates[key] = min(first_dates.get(key, event['firstPublished']), event['firstPublished'])
    negatives = {}
    for event in groups.values():
        if event.get('polarity') == 'negative':
            key = event.get('independentEventId', event['eventId'])
            negatives[key] = max(negatives.get(key, ''), event.get('publishedAt', ''))
    for event in groups.values():
        key = event.get('independentEventId', event['eventId'])
        event['firstPublished'] = first_dates[key]
        if event.get('polarity') == 'positive' and negatives.get(key, '') >= event.get('publishedAt', ''):
            event['status'] = '무효'
    return list(groups.values())


def collect_disclosures(config, universe, now=None, *, reuse=False):
    now = now or datetime.now(KST)
    checked = now.isoformat(timespec='seconds')
    cache = Path(config['cache_dir']) / 'growth'
    ledger_path = cache / 'event_ledger.json'
    status_path = cache / 'collection_status.json'
    prior = read_json(ledger_path, [])
    old_status = read_json(status_path, {'source': 'OpenDART 공시 원문', 'status': '미수집'})
    universe = {str(t).zfill(6) for t in universe}
    if reuse:
        return prior, cached_source_status(old_status, now, ledger_path.exists())
    cache.mkdir(parents=True, exist_ok=True)
    status = {'source': 'OpenDART 공시 원문', 'checkedAt': checked, 'status': '정상',
              'requestedTickers': len(universe), 'requestedTickerSet': sorted(universe),
              'universeVersion': fingerprint(*sorted(universe)),
              'failures': [], 'documentsFailed': [], 'reviewOnlyCount': 0,
              'documentsDownloaded': 0, 'documentsReparsed': 0, 'parsedCacheHits': 0}
    key = _api_key()
    if not key:
        status.update(status='설정필요', problem='DART_API_KEY 없음', completedTickers=0,
                      missingTickers=sorted(universe), checkedAt=old_status.get('checkedAt'), attemptedAt=checked)
        return prior, status
    list_cache = cache / 'disclosure_index.json'
    previous_index = read_json(list_cache, {'items': [], 'scannedThrough': None})
    history_start = now.date() - timedelta(days=int(config.get('growth_history_days', 400)))
    covered = set(previous_index.get('historyCoveredTickers', []))
    # Legacy indexes did not record their requested universe: migrate with one
    # market-wide history scan rather than claiming their old coverage was known.
    initial = not previous_index.get('coverageVersion')
    new_tickers = universe - covered
    start = history_start
    if not initial and previous_index.get('scannedThrough'):
        start = max(start, pd.Timestamp(previous_index['scannedThrough']).date() - timedelta(days=7))
    items = list(previous_index.get('items', []))
    market_by_ticker = dict(previous_index.get('marketByTicker', {}))
    market_by_ticker.update({r['stock_code']: r['corp_cls'] for r in items if r.get('corp_cls') in {'Y', 'K'}})
    market_by_ticker.update(config.get('growth_market_by_ticker', {}))

    def scan(begin, finish, cls=None, ticker=None, corp_code=None):
        cursor = begin
        success = True
        while cursor <= finish:
            end = min(finish, cursor + timedelta(days=75))
            page = 1
            while True:
                params = {'crtfc_key': key, 'bgn_de': cursor.strftime('%Y%m%d'),
                          'end_de': end.strftime('%Y%m%d'), 'pblntf_ty': 'I',
                          'page_count': '100', 'page_no': str(page), 'last_reprt_at': 'N'}
                params.update({'corp_code': corp_code} if corp_code else {'corp_cls': cls})
                try:
                    payload = _request_json('list.json', params, 3, 0.18)
                    if payload.get('status') == '013':
                        break
                    if payload.get('status') != '000':
                        raise RuntimeError('DART status ' + str(payload.get('status')))
                    found = [r for r in payload.get('list', []) if r.get('stock_code') in universe
                             and (ticker is None or r.get('stock_code') == ticker)
                             and EVENT_NAMES.search(r.get('report_nm', ''))]
                    items.extend(found)
                    market_by_ticker.update({r['stock_code']: r.get('corp_cls', cls) for r in found
                                             if r.get('corp_cls', cls) in {'Y', 'K'}})
                    if page >= int(payload.get('total_page', 1)):
                        break
                    page += 1
                except Exception as exc:
                    status['failures'].append({'period': [str(cursor), str(end)], 'market': cls,
                                              'ticker': ticker, 'page': page, 'error': type(exc).__name__})
                    success = False
                    break
            cursor = end + timedelta(days=1)
        return success

    market_success = {cls: scan(start, now.date(), cls=cls) for cls in ('Y', 'K')}
    failed_scope = {t for t in universe if not market_success.get(market_by_ticker.get(t), all(market_success.values()))}
    if initial:
        covered.update(universe - failed_scope)
    elif new_tickers:
        corp_path = Path(config['cache_dir']) / 'dart_corp_codes.csv'
        corp_frame = pd.read_csv(corp_path, dtype=str) if corp_path.exists() else pd.DataFrame()
        corp_codes = dict(zip(corp_frame.get('ticker', []), corp_frame.get('corp_code', [])))
        for ticker in sorted(new_tickers):
            corp_code = corp_codes.get(ticker)
            if not corp_code:
                status['failures'].append({'ticker': ticker, 'period': [str(history_start), str(start)],
                                          'error': 'DART 법인코드 매핑 없음; 신규 종목 과거 공시 미검증'})
                failed_scope.add(ticker)
            elif scan(history_start, start - timedelta(days=1), ticker=ticker, corp_code=corp_code):
                covered.add(ticker)
            else:
                failed_scope.add(ticker)
    status['backfillTickers'] = sorted(new_tickers)
    for failure in status['failures']:
        if failure.get('ticker'):
            failed_scope.add(failure['ticker'])
    items = list({r['rcept_no']: r for r in items}.values())
    through = str(now.date()) if all(market_success.values()) else previous_index.get('scannedThrough')
    json_write(list_cache, {'items': items, 'scannedThrough': through, 'coverageVersion': 1,
                           'historyCoveredTickers': sorted(covered), 'marketByTicker': market_by_ticker,
                           'historyStart': str(history_start), 'universeVersion': status['universeVersion']})
    # Review-only IR schedules are indexed, but never counted as positive facts.
    relevant_items = [r for r in items if r.get('stock_code') in universe]
    selected = [r for r in relevant_items if re.search('단일판매|공급계약|신규시설투자', r['report_nm'])]
    status['reviewOnlyCount'] = len(relevant_items) - len(selected)
    docs = cache / 'documents'
    docs.mkdir(exist_ok=True)
    parsed_dir = cache / 'parsed_documents'
    parsed_dir.mkdir(exist_ok=True)
    prior_fetched = {receipt: e.get('fetchedAt') for e in prior
                     for receipt in e.get('receipts', [e.get('receipt')]) if receipt}
    def fetch(item):
        path = docs / (item['rcept_no'] + '.html')
        downloaded = not path.exists()
        if not downloaded:
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
        raw_hash = hashlib.sha256(markup.encode('utf-8')).hexdigest()
        parsed_path = parsed_dir / (item['rcept_no'] + '.json')
        saved = read_json(parsed_path, {})
        item_hash = fingerprint(item['report_nm'], item['rcept_dt'], item['stock_code'])
        hit = (saved.get('rawHash') == raw_hash and saved.get('parserVersion') == DISCLOSURE_PARSER_VERSION
               and saved.get('itemHash') == item_hash)
        if hit:
            parsed_event = saved['event']
        else:
            parsed_event = parse_disclosure(item, markup, checked)
            parsed_event['fetchedAt'] = (checked if downloaded else
                                        saved.get('event', {}).get('fetchedAt') or prior_fetched.get(item['rcept_no']))
            parsed_event['parsedAt'] = checked
            json_write(parsed_path, dict(rawHash=raw_hash, parserVersion=DISCLOSURE_PARSER_VERSION,
                                         itemHash=item_hash, event=parsed_event))
        return parsed_event, downloaded, hit
    parsed = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(fetch, r): r for r in selected}
        for idx, future in enumerate(as_completed(tasks), 1):
            item = tasks[future]
            try:
                parsed_event, downloaded, hit = future.result()
                parsed.append(parsed_event)
                status['documentsDownloaded'] += int(downloaded)
                status['parsedCacheHits'] += int(hit)
                status['documentsReparsed'] += int(not hit)
            except Exception as exc:
                status['documentsFailed'].append({'ticker': item['stock_code'], 'receipt': item['rcept_no'],
                                                  'correction': '정정' in item['report_nm'] or '해지' in item['report_nm'] or '취소' in item['report_nm'],
                                                  'error': type(exc).__name__})
            if idx % 200 == 0:
                print(f'Growth documents: {idx}/{len(selected)}', flush=True)
    # Failed correction retrieval makes the related company's old evidence unsafe.
    failed_tickers = {x['ticker'] for x in status['documentsFailed']}
    unsafe_tickers = failed_scope | {x['ticker'] for x in status['documentsFailed'] if x['correction']}
    # Reparsed receipts replace old representations even after a parser upgrade.
    parsed_receipts = {e['receipt'] for e in parsed}
    events = merge_events([e for e in prior if e.get('receipt') not in parsed_receipts] + parsed)
    for e in events:
        if e['ticker'] in unsafe_tickers:
            e['previousVerifiedStatus'] = e.get('previousVerifiedStatus', e['status'])
            e['status'] = '상태확인필요'
            e['verificationIssue'] = '관련 정정 원문 또는 해당 공시 검색 범위 수집 미완료'
        elif e.get('activeUntil') and e['activeUntil'] < str(now.date()) and e['status'] == '유효':
            e['status'] = '상태확인필요'
        elif e['status'] == '유효' and e['ticker'] in universe:
            e['lastVerified'] = checked
            e.pop('verificationIssue', None)
    status.update(indexedReports=len(relevant_items), retainedIndexReports=len(items),
                  parsedDocuments=len(parsed), ledgerCount=len(events),
                  evidenceTickers=len({e['ticker'] for e in events if e['status'] == '유효' and e['polarity'] == 'positive'}),
                  historyStart=str(history_start), incrementalStart=str(start), scannedThrough=through,
                  completedTickers=len(universe - failed_scope - failed_tickers - (universe - covered)),
                  completedTickerSet=sorted(universe - failed_scope - failed_tickers - (universe - covered)),
                  missingTickers=sorted(failed_scope | failed_tickers | (universe - covered)))
    if status['failures'] or status['documentsFailed']:
        status['status'] = '부분수집'
    json_write(ledger_path, events)
    json_write(status_path, status)
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


def collect_news_hints(config, names, now=None, *, reuse=False):
    """Discovery queue only. Search snippets are not verified business evidence."""
    now = now or datetime.now(KST)
    checked = now.isoformat(timespec='seconds')
    cache = Path(config['cache_dir']) / 'growth'
    queue_path, status_path = cache / 'news_queue.json', cache / 'news_status.json'
    state_path = cache / 'news_search_state.json'
    prior = read_json(queue_path, [])
    previous_status = read_json(status_path, {'status': '미수집', 'source': 'NAVER 뉴스'})
    if reuse:
        return prior, cached_source_status(previous_status, now, queue_path.exists())
    key, secret = credential('NAVER_CLIENT_ID'), credential('NAVER_CLIENT_SECRET')
    if not key or not secret:
        return prior, {'status': '설정필요', 'source': 'NAVER 뉴스', 'checkedAt': previous_status.get('checkedAt'),
                       'attemptedAt': checked, 'hints': len(prior),
                       'problem': '뉴스 검색 키 미설정; 기존 검증대기 큐 보존, 신규 뉴스 근거 없음'}
    state = read_json(state_path, {})
    cache_hours = float(config.get('growth_news_cache_hours', 12))
    max_pages = max(1, min(10, int(config.get('growth_news_max_pages', 3))))
    page_size = 100
    queue = {}
    def add(row, entity=None):
        url = row.get('originallink') or row.get('link') or row.get('canonicalUrl') or ''
        parsed_url = urllib.parse.urlsplit(url)
        # Preserve meaningful query parameters (many publishers identify articles
        # there), but remove tracking tags. Prefer publisher URL over Naver mirror.
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_')]
        canonical = urllib.parse.urlunsplit((parsed_url.scheme.lower(), parsed_url.netloc.lower(),
                                            parsed_url.path, urllib.parse.urlencode(sorted(query)), ''))
        identity = fingerprint(canonical) if canonical else fingerprint(row.get('title'), row.get('pubDate'))
        old = queue.get(identity, {})
        entities = set(old.get('entities', [])) | set(row.get('entities', []))
        if row.get('entity'):
            entities.add(row['entity'])
        if entity:
            entities.add(entity)
        merged = dict(old)
        merged.update(row)
        merged.update(hintId=identity, canonicalUrl=canonical, entities=sorted(entities))
        merged['status'] = old.get('status', row.get('status', '원문검증대기'))
        merged['firstSeenAt'] = old.get('firstSeenAt', row.get('firstSeenAt', checked))
        merged['lastSeenAt'] = checked if entity else row.get('lastSeenAt', checked)
        queue[identity] = merged
    for row in prior:
        add(row)
    failures, attempted, skipped = [], 0, 0
    for name in sorted(set(names)):
        previous = state.get(name, {})
        last = pd.to_datetime(previous.get('checkedAt'), errors='coerce', utc=True)
        if pd.notna(last) and (now - last.to_pydatetime()).total_seconds() < cache_hours * 3600:
            skipped += 1
            continue
        attempted += 1
        cutoff = pd.to_datetime(previous.get('newestPublishedAt'), errors='coerce', utc=True)
        pending_newest = pd.to_datetime(previous.get('pendingNewestPublishedAt'), errors='coerce', utc=True)
        newest = max(value for value in (cutoff, pending_newest) if pd.notna(value)) if pd.notna(cutoff) or pd.notna(pending_newest) else None
        complete = False
        resume = int(previous.get('nextSearchStart', 1))
        # While completing a bounded earlier batch, also inspect the head so new
        # articles are not missed. Overlap one page after a restart for insertions.
        starts = [1] if resume > 1 and max_pages > 1 else []
        first_start = max(1, resume - page_size) if resume > 1 and max_pages >= 3 else resume
        starts += list(range(first_start, first_start + (max_pages - len(starts)) * page_size, page_size))
        starts = sorted(set(starts))
        last_start = 1
        try:
            for search_start in starts:
                if search_start > 1000:
                    break
                last_start = search_start
                query = urllib.parse.urlencode({'query': f'{name} 수주 성장 해외', 'sort': 'date',
                                                'display': page_size, 'start': search_start})
                request = urllib.request.Request('https://openapi.naver.com/v1/search/news.json?' + query,
                                                headers={'X-Naver-Client-Id': key, 'X-Naver-Client-Secret': secret})
                result = None
                for attempt in range(3):
                    try:
                        with urllib.request.urlopen(request, timeout=15) as response:
                            result = json.load(response)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(.3 * (attempt + 1))
                rows = result.get('items', [])
                if not isinstance(rows, list):
                    raise ValueError('News items unavailable')
                reached_prior = False
                for row in rows:
                    published = pd.to_datetime(row.get('pubDate'), errors='coerce', utc=True)
                    if pd.notna(published):
                        if pd.isna(newest) or published > newest:
                            newest = published
                        if pd.notna(cutoff) and published < cutoff:
                            reached_prior = True
                    add(dict(row, entity=name, status='원문검증대기'), name)
                if len(rows) < page_size or reached_prior:
                    complete = True
                    break
            if complete:
                state[name] = {'checkedAt': checked, 'newestPublishedAt': newest.isoformat() if pd.notna(newest) else None,
                               'historyGap': previous.get('historyGap')}
            else:
                # Never advance the watermark across an unexamined result range.
                state[name] = dict(previous, nextSearchStart=last_start + page_size,
                                   pendingNewestPublishedAt=newest.isoformat() if pd.notna(newest) else None)
                if last_start + page_size > 1000:
                    # The provider exposes only a bounded result range. Keep the
                    # explicit history gap but move subsequent checks to new news.
                    state[name] = dict(checkedAt=checked,
                                       newestPublishedAt=newest.isoformat() if pd.notna(newest) else None,
                                       historyGap='검색 API 과거 결과 범위 한도; 이보다 오래된 뉴스 미확인')
                failures.append({'name': name, 'error': '검색 페이지 한도 도달; 이전 수집 지점 연결 미완료'})
        except Exception as exc:
            failures.append({'name': name, 'error': type(exc).__name__})
    hints = sorted(queue.values(), key=lambda row: row['hintId'])
    history_gaps = [{'name': name, 'reason': state.get(name, {}).get('historyGap')}
                    for name in sorted(set(names)) if state.get(name, {}).get('historyGap')]
    status = {'status': '부분수집' if failures or history_gaps else '정상' if attempted else '캐시유지',
              'source': 'NAVER 뉴스', 'checkedAt': checked if attempted else previous_status.get('checkedAt'),
              'cacheReadAt': checked, 'hints': len(hints), 'failures': failures,
              'attemptedNames': attempted, 'cachedNames': skipped, 'requestedNames': len(set(names)),
              'historyGaps': history_gaps,
              'scope': '검색 결과 발견 큐; 원문 사실 검증 전에는 성장 근거로 가산하지 않음'}
    json_write(queue_path, hints)
    json_write(state_path, state)
    json_write(status_path, status)
    return hints, status


def confidence(events):
    positives = {e.get('independentEventId', e['eventId']): e for e in events if e['status'] == '유효' and e['polarity'] == 'positive'}
    negatives = {e.get('independentEventId', e['eventId']) for e in events if e['polarity'] == 'negative'}
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
            'dataStatus': dict(source_status, candidateCount=len(candidates), evaluatedTickers=len(latest),
                               oldEvidenceUsed=sum(r['oldEvidenceCount'] for r in candidates)),
            'methodVersion': 'growth-evidence-v1', 'updatedKST': now.strftime('%Y-%m-%d %H:%M'),
            'notice': '신뢰도는 독립 근거 충실도이며 성공확률이 아닙니다. 수주·외부 컨센서스·공식 수출통계 사용 범위는 아래에 표시합니다.',
            '_audit': audited}
