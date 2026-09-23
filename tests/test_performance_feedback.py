import unittest
import copy
import pandas as pd
from recommendation_performance import evaluate
from performance_feedback import learn_patterns, rank_recent


class FeedbackTest(unittest.TestCase):
    def cohort(self,section='p1',day='2026-09-01',ticker='000001',**extra):
        return dict(id=section+day,section=section,observedPublishedAt=day+'T18:00:00+09:00',
            generatedAt=day+'T17:00:00+09:00',engineVersion='v1',ruleVersion='v1',sourceDate=day,
            records=[dict(ticker=ticker,name=ticker,rank=1,sourceRow={'entryState':'진입가능'})],**extra)

    def prices(self):
        return pd.DataFrame([dict(date=d,ticker='000001',close=100+i*10,open=50,
            volume=100,adjusted=True,price_date_verified=True) for i,d in enumerate(pd.bdate_range('2026-09-01','2026-09-08'))])

    def evaluate(self,cohorts,prices=None):
        return evaluate({'cohorts':cohorts},self.prices() if prices is None else prices,
            generated_at='2026-09-08T18:00:00+09:00',trading_sessions=pd.bdate_range('2026-09-01','2026-09-08'))

    def test_first_date_frozen_across_repeat_and_version(self):
        later=self.cohort(day='2026-09-04');later['engineVersion']='v2'
        rows=self.evaluate([self.cohort(),later])['rows']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['entryPrice'],100)
        self.assertEqual(rows[0]['recommendationDate'],'2026-09-01')
        self.assertEqual(rows[0]['currentReturnPct'],50)
        self.assertEqual(rows[0]['horizons']['1']['returnPct'],10)
        self.assertEqual(rows[0]['horizons']['5']['exitDate'],'2026-09-08')

    def test_projects_keep_separate_first_dates_and_no_future_overlap(self):
        rows=self.evaluate([self.cohort(),self.cohort('p2','2026-09-03')])['rows']
        entry=next(r for r in rows if r['section']=='p1')
        self.assertNotIn('가치',entry['features'])
        self.assertEqual(len(rows),2)

    def test_simultaneous_overlap_independent_of_serialization(self):
        a,b=self.cohort(),self.cohort('p2')
        for rows in (self.evaluate([a,b])['rows'],self.evaluate([b,a])['rows']):
            self.assertTrue(all('가치' in r['features'] and '진입' in r['features'] for r in rows))

    def test_missing_start_does_not_move_date(self):
        row=self.evaluate([self.cohort()],self.prices().iloc[1:])['rows'][0]
        self.assertEqual(row['entryDate'],'2026-09-01')
        self.assertIsNone(row['entryPrice'])
        self.assertIsNone(row['currentReturnPct'])

    def test_archive_date_is_not_claimed_as_publication(self):
        c=self.cohort();c['recordedAt']=c.pop('observedPublishedAt');c['recordBasis']='과거 저장일'
        row=self.evaluate([c])['rows'][0]
        self.assertIsNone(row['publishedAt'])
        self.assertEqual(row['recordBasis'],'과거 저장일')

    def samples(self):
        return {'rows':[dict(section='p1',ticker=str(i),recommendationDate='2026-09-01',
            features=['진입','순환'],horizons={'1':{'returnPct':10+i,'exitDate':'2026-09-02'}})
            for i in range(5)]}

    def test_no_same_day_or_future_outcomes(self):
        self.assertEqual(learn_patterns(self.samples(),cutoff_date='2026-09-02')['patterns'],[])
        self.assertTrue(learn_patterns(self.samples(),cutoff_date='2026-09-03')['patterns'])

    def test_duplicate_project_recommendations_do_not_inflate_samples(self):
        samples=self.samples(); duplicate=copy.deepcopy(samples['rows'])
        for r in duplicate:r['section']='p11'
        samples['rows']+=duplicate
        model=learn_patterns(samples,cutoff_date='2026-09-03')
        self.assertTrue(all(p['sampleCount']==5 for p in model['patterns']))

    def test_recent_rotation_only_can_be_recommended_and_sources_unchanged(self):
        board={'p11':{'sourceDate':'2026-09-03','rows':[{'ticker':'123456','name':'회전','rank':1}]}}
        old=copy.deepcopy(board)
        result=rank_recent(board,self.samples(),source_date='2026-09-03',generated_at='2026-09-03T18:00:00+09:00',snapshot_id='x')
        self.assertEqual(result['rows'][0]['condition'],'순환')
        self.assertEqual(result['rows'][0]['entryState'],'진입 미충족')
        self.assertEqual(board,old)

    def test_integrated_rotation_ignores_retired_entry_and_watch_candidates(self):
        strict=dict(ticker='123456',name='통과',rank=1,entryState='진입 검토',ruleVersion='rotation-entry-3.0')
        board={'p11':{'projectType':'rotation-entry','watchCandidates':[dict(strict,ticker='000004',watchOnly=True)],'rows':[strict,dict(strict,ticker='000002',watchOnly=True)]},
               'p1':{'rows':[dict(strict,ticker='000003',entryState='진입가능')]}}
        samples=self.samples()
        for r in samples['rows']: r['features']=['순환진입']
        result=rank_recent(board,samples,source_date='2026-09-03',generated_at='2026-09-03T18:00:00+09:00',snapshot_id='x')
        self.assertEqual(result['candidateCount'],1)
        self.assertEqual(result['rows'][0]['entryState'],'진입 검토')
        self.assertEqual(result['rows'][0]['conditions'],['순환'])
        self.assertEqual(result['ruleVersion'],'combined-feedback-3.0')
        self.assertEqual(rank_recent(board,self.samples(),source_date='2026-09-03',generated_at='2026-09-03T18:00:00+09:00',snapshot_id='x')['rows'],[])

    def test_sector_trend_combined_uses_union_intersection_and_anti_chase(self):
        active = {
            'name': '활성', 'trendState': '추세확인', 'stage': '②확산',
            'riskGauge': 40, 'trendScore': 72,
        }
        inactive = {
            'name': '관찰', 'trendState': '신규포착', 'stage': '①초기',
            'riskGauge': 20, 'trendScore': 70,
        }
        base = {
            'name': '통과', 'sector': '활성', 'rank': 1, 'growthScore': 70,
            'nonOverheated': True, 'currentPrice': 100,
            'fiveDayReturnPct': 5, 'ma20DistancePct': 2, 'ma20SlopePct': .3,
            'sourceDate': '2026-09-03',
        }
        board = {
            'p11': {
                'projectType': 'rotation-sector-trend',
                'sectors': [active, inactive],
            },
            'p2': {
                'sourceDate': '2026-09-03',
                'interestGrowth': {'rows': [
                    dict(base, ticker='000001', interestGrowthScore=80),
                    dict(base, ticker='000002', name='과열', interestGrowthScore=79,
                         nonOverheated=False),
                    dict(base, ticker='000003', name='비활성', sector='관찰',
                         interestGrowthScore=78),
                ]},
                'absoluteValueGrowth': {'rows': [
                    dict(base, ticker='000004', name='절대', valueGrowthScore=75,
                         valueScore=80),
                ]},
            },
        }
        result = rank_recent(
            board, {'rows': []}, source_date='2026-09-03',
            generated_at='2026-09-03T18:00:00+09:00', snapshot_id='x',
        )
        self.assertEqual(result['ruleVersion'], 'combined-trend-4.0')
        self.assertEqual(
            {row['ticker'] for row in result['rows']}, {'000001', '000004'},
        )
        self.assertTrue(all(row['sectorRelation'] == '추세확인' for row in result['rows']))
        self.assertTrue(all('비과열' in row['conditions'] for row in result['rows']))
        rejected = {row['ticker']: row['reasons'] for row in result['_audit']}
        self.assertIn('5일 상승률·20일선 이격 또는 추세 조건 미충족', rejected['000002'])
        self.assertIn('추세확인 ②확산·③주도 섹터 아님', rejected['000003'])

    def test_insufficient_samples_not_fabricated(self):
        self.assertFalse(learn_patterns({'rows':self.samples()['rows'][:2]},cutoff_date='2026-09-03')['patterns'])


if __name__=='__main__': unittest.main()
