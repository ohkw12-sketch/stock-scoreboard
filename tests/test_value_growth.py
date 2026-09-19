import unittest
from datetime import datetime

import pandas as pd

from value_growth import KST, build_value_growth_board


class ValueGrowthTest(unittest.TestCase):
    def sources(self, count=1):
        values, growth, fundamentals, prices = [], [], [], []
        for index in range(count):
            ticker = str(index + 1).zfill(6)
            values.append({'ticker': ticker, 'name': f'후보{index}', 'sector': '검증',
                           'valueScore': 80-index, 'normalizedPOP': 6,
                           'sectorNormalizedPOP': 10, 'normalizedPremiumPct': -40,
                           'confidence': 'A'})
            growth.append({'ticker': ticker, 'name': f'후보{index}', 'sector': '검증',
                           'score': 70-index, 'growthRate': 20, 'fundamentalScore': 75,
                           'priceReflection': '미반영 가능', 'confidence': '보통',
                           'evidenceCount': 2, 'evidenceContents': [],
                           'events': [{'status': '유효', 'polarity': 'positive', 'kind': '수주'}],
                           'counterEvidence': [], 'sourceDate': '2026-09-18'})
            fundamentals.append({'ticker': ticker,
                                 'normalized_op_q3': 20, 'normalized_op_q4': 22,
                                 'normalized_op_q1': 24, 'normalized_op_q2': 26,
                                 'normalized_sales_q3': 100, 'normalized_sales_q4': 100,
                                 'normalized_sales_q1': 100, 'normalized_sales_q2': 100})
            prices.append({'ticker': ticker, 'date': pd.Timestamp('2026-09-18'), 'market_cap': 500})
        return ({'_allRows': values, 'dataStatus': {}, '_meta': {'asOfDate': '2026-09-18'}},
                {'dataStatus': {}}, growth, pd.DataFrame(fundamentals), pd.DataFrame(prices))

    def build(self, count=1):
        return build_value_growth_board(*self.sources(count), now=datetime(2026, 9, 19, tzinfo=KST))

    def test_score_is_equal_value_growth_mix_less_capped_risk(self):
        value, growth_board, growth, fundamentals, prices = self.sources()
        row = growth[0]
        row['events'] = [{'status': '유효', 'polarity': 'positive', 'kind': '컨센서스'}]
        fundamentals.loc[0, ['normalized_op_q3','normalized_op_q4','normalized_op_q1','normalized_op_q2']] = [40,30,20,10]
        prices.loc[0, 'market_cap'] = 1000
        result = build_value_growth_board(value, growth_board, growth, fundamentals, prices,
                                          now=datetime(2026, 9, 19, tzinfo=KST))
        self.assertEqual(result['rows'][0]['riskPenalty'], 25)
        self.assertEqual(result['rows'][0]['valueGrowthScore'], 50)

    def test_recent_valid_negative_is_excluded(self):
        sources = list(self.sources())
        sources[2][0]['counterEvidence'] = [{'status': '유효', 'polarity': 'negative',
                                             'publishedAt': '2026-09-01'}]
        result = build_value_growth_board(*sources, now=datetime(2026, 9, 19, tzinfo=KST))
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['dataStatus']['recentNegativeExcludedCount'], 1)

    def test_final_display_is_capped_at_twenty_without_padding(self):
        result = self.build(22)
        self.assertEqual(len(result['_allRows']), 22)
        self.assertEqual(len(result['rows']), 20)
        self.assertEqual([row['rank'] for row in result['rows']], list(range(1, 21)))


if __name__ == '__main__':
    unittest.main()
