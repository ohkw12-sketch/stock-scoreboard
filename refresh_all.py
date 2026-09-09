"""Rebuild every numeric board without publishing or changing the user's holdings."""
import argparse
import copy
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from growth_discovery import KST, build_growth_board, collect_disclosures, collect_news_hints, number
from growth_sources import collect_trade_evidence, consensus_evidence, product_exposure
from growth_documents import collect_verified_documents
from youtube_content import collect_youtube_content
from refresh_store import (json_write, read_json, run_lock, snapshot_files, public_fields, digest,
                           store_verified_frames, load_verified_frames)
from recommendation_performance import empty_ledger, evaluate
from recommendation_continuity import attach_recommendation_history
from performance_prices import collect_performance_prices
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
    result['meta']['priceUpdatedKST'] = datetime.now(KST).strftime('%Y-%m-%d %H:%M')
    result.setdefault('refreshStatus', {})['prices'] = {'status': '가격갱신', 'priceCount': count}
    return result


def isolated_section(name, build, previous, states, context):
    try:
        result = build()
        if not isinstance(result, dict):
            raise RuntimeError('평가 결과 형식 오류')
        raw_status = result.get('dataStatus', {})
        raw_status = raw_status.get('status', '') if isinstance(raw_status, dict) else ''
        if result.get('status') in ('실패', '자료없음', '수집실패') or raw_status in ('실패', '자료없음', '수집실패'):
            raise RuntimeError('필수 원자료를 확보하지 못했습니다.')
        states[name] = {'status': '계산완료', **context}
        result['refreshState'] = states[name]
        return result
    except Exception as exc:
        states[name] = {'status': '실패·이전유지', 'error': type(exc).__name__ + ': 원자료 또는 계산 검증 실패',
                        'attemptedAt': context['generatedAt']}
        result = copy.deepcopy(previous.get(name, {}))
        result['refreshState'] = {**result.get('refreshState', {}), **states[name]}
        return result


def rebuild(args, config):
    out = config['output_dir']
    out.mkdir(parents=True, exist_ok=True)
    previous = read_json(config['base_data_file'], {})
    generated = datetime.now(KST).isoformat(timespec='seconds')
    if args.reuse_snapshot:
        prices, fundamentals, report = load_verified_frames(out, config['cache_dir'])
    else:
        config['lookback_business_days'] = max(280, config.get('lookback_business_days', 80))
        if args.full_refresh:
            config.update(refresh_universe=True, dart_cache_max_age_hours=0,
                          market_snapshot_cache_max_days=0, force_refresh=True)
        loader = MarketDataLoader(config)
        prices, source = loader.load()
        prices, snapshot_status = attach_market_snapshot(prices, config)
        fundamentals, fundamental_status = load_fundamentals(prices, config)
        report = dict(loader.report, fundamentals=fundamental_status, marketSnapshot=snapshot_status, engineSource=source)
        if report.get('qualityStatus') != '정상':
            json_write(out/'failed_collection_report.json', report)
            raise RuntimeError('전체시장 가격 검증 실패 · 이전 검증 원자료와 게시 자료를 보존했습니다.')
        required_financials = {'ticker', 'sector', 'normalized_ttm_op'}
        financial_failed = (fundamentals.empty or not required_financials.issubset(fundamentals.columns)
                            or fundamental_status.get('status') in ('실패', '자료없음', '수집실패'))
        if financial_failed:
            _, retained, retained_report = load_verified_frames(out, config['cache_dir'])
            fundamentals = retained
            report['fundamentals'] = dict(retained_report['fundamentals'],
                retentionStatus='최신 수집 실패·원래 기준일 유지', failedAttemptAt=generated)
        store_verified_frames(out, config['cache_dir'], prices, fundamentals, report, generated)
    if report.get('qualityStatus') != '정상':
        raise RuntimeError('Whole-market price validation incomplete; live data not overwritten')
    if prices.empty or str(pd.to_datetime(prices.date).max().date()) != report.get('latestPriceDate'):
        raise RuntimeError('가격 파일과 검증 보고서 기준일이 다릅니다.')
    report['runMode'] = '저장자료 재계산' if args.reuse_snapshot else '증분 수집'
    report['attemptedAt'] = generated
    pointer = read_json(out/'verified_snapshot.json')
    if pointer:
        manifest = read_json(config['cache_dir']/'snapshots/manifests'/f"{pointer['snapshotId']}.json")
    else:
        manifest = store_verified_frames(out, config['cache_dir'], prices, fundamentals, report, generated)
    engine_version = digest({name: (Path(__file__).parent/name).read_text('utf-8') for name in (
        'rotation_screener.py', 'growth_discovery.py', 'dart_fundamentals.py', 'kis_consensus.py',
        'growth_sources.py', 'growth_documents.py', 'combined_recommendations.py', 'performance_feedback.py')})[:16]
    context = {'generatedAt': generated, 'snapshotId': manifest['snapshotId'],
               'sourceCutoff': report['latestPriceDate'], 'mode': report['runMode'], 'engineVersion': engine_version}
    context['runId'] = digest(context)[:24]
    states = {}
    p11 = isolated_section('p11', lambda: run_engine(prices, config, report.get('engineSource', 'verified-snapshot')),
                           previous, states, context)
    def entry():
        if states['p11']['status'] != '계산완료':
            raise RuntimeError('순환 계산 실패로 진입 계산을 보류했습니다.')
        return build_entry_board(prices, p11['_allSectors'], fundamentals, config)
    p1 = isolated_section('p1', entry, previous, states, context)
    p2 = isolated_section('p2', lambda: build_value_board(fundamentals, config, report['fundamentals'], prices),
                          previous, states, context)
    collection = {}
    growth_candidates = []
    def growth_section():
        nonlocal collection, growth_candidates
        events, collection = collect_disclosures(config, set(prices.ticker), reuse=args.reuse_evidence)
        _, collection['news'] = collect_news_hints(config, sorted(set(prices['name'])), reuse=args.reuse_evidence)
        sector_events, collection['industryStatistics'] = collect_trade_evidence(config, reuse=args.reuse_evidence)
        documents, collection['verifiedDocuments'] = collect_verified_documents(config, set(prices.ticker), reuse=args.reuse_evidence)
        collection['ir'] = collection['verifiedDocuments']
        consensus_status = report['fundamentals'].get('consensus', {})
        forecasts = consensus_evidence(fundamentals, consensus_status)
        events = list(events) + forecasts + documents
        collection['consensus'] = {'status': consensus_status.get('status', '미수집'),
                                  'asOfDate': consensus_status.get('asOfDate'),
                                  'evidenceCount': sum(e['status'] == '유효' for e in forecasts)}
        listing_path = config['cache_dir']/'krx_listing_desc.csv'
        listing = pd.read_csv(listing_path, dtype={'Code': str}) if listing_path.exists() else pd.DataFrame()
        links = {e['eventId']: product_exposure(listing, e) for e in sector_events}
        result = build_growth_board(
            prices, fundamentals, events, collection,
            sector_events=sector_events, sector_links=links, config=config,
        )
        growth_candidates = result.pop('_audit')
        json_write(out/'growth_audit.test.json', growth_candidates)
        portable_events = [{k: v for k, v in event.items() if not (
            event.get('sourceType') in ('뉴스', 'IR') and k in ('excerpt', 'verifiedBy'))} for event in events]
        json_write(out/'evidence_snapshot.json', {'events': portable_events, 'sectors': sector_events, 'collection': collection})
        evidence_manifest = snapshot_files(config['cache_dir']/'snapshots', {'evidence': out/'evidence_snapshot.json'},
            {'sourceCutoff': report['latestPriceDate'], 'firstStoredAt': generated})
        context['evidenceSnapshotId'] = evidence_manifest['snapshotId']
        return result
    growth = isolated_section('growth', growth_section, previous, states, context)
    _, board_path, _ = write_outputs(p1, p11, p2, report, config)
    board = read_json(board_path)
    board['p2'] = public_fields(p2)
    board['p2'].pop('turnaroundRows', None)
    board['p2'].pop('turnaroundStatus', None)
    board['growth'] = growth
    board['p3'] = isolated_section('p3', lambda: refresh_holdings(previous.get('p3', {}), prices, fundamentals),
                                   previous, states, context)
    board['meta']['sourceSummary'] = f"가격 {report['latestPriceDate']} · 공시 {report['fundamentals'].get('asOfDate') or '미확인'} · 컨센서스 {report['fundamentals'].get('consensusAsOfDate') or '공급일 미확인'} · {report['runMode']}"
    board['meta']['uiContractVersion'] = load_contract()['version']
    board['meta']['audit'] = {'checkedAtKST': datetime.now(KST).strftime('%Y-%m-%d %H:%M'), 'sourceDate': report['latestPriceDate'],
                            'summary': report['runMode'] + ' · 구역별 검증 · 보유수량·평단 보존'}
    board['meta']['nextTradingDay'] = '거래소 개장일 확인 후 확정'
    board['meta']['runId'] = context['runId']
    board['meta']['refreshState'] = context
    board['meta']['note'] = '성장 조기포착은 공개 근거 기반 후보입니다. 주가 미반영 판단·신뢰도는 예측 확률이 아닙니다.'
    section_dir = out / 'sections'
    youtube_path = config['base_data_file'].parent/'youtube-market.json'
    if youtube_path.exists():
        youtube = refresh_youtube_prices(json.loads(youtube_path.read_text('utf-8-sig')), prices)
        # Morning captions, summaries and their verification dates remain intact.
        json_write(out/'youtube-market.test.json', public_fields(youtube))
    report['growth'] = collection
    report['holdings'] = board['p3'].get('refreshStatus', {})
    ledger = read_json(config['base_data_file'].parent/'recommendation-history.json', empty_ledger())
    performance_prices, performance_price_status = collect_performance_prices(ledger, prices, config, reuse=args.reuse_snapshot)
    performance = evaluate(ledger, performance_prices, generated_at=generated, cost_bps=config.get('performance_cost_bps', 0))
    performance['priceCollection'] = performance_price_status
    from performance_feedback import rank_recent
    combined = rank_recent(board, performance, source_date=report['latestPriceDate'],
                           generated_at=generated, snapshot_id=manifest['snapshotId'])
    combined['refreshState'] = dict(context, status='계산완료')
    combined['runId'] = context['runId']
    board, combined = attach_recommendation_history(
        board, combined, ledger, trading_sessions=list(pd.to_datetime(prices['date']).dt.date),
    )
    # Reuse this full run for the morning/afternoon issue stage.
    from issue_spread import refresh as refresh_issues
    refresh_issues(board=board, slot=getattr(args, 'issue_slot', None) or ('08:00' if datetime.now(KST).hour < 12 else '15:00'))
    json_write(board_path, public_fields(board))
    for section in ('p1', 'p11', 'p2', 'growth', 'p3', 'meta'):
        json_write(section_dir / f'{section}.test.json', public_fields(board[section]))
    states['combined'] = combined['refreshState']
    json_write(out/'combined_audit.test.json', combined)
    json_write(out/'combined-recommendations.test.json', combined)
    performance['feedback'] = combined['feedback']
    if (config['cache_dir']/'performance_prices.pkl.gz').exists():
        perf_snapshot = snapshot_files(config['cache_dir']/'snapshots',
            {'performancePrices': config['cache_dir']/'performance_prices.pkl.gz'},
            {'sourceCutoff': performance.get('priceDate'), 'firstStoredAt': generated})
        performance['priceSnapshotId'] = perf_snapshot['snapshotId']
    json_write(out/'recommendation-performance.test.json', performance)
    report['sectionStates'] = states
    report['performancePrices'] = performance_price_status
    report['snapshotId'] = manifest['snapshotId']
    if states['growth']['status'] == '계산완료':
        report['evidenceSnapshotId'] = context.get('evidenceSnapshotId')
    json_write(out/'collection_report.test.json', report)
    json_write(out/'refresh-status.test.json', {'runId': context['runId'], 'attemptedAt': generated,
        'sourceDate': report['latestPriceDate'], 'sections': states})
    print(json.dumps({'priceDate':report['latestPriceDate'], 'valueCount':len(p2.get('rows',[])),
                      'growthSectors':len(growth.get('sectors', [])), 'growthStocks':len(growth.get('rows', [])),
                      'combinedCount': len(combined.get('rows', [])), 'sections': states}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--issue-slot', choices=['08:00', '15:00'], help='지연된 전체 실행에도 원래 예약 단계 유지')
    parser.add_argument('--config', type=Path, default=Path('config.kis.example.json'))
    parser.add_argument('--reuse-snapshot', action='store_true', help='저장된 가격·재무만 재사용')
    parser.add_argument('--reuse-evidence', action='store_true', help='외부 근거 수집을 하지 않음')
    parser.add_argument('--full-refresh', action='store_true', help='정기 전체 재확인; 기본은 증분 수집')
    args = parser.parse_args()
    from issue_spread import trading_day
    if not trading_day(datetime.now(KST)):
        return
    config = load_config(args.config, None)
    with run_lock(config['cache_dir']):
        rebuild(args, config)


if __name__ == '__main__':
    main()
