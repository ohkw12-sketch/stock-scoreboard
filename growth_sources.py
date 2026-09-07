"""Primary-source industry data and independently dated consensus evidence."""
import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from growth_discovery import KST, cached_source_status, day, fingerprint, json_write, number, read_json

TRADE_PARSER_VERSION = 'motie-summary-v2'

# Exact product links, not a catch-all theme mapping. These are exposure proxies,
# not a claim that a particular company's export revenues have been measured.
PRODUCT_LINKS = {
    '반도체': ('반도체', r'반도체|메모리|집적회로|DRAM|NAND'),
    '화장품': ('화장품', r'화장품|스킨케어|메이크업|코스메틱'),
    '컴퓨터': ('컴퓨터 및 주변장치 제조업', r'컴퓨터|서버|SSD|저장장치'),
    '바이오헬스': ('바이오/제약', r'의약품|바이오시밀러|완제|의료기기'),
    '자동차': ('자동차', r'자동차|완성차'),
    '선박': ('조선', r'선박|조선'),
}


def fetch_html(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'stock-scoreboard/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode('utf-8')


def parse_trade_summary(markup, url, checked_at):
    """Extract only explicitly labelled item-level YoY percentages from KDI's
    reproduction of the Ministry's release. Unmentioned items remain missing.
    """
    soup = BeautifulSoup(markup, 'html.parser')
    editor = soup.select_one('.editor')
    if editor is None:
        raise ValueError('Trade release body unavailable')
    text = re.sub(r'\s+', ' ', editor.get_text(' ', strip=True))
    header = soup.get_text(' ', strip=True)
    title_match = re.search(r'(20\d{2})년\s*(\d{1,2})월\s*수출입\s*동향', header)
    if not title_match or '산업통상부' not in header:
        raise ValueError('Not a verified ministry monthly release')
    period = f'{int(title_match[1]):04}-{int(title_match[2]):02}'
    # Publication date must be explicit in the release; never substitute fetch time.
    published_match = re.search(r'[’\']?(\d{2})\.(\d{1,2})\.(\d{1,2})\.', text)
    if not published_match:
        published = day(header[header.find(title_match[0]):][:350])
    else:
        published = day('20' + '.'.join(published_match.groups()))
    if not published:
        raise ValueError('Publication date unavailable')
    events = []
    for product, (sector, _) in PRODUCT_LINKS.items():
        patterns = [
            rf'{product}\s*\(\s*([+\-△▲]?\s*\d+(?:\.\d+)?)\s*%',
            rf'{product}\s*[△▲]\s*(\d+(?:\.\d+)?)\s*%',
            rf'{product}\s*수출[은는이가]*\s*([+\-]?\d+(?:\.\d+)?)\s*%\s*(증가|감소)',
        ]
        match = next((m for p in patterns if (m := re.search(p, text))), None)
        if not match:
            continue
        rate = number(re.sub(r'\s+', '', match[1]).replace('△','-').replace('▲','-'))
        if '△' in match[0] or '감소' in match[0]:
            rate = -abs(rate)
        if rate is None:
            continue
        event = dict(eventId=fingerprint('MOTIE', period, product), ticker='@' + sector,
                     sector=sector, product=product, kind='수출통계', source='산업통상부·KDI',
                     sourceType='산업통계', firstPublished=published, publishedAt=published,
                     lastVerified=checked_at, fetchedAt=checked_at, receipt=f'MOTIE-{period}-{product}',
                     status='유효', polarity='positive' if rate > 0 else 'negative',
                     factType='잠정통계', period=period, growthRate=rate, url=url,
                     materiality=min(100, max(0, rate)), activeUntil=None)
        events.append(event)
    if not events:
        raise ValueError('No explicit product growth rates parsed')
    return events


def collect_trade_evidence(config, now=None, *, reuse=False):
    now = now or datetime.now(KST)
    checked = now.isoformat(timespec='seconds')
    cache = Path(config['cache_dir']) / 'growth/trade_ledger.json'
    status_path = cache.with_name('trade_status.json')
    previous = read_json(cache, [])
    prior_status = read_json(status_path, {'source': '산업통상부 월간 수출입 동향(KDI)', 'status': '미수집'})
    if reuse:
        return previous, cached_source_status(prior_status, now, cache.exists())
    if previous and status_path.exists():
        prior_checked = pd.to_datetime(prior_status.get('checkedAt'),errors='coerce',utc=True)
        current = pd.Timestamp(now).tz_convert('UTC')
        if prior_status.get('status') == '정상' and pd.notna(prior_checked) and current-prior_checked <= pd.Timedelta(hours=float(config.get('trade_cache_hours',12))):
            return previous, cached_source_status(prior_status, now, True, '월간 통계 확인 주기 내 재사용')
    status = {'status':'정상', 'source':'산업통상부 월간 수출입 동향(KDI)',
              'checkedAt':checked, 'failures':[], 'scope':'HTML 본문에 품목별 증가율이 명시된 통계만; 전 품목·전 지역 아님'}
    events = []
    releases_path = cache.with_name('trade_release_cache.json')
    releases = read_json(releases_path, {})
    expected_period = (pd.Period(now.strftime('%Y-%m'), freq='M') - 1).strftime('%Y-%m')
    latest_list_period = None
    downloaded = 0
    cache_hits = 0
    successful_urls = set()
    try:
        query = urllib.parse.urlencode({'search_txt':'수출입 동향', 'pp':100})
        listing_url = 'https://eiec.kdi.re.kr/policy/materialList.do?' + query
        soup = BeautifulSoup(fetch_html(listing_url), 'html.parser')
        links = {}
        for link in soup.select('a[href]'):
            label = link.get_text(' ', strip=True)
            if re.match(r'20\d{2}년\s*\d{1,2}월\s*수출입\s*동향', label) and 'materialView' in link['href']:
                query_fields = urllib.parse.parse_qs(urllib.parse.urlsplit(link['href']).query)
                identifier = query_fields.get('num', [''])[0]
                if identifier.isdigit():
                    matched_period = re.match(r'(20\d{2})년\s*(\d{1,2})월', label)
                    period = f'{int(matched_period[1]):04}-{int(matched_period[2]):02}'
                    links['https://eiec.kdi.re.kr/policy/materialView.do?num=' + identifier] = period
        if not links:
            raise ValueError('Monthly trade index empty')
        latest_list_period = max(links.values())
        for url, period in sorted(links.items(), key=lambda pair: (pair[1], pair[0]), reverse=True)[:14]:
            try:
                saved = releases.get(url, {})
                raw_path = cache.parent / 'trade_documents' / (fingerprint(url) + '.html')
                # Completed historical releases are immutable cached inputs until
                # a parser upgrade or an explicit correction recheck is requested.
                recheck = period == latest_list_period or config.get('trade_recheck_history', False)
                if saved.get('parserVersion') == TRADE_PARSER_VERSION and raw_path.exists() and not recheck:
                    parsed = saved.get('events', [])
                    cache_hits += 1
                else:
                    if raw_path.exists() and not recheck:
                        markup = raw_path.read_text('utf-8')
                    else:
                        markup = fetch_html(url)
                        downloaded += 1
                        raw_path.parent.mkdir(parents=True, exist_ok=True)
                        raw_path.write_text(markup, encoding='utf-8')
                    raw_hash = hashlib.sha256(markup.encode('utf-8')).hexdigest()
                    if saved.get('rawHash') == raw_hash and saved.get('parserVersion') == TRADE_PARSER_VERSION:
                        parsed = [dict(e, lastVerified=checked) for e in saved.get('events', [])]
                        cache_hits += 1
                    else:
                        parsed = parse_trade_summary(markup, url, checked)
                    releases[url] = dict(parserVersion=TRADE_PARSER_VERSION, rawHash=raw_hash,
                                         checkedAt=checked, period=period, events=parsed)
                events.extend(parsed)
                if period == latest_list_period:
                    successful_urls.add(url)
            except Exception as exc:
                status['failures'].append({'url':url, 'error':type(exc).__name__})
    except Exception as exc:
        status['failures'].append({'error':type(exc).__name__})
    by_id = {e['eventId']:e for e in previous + events}
    combined = list(by_id.values())
    # Only the latest observed statistic for each item can remain active. Older
    # observations are retained for audit, not stacked as independent evidence.
    for sector in {e['sector'] for e in combined}:
        members = [e for e in combined if e['sector']==sector]
        newest = max(max(e['period'] for e in members), latest_list_period or '', expected_period)
        for e in members:
            if e['period'] != newest:
                e['status'] = '후속통계로대체'
            elif e.get('url') not in successful_urls or e.get('publishedAt', '') > str(now.date()):
                e['status'] = '상태확인필요'
            else:
                e['status'] = '유효'
    active_count = sum(e['status']=='유효' for e in combined)
    latest_products = {e['product'] for e in combined if e['status']=='유효'}
    missing_products = sorted(set(PRODUCT_LINKS) - latest_products)
    status.update(status='정상' if active_count and not missing_products else '부분수집' if active_count else '수집실패',
                  historyGaps=status.pop('failures'), activeCount=active_count,
                  latestPeriod=max((e['period'] for e in combined if e['status']=='유효'), default=None),
                  expectedPeriod=expected_period, latestReleasePeriod=latest_list_period,
                  missingProducts=missing_products, documentsDownloaded=downloaded, parsedCacheHits=cache_hits)
    json_write(releases_path, releases)
    json_write(cache, combined)
    json_write(status_path, status)
    return combined, status


def product_exposure(listing, event):
    """Return only product-matched tickers; own growth still checked by caller."""
    rule = PRODUCT_LINKS.get(event['product'])
    if not rule or listing.empty or 'Products' not in listing:
        return set()
    product = listing.Products.fillna('').astype(str)
    matched = product.str.contains(rule[1], case=False, regex=True)
    if event['product']=='반도체':
        matched &= ~product.str.contains('장비|검사기|세정|부품|소재', regex=True)
    return set(listing.loc[matched, 'Code'].astype(str).str.zfill(6))


def consensus_evidence(fundamentals, status, now=None):
    """Only provider-sourced forward growth, dated by provider, no extrapolation.
    A ticker/estimate-period snapshot counts once regardless of estimate fields.
    """
    now = now or datetime.now(KST)
    events = []
    for row in fundamentals.to_dict('records'):
        raw_value = row.get('consensus_as_of')
        raw_date = str(raw_value) if pd.notna(raw_value) else ''
        precision = row.get('consensus_as_of_precision', row.get('as_of_precision'))
        # Month-only provider dates are useful financial metadata, but they do
        # not identify the first public day required by the price-response test.
        if precision in {'month', 'unknown'} or not re.fullmatch(r'20\d{2}[-./]?\d{2}[-./]?\d{2}', raw_date):
            continue
        published = day(raw_date)
        sales = number(row.get('consensus_sales_1y_growth'))
        op = number(row.get('consensus_op_1y_growth'))
        period = str(row.get('estimate_period',''))
        period_match = re.fullmatch(r'(20\d{2})[.-](\d{2})E?', period)
        try:
            future = pd.Period('-'.join(period_match.groups()), freq='M').end_time.date().isoformat() if period_match else None
        except ValueError:
            future = None
        # Presence of raw amounts and provider date guards against synthetic fields.
        prior_sales, forward_sales = number(row.get('consensus_prior_sales')), number(row.get('consensus_forward_sales'))
        forward_op = number(row.get('consensus_forward_op'))
        if not published or not future or not prior_sales or prior_sales<=0 or forward_sales is None or forward_op is None:
            continue
        if published > str(now.date()) or future < str(now.date()) or sales is None:
            continue
        polarity = 'positive' if sales>=10 and forward_op>0 and op is not None and op>=10 else 'negative' if sales<0 else 'neutral'
        if polarity=='neutral':
            continue
        verified_raw = status.get('verifiedAtByTicker', {}).get(row['ticker'])
        verified = pd.to_datetime(verified_raw, errors='coerce', utc=True)
        verification_valid = pd.notna(verified) and verified <= pd.Timestamp(now).tz_convert('UTC')
        verified_at = verified_raw if verification_valid else None
        events.append(dict(ticker=row['ticker'], eventId=fingerprint(row['ticker'],'KIS',period),
                           receipt=f"KIS-{row['ticker']}-{period}", kind='컨센서스',
                           source='KIS 종목추정실적',sourceType='컨센서스',
                           url='https://apiportal.koreainvestment.com/apiservice',
                           firstPublished=published,publishedAt=published,lastVerified=verified_at,fetchedAt=verified_at,
                           activeUntil=future,status='유효' if verification_valid and row['ticker'] in status.get('freshTickers',[]) else '상태확인필요',polarity=polarity,
                           factType='외부기관전망',materiality=min(100,max(0,sales)*2),
                           salesGrowth=sales,opGrowth=op,estimatePeriod=period,
                           datePrecision='day', dateCaveat='공급자 일 단위 추정 기준일; 공급자 날짜·실제 수집확인 시각을 보존'))
    return events
