"""Recover dated recommendation snapshots from canonical Git; never invent publication times."""
import argparse
import json
import subprocess
from pathlib import Path
from refresh_store import digest, read_json, json_write


def restore(ledger):
    log = subprocess.check_output(['git','log','--reverse','--format=%H %cI','--','data.json'], text=True)
    seen, skipped = set(), 0
    for line in log.splitlines():
        commit, date = line.split(' ',1)
        # Only older history preceding the real publication ledger needs recovery.
        if date[:10] >= '2026-09-07':
            continue
        try:
            board = json.loads(subprocess.check_output(['git','show',commit+':data.json']))
        except (ValueError, subprocess.CalledProcessError):
            continue
        for section in ('p1','p11','p2','growth'):
            data = board.get(section,{})
            rows = list(data.get('rows',[]))
            if section == 'growth':
                rows += [r for s in data.get('sectors',[]) for r in s.get('stocks',[])]
            records = []
            for row in rows:
                ticker = str(row.get('ticker',''))
                if len(ticker) != 6 or not ticker.isdigit():
                    skipped += 1
                    continue
                records.append(dict(ticker=ticker,name=row['name'],sector=row.get('sector'),
                    group=section,rank=row.get('rank',row.get('typeRank')),sourceRow=row))
            fingerprint = (section, digest(records))
            if not records or fingerprint in seen:
                continue
            seen.add(fingerprint)
            identity = 'git-'+digest({'commit':commit,'section':section})
            if any(c['id']==identity for c in ledger['cohorts']):
                continue
            source_date = data.get('sourceDate') or data.get('refreshState',{}).get('sourceCutoff') or board.get('meta',{}).get('audit',{}).get('sourceDate') or date[:10]
            ledger['cohorts'].append(dict(id=identity,section=section,recordedAt=date,
                generatedAt=date,recordBasis='과거 Git 저장일 복원 · 사이트 공개시각 미확인',
                sourceCommit=commit,sourceDate=source_date,engineVersion='archive-'+commit[:8],
                ruleVersion=data.get('methodVersion',section+'-historical'),records=records,universeId=None))
    ledger['archiveRecovery'] = dict(source='canonical Git data.json',skippedWithoutTicker=skipped,
        notice='종목코드가 확인되는 저장본만 복원. 저장 이전의 최초 추천 여부와 사이트 공개시각은 미확인.')
    return ledger


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('test_output/recommendation-history.restored.json'))
    args=parser.parse_args()
    result=restore(read_json(Path('recommendation-history.json'),{'schemaVersion':1,'cohorts':[],'universes':{}}))
    json_write(args.output,result)
    print(json.dumps({'cohorts':len(result['cohorts']),**result['archiveRecovery']},ensure_ascii=False))
