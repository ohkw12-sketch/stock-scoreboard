import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pandas as pd

from growth_discovery import (KST, build_growth_board, collect_disclosures, collect_news_hints,
                              confidence, json_write, merge_events, parse_disclosure)
from growth_documents import collect_verified_documents
from growth_sources import collect_trade_evidence, consensus_evidence
from youtube_content import collect_youtube_content
from tests.test_growth_discovery import event, prices

NOW = datetime(2026, 9, 7, 12, tzinfo=KST)


def contract_html(subject='장비 공급'):
    fields = [('판매ㆍ공급계약 내용', subject), ('확정 계약금액', '1000'),
              ('매출액 대비', '20'), ('계약상대방', '고객사'), ('시작일', '2026-08-01'),
              ('종료일', '2027-08-01'), ('계약(수주)일자', '2026-08-01')]
    return '<table>' + ''.join(f'<tr><td>{k}</td><td>{v}</td></tr>' for k, v in fields) + '</table>'


def disclosure(ticker='000001', receipt='20260801000001', market='Y', title='단일판매ㆍ공급계약체결'):
    return dict(stock_code=ticker, rcept_no=receipt, rcept_dt='20260801', corp_cls=market, report_nm=title)


def document(**changes):
    value = dict(source='기업 IR', sourceType='IR', url='https://company.test/ir/1',
                 title='해외 계약 설명', publishedAt='2026-08-01', fetchedAt='2026-09-06T09:00:00+09:00',
                 text='유럽 고객과 장비 공급계약을 체결했습니다.', accessBasis='user_supplied',
                 verification=dict(status='verified', reviewer='검토자', verifiedAt='2026-09-06T10:00:00+09:00'),
                 facts=[dict(ticker='000001', independentEventId='original-contract', kind='수주',
                             polarity='positive', validity='active', excerpt='유럽 고객과 장비 공급계약을 체결했습니다.')])
    value.update(changes)
    return value


class IngestionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.config = {'cache_dir': self.cache, 'growth_history_days': 40}
        self.growth = self.cache / 'growth'
        self.growth.mkdir()

    def raw(self, item):
        path = self.growth / 'documents' / (item['rcept_no'] + '.html')
        path.parent.mkdir(exist_ok=True)
        path.write_text(contract_html(), encoding='utf-8')

    def index(self, items, covered=('000001', '000002')):
        json_write(self.growth / 'disclosure_index.json', dict(items=items, scannedThrough='2026-09-06',
                   coverageVersion=1, historyCoveredTickers=list(covered),
                   marketByTicker={r['stock_code']: r['corp_cls'] for r in items}))

    def test_reuse_all_sources_performs_zero_network_and_keeps_dates(self):
        old = '2026-09-01T12:00:00+09:00'
        for ledger, status in [('event_ledger.json', 'collection_status.json'),
                               ('news_queue.json', 'news_status.json'),
                               ('trade_ledger.json', 'trade_status.json'),
                               ('verified_document_ledger.json', 'verified_document_status.json')]:
            json_write(self.growth / ledger, [])
            json_write(self.growth / status, dict(status='정상', checkedAt=old))
        with patch('growth_discovery._api_key', side_effect=AssertionError('credential read')), \
             patch('growth_discovery.credential', side_effect=AssertionError('credential read')), \
             patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            results = [collect_disclosures(self.config, {'000001'}, NOW, reuse=True),
                       collect_news_hints(self.config, ['기업'], NOW, reuse=True),
                       collect_trade_evidence(self.config, NOW, reuse=True),
                       collect_verified_documents(self.config, {'000001'}, NOW, reuse=True),
                       collect_youtube_content(self.config, NOW, reuse=True)]
        for _, status in results[:4]:
            self.assertEqual(status['checkedAt'], old)
            self.assertEqual(status['networkRequests'], 0)
        self.assertEqual(results[-1][1]['status'], '미수집')

    def test_source_request_count_not_overwritten_by_evaluation_universe(self):
        board = build_growth_board(prices(), pd.DataFrame(), [], {'requestedTickers': 2}, NOW)
        self.assertEqual(board['dataStatus']['requestedTickers'], 2)
        self.assertEqual(board['dataStatus']['evaluatedTickers'], 6)

    def test_unchanged_disclosure_is_not_downloaded_or_reparsed(self):
        item = disclosure()
        self.raw(item)
        def listing(endpoint, params, *_):
            return {'status': '000', 'total_page': 1, 'list': [item]} if params.get('corp_cls') == 'Y' else {'status': '013'}
        with patch('growth_discovery._api_key', return_value='test'), \
             patch('growth_discovery._request_json', side_effect=listing), \
             patch('growth_discovery._request_bytes', side_effect=AssertionError('download')):
            initial, first = collect_disclosures(self.config, {'000001'}, NOW)
            with patch('growth_discovery.parse_disclosure', side_effect=AssertionError('reparse')):
                events, second = collect_disclosures(self.config, {'000001'}, NOW + timedelta(days=1))
        self.assertEqual(first['documentsReparsed'], 1)
        self.assertEqual(second['parsedCacheHits'], 1)
        self.assertEqual(second['documentsDownloaded'], 0)
        self.assertEqual(events[0]['fetchedAt'], initial[0]['fetchedAt'])
        self.assertNotEqual(events[0]['lastVerified'], initial[0]['lastVerified'])

    def test_changed_raw_document_invalidates_parsed_cache(self):
        item = disclosure()
        self.raw(item)
        self.index([item], ('000001',))
        with patch('growth_discovery._api_key', return_value='test'), \
             patch('growth_discovery._request_json', return_value={'status': '013'}):
            collect_disclosures(self.config, {'000001'}, NOW)
            (self.growth / 'documents' / (item['rcept_no'] + '.html')).write_text(contract_html('다른 계약'), encoding='utf-8')
            events, status = collect_disclosures(self.config, {'000001'}, NOW)
        self.assertEqual(status['documentsReparsed'], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['subject'], '다른 계약')

    def test_new_ticker_gets_targeted_history_backfill(self):
        first, second = disclosure(), disclosure('000002', '20260801000002', 'K')
        self.index([first], ('000001',))
        self.raw(first)
        self.raw(second)
        (self.cache / 'dart_corp_codes.csv').write_text('ticker,corp_code\n000002,00123456\n', encoding='utf-8')
        requests = []
        def listing(endpoint, params, *_):
            requests.append(params)
            return {'status': '000', 'list': [second], 'total_page': 1} if params.get('corp_code') else {'status': '013'}
        with patch('growth_discovery._api_key', return_value='test'), patch('growth_discovery._request_json', side_effect=listing):
            _, status = collect_disclosures(self.config, {'000001', '000002'}, NOW)
        self.assertTrue(any(r.get('corp_code') == '00123456' for r in requests))
        self.assertEqual(status['backfillTickers'], ['000002'])
        self.assertEqual(status['completedTickers'], 2)
        self.assertEqual(status['missingTickers'], [])

    def test_failed_market_does_not_invalidate_other_market(self):
        first, second = disclosure(), disclosure('000002', '20260801000002', 'K')
        self.index([first, second])
        self.raw(first)
        self.raw(second)
        def listing(endpoint, params, *_):
            if params.get('corp_cls') == 'Y':
                raise OSError('source unavailable')
            return {'status': '013'}
        with patch('growth_discovery._api_key', return_value='test'), patch('growth_discovery._request_json', side_effect=listing):
            events, status = collect_disclosures(self.config, {'000001', '000002'}, NOW)
        by_ticker = {e['ticker']: e for e in events}
        self.assertEqual(by_ticker['000001']['status'], '상태확인필요')
        self.assertEqual(by_ticker['000002']['status'], '유효')
        self.assertEqual(status['missingTickers'], ['000001'])
        self.assertEqual(status['scannedThrough'], '2026-09-06')

    def test_failed_correction_quarantines_related_company_only(self):
        first, second = disclosure(), disclosure('000002', '20260801000002', 'K')
        correction = disclosure(receipt='20260907000001', title='[정정]단일판매ㆍ공급계약체결')
        self.index([first, second, correction])
        self.raw(first)
        self.raw(second)
        with patch('growth_discovery._api_key', return_value='test'), \
             patch('growth_discovery._request_json', return_value={'status': '013'}), \
             patch('growth_discovery._request_bytes', side_effect=OSError('failed')), \
             patch('growth_discovery.time.sleep'):
            events, status = collect_disclosures(self.config, {'000001', '000002'}, NOW)
        self.assertEqual({e['ticker']: e['status'] for e in events}, {'000001': '상태확인필요', '000002': '유효'})
        self.assertEqual(len(status['documentsFailed']), 1)

    def test_news_queue_merges_republication_and_keeps_prior_on_failure(self):
        json_write(self.growth / 'news_queue.json', [dict(originallink='https://news.test/a?utm_source=old', entity='기업A', status='원문검증대기')])
        new = dict(originallink='https://news.test/a', title='계약', pubDate='Mon, 07 Sep 2026 09:00:00 +0900')
        calls = 0
        def response(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError('failed second company')
            return io.BytesIO(json.dumps({'items': [new]}).encode())
        with patch('growth_discovery.credential', return_value='test'), patch('urllib.request.urlopen', side_effect=response), patch('growth_discovery.time.sleep'):
            queue, status = collect_news_hints(self.config, ['기업A', '기업B'], NOW)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['entities'], ['기업A'])
        self.assertEqual(status['status'], '부분수집')
        with patch('growth_discovery.credential', return_value=None):
            again, missing = collect_news_hints(self.config, ['기업A'], NOW)
        self.assertEqual(again, queue)
        self.assertEqual(missing['status'], '설정필요')

    def test_repeated_news_run_uses_saved_check_time_without_network(self):
        with patch('growth_discovery.credential', return_value='test'), \
             patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"items":[]}')):
            collect_news_hints(self.config, ['기업'], NOW)
        with patch('growth_discovery.credential', return_value='test'), patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            _, status = collect_news_hints(self.config, ['기업'], NOW + timedelta(hours=1))
        self.assertEqual(status['attemptedNames'], 0)
        self.assertEqual(status['cachedNames'], 1)

    def test_news_page_limit_resumes_without_claiming_complete_history(self):
        self.config['growth_news_max_pages'] = 2
        starts = []
        def response(request, **kwargs):
            start = int(parse_qs(urlsplit(request.full_url).query)['start'][0])
            starts.append(start)
            rows = [dict(originallink=f'https://news.test/{start+i}', title='계약',
                         pubDate='Mon, 07 Sep 2026 09:00:00 +0900') for i in range(100)]
            return io.BytesIO(json.dumps({'items': rows}).encode())
        with patch('growth_discovery.credential', return_value='test'), patch('urllib.request.urlopen', side_effect=response):
            _, first = collect_news_hints(self.config, ['기업'], NOW)
            _, second = collect_news_hints(self.config, ['기업'], NOW + timedelta(minutes=1))
        self.assertEqual(starts, [1, 101, 1, 201])
        self.assertEqual(first['status'], '부분수집')
        self.assertEqual(second['status'], '부분수집')
        state = json.loads((self.growth / 'news_search_state.json').read_text('utf-8'))
        self.assertEqual(state['기업']['nextSearchStart'], 301)
        self.assertNotIn('checkedAt', state['기업'])

    def test_news_and_ir_same_event_count_once_and_keep_original_date(self):
        a = event(identity='original-contract', published='2026-04-01')
        b = event(identity='article', published='2026-09-01', independentEventId='original-contract', sourceType='뉴스')
        merged = merge_events([a, b])
        self.assertEqual(confidence(merged)['evidenceCount'], 1)
        self.assertEqual({e['firstPublished'] for e in merged}, {'2026-04-01'})
        cancelled = event(identity='cancel', published='2026-09-06', independentEventId='original-contract', polarity='negative', status='무효')
        self.assertEqual(confidence(merge_events(merged + [cancelled]))['evidenceCount'], 0)

    def test_same_day_contracts_for_distinct_customers_are_independent(self):
        first = parse_disclosure(disclosure(), contract_html(), NOW.isoformat())
        second = parse_disclosure(disclosure(receipt='20260801000002'), contract_html().replace('고객사', '다른 고객'), NOW.isoformat())
        self.assertEqual(confidence(merge_events([first, second]))['evidenceCount'], 2)

    def test_changed_contract_date_correction_quarantines_old_positive(self):
        original = event(identity='original', published='2026-04-01')
        correction = event(identity='correction', published='2026-09-01', signedAt='2026-08-01',
                           originalPublished='2026-04-01', correction=True)
        merged = merge_events([original, correction])
        self.assertEqual(next(e for e in merged if e['eventId']=='original')['status'], '정정관계확인필요')
        self.assertEqual(confidence(merged)['evidenceCount'], 1)

    def test_unreviewed_document_never_becomes_growth_evidence(self):
        json_write(self.growth / 'verified_documents_input.json', {'documents': [document(verification={})]})
        events, status = collect_verified_documents(self.config, {'000001'}, NOW)
        self.assertEqual(events, [])
        self.assertEqual(len(status['failures']), 1)

    def test_verified_document_idempotent_and_failed_revision_is_quarantined(self):
        path = self.growth / 'verified_documents_input.json'
        json_write(path, {'documents': [document()]})
        events, status = collect_verified_documents(self.config, {'000001'}, NOW)
        again, _ = collect_verified_documents(self.config, {'000001'}, NOW + timedelta(hours=1))
        self.assertEqual(events, again)
        self.assertEqual(events[0]['materiality'], 0)
        self.assertEqual(events[0]['lastVerified'], '2026-09-06T10:00:00+09:00')
        bad = document(text='다른 내용으로 변경되었습니다.')
        json_write(path, {'documents': [bad]})
        retained, status = collect_verified_documents(self.config, {'000001'}, NOW)
        self.assertEqual(retained[0]['status'], '상태확인필요')
        self.assertEqual(len(status['failures']), 1)

    def test_missing_document_and_youtube_input_are_honestly_pending(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            _, docs = collect_verified_documents(self.config, {'000001'}, NOW)
            _, youtube = collect_youtube_content(self.config, NOW)
        self.assertEqual(docs['status'], '원문검증대기')
        self.assertEqual(youtube['status'], '원문검증대기')

    def test_consensus_month_and_unknown_dates_never_invent_public_day(self):
        common = dict(ticker='000001', estimate_period='2026.12E', consensus_sales_1y_growth=20,
                      consensus_op_1y_growth=30, consensus_prior_sales=100,
                      consensus_forward_sales=120, consensus_forward_op=10)
        frame = pd.DataFrame([dict(common, consensus_as_of='2026-08', consensus_as_of_precision='month'),
                              dict(common, consensus_as_of=None, consensus_as_of_precision='unknown')])
        self.assertEqual(consensus_evidence(frame, {'freshTickers': ['000001']}, NOW), [])

    def test_consensus_keeps_actual_verification_time_not_run_time(self):
        frame = pd.DataFrame([dict(ticker='000001', consensus_as_of='2026-08-31',
                                   consensus_as_of_precision='day', estimate_period='2026.12E',
                                   consensus_sales_1y_growth=20, consensus_op_1y_growth=30,
                                   consensus_prior_sales=100, consensus_forward_sales=120, consensus_forward_op=10)])
        verified = '2026-09-05T11:00:00+09:00'
        status = {'freshTickers': ['000001'], 'verifiedAtByTicker': {'000001': verified}}
        events = consensus_evidence(frame, status, NOW)
        self.assertEqual(events[0]['status'], '유효')
        self.assertEqual(events[0]['lastVerified'], verified)
        self.assertEqual(events[0]['fetchedAt'], verified)
        self.assertEqual(events[0]['firstPublished'], '2026-08-31')
        missing = consensus_evidence(frame, {'freshTickers': ['000001']}, NOW)
        self.assertEqual(missing[0]['status'], '상태확인필요')
        self.assertIsNone(missing[0]['lastVerified'])

    def test_youtube_content_preserves_verified_time_on_repeat(self):
        video = dict(videoId='abcdefghijk', accessBasis='user_supplied', transcript='신규 수주를 확인해야 합니다.',
                     channel='채널', title='시황', publishedAt='2026-09-01',
                     verification=dict(status='verified', reviewer='검토자', verifiedAt='2026-09-06T10:00:00+09:00'),
                     statements=[dict(quote='신규 수주를 확인해야 합니다.', kind='관찰')])
        json_write(self.cache / 'youtube' / 'verified_transcripts_input.json', {'videos': [video]})
        first, initial = collect_youtube_content(self.config, NOW)
        second, again = collect_youtube_content(self.config, NOW + timedelta(hours=1))
        self.assertEqual(first, second)
        self.assertEqual(initial['contentUpdatedKST'], again['contentUpdatedKST'])
        self.assertEqual(again['changedVideos'], 0)


class TradeFreshnessTest(unittest.TestCase):
    def test_missing_product_in_latest_release_does_not_revalidate_old_month(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'cache_dir': Path(directory), 'trade_cache_hours': 0}
            url_old = 'https://eiec.kdi.re.kr/policy/materialView.do?num=1'
            url_new = 'https://eiec.kdi.re.kr/policy/materialView.do?num=2'
            listing = '<a href="/policy/materialView.do?num=2">2026년 8월 수출입 동향</a><a href="/policy/materialView.do?num=1">2026년 7월 수출입 동향</a>'
            def release(month, product):
                return f'<html>2026년 {month}월 수출입 동향 산업통상부<div class="editor">산업통상부는 ’26.{month+1}.1. 발표. {product}(+20%)</div></html>'
            pages = {url_old: release(7, '화장품'), url_new: release(8, '반도체')}
            def html(url):
                return pages[url] if url in pages else listing
            with patch('growth_sources.fetch_html', side_effect=html):
                events, status = collect_trade_evidence(config, NOW)
            cosmetics = next(e for e in events if e['product'] == '화장품')
            self.assertNotEqual(cosmetics['status'], '유효')
            self.assertIn('화장품', status['missingProducts'])
            self.assertEqual(status['latestPeriod'], '2026-08')
            fetched = []
            def check_cached(url):
                fetched.append(url)
                return html(url)
            with patch('growth_sources.fetch_html', side_effect=check_cached):
                collect_trade_evidence(config, NOW + timedelta(days=1))
            self.assertNotIn(url_old, fetched)
            self.assertIn(url_new, fetched)


if __name__ == '__main__':
    unittest.main()
