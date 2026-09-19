"""Reproduce the named historical cases using downloaded OHLCV, without tuning."""
import json
from pathlib import Path
import pandas as pd
import yfinance as yf
import requests
import xml.etree.ElementTree as ET
from rotation_rules import features, score_candidate

ROOT = Path(__file__).resolve().parent
CASES = [
    ('403870', 'HPSP', '반도체', '2026-08-18', 'success'),
    ('003350', '한국화장품제조', '화장품', '2026-08-11', 'success'),
    ('101490', '에스앤에스텍', '반도체', '2026-09-10', 'false_positive'),
    ('088800', '에이스테크', '통신장비', '2026-08-05', 'false_positive'),
    ('222800', '심텍', '반도체', '2026-07-31', 'success'),
    ('240810', '원익IPS', '반도체', '2026-09-10', 'false_positive'),
    ('084370', '유진테크', '반도체', '2026-09-10', 'false_positive'),
]


def main():
    out = ROOT / 'test_output' / 'rotation_replay'
    out.mkdir(parents=True, exist_ok=True)
    results, coverage = [], []
    for ticker, name, sector, date, label in CASES:
        path = out / f'{ticker}.json'
        source = 'Yahoo Finance adjusted OHLCV; close*volume turnover proxy'
        attempts = []
        try:
            if path.exists():
                raw = pd.read_json(path, orient='table')
            else:
                symbol = ticker + ('.KS' if ticker == '003350' else '.KQ')
                raw = yf.Ticker(symbol).history(start='2026-03-01', end='2026-09-19', auto_adjust=True, timeout=20)
                attempts.append('Yahoo Finance')
                if raw.empty:
                    raise ValueError('empty response')
                raw = raw.reset_index().rename(columns=str.lower)
                raw['date'] = pd.to_datetime(raw.date).dt.tz_localize(None).dt.normalize()
                raw.to_json(path, orient='table', force_ascii=False)
            raw = raw.assign(ticker=ticker, name=name, sector=sector, market='KOSPI' if ticker == '003350' else 'KOSDAQ')
            raw['value'] = raw.close * raw.volume
            # Independent OHLCV check; retain discrepancies instead of silently choosing.
            check_path = out / f'{ticker}.naver.xml'
            check_url = f'https://fchart.stock.naver.com/sise.nhn?symbol={ticker}&timeframe=day&count=200&requestType=0'
            if not check_path.exists():
                response = requests.get(check_url, timeout=20)
                response.raise_for_status()
                check_path.write_bytes(response.content)
            alternate = pd.DataFrame([node.attrib['data'].split('|') for node in ET.fromstring(check_path.read_bytes().decode('euc-kr')).iter('item')],
                columns=['date','open','high','low','close','volume'])
            alternate['date'] = pd.to_datetime(alternate.date)
            for field in ['open','high','low','close','volume']:
                alternate[field] = pd.to_numeric(alternate[field])
            overlap = raw.merge(alternate, on='date', suffixes=('_yahoo','_naver'))
            overlap = overlap[overlap.date.le(pd.Timestamp(date))].tail(35)
            discrepancies = {field:int(((overlap[field+'_yahoo'] / overlap[field+'_naver'] - 1).abs() > .005).sum())
                for field in ['open','high','low','close','volume']}
            evaluated = features(raw)
            target = evaluated[evaluated.date.eq(pd.Timestamp(date))]
            if target.empty:
                raise ValueError('signal session missing')
            row = target.iloc[0].to_dict()
            outcome = score_candidate(row, [], 1_000_000_000)
            index = list(raw.date).index(pd.Timestamp(date))
            returns = {f'return{n}': round((float(raw.close.iloc[index+n])/row['close']-1)*100, 3)
                       if index+n < len(raw) else None for n in (1, 5, 10, 20)}
            # The actual old implementation had only turnover/history gates and a heat penalty.
            old_pass = row['value'] >= 1_000_000_000 and pd.notna(row['rotation_ret5'])
            old_heat = row['rotation_ret1'] > .1 or row['rotation_ret3'] > .15 or row['rotation_ret5'] > .25
            agreed_old_pass = (row['macd_ok'] and row['close'] >= row['rotation_ma20'] and
                               row['rotation_volume_ratio'] >= 2 and not old_heat)
            results.append(dict(ticker=ticker, name=name, signalDate=date, priorLabel=label,
                oldCodePriceGate=bool(old_pass), priorDiscussionTwoTimesGate=bool(agreed_old_pass),
                newPriceGate=outcome['eligible'], newResult=outcome,
                close=row['close'], resistance=row['resistance20'],
                prior20ClosingHigh=float(raw.close.iloc[max(0,index-20):index].max()),
                closeLocation=row['close_location'], upperWick=row['upper_wick'],
                highFade=row['high_fade'], macdAge=None if pd.isna(row['macd_age']) else row['macd_age'],
                ret3=row['rotation_ret3'], **returns))
            coverage.append(dict(ticker=ticker, name=name, status='collected', source=source,
                firstDate=str(raw.date.min().date()), lastDate=str(raw.date.max().date()),
                rows=len(raw), attempts=attempts, independentSource=check_url,
                checkedSessions=len(overlap), discrepanciesOverHalfPercent=discrepancies,
                fetchedAt=pd.Timestamp(path.stat().st_mtime, unit='s', tz='UTC').isoformat(),
                checkedAt=pd.Timestamp.now(tz='UTC').isoformat()))
        except Exception as exc:
            coverage.append(dict(ticker=ticker, name=name, status='failed', attempts=attempts, reason=str(exc)))
        print(ticker, coverage[-1]['status'], flush=True)
    report = dict(cases=results, coverage=coverage,
        limitations=['No point-in-time fundamental/catalyst evidence supplied: heat exception stays closed.',
                     'Single-stock price gates only; historical whole-market Top5 and sector stages not reconstructed.',
                     'Turnover is close times volume, not exchange-reported traded value.',
                     'Old code gate is not the former discussion rule; both are reported separately.',
                     'Unmatured forward returns are null, not zero.'])
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
