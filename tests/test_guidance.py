import copy
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
from guidance_engine import *

TITLE='연결재무제표 기준 영업실적 등에 대한 전망(공정공시)'
def filing(receipt='20260101000001',published='20260101',title=TITLE):
    return dict(report_nm=title,rcept_no=receipt,rcept_dt=published,stock_code='123456',corp_name='샘플기업')
def markup(sales='100~120',op='10',end='2026-12-31'):
    return f'''<table><tr><td colspan="2">구분(단위 : 억원)</td><td>2026 사업연도</td></tr>
<tr><td rowspan="2">대상기간</td><td>시작일</td><td>2026-01-01</td></tr>
<tr><td>종료일</td><td>{end}</td></tr><tr><td colspan="2">매출액</td><td>{sales}</td></tr>
<tr><td colspan="2">영업이익</td><td>{op}</td></tr></table>'''
def record(**kwargs):
    return merge_history([],parse_document(markup(**kwargs),filing()))[0]
def consensus(**kw):
    return dict(ticker='123456',period='2026',basis='consolidated',amount_unit='원',sales=100e8,operating_profit=10e8,**kw)

class GuidanceTests(unittest.TestCase):
    def test_titles(self):
        for t in [TITLE,'[기재정정]영업실적등에대한전망(공정공시)']:
            self.assertTrue(is_guidance(t))
        self.assertFalse(is_guidance('영업실적 잠정실적 공정공시'))
    def test_range_opm(self):
        r=record()
        self.assertEqual(r['sales']['mid'],110e8)
        self.assertAlmostEqual(r['opm']['mid'],1000/110)
        self.assertEqual(r['opm']['high'],10)
    def test_revision_idempotent_out_of_order(self):
        old=record()
        new=parse_document(markup('150','-'),filing('20260201000001','20260201','[기재정정]'+TITLE))[0]
        h=merge_history([new],[old,new])
        self.assertEqual(len(h),2)
        self.assertFalse(h[0]['active'])
        self.assertTrue(h[1]['active'])
        self.assertIsNone(h[1]['operating_profit'])
        self.assertEqual(h[1]['changes']['sales'],'상향')
        self.assertEqual(merge_history(h,[new]),h)
    def test_comparison_new_up_risk(self):
        r=record(op='8')
        result=compare(r,consensus(),as_of=date(2026,1,15))
        self.assertEqual(result['tags'],['NEW','UP','RISK'])
        self.assertAlmostEqual(result['guidance_vs_consensus_gap']['sales'],.1)
    def test_zero_negative_missing_mismatch(self):
        for c in [None,dict(consensus(),period='2027'),dict(consensus(),basis=None),dict(consensus(),sales=0)]:
            self.assertIsNone(compare(record(),c)['guidance_vs_consensus_gap']['sales'])
        r=compare(record(),dict(consensus(),sales=-100e8),as_of=date(2026,9,21))
        self.assertNotIn('RISK',r['tags'])
    def test_boundary_no_bonus_and_immutability(self):
        c=consensus();before=copy.deepcopy(c)
        self.assertEqual(compare(record(sales='105'),c,as_of=date(2026,9,21))['tags'],[])
        self.assertEqual(c,before)
        self.assertEqual(build_board([], [c],{'status':'正常'}, {})['rows'],[])
    def test_unsafe_parse(self):
        for value in ['100 이상','120~100','설명 100','100-120']:
            with self.assertRaises(ValueError): record(sales=value)
        with self.assertRaises(ValueError): parse_document(markup()+markup(),filing())
    def test_quarter_and_ytd(self):
        self.assertEqual(record(end='2026-03-31')['period'],'2026Q1')
        self.assertEqual(record(end='2026-06-30')['period'],'2026-01-01/2026-06-30')
    def test_failed_correction_retains_with_warning(self):
        rows=build_board([record()],[],{'status':'부분실패','failures':[{'ticker':'123456'}]}, {},date(2026,1,15))['rows']
        self.assertIn('이전값',rows[0]['fetch_status'])
        self.assertEqual(rows[0]['tags'],[])
    def test_expired(self):
        self.assertEqual(build_board([record()],[],{'status':'정상'}, {},date(2027,1,1))['rows'],[])
    def test_narrative_ranges_and_opm(self):
        note='<tr><td colspan="3">5. 기타 투자판단과 관련한 중요사항 연결 기준 매출액 : 52,770 억원 ~ 54,480 억원 영업이익 : 9,200 억원 ~ 9,550 억원</td></tr>'
        text=markup('-','-').replace('</table>',note+'</table>')
        r=parse_document(text,filing())[0]
        self.assertEqual(r['sales']['mid'],53625e8)
        self.assertEqual(r['operating_profit']['mid'],9375e8)
        note='<tr><td colspan="3">5. 기타 투자판단과 관련한 중요사항 영업이익률 10% 달성</td></tr>'
        r=parse_document(markup('-','-').replace('</table>',note+'</table>'),filing())[0]
        self.assertIsNone(r['sales'])
        self.assertEqual(r['opm']['mid'],10)
    def test_managed_scope_and_unaffected_failure(self):
        note='<tr><td colspan="3">관리연결기준</td></tr>'
        r=merge_history([],parse_document(markup().replace('</table>',note+'</table>'),filing()))[0]
        self.assertEqual(r['basis'],'managed_consolidated')
        self.assertIsNone(compare(r,consensus())['guidance_vs_consensus_gap']['sales'])
        rows=build_board([record()],[consensus()],{'status':'부분실패','failures':[{'ticker':'999999'}]}, {},date(2026,1,15))['rows']
        self.assertEqual(rows[0]['fetch_status'],'정상')
        self.assertIn('UP',rows[0]['tags'])
    def test_missing_key(self):
        with patch('guidance_engine._api_key',return_value=None),patch.dict(os.environ,{'OPEN_DART_API_KEY':''}):
            h,s=collect({},date(2026,1,1),date(2026,1,2),[record()])
        self.assertEqual(s['status'],'설정필요');self.assertEqual(len(h),1)
    def test_pagination_archive(self):
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z:z.writestr('20260101000001.xml',markup())
        calls=[]
        def request(endpoint,params):
            calls.append((endpoint,params))
            if endpoint=='document.xml':return archive.getvalue()
            return json.dumps({'status':'000','total_page':2,'list':[filing()] if params['page_no']==2 else []}).encode()
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'OPEN_DART_API_KEY':'test'}):
            h,s=collect({'cache_dir':d,'dart_pause_seconds':0},date(2026,1,1),date(2026,1,2),[],request)
        self.assertEqual(len(h),1);self.assertEqual(s['status'],'정상')
        self.assertEqual([c[1]['page_no'] for c in calls if c[0]=='list.json'],[1,2])
    def test_cache_adapter(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'c.csv';p.write_text('ticker,estimate_period,amount_unit,forward_sales,forward_op,as_of\n123456,2026.12E,억원,100,10,2026-01-01\n',encoding='utf-8')
            c=consensus_rows(p)[0]
            self.assertIsNone(c['basis']);self.assertEqual(c['sales'],100e8)

    def test_broker_forecast_json_adapter_rejects_disagreement(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'forecast.json'
            p.write_text(json.dumps([
                {'ticker':'123456','period':'2026FY','scope':'annual','status':'참고 컨센서스',
                 'salesMedianKrw100m':100,'operatingProfitMedianKrw100m':10,'latestReportDate':'2026-09-01'},
                {'ticker':'999999','period':'2026FY','scope':'annual','status':'불일치 검토',
                 'salesMedianKrw100m':200,'operatingProfitMedianKrw100m':20,'latestReportDate':'2026-09-01'},
            ],ensure_ascii=False),encoding='utf-8')
            rows=consensus_rows(p,'consolidated')
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['sales'],100e8)
            self.assertEqual(rows[0]['basis'],'consolidated')

if __name__=='__main__': unittest.main()
