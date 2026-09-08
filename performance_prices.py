"""Targeted adjusted-price recovery for published recommendations; never changes valuation inputs."""
from pathlib import Path
import os
from datetime import datetime, timezone
import pandas as pd

from refresh_store import json_write, read_json
from rotation_screener import MarketDataLoader


def collect_performance_prices(ledger, base_prices, config, *, reuse=False):
    tickers = {str(r['ticker']).zfill(6) for c in ledger.get('cohorts', []) for r in c.get('records', [])}
    if not tickers:
        return base_prices, {'status': '공개 확인 추천 없음', 'requested': 0, 'networkRequests': 0}
    cache = Path(config['cache_dir'])
    path, status_path = cache/'performance_prices.pkl.gz', cache/'performance_price_status.json'
    prior = pd.read_pickle(path) if path.exists() else pd.DataFrame()
    previous_status = read_json(status_path, {})
    status = {'source': '추천 종목 Yahoo 공급자 수정주가', 'requested': len(tickers)}
    latest = pd.Timestamp(base_prices.date.max()).normalize()
    if not reuse and not prior.empty and previous_status.get('checkedAt'):
        elapsed = (pd.Timestamp.now(tz='UTC')-pd.Timestamp(previous_status['checkedAt'])).total_seconds()
        covered = set(prior.loc[pd.to_datetime(prior.date).eq(latest), 'ticker'])
        reuse = elapsed < 86400 and tickers.issubset(covered)
    if reuse:
        status.update(previous_status, status='저장 수정주가 재사용' if not prior.empty else '수정주가 수집 대기',
                      networkRequests=0)
    else:
        try:
            # The existing provider adapter handles new dates, correction overlap and
            # full-history repair when an adjustment factor changes. It is used only
            # for published tickers; the primary whole-market price set stays untouched.
            earliest = min(pd.Timestamp(c.get('recordedAt') or c['observedPublishedAt']).tz_localize(None).normalize()
                           for c in ledger['cohorts'] if c.get('records'))
            missing_history = tickers - (set(prior.ticker) if not prior.empty else set())
            adapted = dict(config, refresh_universe=False,
                           force_full_prices=bool(missing_history),
                           lookback_business_days=max(int(config.get('lookback_business_days', 280)),
                                                      (latest-earliest).days+10))
            loader = MarketDataLoader(adapted)
            loader._requested_tickers = tickers
            loader._history = prior if not prior.empty else None
            fresh = loader._yfinance()
            valid = fresh.get('adjusted_basis', pd.Series('', index=fresh.index)).eq('provider_adjusted_close')
            valid &= fresh.get('price_date_verified', pd.Series(False, index=fresh.index)).eq(True)
            fresh = fresh[valid & fresh.ticker.isin(tickers)].copy()
            if fresh.empty:
                raise RuntimeError('검증된 수정주가 응답 없음')
            prior = pd.concat([prior, fresh], ignore_index=True).drop_duplicates(['ticker', 'date'], keep='last').sort_values('date')
            cache.mkdir(parents=True, exist_ok=True)
            temporary = cache/'performance_prices.tmp.gz'
            prior.to_pickle(temporary)
            os.replace(temporary, path)
            covered = set(prior.loc[pd.to_datetime(prior.date).eq(latest), 'ticker']) & tickers
            status.update(status='정상' if covered == tickers else '일부 누락', covered=len(covered),
                          missingTickers=sorted(tickers-covered), priceDate=str(latest.date()),
                          checkedAt=datetime.now(timezone.utc).isoformat())
            json_write(status_path, status)
        except Exception as exc:
            status.update(status='갱신 실패·기존 수정주가 유지', error=type(exc).__name__,
                          previousPriceDate=previous_status.get('priceDate'))
            json_write(status_path, status)
    if prior.empty:
        return base_prices, status
    selected = prior[prior.ticker.isin(tickers)]
    merged = pd.concat([base_prices, selected], ignore_index=True).drop_duplicates(['ticker', 'date'], keep='last').sort_values('date')
    return merged, status
