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
                                  'normalized_sales_q1': 100, 'normalized_sales_q2': 100,
                                  'value_fundamental_q1_op': 24, 'value_fundamental_q2_op': 26,
                                  'value_fundamental_q1_sales': 100, 'value_fundamental_q2_sales': 100,
                                  'value_fundamental_next_op_growth_pct': 10})
            prices.append({'ticker': ticker, 'date': pd.Timestamp('2026-09-18'), 'market_cap': 500})
        return ({'_allRows': values, 'dataStatus': {}, '_meta': {'asOfDate': '2026-09-18'}},
                {'dataStatus': {}}, growth, pd.DataFrame(fundamentals), pd.DataFrame(prices))

    def build(self, count=1):
        return build_value_growth_board(*self.sources(count), now=datetime(2026, 9, 19, tzinfo=KST))

    def test_score_is_equal_value_growth_mix_less_capped_risk(self):
        value, growth_board, growth, fundamentals, prices = self.sources()
        row = growth[0]
        row['events'] = [{'status': '유효', 'polarity': 'positive', 'kind': '컨센서스'}]
        fundamentals.loc[0, ['value_fundamental_q1_op','value_fundamental_q2_op',
                             'value_fundamental_q1_sales','value_fundamental_q2_sales',
                             'value_fundamental_next_op_growth_pct']] = [30, 10, 100, 100, -25]
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

    def test_sector_premium_costs_five_and_seasonality_only_costs_ten(self):
        value, growth_board, growth, fundamentals, prices = self.sources()
        value['_allRows'][0]['normalizedPOP'] = 11
        value['_allRows'][0]['sectorNormalizedPOP'] = 10
        value['_allRows'][0]['seasonalityFallback'] = True
        result = build_value_growth_board(value, growth_board, growth, fundamentals, prices,
                                          now=datetime(2026, 9, 19, tzinfo=KST))
        row = result['rows'][0]
        self.assertEqual(row['riskPenalty'], 15)
        self.assertIn('당해연도 P/OP가 섹터 중앙보다 높음 -5', row['riskWarnings'])
        self.assertIn('컨센서스·가이던스 없음·계절성 추정 -10', row['riskWarnings'])
        self.assertTrue(row['seasonalityEstimateOnly'])

    def test_final_display_is_capped_at_twenty_without_padding(self):
        result = self.build(22)
        self.assertEqual(len(result['_allRows']), 22)
        self.assertEqual(len(result['rows']), 20)
        self.assertEqual([row['rank'] for row in result['rows']], list(range(1, 21)))

    def test_market_interest_ranking_uses_growth_and_sector_attention_without_valuation(self):
        value, growth_board, growth, fundamentals, prices = self.sources()
        rotation = {'_allSectors': [{
            'name': '검증', 'score': 90, 'trendScore': 90, 'todayScore': 92,
            'top20Days10': 8, 'trendState': '추세확인',
            'rank': 1, 'stage': '②확산',
            'entryFit': '진입적합', 'riskGauge': 20,
        }]}
        result = build_value_growth_board(
            value, growth_board, growth, fundamentals, prices, rotation,
            now=datetime(2026, 9, 19, tzinfo=KST),
        )
        row = result['interestGrowth']['rows'][0]
        self.assertEqual(row['interestGrowthScore'], 74)
        self.assertEqual(row['growthScore'], 70)
        self.assertEqual(row['sectorAttentionScore'], 90)
        self.assertEqual(row['sectorTodayScore'], 92)
        self.assertFalse(row['valuationMetricsUsed'])
        self.assertEqual(row['entryState'], '추세확인')

    def test_split_board_marks_names_present_in_both_top_lists(self):
        value, growth_board, growth, fundamentals, prices = self.sources()
        rotation = {'_allSectors': [{
            'name': '검증', 'score': 80, 'rank': 1, 'stage': '⑥후반',
            'entryFit': '추격금지', 'riskGauge': 60,
        }]}
        result = build_value_growth_board(
            value, growth_board, growth, fundamentals, prices, rotation,
            now=datetime(2026, 9, 19, tzinfo=KST),
        )
        self.assertTrue(result['rows'][0]['alsoInterestTop20'])
        self.assertTrue(result['interestGrowth']['rows'][0]['alsoAbsoluteTop20'])
        self.assertEqual(result['interestGrowth']['rows'][0]['entryState'], '추격주의')


if __name__ == '__main__':
    unittest.main()
