"""Rebuild every numeric board without publishing or changing the user's holdings."""
import argparse
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from growth_discovery import (KST, build_growth_board, collect_disclosures, collect_news_hints,
                              json_write, number)
from growth_sources import collect_trade_evidence, consensus_evidence, product_exposure
from board_contract import load_contract
from rotation_screener import (MarketDataLoader, attach_market_snapshot, build_entry_board,
                              build_value_board, load_config, load_fundamentals, run_engine, write_outputs)


def refresh_holdings(previous, prices, fundamentals):
    result = copy.deepcopy(previous)
    latest = prices.sort_values('date').groupby('ticker').tail(1)
    names = {r['name']: r for r in latest.to_dict('records')}
    financials = {r['ticker']: r for r in fundamentals.to_dict('records')}
    price_date = prices.date.max().strftime('%Y-%m-%d')
    valuation = latest[['ticker', 'market_cap']].merge(
        fundamentals[['ticker', 'sector', 'normalized_ttm_op']], on='ticker', how='inner')
    valuation['pop'] = valuation.market_cap / valuation.normalized_ttm_op.where(valuation.normalized_ttm_op > 0)
    medians = valuation[valuation['pop'].between(.1, 300)].groupby('sector')['pop'].median()
    values, costs, updated, missing = {}, 0.0, 0, []
    for row in result.get('rows', []):
        stock = names.get(row['name'])
        if not stock or pd.Timestamp(stock['date']).strftime('%Y-%m-%d') != price_date:
            missing.append(row['name'])
            row['special'] = f"가격 갱신 실패 · 이전 기준 {row.get('basis', '미확인')}"
            continue
        # Quantities and average purchase costs remain exactly as entered by user.
        quantity, avg = number(row.get('qty')), number(row.get('avg'))
        close = float(stock['close'])
        if quantity is None or avg is None or avg <= 0:
            missing.append(row['name'])
            continue
        old = {k: row.get(k) for k in ('judgment', 'action', 'fairRange', 'valuePosition', 'basis')}
        row.setdefault('previousAssessment', old)
        # Presentation judgments are user-owned display fields. Restore and preserve them;
        # only objective prices, returns, dates and ratios are refreshed automatically.
        locked = row.get('previousAssessment') or old
        judgment = locked.get('judgment', row.get('judgment'))
        action = locked.get('action', row.get('action'))
        fair_range = locked.get('fairRange', row.get('fairRange'))
        f = financials.get(stock['ticker'], {})
        op, prev_op = number(f.get('op_current')), number(f.get('op_previous'))
        if op is not None and prev_op is not None and prev_op > 0 and op >= 0:
            op_text = f'{(op/prev_op-1)*100:+.1f}% 실제'
        elif op is not None and prev_op is not None:
            op_text = '흑자전환' if op > 0 >= prev_op else '적자' if op < 0 else '비교불가'
        else:
            op_text = '자료없음'
        history = prices[prices.ticker.eq(stock['ticker'])].sort_values('date')
        recent = history.tail(63)
        high = float(recent.high.max())
        drawdown = (close/high-1)*100 if high > 0 else None
        ttm, cap = number(f.get('normalized_ttm_op')), number(stock.get('market_cap'))
        pop = cap/ttm if ttm and ttm > 0 and cap else None
        sector = f.get('sector')
        median = number(medians.get(sector))
        premium = (pop/median-1)*100 if pop and median and median > 0 else None
        value_text = f'{abs(premium):.1f}% ' + ('할인' if premium < 0 else '프리미엄') if premium is not None else '산출불가'
        row.update(ticker=stock['ticker'], close=close, ret=f'{(close/avg-1)*100:+.2f}%',
                   drawdown3m=f'{drawdown:.2f}%' if drawdown is not None else '자료없음',
                   opGrowth=op_text, valuePosition=value_text, fairRange=fair_range,
                   judgment=judgment, action=action,
                   special=f"실적 {str(f.get('as_of', '미수집'))[:10]} · 종가 {close:,.0f}원",
                   basis=f'{price_date} 검증 종가', priceChange='가격·공시 수치 재계산')
        values[row['name']] = close * quantity
        costs += avg * quantity
        updated += 1
    total = sum(values.values())
    # Preserve the user's existing exposure taxonomy, recompute weights only.
    sector_map = {'SK하이닉스':'메모리', '코리아써키트':'PCB·기판·광학', 'LG이노텍':'PCB·기판·광학',
                  '대덕전자':'PCB·기판·광학', '테스':'반도체 장비', '피에스케이':'반도체 장비',
                  '일진전기':'전력기기', 'HD현대일렉트릭':'전력기기', '엠앤씨솔루션':'방산'}
    exposure = result.setdefault('exposure', {})
    for entry in exposure.get('sectors', []):
        weight = sum(v for n,v in values.items() if sector_map.get(n) == entry['name']) / total * 100 if total else 0
        entry.update(weight=f'{weight:.1f}%', status='과밀' if weight >= 30 else '부족' if weight == 0 else '적정')
    axes = {'AI 직접': {'메모리','PCB·기판·광학','반도체 장비'}, 'AI 인프라': {'전력기기'},
            'Physical AI': {'로봇·자동화'}, '비AI 산업': {'방산','조선','자동차·소비재'}}
    for entry in exposure.get('topAxes', []):
        weight = sum(v for n,v in values.items() if sector_map.get(n) in axes.get(entry['name'],set())) / total * 100 if total else 0
        entry.update(weight=f'{weight:.1f}%', status='과밀' if weight >= 50 else '부족' if weight == 0 else '적정')
    exposure['basis'] = f'{price_date} 검증 종가 × 기존 보유수량' + (' · 일부 누락' if missing else '')
    result['valuationBasis'] = f'보유수량·평균매입가 유지 · {price_date} 종가 · 총매입 {costs:,.0f}원 · 총평가 {total:,.0f}원'
    result['status'] = f'보유 {updated}/{len(result.get("rows", []))}종목 가격·공시 재평가 · 실거래·보유수량 변경 없음'
    result['events'] = []
    result['refreshStatus'] = {'priceDate': price_date, 'updated': updated, 'missing': missing,
                               'assessment': '기존 주관적 목표가 대신 검증된 수치로 재평가; 매매 지시 아님'}
    return result


def refresh_youtube_prices(source, prices):
    """Never replace historical speaker statements with invented fresh summaries."""
    result = copy.deepcopy(source)
    latest = prices.sort_values('date').groupby('ticker').tail(1)
    by_name = {r['name']: r for r in latest.to_dict('records')}
    price_date = prices.date.max().strftime('%Y-%m-%d')
    count = 0
    for row in result.get('recommendations', []):
        stock = by_name.get(row[1])
        if stock and pd.Timestamp(stock['date']).strftime('%Y-%m-%d') == price_date:
            row[6] = f'{float(stock["close"]):,.0f}원 · {price_date} 종가 / 발언 내용은 원래 날짜 기준'
            count += 1
    result['meta']['priceBasis'] = f'일치 종목 {count}개 가격 {price_date} 갱신 · 영상 발언은 기존 공개일 기준'
    result['meta']['status'] = '가격 갱신 · 신규 영상 내용 검증 미완료(기존 발언 보존)'
    result['meta']['priceUpdatedKST'] = datetime.now(KST).strftime('%Y-%m-%d %H:%M')
    result['refreshStatus'] = {'status': '부분갱신', 'priceCount': count,
                               'problem': '영상 원문·자막 미확보; 최신 발언으로 재표시하지 않음'}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path('config.kis.example.json'))
    parser.add_argument('--reuse-snapshot', action='store_true')
    parser.add_argument('--reuse-evidence', action='store_true')
    args = parser.parse_args()
    config = load_config(args.config, None)
    out = config['output_dir']
    if args.reuse_snapshot:
        prices = pd.read_pickle(out/'verified_prices.pkl')
        fundamentals = pd.read_pickle(out/'verified_fundamentals.pkl')
        report = json.loads((out/'verified_report.json').read_text('utf-8'))
    else:
        config.update(lookback_business_days=280, dart_retry_batch_size=4000, kis_consensus_batch_size=4000,
                      refresh_universe=True, dart_cache_max_age_hours=0, market_snapshot_cache_max_days=0)
        loader = MarketDataLoader(config)
        prices, source = loader.load()
        prices, snapshot_status = attach_market_snapshot(prices, config)
        fundamentals, fundamental_status = load_fundamentals(prices, config)
        report = dict(loader.report, fundamentals=fundamental_status, marketSnapshot=snapshot_status, engineSource=source)
        prices.to_pickle(out/'verified_prices.pkl')
        fundamentals.to_pickle(out/'verified_fundamentals.pkl')
        json_write(out/'verified_report.json', report)
    if report.get('qualityStatus') != '정상':
        raise RuntimeError('Whole-market price validation incomplete; live data not overwritten')
    growth_cache = config['cache_dir'] / 'growth'
    if args.reuse_evidence:
        events = json.loads((growth_cache/'event_ledger.json').read_text('utf-8'))
        collection = json.loads((growth_cache/'collection_status.json').read_text('utf-8'))
    else:
        events, collection = collect_disclosures(config, set(prices.ticker))
    hints, news_status = collect_news_hints(config, sorted(set(prices['name'])))
    collection['news'] = news_status
    sector_events, trade_status = collect_trade_evidence(config)
    collection['industryStatistics'] = trade_status
    collection['ir'] = {'status':'원문검증대기', 'indexed': collection.get('reviewOnlyCount',0), 'problem':'IR 개최 자체는 성장 근거로 가산하지 않음'}
    consensus_status = report['fundamentals'].get('consensus',{})
    forecasts = consensus_evidence(fundamentals, consensus_status)
    events += forecasts
    collection['consensus'] = {'status':consensus_status.get('status','미수집'),
                               'asOfDate':consensus_status.get('asOfDate'),
                               'evidenceCount':sum(e['status']=='유효' for e in forecasts)}
    listing_path = config['cache_dir']/'krx_listing_desc.csv'
    listing = pd.read_csv(listing_path,dtype={'Code':str}) if listing_path.exists() else pd.DataFrame()
    links = {e['eventId']:product_exposure(listing,e) for e in sector_events}
    growth = build_growth_board(prices, fundamentals, events, collection, sector_events=sector_events, sector_links=links)
    json_write(out/'growth_audit.test.json', growth.pop('_audit'))
    p11 = run_engine(prices, config, report.get('engineSource', 'verified-snapshot'))
    p1 = build_entry_board(prices, p11['_allSectors'], fundamentals, config)
    p2 = build_value_board(fundamentals, config, report['fundamentals'], prices)
    _, board_path, _ = write_outputs(p1, p11, p2, report, config)
    board = json.loads(board_path.read_text('utf-8'))
    board['p2'].pop('turnaroundRows', None)
    board['p2'].pop('turnaroundStatus', None)
    board['growth'] = growth
    board['p3'] = refresh_holdings(board.get('p3',{}), prices, fundamentals)
    board['meta']['sourceSummary'] = f"가격 {report['latestPriceDate']} · 공시 {report['fundamentals'].get('asOfDate')} · 컨센서스 {report['fundamentals'].get('consensusAsOfDate')} · 성장: 수주·컨센서스·수출통계 {trade_status.get('latestPeriod') or '미수집'}"
    board['meta']['uiContractVersion'] = load_contract()['version']
    board['meta']['audit'] = {'checkedAtKST': datetime.now(KST).strftime('%Y-%m-%d %H:%M'), 'sourceDate': report['latestPriceDate'],
                            'summary':'전 종목 가격·재무 재계산, 성장 공시 전수 탐색, 보유수량·평단 보존'}
    next_day = pd.Timestamp(report['latestPriceDate']).date() + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    board['meta']['nextTradingDay'] = next_day.isoformat()
    board['meta']['note'] = '성장 조기포착은 공개 근거 기반 후보입니다. 주가 미반영 판단·신뢰도는 예측 확률이 아닙니다.'
    json_write(board_path, board)
    section_dir = out / 'sections'
    for section in ('p1', 'p11', 'p2', 'growth', 'p3', 'meta'):
        json_write(section_dir / f'{section}.test.json', board[section])
    youtube_path = config['base_data_file'].parent/'youtube-market.json'
    if youtube_path.exists():
        youtube = refresh_youtube_prices(json.loads(youtube_path.read_text('utf-8-sig')), prices)
        json_write(out/'youtube-market.test.json', youtube)
    report['growth'] = collection
    report['holdings'] = board['p3']['refreshStatus']
    json_write(out/'collection_report.test.json', report)
    print(json.dumps({'priceDate':report['latestPriceDate'], 'valueCount':len(p2.get('rows',[])),
                      'growthSectors':len(growth['sectors']), 'growthStocks':len(growth['rows']),
                      'growthCandidates':growth['dataStatus']['candidateCount']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
