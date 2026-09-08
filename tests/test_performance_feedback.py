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

    def test_insufficient_samples_not_fabricated(self):
        self.assertFalse(learn_patterns({'rows':self.samples()['rows'][:2]},cutoff_date='2026-09-03')['patterns'])


if __name__=='__main__': unittest.main()
