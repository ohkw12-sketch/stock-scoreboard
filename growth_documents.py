"""Ingest explicitly reviewed source text; never infer growth from search titles.

This is a local input boundary for lawfully obtained news/IR documents. It does
not download paywalled articles or third-party video captions and calls no AI.
The input is {"documents": [...]}; each document must contain source, sourceType
("뉴스" or "IR"), url, publishedAt, fetchedAt, text, accessBasis, verification
({status: "verified", reviewer, verifiedAt}), and facts. Each fact needs ticker,
independentEventId (the original business event, reused across republications),
kind, polarity, excerpt copied from text, and validity ("active", "invalid", or
"review"). Reported amounts/rates may be carried as metadata, never forecast.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from growth_discovery import (KST, cached_source_status, day, fingerprint, json_write,
                              merge_events, read_json)

PARSER_VERSION = 'verified-growth-documents-v1'
ALLOWED_KINDS = {'수주', '해외진출', '판매성장', '생산능력', '사업확장', '실적', 'IR'}


def _timestamp(value, now):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed > now:
        raise ValueError('확인·수집 시각은 시간대가 있는 과거 시각이어야 함')
    return parsed.isoformat(timespec='seconds')


def parse_verified_document(document, universe, now):
    source_type = document.get('sourceType')
    if source_type not in {'뉴스', 'IR'}:
        raise ValueError('뉴스 또는 IR 원문만 허용')
    url = str(document.get('url', ''))
    if urlsplit(url).scheme not in {'https', 'http'} or not urlsplit(url).netloc:
        raise ValueError('원문 URL 없음')
    if not document.get('source') or document.get('accessBasis') not in {'public', 'source_permission', 'user_supplied'}:
        raise ValueError('원자료 출처·확보 근거 없음')
    text = str(document.get('text', '')).strip()
    if not text:
        raise ValueError('원문 없음; 제목·요약만으로 근거 생성 불가')
    published = day(document.get('publishedAt'))
    if not published or published > str(now.date()):
        raise ValueError('원문 최초 공개일 미확인 또는 미래 날짜')
    verified = document.get('verification') or {}
    if verified.get('status') != 'verified' or not verified.get('reviewer'):
        raise ValueError('원문 사실 검증 미완료')
    verified_at = _timestamp(verified.get('verifiedAt'), now)
    fetched_at = _timestamp(document.get('fetchedAt'), now)
    if day(verified_at) < published or day(fetched_at) < published:
        raise ValueError('확인·수집일이 원문 공개일보다 이름')
    content_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    document_id = fingerprint(url, published)
    compact_text = re.sub(r'\s+', ' ', text)
    facts = document.get('facts')
    if not isinstance(facts, list):
        raise ValueError('검증된 개별 사실 목록 없음')
    events = []
    for fact in facts:
        ticker = str(fact.get('ticker', '')).zfill(6)
        if ticker not in universe:
            continue
        identity = str(fact.get('independentEventId', '')).strip()
        excerpt = re.sub(r'\s+', ' ', str(fact.get('excerpt', '')).strip())
        if not identity or not excerpt or excerpt not in compact_text:
            raise ValueError('독립 사업 사건 식별자 또는 원문에 일치하는 근거 문장 없음')
        kind, polarity, validity = fact.get('kind'), fact.get('polarity'), fact.get('validity')
        if kind not in ALLOWED_KINDS or polarity not in {'positive', 'neutral', 'negative'} or validity not in {'active', 'invalid', 'review'}:
            raise ValueError('검증된 사건 종류·방향·유효성 없음')
        first = day(fact.get('firstPublished')) or published
        until = day(fact.get('activeUntil'))
        if first > published:
            raise ValueError('최초 공개일이 현재 문서 공개일보다 늦음')
        status = {'active': '유효', 'invalid': '무효', 'review': '상태확인필요'}[validity]
        if until and until < str(now.date()) and status == '유효':
            status = '상태확인필요'
        event_id = fingerprint(document_id, ticker, identity)
        # Text ingestion proves an attributed fact exists; no model-generated
        # materiality, future revenue, target price or growth rate is invented.
        events.append(dict(eventId=event_id, independentEventId=identity, ticker=ticker,
                           receipt='DOC-' + event_id, documentId=document_id,
                           title=str(document.get('title', '')), source=document['source'],
                           sourceType=source_type, url=url, kind=kind, polarity=polarity,
                           status=status, firstPublished=first, publishedAt=published,
                           fetchedAt=fetched_at, lastVerified=verified_at, activeUntil=until,
                           factType='원문확인사실', materiality=0.0, excerpt=excerpt[:1000],
                           contentHash=content_hash, verifiedBy=verified['reviewer'],
                           parserVersion=PARSER_VERSION))
    return document_id, content_hash, events


def collect_verified_documents(config, universe, now=None, *, reuse=False):
    """Return (events, status). Both modes perform zero network calls."""
    now = now or datetime.now(KST)
    cache = Path(config['cache_dir']) / 'growth'
    ledger_path, status_path = cache / 'verified_document_ledger.json', cache / 'verified_document_status.json'
    prior = read_json(ledger_path, [])
    previous_status = read_json(status_path, {'source': '검증 뉴스·IR 원문', 'status': '미수집'})
    if reuse:
        return prior, cached_source_status(previous_status, now, ledger_path.exists())
    input_path = Path(config.get('growth_verified_documents_file', cache / 'verified_documents_input.json'))
    if not input_path.exists():
        return prior, dict(previous_status, status='원문검증대기', networkRequests=0,
                           checkedAt=previous_status.get('checkedAt'),
                           problem='검증 뉴스·IR 원문 입력 없음; 검색 요약·IR 일정은 근거로 가산하지 않음')
    payload = read_json(input_path, {})
    documents = payload.get('documents') if isinstance(payload, dict) else None
    if not isinstance(documents, list):
        raise ValueError('검증 원문 입력 documents 목록 없음')
    universe = {str(t).zfill(6) for t in universe}
    parsed, failures, refreshed_ids = [], [], set()
    for index, document in enumerate(documents):
        try:
            identity, _, events = parse_verified_document(document, universe, now)
            refreshed_ids.add(identity)
            parsed.extend(events)
        except (ValueError, TypeError, AttributeError) as exc:
            failures.append({'inputIndex': index, 'url': document.get('url') if isinstance(document, dict) else None,
                             'error': str(exc)})
    # Update only explicitly reverified documents. Missing input rows are retained
    # with their original dates, so a partial batch cannot erase earlier facts.
    retained = [event for event in prior if event.get('documentId') not in refreshed_ids]
    events = merge_events(retained + parsed)
    failed_urls = {failure['url'] for failure in failures if failure.get('url')}
    for event in events:
        if event.get('url') in failed_urls and event.get('polarity') != 'negative':
            event['status'] = '상태확인필요'
            event['verificationIssue'] = '같은 원문의 새 검증 입력을 처리하지 못함'
        elif event.get('activeUntil') and event['activeUntil'] < str(now.date()) and event['status'] == '유효':
            event['status'] = '상태확인필요'
    status = dict(source='검증 뉴스·IR 원문', status='부분수집' if failures else '정상',
                  checkedAt=max((e['lastVerified'] for e in parsed), default=previous_status.get('checkedAt')),
                  processedAt=now.isoformat(timespec='seconds'), networkRequests=0,
                  documentCount=len(refreshed_ids), eventCount=len(events), failures=failures,
                  scope='제공된 검증 원문만; 뉴스·IR 전수 수집 또는 신규 원문 접근 권한을 의미하지 않음')
    json_write(ledger_path, events)
    json_write(status_path, status)
    return events, status
