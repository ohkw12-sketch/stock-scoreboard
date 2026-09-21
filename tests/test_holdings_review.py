import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from holdings_review import select_holdings, validate_assessments
from promote_sections import promote


class HoldingsReviewTests(unittest.TestCase):
    def inputs(self):
        base = dict(name='예시', ticker='000001', qty=10, avg=100, quotedPrice=110)
        return {'sourceDate':'2026-09-21','images':[
            {'id':1,'rows':[base]}, {'id':2,'rows':[{**base,'qty':3,'avg':90,'quotedPrice':105}]}]}

    def test_selects_whole_lower_cost_row_in_either_order(self):
        payload=self.inputs()
        for _ in range(2):
            row=select_holdings(payload)[0]
            self.assertEqual((row['qty'],row['avg'],row['quotedPrice'],row['sourceImage']),(3,90,105,2))
            payload['images'].reverse()

    def test_invalid_and_ambiguous_inputs_rejected(self):
        for key,value in [('qty',0),('avg',float('nan')),('ticker','1')]:
            payload=self.inputs()
            payload['images'][0]['rows'][0][key]=value
            with self.assertRaises(ValueError): select_holdings(payload)
        payload=self.inputs()
        payload['images'][1]['rows'][0]['avg']=100
        with self.assertRaises(ValueError): select_holdings(payload)

    def test_scoped_import_requires_exact_authorized_roster(self):
        live={'p3':{'rows':[]},'p11':{'rows':['preserve']},'p2':{},'p1':{},'meta':{'date':'preserve'}}
        candidate=copy.deepcopy(live)
        candidate['p3']['rows']=select_holdings(self.inputs())
        with self.assertRaises(RuntimeError): promote(live,candidate,['p3'])
        result,report=promote(live,candidate,['p3'],self.inputs())
        self.assertEqual(result['p11'],live['p11'])
        self.assertEqual(result['meta'],live['meta'])
        self.assertFalse(report['holdingsLocked'])
        with self.assertRaises(RuntimeError): promote(live,candidate,['p3','p11'],self.inputs())
        candidate['p3']['rows'][0]['qty']=13
        with self.assertRaises(RuntimeError): promote(live,candidate,['p3'],self.inputs())

    def test_assessments_reject_missing_sources_and_nonheld_stocks(self):
        board=json.loads((ROOT/'data.json').read_text('utf-8'))['p3']
        validate_assessments(board)
        for mutation in ('source','ticker'):
            bad=copy.deepcopy(board)
            if mutation=='source': bad['assessments'][0]['sources']=[]
            else: bad['assessments'][0]['ticker']='000000'
            with self.assertRaises(ValueError): validate_assessments(bad)

    def test_published_roster_and_exposure_are_complete(self):
        p3=json.loads((ROOT/'data.json').read_text('utf-8'))['p3']
        self.assertEqual(len(p3['rows']),14)
        rows={r['name']:r for r in p3['rows']}
        self.assertEqual((rows['일진전기']['qty'],rows['일진전기']['avg']),(22,74300))
        self.assertEqual((rows['코리아써키트']['qty'],rows['코리아써키트']['avg']),(89,54331))
        for group in ('sectors','topAxes'):
            self.assertAlmostEqual(sum(float(r['weight'].rstrip('%')) for r in p3['exposure'][group]),100,delta=.3)
        self.assertEqual(len(p3['assessments']),11)
        self.assertTrue(all('2026-09-18' in r['basis'] for r in p3['rows']))

if __name__=='__main__': unittest.main()
