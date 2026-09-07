"""Verify the published generation before recording recommendations. Never backdate availability."""
import argparse
import hashlib
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

from refresh_store import digest, json_write, read_json, run_lock, load_verified_frames
from recommendation_performance import KST, empty_ledger, evaluate, record_publication
from performance_prices import collect_performance_prices

ROOT = Path(__file__).resolve().parent
SITE = 'https://stock-scoreboard.pages.dev/'


def fetch_json(name):
    request = urllib.request.Request(SITE + name + '?verify=' + str(time.time_ns()),
                                     headers={'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verified_generation(board, combined, remote_board, remote_combined):
    run_id = board.get('meta', {}).get('runId')
    return bool(run_id and remote_board.get('meta', {}).get('runId') == run_id
                and digest(board) == digest(remote_board) and digest(combined) == digest(remote_combined))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--attempts', type=int, default=1)
    parser.add_argument('--interval', type=int, default=20)
    parser.add_argument('--cost-bps', type=float)
    args = parser.parse_args()
    cost_bps = args.cost_bps if args.cost_bps is not None else read_json(ROOT/'config.kis.example.json', {}).get('performance_cost_bps', 0)
    board, combined = read_json(ROOT/'data.json'), read_json(ROOT/'combined-recommendations.json')
    for attempt in range(max(1, args.attempts)):
        try:
            confirmed = verified_generation(board, combined, fetch_json('data.json'), fetch_json('combined-recommendations.json'))
        except (OSError, ValueError):
            confirmed = False
        if confirmed:
            break
        if attempt+1 < args.attempts:
            time.sleep(max(1, args.interval))
    else:
        raise RuntimeError('정확한 사이트 반영을 확인하지 못했습니다. 추천 기록·공개일을 만들지 않았습니다.')
    observed = datetime.now(KST).isoformat(timespec='seconds')
    prices, _, _ = load_verified_frames(ROOT/'test_output', ROOT/'cache')
    source_date = board.get('meta', {}).get('refreshState', {}).get('sourceCutoff')
    if str(pd.to_datetime(prices.date).max().date()) != source_date:
        raise RuntimeError('공개 자료와 성과 가격 스냅샷 기준일이 다릅니다.')
    version = digest({name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
        'rotation_screener.py', 'growth_discovery.py', 'dart_fundamentals.py', 'kis_consensus.py',
        'growth_sources.py', 'growth_documents.py', 'combined_recommendations.py')})[:16]
    with run_lock(ROOT/'cache/publication'):
        ledger = record_publication(read_json(ROOT/'recommendation-history.json', empty_ledger()),
                                    board, combined, set(prices.ticker), observed_at=observed, engine_version=version)
        json_write(ROOT/'recommendation-history.json', ledger)
        evaluation_prices, price_status = collect_performance_prices(ledger, prices, {'cache_dir': ROOT/'cache'}, reuse=True)
        performance = evaluate(ledger, evaluation_prices, generated_at=observed, cost_bps=cost_bps)
        performance['priceCollection'] = price_status
        json_write(ROOT/'recommendation-performance.json', performance)
        if combined.get('runId') == board['meta']['runId']:
            combined.update(publicationState='confirmed', status='공개 확인 완료 · 추천 기록 보존',
                            lastPublicationCheckedAt=observed)
            combined.setdefault('observedPublishedAt', observed)
            json_write(ROOT/'combined-recommendations.json', combined)
    print(json.dumps({'status': '공개 확인 기록 완료', 'observedAt': observed,
                      'cohortCount': len(ledger['cohorts'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
