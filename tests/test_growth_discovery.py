import copy
import json
import unittest
from datetime import datetime

import pandas as pd

from growth_discovery import (KST, PriceResponse, build_growth_board, confidence,
                              fundamental_profile, merge_events, parse_disclosure)

NOW = datetime(2026, 9, 5, 9, tzinfo=KST)


def event(ticker='000001', identity='a', published='2026-04-01', **changes):
    e = dict(ticker=ticker, eventId=identity, receipt=identity, kind='수주', subject='장비공급',
             signedAt=published, firstPublished=published, publishedAt=published, source='DART',
             sourceType='공시', status='유효', polarity='positive', activeUntil='2027-06-30',
             materiality=70, lastVerified='2026-09-05T09:00:00+09:00')
    e.update(changes)
    return e


def prices(count=6):
    dates = pd.bdate_range('2026-01-01', '2026-09-04')
    return pd.DataFrame([dict(ticker=f'{i:06d}', name=f'종목{i}', sector=f'산업{i//3}', date=d,
                              close=100.0, high=101, low=99, open=100, adjusted=True)
                         for i in range(1,count+1) for d in dates])


class GrowthTest(unittest.TestCase):
    def test_no_positive_evidence_means_no_candidates(self):
        board = build_growth_board(prices(), pd.DataFrame(), [], {}, NOW)
        self.assertEqual(board['rows'], [])
        self.assertEqual(board['sectors'], [])

    def test_old_unreacted_event_survives(self):
        board = build_growth_board(prices(), pd.DataFrame(), [event()], {}, NOW)
        self.assertEqual(len(board['rows']),1)
        self.assertEqual(board['rows'][0]['oldEvidenceCount'],1)
        self.assertEqual(board['rows'][0]['priceReflection'],'미반영 가능')

    def test_old_unknown_price_adjustments_do_not_claim_underreaction(self):
        p = prices().drop(columns='adjusted')
        board = build_growth_board(p, pd.DataFrame(), [event()], {}, NOW)
        self.assertEqual(board['rows'], [])

    def test_recent_single_evidence_can_enter_with_unknown_price(self):
        p = prices().drop(columns='adjusted')
        board = build_growth_board(p, pd.DataFrame(), [event(published='2026-09-03')], {}, NOW)
        self.assertEqual(len(board['rows']),1)
        self.assertEqual(board['rows'][0]['confidence'],'초기')

    def test_expired_contract_not_counted(self):
        board = build_growth_board(prices(), pd.DataFrame(), [event(activeUntil='2026-08-01')], {}, NOW)
        self.assertEqual(board['rows'], [])

    def test_cancelled_event_not_counted(self):
        board = build_growth_board(prices(), pd.DataFrame(), [event(status='무효',polarity='negative')], {}, NOW)
        self.assertEqual(board['rows'], [])

    def test_future_information_not_used(self):
        board = build_growth_board(prices(), pd.DataFrame(), [event(published='2026-09-07')], {}, NOW)
        self.assertEqual(board['rows'], [])

    def test_republication_not_extra_evidence(self):
        a = event()
        b = event(receipt='b',publishedAt='2026-09-01',correction=True)
        merged = merge_events([a,b])
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]['firstPublished'],'2026-04-01')
        self.assertEqual(confidence(merged)['evidenceCount'],1)

    def test_confidence_increases_with_independent_information(self):
        self.assertGreater(confidence([event(),event(identity='b')])['score'],confidence([event()])['score'])

    def test_counter_evidence_lowers_confidence(self):
        p = [event(), event(identity='b')]
        self.assertLess(confidence(p+[event(identity='c',polarity='negative',status='무효')])['score'],confidence(p)['score'])

    def test_confidence_does_not_depend_on_age(self):
        self.assertEqual(confidence([event()])['score'],confidence([event(published='2026-09-01')])['score'])

    def test_ir_schedule_alone_is_not_growth_evidence(self):
        item=dict(report_nm='기업설명회(IR)개최',rcept_no='1',rcept_dt='20260901',stock_code='000001')
        parsed=parse_disclosure(item,'<table><tr><td>목적</td><td>기업설명</td></tr></table>',NOW.isoformat())
        self.assertEqual(parsed['polarity'],'neutral')
        self.assertEqual(parsed['status'],'검토필요')

    def test_contract_parser_size_duration(self):
        pairs=[('1. 판매ㆍ공급계약 내용','신규장비'),('확정 계약금액','1,000,000'),
               ('최근 매출액(원)','2,000,000'),('매출액 대비(%)','50'),
               ('3. 계약상대방','고객사'),('시작일','2026-01-01'),('종료일','2028-12-31'),
               ('8. 계약(수주)일자','2026-01-01')]
        html='<table>'+''.join(f'<tr><td>{k}</td><td>{v}</td></tr>' for k,v in pairs)+'</table>'
        item=dict(report_nm='단일판매ㆍ공급계약체결',rcept_no='1',rcept_dt='20260102',stock_code='000001')
        p=parse_disclosure(item,html,NOW.isoformat())
        self.assertEqual(p['status'],'유효')
        self.assertEqual(p['revenueRatio'],50)
        self.assertLess(p['sizePerYearProxy'],20)
        self.assertEqual(p['firstPublished'],'2026-01-02')

    def test_loss_making_company_not_automatically_excluded(self):
        f=pd.DataFrame([dict(ticker='000001',sales_current=200,sales_previous=100,op_current=-5,as_of='2026-06-30')])
        b=build_growth_board(prices(),f,[event()],{},NOW)
        self.assertEqual(b['rows'][0]['growthRate'],100)
        self.assertEqual(len(b['rows']),1)

    def test_growth_uses_sales_not_turnaround_999(self):
        p=fundamental_profile(dict(sales_current=120,sales_previous=100,op_current=10,op_1y_growth=999))
        self.assertAlmostEqual(p['growthRate'],20)

    def test_sector_requires_multiple_linked_companies(self):
        p=prices(18)
        es=[event(ticker=f'{i:06d}',identity=str(i)) for i in range(1,19)]
        b=build_growth_board(p,pd.DataFrame(),es,{},NOW)
        self.assertEqual(len(b['rows']),10)
        self.assertEqual(len(b['sectors']),5)
        self.assertTrue(all(len(s['stocks'])==3 for s in b['sectors']))
        json.dumps(b,allow_nan=False)

    def test_adjustment_jump_blocks_underreaction(self):
        p=prices()
        p.loc[p.ticker.eq('000001') & p.date.ge('2026-05-01'),'close']=50
        r=PriceResponse(p).evaluate('000001','산업0',event())
        self.assertEqual(r['label'],'판정 불가')

    def test_peak_then_reversal_requires_review(self):
        p=prices()
        ix=p.index[p.ticker.eq('000001') & p.date.eq('2026-06-01')][0]
        p.loc[ix,'close']=130
        r=PriceResponse(p).evaluate('000001','산업0',event())
        self.assertEqual(r['label'],'판정 불가')


if __name__ == '__main__':
    unittest.main()
