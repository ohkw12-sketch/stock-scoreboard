"""Explicit user holdings imports and dated, sourced assessments."""
import copy
import math
from datetime import date
from urllib.parse import urlparse


def select_holdings(payload):
    date.fromisoformat(payload['sourceDate'])
    chosen, identities = {}, {}
    for image in payload['images']:
        seen = set()
        for entry in image['rows']:
            row = copy.deepcopy(entry)
            ticker, name = row['ticker'], row['name']
            if not isinstance(ticker, str) or len(ticker) != 6 or not ticker.isdigit() or not name:
                raise ValueError('종목 식별 오류')
            if ticker in seen or (name in identities and identities[name] != ticker):
                raise ValueError('중복 또는 불일치 종목')
            seen.add(ticker)
            identities[name] = ticker
            for key in ('qty', 'avg', 'quotedPrice'):
                value = row[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError('보유 입력은 양수여야 합니다.')
            if row['qty'] != int(row['qty']):
                raise ValueError('수량은 정수여야 합니다.')
            row['sourceImage'] = image['id']
            row['sourceDate'] = payload['sourceDate']
            previous = chosen.get(ticker)
            if previous and previous['name'] != name:
                raise ValueError('동일 코드의 이름 불일치')
            if previous and previous['avg'] == row['avg'] and previous['qty'] != row['qty']:
                raise ValueError('동일 평단에 수량이 달라 확인 필요')
            if previous is None or row['avg'] < previous['avg']:
                chosen[ticker] = row
    if not chosen:
        raise ValueError('보유 입력 없음')
    return list(chosen.values())


def validate_assessments(p3):
    holdings = {r['ticker']: r['name'] for r in p3['rows']}
    seen = set()
    for item in p3.get('assessments', []):
        ticker = item['ticker']
        if ticker in seen or holdings.get(ticker) != item['name']:
            raise ValueError('평가 대상 보유 종목 불일치')
        seen.add(ticker)
        date.fromisoformat(item['checkedAt'])
        for key in ('title', 'fact', 'interpretation', 'watch', 'period'):
            if not item.get(key):
                raise ValueError('근거 없는 평가')
        if not item.get('sources'):
            raise ValueError('평가 출처 없음')
        for source in item['sources']:
            parsed = urlparse(source['url'])
            if parsed.scheme != 'https' or not parsed.netloc or not source.get('title'):
                raise ValueError('평가 출처 오류')
