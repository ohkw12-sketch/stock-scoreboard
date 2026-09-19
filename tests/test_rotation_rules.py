import sys
import unittest
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rotation_rules import features, macd_weight, score_candidate, select_top, build_rotation, evidence_from_sources


def candidate(**changes):
    row = dict(ticker='123456', name='Test', sector='장비', date=pd.Timestamp('2026-08-18'),
        valid_bars=True, macd_ok=True, macd_weight=1, close=103, rotation_ma20=95,
        rotation_volume_ratio=2, value=2e9, turnover3=2e9, resistance_distance=.03,
        close_breakout=True, rotation_ret1=.03, rotation_ret3=.04, rotation_ret5=.05,
        close_location=.9, upper_wick=.1, high_fade=.01, intraday_rise=.04)
    row.update(changes)
    return row


def event(**changes):
    row = dict(ticker='123456', eventId='contract-1', publishedAt='2026-08-17',
        status='verified', source='exchange', kind='catalyst', points=25, strong=True)
    row.update(changes)
    return row


class RotationRulesTest(unittest.TestCase):
    def test_volume_windows_do_not_overlap_and_have_strict_minimum(self):
        frame = pd.DataFrame(dict(ticker=['a']*60, date=pd.bdate_range('2026-05-01', periods=60),
            close=np.arange(100,160), open=np.arange(100,160), high=np.arange(101,161),
            low=np.arange(99,159), volume=[100]*57+[300,350,400], value=[2e9]*60))
        frame = frame.sample(frac=1, random_state=7)
        calculated = features(frame)
        self.assertEqual(calculated.iloc[-1].volume14_before3,100)
        self.assertEqual(calculated.iloc[-1].rotation_volume_ratio,3.5)
        self.assertTrue(pd.isna(calculated.iloc[15].rotation_volume_ratio))
        prefix = features(frame[frame.date <= frame.date.sort_values().iloc[54]])
        pd.testing.assert_frame_equal(calculated.iloc[:55].reset_index(drop=True), prefix.reset_index(drop=True))

    def test_thresholds_and_liquidity(self):
        for ratio, expected in [(1.4999,False),(1.5,True),(1.9999,True),(2,True)]:
            result=score_candidate(candidate(rotation_volume_ratio=ratio),[],1e9)
            self.assertEqual(result['eligible'],expected)
            self.assertEqual(result['volumeGrade'],'A' if ratio>=2 else 'B')
        self.assertFalse(score_candidate(candidate(turnover3=999999999),[],1e9)['eligible'])

    def test_macd_aging_and_reignition(self):
        for age, expected in [(0,1),(3,1),(4,.9),(7,.9),(8,.75),(12,.75),(13,.55),(20,.55),(21,.3)]:
            self.assertEqual(macd_weight(age),expected)
        self.assertEqual(macd_weight(40,True),1)
        self.assertFalse(score_candidate(candidate(macd_ok=False),[],1e9)['eligible'])

    def test_close_resistance_not_intraday_and_extension(self):
        approach=score_candidate(candidate(close_breakout=False,resistance_distance=-.03),[],1e9)
        self.assertTrue(approach['eligible'])
        self.assertTrue(approach['watchOnly'])
        self.assertFalse(score_candidate(candidate(resistance_distance=-.0301),[],1e9)['eligible'])
        breakout=score_candidate(candidate(),[],1e9)
        self.assertGreater(breakout['score'],approach['score'])
        extended=score_candidate(candidate(resistance_distance=.101),[],1e9)
        self.assertGreater(extended['penalty'],0)
        self.assertTrue(extended['watchOnly'])

    def test_heat_exception_requires_every_condition_and_never_allows_distribution(self):
        hot=candidate(rotation_ret1=.15)
        self.assertFalse(score_candidate(hot,[],1e9)['eligible'])
        result=score_candidate(hot,[event()],1e9)
        self.assertTrue(result['eligible'])
        self.assertTrue(result['watchOnly'])
        for changes in [dict(rotation_volume_ratio=1.9),dict(close_breakout=False),dict(close_location=.79),dict(high_fade=.06,intraday_rise=.2)]:
            self.assertFalse(score_candidate(dict(hot,**changes),[event()],1e9)['eligible'])
        distribution=score_candidate(candidate(upper_wick=.6,close_location=.4),[],1e9)
        self.assertTrue(distribution['watchOnly'])
        self.assertGreaterEqual(distribution['penalty'],20)

    def test_future_release_cannot_rescue_historical_heat(self):
        self.assertFalse(score_candidate(candidate(rotation_ret1=.15),[event(publishedAt='2026-08-19')],1e9)['eligible'])
        self.assertFalse(score_candidate(candidate(rotation_ret1=.15),[event(publishedAt='2026-08-18T16:00:00+09:00')],1e9)['eligible'])

    def test_domain_caps_and_event_deduplication(self):
        events=[event(),event(kind='earnings',points=20)]
        result=score_candidate(candidate(),events,1e9)
        self.assertEqual(sum(result['scoreBlocks'].values()),65)
        many=[event(eventId=str(i),kind=kind,points=100) for i,kind in enumerate(['earnings','consensus','catalyst','industry'])]
        self.assertEqual(score_candidate(candidate(),many,1e9)['scoreBlocks'],{'수급·차트':40,'실적·컨센서스':35,'신규촉매·업황':25})

    def test_diversity_and_watch_slots(self):
        rows=[dict(ticker=str(i),sector='a' if i<4 else str(i),detailTheme='same' if i<3 else str(i),
            stockEntryScore=100-i,volumeRatio=2,watchOnly=i==0,heatException=i==0) for i in range(9)]
        chosen=select_top(rows)
        self.assertEqual(len(chosen),5)
        self.assertLessEqual(sum(r['sector']=='a' for r in chosen),2)
        top3=[r for r in chosen if r['rank']<=3]
        self.assertEqual(len({r['detailTheme'] for r in top3}),len(top3))
        self.assertFalse(any(r['watchOnly'] for r in top3))
        self.assertTrue(all(r['entryFit']=='관찰' for r in chosen if r['rank']>=4))
        only_watch=select_top([rows[0]])
        self.assertEqual(only_watch[0]['rank'],4)
        self.assertEqual(only_watch[0]['signal'],'과열 관찰')

    def test_excluded_sectors_and_semiconductor_rotation(self):
        for sector in ['건설','바이오/제약']:
            self.assertFalse(score_candidate(candidate(sector=sector),[],1e9)['eligible'])
        self.assertTrue(score_candidate(candidate(sector='반도체'),[],1e9)['eligible'])

    def test_real_output_shape_with_qualifying_price_fixture(self):
        n=70
        close=np.linspace(100,110,n)
        close[-3:]=[111,112,114]
        frame=pd.DataFrame(dict(ticker=['123456']*n,name=['Test']*n,sector=['장비']*n,
            date=pd.bdate_range('2026-05-01',periods=n),close=close,open=close-.1,
            high=close+.1,low=close-1,volume=[100]*67+[250]*3,value=[2e9]*n))
        template=dict(ticker='123456',name='Test',sector='장비',marketState='초기')
        rows,pool,audit=build_rotation(frame,[{'name':'장비','stage':'①초기'}],[template],
            dict(top_stock_count=20,minimum_daily_turnover=1e9),[])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['signal'],'진입 검토')
        self.assertTrue(audit[0]['eligible'])
        self.assertEqual(rows[0]['volumeGrade'],'A')
        frames, templates, sectors = [], [], []
        for i in range(8):
            ticker, sector = f'{i:06d}', f'sector-{i}'
            frames.append(frame.assign(ticker=ticker, sector=sector))
            templates.append(dict(template, ticker=ticker, sector=sector))
            sectors.append(dict(name=sector, stage='①초기'))
        rows, pool, audit = build_rotation(pd.concat(frames), sectors, templates,
            dict(top_stock_count=5, minimum_daily_turnover=1e9), [])
        self.assertEqual(len(pool), 8)
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(audit), 8)



if __name__=='__main__':
    unittest.main()
