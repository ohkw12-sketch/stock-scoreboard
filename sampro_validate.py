import argparse
import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

def validate(data):
    assert data['schemaVersion'] == 1
    datetime.fromisoformat(data['checkedAt'])
    assert data['status'] and data['editions']
    today=datetime.now(timezone(timedelta(hours=9))).date()
    seen=set()
    for edition in data['editions']:
        day=date.fromisoformat(edition['date'])
        assert day<=today and day not in seen, 'future or duplicate date'
        seen.add(day)
        assert edition['headline'] and edition['overview'] and edition['items']
        datetime.fromisoformat(edition['updatedAt'])
        sources={s['id']:s for s in edition['sources']}
        assert sources
        for s in sources.values():
            url=urlparse(s['url'])
            assert url.scheme=='https' and url.netloc=='apps.3protv.com' and url.path.startswith('/news/view/')
            assert datetime.fromisoformat(s['publishedAt']).date()==day, 'source date mismatch'
        for item in edition['items']:
            assert item['category'] in ('매크로','기업')
            assert item['source'] in sources
            assert all(isinstance(item[k],str) and item[k].strip() for k in ('title','fact','view','watch','attribution'))

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('path',nargs='?',default='sampro-market.json')
    p.add_argument('--promote',action='store_true')
    args=p.parse_args()
    raw=Path(args.path).read_text(encoding='utf-8')
    data=json.loads(raw)
    validate(data)
    if args.promote:
        target=Path(__file__).parent/'sampro-market.json'
        temp=target.with_suffix('.json.tmp')
        temp.write_text(raw,encoding='utf-8')
        temp.replace(target)
    print('Sampro validation passed:',len(data['editions']),'editions')
