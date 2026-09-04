import unittest
from datetime import datetime

import pandas as pd

from growth_discovery import KST
from growth_sources import consensus_evidence, parse_trade_summary, product_exposure


class GrowthSourceTest(unittest.TestCase):
    def test_official_trade_summary_uses_explicit_values(self):
        html='''<html><body>2026년 8월 수출입 동향 산업통상부 2026.09.01
        <div class="editor">산업통상부는 ’26.9.1. 발표했다. 반도체( +209.0%),
        화장품(+52.1%), 자동차 △29.8%</div></body></html>'''
        events=parse_trade_summary(html,'https://example.test/release','2026-09-05T09:00:00+09:00')
        rates={e['product']:e['growthRate'] for e in events}
        self.assertEqual(rates,{'반도체':209.0,'화장품':52.1,'자동차':-29.8})
        self.assertTrue(all(e['firstPublished']=='2026-09-01' for e in events))

    def test_product_link_is_not_whole_theme(self):
        listing=pd.DataFrame([{'Code':'1','Products':'화장품 OEM'},{'Code':'2','Products':'유통 플랫폼'}])
        evidence={'product':'화장품'}
        self.assertEqual(product_exposure(listing,evidence),{'000001'})

    def test_stale_consensus_is_not_counted_as_fresh(self):
        frame=pd.DataFrame([dict(ticker='000001',consensus_as_of='2026-08-01',estimate_period='2026.12E',
                                 consensus_sales_1y_growth=20,consensus_op_1y_growth=30,
                                 consensus_prior_sales=100,consensus_forward_sales=120,consensus_forward_op=10)])
        rows=consensus_evidence(frame,{'freshTickers':[]},datetime(2026,9,5,tzinfo=KST))
        self.assertEqual(rows[0]['status'],'상태확인필요')


if __name__=='__main__':
    unittest.main()
