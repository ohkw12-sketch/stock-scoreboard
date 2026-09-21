"""Independent official guidance ledger. Amounts are KRW; scores are never changed."""
from __future__ import annotations
import argparse
import hashlib
import csv
import io
import json
import math
import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen, Request
from bs4 import BeautifulSoup
from dart_fundamentals import _api_key, _request_bytes, _json_save

UNITS = {'원': 1, '천원': 1000, '백만원': 1000000, '억원': 100000000, '조원': 1000000000000}
KST = timezone(timedelta(hours=9))

def is_guidance(title):
    return '영업실적등에대한전망' in re.sub(r'\s+', '', title)

def numeric(value):
    try:
        n = float(str(value).replace(',', ''))
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None

def amount(text, unit):
    text = re.sub(r'\s+', '', text).replace('△', '-').replace('▲', '-')
    if text in {'', '-', '--'}:
        return None
    # Only closed ranges or exact figures; do not invent an upper bound for '이상'.
    match = re.fullmatch(r'(-?[\d,]+(?:\.\d+)?)(?:[~～∼](-?[\d,]+(?:\.\d+)?))?', text)
    if not match:
        raise ValueError('unsupported amount expression')
    low = numeric(match[1]) * UNITS[unit]
    high = numeric(match[2] or match[1]) * UNITS[unit]
    if low > high:
        raise ValueError('reversed range')
    return {'low': low, 'high': high, 'mid': (low + high) / 2}

def period_label(start, end):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a > b:
        raise ValueError('reversed period')
    if a.year == b.year:
        if a.month == 1 and a.day == 1 and b.month == 12 and b.day == 31:
            return str(a.year)
        for q, (m, d) in enumerate(((3,31),(6,30),(9,30),(12,31)), 1):
            if (a.month,a.day) == (m-2,1) and (b.month,b.day) == (m,d):
                return f'{a.year}Q{q}'
    return f'{a.isoformat()}/{b.isoformat()}'

def table_grid(table):
    """Expand HTML row/col spans so period columns never shift."""
    occupied, grid = {}, []
    for r, tr in enumerate(table.find_all('tr')):
        c = 0
        for cell in tr.find_all(['td','th'], recursive=False):
            while (r,c) in occupied:
                c += 1
            value = cell.get_text(' ', strip=True)
            rs, cs = int(cell.get('rowspan', 1)), int(cell.get('colspan', 1))
            if not (1 <= rs <= 50 and 1 <= cs <= 50):
                raise ValueError('invalid table span')
            for rr in range(r,r+rs):
                for cc in range(c,c+cs):
                    occupied[rr,cc] = value
            c += cs
        grid.append([occupied.get((r,col),'') for col in range(max((col for rr,col in occupied if rr==r), default=-1)+1)])
    return grid

def parse_document(markup, filing):
    """Parse the forecast table only; correction-before and recent actuals excluded."""
    if not is_guidance(filing['report_nm']):
        return []
    soup = BeautifulSoup(markup, 'html.parser')
    candidates = []
    for table in soup.find_all('table'):
        if table.find('table'):
            continue
        grid = table_grid(table)
        joined = ' '.join(' '.join(row) for row in grid)
        if '시작일' in joined and '종료일' in joined and '매출액' in joined and '정정전' not in re.sub(r'\s+','',joined):
            candidates.append((grid,joined))
    if len(candidates) != 1:
        raise ValueError('forecast table ambiguous or unavailable')
    grid, text = candidates[0]
    unit_match = re.search(r'단위\s*[:：]\s*(조원|억원|백만원|천원|원)', text)
    if not unit_match:
        raise ValueError('amount unit unavailable')
    start_row = next(row for row in grid if any('시작일' in c for c in row))
    end_row = next(row for row in grid if any('종료일' in c for c in row))
    basis = 'consolidated' if '연결' in filing['report_nm'] else 'separate'
    if '관리연결' in text:
        basis = 'managed_consolidated'
    if re.search(r'자회사의?\s*주요경영사항', text):
        basis = 'subsidiary'
    unique_starts = {v for v in start_row if re.fullmatch(r'20\d{2}-\d{2}-\d{2}',v)}
    notes = text.split('기타 투자판단',1)[-1] if '기타 투자판단' in text else ''
    records = []
    seen = set()
    for col, start in enumerate(start_row):
        if not re.fullmatch(r'20\d{2}-\d{2}-\d{2}', start):
            continue
        end = end_row[col]
        period = period_label(start,end)
        if period in seen:
            continue  # a colspan duplicated the same period cell
        seen.add(period)
        values = {}
        for metric, label in [('sales','매출액'),('operating_profit','영업이익')]:
            rows = [row for row in grid if any(re.sub(r'\s+','',c)==label for c in row[:col])]
            if len(rows)>1:
                raise ValueError('ambiguous metric rows')
            values[metric] = amount(rows[0][col],unit_match[1]) if rows else None
        # Only a single explicit target period and one unambiguous labelled range.
        if len(unique_starts)==1:
            for metric,label in [('sales','매출액'),('operating_profit','영업이익')]:
                if values[metric] is not None:
                    continue
                pattern=rf'{label}\s*[:：]\s*([\d,]+(?:\.\d+)?)\s*(억원|백만원|천원|조원|원)?\s*[~～∼]\s*([\d,]+(?:\.\d+)?)\s*(억원|백만원|천원|조원|원)'
                matches=list({m.group(0):m for m in re.finditer(pattern,notes)}.values())
                if len(matches)==1:
                    m=matches[0]
                    if m[2] and m[2]!=m[4]:
                        raise ValueError('mixed units in narrative range')
                    values[metric]=amount(m[1]+'~'+m[3],m[4])
        explicit_opm = list(set(re.findall(r'영업이익률\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*달성', notes))) if len(unique_starts)==1 else []
        if not any(values.values()) and len(explicit_opm)!=1:
            continue
        sales, op = values['sales'], values['operating_profit']
        opm = ({'low':float(explicit_opm[0]),'high':float(explicit_opm[0]),'mid':float(explicit_opm[0])} if len(explicit_opm)==1 else None)
        if sales and op and sales['low']>0:
            ratios = [100*o/s for o in (op['low'],op['high']) for s in (sales['low'],sales['high'])]
            opm = {'low':min(ratios),'high':max(ratios),'mid':100*op['mid']/sales['mid']}
        receipt = str(filing['rcept_no'])
        published = datetime.strptime(filing['rcept_dt'].replace('-',''),'%Y%m%d').date().isoformat()
        records.append(dict(ticker=str(filing['stock_code']).zfill(6), name=filing.get('corp_name',''),
            period=period, period_start=start, period_end=end, basis=basis, amount_unit='원',
            published_at=published, receipt=receipt, is_correction='정정' in filing['report_nm'],
            source=filing.get('source','OpenDART'), source_url=filing.get('source_url',f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}'),
            report_name=filing['report_nm'], opm=opm, **values))
    if not records:
        raise ValueError('no explicit forecast numbers')
    return records

def merge_history(previous, incoming):
    by_id = {(r['ticker'],r['period'],r['basis'],r['receipt']):dict(r) for r in previous}
    for r in incoming:
        by_id[(r['ticker'],r['period'],r['basis'],r['receipt'])] = dict(r)
    groups = {}
    for r in by_id.values():
        groups.setdefault((r['ticker'],r['period'],r['basis']),[]).append(r)
    result = []
    for members in groups.values():
        members.sort(key=lambda r:(r['published_at'],r['receipt']))
        for i,r in enumerate(members):
            r['active'] = i==len(members)-1
            r['revision'] = i+1
            r['changes'] = {}
            for metric in ('sales','operating_profit'):
                before = members[i-1].get(metric) if i else None
                after = r.get(metric)
                delta = after['mid']-before['mid'] if before and after else None
                r['changes'][metric] = '신규' if i==0 else '확인불가' if delta is None else '상향' if delta>0 else '하향' if delta<0 else '유지'
            result.append(r)
    return sorted(result,key=lambda r:(r['ticker'],r['period'],r['basis'],r['revision']))

def compare(record, consensus, threshold=.05, as_of=None, new_days=30):
    if not 0 <= threshold < 1:
        raise ValueError('guidance_gap_threshold must be 0..1')
    today = as_of or date.today()
    result = dict(record, consensus=consensus, guidance_vs_consensus_gap={}, tags=[])
    if record['revision']==1 and not record.get('is_correction') and 0 <= (today-date.fromisoformat(record['published_at'])).days <= new_days:
        result['tags'].append('NEW')
    compatible = consensus and all(record.get(k)==consensus.get(k) for k in ('ticker','period','basis','amount_unit'))
    for metric in ('sales','operating_profit'):
        g, c = record.get(metric), numeric(consensus.get(metric)) if compatible else None
        gap = (g['mid']-c)/c if g and c is not None and c != 0 else None
        result['guidance_vs_consensus_gap'][metric] = gap
        # Negative consensus retains the requested formula but its sign inverts economics.
        if gap is not None and c>0:
            tag = 'UP' if gap>threshold else 'RISK' if gap < -threshold else None
            if tag and tag not in result['tags']:
                result['tags'].append(tag)
    result['comparison_status'] = '동일 기간·기준' if compatible else '컨센서스 기간·회계기준 확인 필요'
    return result

def consensus_rows(path, configured_basis=None):
    """Adapt the KIS CSV or the broker-report consensus JSON."""
    if not path or not Path(path).exists():
        return []
    result=[]
    if Path(path).suffix.lower()=='.json':
        for raw in read_json(path,[]):
            if raw.get('scope')!='annual' or raw.get('status')=='불일치 검토':
                continue
            p=re.fullmatch(r'(20\d{2})FY',raw.get('period','') or '')
            if not p:
                continue
            result.append(dict(ticker=str(raw.get('ticker','')).zfill(6),period=p[1],basis=configured_basis,
                amount_unit='원',sales=(numeric(raw.get('salesMedianKrw100m'))*UNITS['억원']
                    if numeric(raw.get('salesMedianKrw100m')) is not None else None),
                operating_profit=(numeric(raw.get('operatingProfitMedianKrw100m'))*UNITS['억원']
                    if numeric(raw.get('operatingProfitMedianKrw100m')) is not None else None),
                as_of=raw.get('latestReportDate'),source=raw.get('status') or '증권사 리포트 컨센서스'))
        return result
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        for raw in csv.DictReader(stream):
            unit=raw.get('amount_unit')
            if unit not in UNITS:
                continue
            for prefix, period_field in [('forward','estimate_period'),('next','next_estimate_period')]:
                p=re.fullmatch(r'(20\d{2})(?:[/.]?12)?\s*\(?E\)?',raw.get(period_field,'') or '')
                if not p:
                    continue
                result.append(dict(ticker=raw['ticker'].zfill(6),period=p[1],basis=raw.get('basis') or configured_basis,
                    amount_unit='원', sales=(numeric(raw.get(prefix+'_sales'))*UNITS[unit] if numeric(raw.get(prefix+'_sales')) is not None else None),
                    operating_profit=(numeric(raw.get(prefix+'_op'))*UNITS[unit] if numeric(raw.get(prefix+'_op')) is not None else None),
                    as_of=raw.get('as_of'),source=raw.get('source','consensus cache')))
    return result

def read_json(path, default):
    return json.loads(Path(path).read_text('utf-8-sig')) if Path(path).exists() else default

def collect(config, start, end, previous, request=_request_bytes):
    key=os.getenv('OPEN_DART_API_KEY') or _api_key()
    status={'status':'정상','checked_at':datetime.now(KST).isoformat(),'failures':[], 'from':str(start),'to':str(end)}
    if not key:
        status.update(status='설정필요',problem='OPEN_DART_API_KEY 또는 DART_API_KEY 없음')
        return previous,status
    incoming=[]
    cache=Path(config.get('cache_dir','cache'))/'guidance'/'documents'
    cache.mkdir(parents=True,exist_ok=True)
    known={r['receipt'] for r in previous}
    reviewed=read_json(cache.parent/'reviewed_exclusions.json',{})
    status['excluded']=[]
    def fetch(endpoint, params):
        for attempt in range(3):
            try:
                time.sleep(float(config.get('dart_pause_seconds',.2)))
                return request(endpoint,dict(params,crtfc_key=key))
            except Exception:
                if attempt==2:
                    raise RuntimeError('official source request failed') from None
                time.sleep(.5*(attempt+1))
    cursor=start
    while cursor<=end:
        until=min(end,cursor+timedelta(days=89))
        page=1
        try:
            while True:
                payload=json.loads(fetch('list.json',dict(bgn_de=cursor.strftime('%Y%m%d'),end_de=until.strftime('%Y%m%d'),
                    last_reprt_at='N',pblntf_detail_ty='I002',page_no=page,page_count=100,sort='date',sort_mth='asc')))
                if payload.get('status')=='013':
                    break
                if payload.get('status')!='000':
                    raise RuntimeError('DART status '+str(payload.get('status')))
                for filing in payload.get('list',[]):
                    if not is_guidance(filing.get('report_nm','')) or not filing.get('stock_code'):
                        continue
                    receipt=filing['rcept_no']
                    if receipt in known:
                        continue
                    review=reviewed.get(receipt)
                    if review:
                        evidence=cache / review['document_name']
                        if evidence.exists() and hashlib.sha256(evidence.read_bytes()).hexdigest()==review['sha256']:
                            status['excluded'].append(dict(review,receipt=receipt,ticker=filing['stock_code'],name=filing.get('corp_name')))
                            continue
                    try:
                        if not re.fullmatch(r'\d{14}',receipt):
                            raise ValueError('invalid receipt')
                        path=cache/(receipt+'.xml')
                        if path.exists():
                            markup=path.read_text('utf-8')
                        else:
                            archive=zipfile.ZipFile(io.BytesIO(fetch('document.xml',{'rcept_no':receipt})))
                            files=[n for n in archive.namelist() if n.lower().endswith('.xml')]
                            primary=next((n for n in files if Path(n).stem==receipt),None)
                            if not primary and len(files)!=1:
                                raise ValueError('ambiguous archive')
                            raw=archive.read(primary or files[0])
                            try: markup=raw.decode('utf-8')
                            except UnicodeDecodeError: markup=raw.decode('cp949')
                            path.write_text(markup,encoding='utf-8')
                        incoming.extend(parse_document(markup,filing))
                    except Exception as exc:
                        status['failures'].append({'ticker':filing['stock_code'],'name':filing.get('corp_name'),
                            'receipt':receipt,'published_at':filing['rcept_dt'],'reason':type(exc).__name__,'status':'원문확인필요'})
                if page>=int(payload.get('total_page',1)):
                    break
                page+=1
        except Exception as exc:
            status['failures'].append({'from':str(cursor),'to':str(until),'reason':str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__})
        cursor=until+timedelta(days=1)
    if status['failures']:
        status['status']='부분실패·이전자료유지'
    return merge_history(previous,incoming),status

def build_board(history, consensus, status, config, today=None):
    today=today or datetime.now(KST).date()
    rows=[]
    failed={f.get('ticker') for f in status.get('failures',[])}
    for r in history:
        if not r['active'] or r['period_end']<str(today) or r['published_at']>str(today):
            continue
        c=next((c for c in consensus if all(c.get(k)==r.get(k) for k in ('ticker','period'))),None)
        row=compare(r,c,float(config.get('guidance_gap_threshold',.05)),today,int(config.get('guidance_new_days',30)))
        row['fetch_status']='정정/후속 원문확인필요·이전값' if r['ticker'] in failed else ('수집범위 확인필요' if any(not f.get('ticker') for f in status.get('failures',[])) else '정상' if status['status'].startswith(('정상','부분')) else status['status'])
        if row['fetch_status']!='정상':
            row['tags']=[]
        rows.append(row)
    return {'status':status,'rows':rows,'gap_threshold':config.get('guidance_gap_threshold',.05),
            'coverage':{'activeRows':len(rows),'activeTickers':len({r['ticker'] for r in rows}),
                        'comparableRows':sum(r.get('comparison_status')=='동일 기간·기준' for r in rows)},
            'policy':'가이던스와 외부 컨센서스를 함께 표시. 같은 종목·기간·항목에서 최신 컨센서스와 가이던스 중 발표일이 더 최신인 값을 사용하고 같은 날이면 가이던스를 우선. 날짜·증권사 수 점수·가중치 없음. 가치 원점수에는 미반영.'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config.example.json')
    parser.add_argument('--start');parser.add_argument('--end')
    parser.add_argument('--output',default='test_output/guidance.json')
    parser.add_argument('--ledger',default='cache/guidance/history.json')
    parser.add_argument('--consensus',default='test_output/forecast_engine/forecast_consensus.json')
    parser.add_argument('--kind-input',help='JSON list of official KIND filing metadata and local markup_path')
    args=parser.parse_args()
    config=read_json(args.config,{})
    end=date.fromisoformat(args.end) if args.end else datetime.now(KST).date()
    start=date.fromisoformat(args.start) if args.start else end-timedelta(days=400)
    if start>end: parser.error('start must precede end')
    previous=read_json(args.ledger,[])
    history,status=collect(config,start,end,previous)
    # Explicit official KIND documents are a fallback for unavailable DART originals.
    if args.kind_input:
        for filing in read_json(args.kind_input,[]):
            try:
                url=urlsplit(filing['source_url'])
                if url.scheme!='https' or url.hostname!='kind.krx.co.kr':
                    raise ValueError('official KIND URL required')
                parsed=parse_document(Path(filing['markup_path']).read_text('utf-8'),dict(filing,source='KIND'))
                history=merge_history(history,parsed)
                status['failures']=[f for f in status['failures'] if f.get('receipt')!=filing['rcept_no']]
                if not status['failures'] and status['status']=='부분실패·이전자료유지':
                    status['status']='정상'
            except Exception as exc:
                status['failures'].append({'receipt':filing.get('rcept_no'),'reason':type(exc).__name__})
    _json_save(Path(args.ledger),history)
    board=build_board(history,consensus_rows(args.consensus,config.get('guidance_consensus_basis')),status,config)
    _json_save(Path(args.output),board)
    print(json.dumps({'status':status['status'],'history':len(history),'active':len(board['rows']),'failures':len(status['failures']),'output':args.output},ensure_ascii=False))

if __name__=='__main__':
    main()
