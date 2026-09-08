"""Refresh only research outputs against the unchanged published project candidates."""
from pathlib import Path
from datetime import datetime
import argparse
from refresh_store import read_json, json_write, load_verified_frames
from recommendation_performance import evaluate, KST
from performance_prices import collect_performance_prices
from performance_feedback import rank_recent
from rotation_screener import load_config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--reuse-prices',action='store_true')
    parser.add_argument('--ledger',type=Path,default=Path('recommendation-history.json'))
    args=parser.parse_args()
    board=read_json(Path('data.json'))
    ledger=read_json(args.ledger)
    prices,_,report=load_verified_frames(Path('test_output'),Path('cache'))
    config=load_config(Path('config.kis.example.json'),None)
    prices,status=collect_performance_prices(ledger,prices,config,reuse=args.reuse_prices)
    generated=datetime.now(KST).isoformat(timespec='seconds')
    performance=evaluate(ledger,prices,generated_at=generated)
    performance['priceCollection']=status
    context=board['meta']['refreshState']
    combined=rank_recent(board,performance,source_date=context['sourceCutoff'],
                         generated_at=generated,snapshot_id=context['snapshotId'])
    # New research publication belongs to the currently published price/candidate snapshot.
    combined['runId']=board['meta']['runId']
    combined['refreshState']={**context,'generatedAt':generated,'status':'계산완료','engineVersion':'combined-feedback-2.0'}
    performance['feedback']=combined['feedback']
    for name,data in [('combined-recommendations',combined),('recommendation-performance',performance)]:
        json_write(Path('test_output')/(name+'.test.json'),data)
    print(__import__('json').dumps({'priceStatus':status,'records':len(performance['rows']),
        'evaluated':sum(r['currentReturnPct'] is not None for r in performance['rows']),
        'topReturns':[{k:r.get(k) for k in ('name','group','recommendationDate','currentReturnPct')} for r in performance['rows'][:5]],
        'horizon':combined['feedback']['horizon'],'patterns':len(combined['feedback']['patterns']),
        'recommendations':[(r['name'],r['combinedScore']) for r in combined['rows']]},ensure_ascii=False))


if __name__=='__main__':
    main()
