"""Primary-source industry data and independently dated consensus evidence."""
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from growth_discovery import KST, day, fingerprint, json_write, number

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


def collect_trade_evidence(config, now=None):
    now = now or datetime.now(KST)
    checked = now.isoformat(timespec='seconds')
    cache = Path(config['cache_dir']) / 'growth/trade_ledger.json'
    status_path = cache.with_name('trade_status.json')
    previous = json.loads(cache.read_text('utf-8')) if cache.exists() else []
    if previous and status_path.exists():
        prior_status = json.loads(status_path.read_text('utf-8'))
        prior_checked = pd.to_datetime(prior_status.get('checkedAt'),errors='coerce',utc=True)
        current = pd.Timestamp(now).tz_convert('UTC')
        if pd.notna(prior_checked) and current-prior_checked <= pd.Timedelta(hours=float(config.get('trade_cache_hours',12))):
            return previous, dict(prior_status, status='캐시유지')
    status = {'status':'정상', 'source':'산업통상부 월간 수출입 동향(KDI)',
              'checkedAt':checked, 'failures':[], 'scope':'HTML 본문에 품목별 증가율이 명시된 통계만; 전 품목·전 지역 아님'}
    events = []
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
                    links['https://eiec.kdi.re.kr/policy/materialView.do?num=' + identifier] = label
        if not links:
            raise ValueError('Monthly trade index empty')
        for url in list(links)[:14]:
            try:
                parsed = parse_trade_summary(fetch_html(url), url, checked)
                events.extend(parsed)
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
        newest = max(e['period'] for e in members)
        for e in members:
            if e['period'] != newest:
                e['status'] = '후속통계로대체'
            elif e.get('url') not in successful_urls:
                e['status'] = '상태확인필요'
    active_count = sum(e['status']=='유효' for e in combined)
    status.update(status='정상' if active_count else '수집실패',
                  historyGaps=status.pop('failures'), activeCount=active_count,
                  latestPeriod=max((e['period'] for e in events), default=None))
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
    checked = now.isoformat(timespec='seconds')
    events = []
    for row in fundamentals.to_dict('records'):
        published = day(row.get('consensus_as_of'))
        sales = number(row.get('consensus_sales_1y_growth'))
        op = number(row.get('consensus_op_1y_growth'))
        period = str(row.get('estimate_period',''))
        future = day(period.replace('E','') + '.31')
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
        events.append(dict(ticker=row['ticker'], eventId=fingerprint(row['ticker'],'KIS',period),
                           receipt=f"KIS-{row['ticker']}-{period}", kind='컨센서스',
                           source='KIS 종목추정실적',sourceType='컨센서스',
                           url='https://apiportal.koreainvestment.com/apiservice',
                           firstPublished=published,publishedAt=published,lastVerified=checked,fetchedAt=checked,
                           activeUntil=future,status='유효' if row['ticker'] in status.get('freshTickers',[]) else '상태확인필요',polarity=polarity,
                           factType='외부기관전망',materiality=min(100,max(0,sales)*2),
                           salesGrowth=sales,opGrowth=op,estimatePeriod=period,
                           dateCaveat='공급자 추정 기준일; 수집일을 최초공개일로 바꾸지 않음'))
    return events
