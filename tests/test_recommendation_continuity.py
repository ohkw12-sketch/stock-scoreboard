import unittest

import pandas as pd

from recommendation_continuity import attach_recommendation_history


def cohort(section, source_date, tickers, observed=True, groups=None):
    groups = groups or {}
    return {
        'section': section,
        'sourceDate': source_date,
        'observedPublishedAt': f'{source_date}T16:00:00+09:00' if observed else None,
        'records': [
            {'ticker': ticker, 'group': groups.get(ticker, section)} for ticker in tickers
        ],
    }


def board(day='2026-09-03', ticker='000001'):
    row = {'ticker': ticker, 'name': '검증종목'}
    return {
        'p1': {'sourceDate': day, 'rows': [dict(row)]},
        'p11': {'sourceDate': day, 'rows': [dict(row)]},
        'p2': {'sourceDate': day, 'rows': [dict(row)]},
        'growth': {
            'sourceDate': day, 'rows': [dict(row)],
            'sectors': [{'stocks': [dict(row)]}],
        },
        'p3': {'sourceDate': day, 'rows': [dict(row)]},
    }


class RecommendationContinuityTests(unittest.TestCase):
    def attach(self, cohorts, day='2026-09-03'):
        current = board(day)
        combined = {'sourceDate': day, 'rows': [{'ticker': '000001', 'name': '검증종목'}]}
        sessions = pd.bdate_range('2026-09-01', day)
        return attach_recommendation_history(current, combined, {'cohorts': cohorts}, sessions)

    def test_first_confirmed_appearance_is_new_and_holdings_are_untouched(self):
        result, combined = self.attach([])
        for scope in ('p1', 'p11', 'p2'):
            self.assertEqual(result[scope]['rows'][0]['recommendationHistory']['label'], '신규 추천')
        self.assertEqual(result['growth']['rows'][0]['recommendationHistory']['label'], '신규 추천')
        self.assertEqual(result['growth']['sectors'][0]['stocks'][0]['recommendationHistory']['label'], '신규 추천')
        self.assertEqual(combined['rows'][0]['recommendationHistory']['label'], '신규 추천')
        self.assertNotIn('recommendationHistory', result['p3']['rows'][0])

    def test_every_confirmed_project_roster_counts_as_consecutive(self):
        cohorts = [
            cohort('p1', '2026-09-01', ['000001']),
            cohort('p1', '2026-09-02', ['000001']),
        ]
        result, _ = self.attach(cohorts)
        history = result['p1']['rows'][0]['recommendationHistory']
        self.assertEqual(history['label'], '3거래일 연속 추천')
        self.assertEqual(history['recommendationDays'], 3)

    def test_short_missing_day_uses_recent_frequency_instead_of_rank_change(self):
        cohorts = [
            cohort('p1', '2026-09-01', ['000001']),
            cohort('p1', '2026-09-02', ['000002']),
        ]
        result, _ = self.attach(cohorts)
        self.assertEqual(result['p1']['rows'][0]['recommendationHistory']['label'],
                         '최근 3거래일 중 2일 추천')

    def test_more_than_seven_calendar_days_is_return(self):
        result, _ = self.attach([cohort('p1', '2026-09-01', ['000001'])], '2026-09-10')
        self.assertEqual(result['p1']['rows'][0]['recommendationHistory']['label'],
                         '9일 만의 재추천')

    def test_unverified_archive_and_other_projects_do_not_change_history(self):
        cohorts = [
            cohort('p1', '2026-09-01', ['000001'], observed=False),
            cohort('p2', '2026-09-02', ['000001']),
        ]
        result, _ = self.attach(cohorts)
        self.assertEqual(result['p1']['rows'][0]['recommendationHistory']['label'], '신규 추천')
        self.assertEqual(result['p2']['rows'][0]['recommendationHistory']['label'], '2거래일 연속 추천')

    def test_growth_stock_and_sector_histories_are_separate(self):
        groups = {'000001': '성장섹터:검증산업'}
        result, _ = self.attach([cohort('growth', '2026-09-02', ['000001'], groups=groups)])
        self.assertEqual(result['growth']['rows'][0]['recommendationHistory']['label'], '신규 추천')
        self.assertEqual(result['growth']['sectors'][0]['stocks'][0]['recommendationHistory']['label'],
                         '2거래일 연속 추천')


if __name__ == '__main__':
    unittest.main()
