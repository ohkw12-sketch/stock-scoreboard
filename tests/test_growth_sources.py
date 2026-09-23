import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from growth_discovery import KST
from growth_sources import (_kis_investor_signal, collect_investor_flows, consensus_evidence,
                            parse_trade_summary, product_exposure)


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

    def test_investor_flow_collector_uses_complete_bulk_windows(self):
        class FakeStock:
            calls = []

            @classmethod
            def get_market_net_purchases_of_equities_by_ticker(
                    cls, start, end, market, investor):
                cls.calls.append((start, end, market, investor))
                value = 100 if investor == '외국인' else 200
                return pd.DataFrame(
                    {'순매수거래대금': [value]},
                    index=['000001'],
                )

        prices = pd.DataFrame({
            'ticker': ['000001'] * 20,
            'date': pd.bdate_range('2026-08-10', periods=20),
        })
        fake_module = types.SimpleNamespace(stock=FakeStock)
        with tempfile.TemporaryDirectory() as directory:
            config = {'cache_dir': Path(directory)}
            with patch.dict(sys.modules, {'pykrx': fake_module}):
                signals, status = collect_investor_flows(
                    config, prices, datetime(2026, 9, 5, tzinfo=KST),
                )
            self.assertEqual(status['status'], '정상')
            self.assertEqual(len(FakeStock.calls), 4)
            self.assertEqual(signals['000001']['foreignNet5'], 100)
            self.assertEqual(signals['000001']['institutionNet20'], 200)

            cached, cached_status = collect_investor_flows(
                config, prices, datetime(2026, 9, 5, tzinfo=KST), reuse=True,
            )
            self.assertEqual(cached, signals)
            self.assertEqual(cached_status['status'], '캐시유지')

    def test_kis_investor_history_builds_5_and_20_session_krw_totals(self):
        rows = [
            {
                'stck_bsop_date': (pd.Timestamp('2026-09-21') - pd.offsets.BDay(i)).strftime('%Y%m%d'),
                'frgn_ntby_tr_pbmn': str(i + 1),
                'orgn_ntby_tr_pbmn': str(-(i + 1)),
            }
            for i in range(20)
        ]
        signal = _kis_investor_signal({'rt_cd': '0', 'output': rows}, '1', '2026-09-21')
        self.assertEqual(signal['foreignNet5'], 15_000_000)
        self.assertEqual(signal['institutionNet5'], -15_000_000)
        self.assertEqual(signal['foreignNet20'], 210_000_000)
        self.assertEqual(signal['institutionNet20'], -210_000_000)


if __name__=='__main__':
    unittest.main()
