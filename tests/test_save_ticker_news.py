import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from save_ticker_news import build_board, refresh_or_retain, select_news, validate_board


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 10, 0, 30, tzinfo=KST)


def article(identity, title, *, author='오선', content='', views=1000, hour=7):
    return {
        'id': str(identity), 'title': title, 'author_name': author, 'content': content,
        'created_at': f'2026-09-09T{hour:02d}:00:00Z', 'view_count': views,
        'source': '블룸버그', 'is_top_story': False,
    }


class SaveTickerNewsTest(unittest.TestCase):
    def test_only_osun_articles_with_korean_market_link_are_selected(self):
        items = [
            article(1, '오픈AI, 삼성전자와 차세대 AI 칩 협력 강화', views=10000),
            article(2, '삼성전자 공급 확대', author='다른작성자', views=20000),
            article(3, '미국 지역 보석업체 실적 발표'),
        ]
        selected = select_news(items, NOW)
        self.assertEqual([item['id'] for item in selected], ['1'])
        self.assertEqual(selected[0]['scope'], '국장 직접')
        self.assertIn('반도체', selected[0]['sectors'][0])

    def test_similar_oil_updates_are_reduced_to_one_sector_card(self):
        items = [
            article(1, '브렌트유 배럴당 100달러 돌파', views=9000),
            article(2, '[2보] 브렌트유 배럴당 100달러 돌파', views=8000),
        ]
        selected = select_news(items, NOW)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['scope'], '매크로 시황')
        self.assertIn('원자재·지정학', selected[0]['sectors'])

    def test_macro_survives_many_domestic_news_without_stock_link(self):
        items=[article(i, f'삼성전자 {i} 사업 확대') for i in range(20)]
        items += [article(100, '연준, 추가 긴축 필요'), article(101,'미국 비농업 고용 감소'),
                  article(102,'국채 수익률 상승'), article(103,'SAVE 마감 리포트 - 텍스트')]
        selected=select_news(items,NOW)
        macros=[r for r in selected if r['scope']=='매크로 시황']
        self.assertEqual({r['id'] for r in macros},{'100','101','102','103'})
        self.assertLessEqual(len([r for r in selected if r['scope']!='매크로 시황']),10)

    def test_macro_author_and_counts_and_no_future_news(self):
        items=[article(1,'연준 금리 인상'),article(2,'연준 금리 인상',author='다른작성자'),
               article(3,'소비자물가 상승',hour=23)]
        board=build_board(items,{'1':{'content':'연준의 기준금리 결정에 대한 원문입니다.'}},NOW)
        validate_board(board)
        self.assertEqual([r['id'] for r in board['items']],['1'])
        self.assertEqual(board['meta']['macroCount'],1)
        self.assertEqual(board['meta']['sectorCount'],0)
        self.assertEqual(board['items'][0]['contentStatus'],'본문 확인')

    def test_board_summarizes_actual_article_content_and_keeps_source_link(self):
        items = [article('news_gCv2QtNQbwmx', '한화오션, 태국 호위함 우선협상대상자 선정', views=7000)]
        details = {'news_gCv2QtNQbwmx': {'source': 'SaveTicker', 'content': [
            {'type': 'text', 'content': '한화오션은 태국 해군 차세대 호위함 사업의 우선협상대상자로 선정됐습니다.'},
            {'type': 'text', 'content': '최종 계약 체결 여부와 사업 규모는 후속 협상에서 정해집니다.'},
        ]}}
        board = build_board(items, details, NOW)
        validate_board(board)
        self.assertIn('우선협상대상자', board['items'][0]['summary'])
        self.assertEqual(board['items'][0]['url'], 'https://saveticker.com/news/news_gCv2QtNQbwmx')
        self.assertEqual(board['meta']['directCount'], 1)

    def test_failed_refresh_retains_previous_board(self):
        previous = build_board([article(1, '삼성전자, AI 칩 협력')], {}, NOW)
        with patch('save_ticker_news.refresh', side_effect=TimeoutError):
            retained = refresh_or_retain(previous, NOW)
        self.assertEqual(retained['items'], previous['items'])
        self.assertEqual(retained['collection']['status'], '실패·이전유지')

    def test_adding_macro_keeps_recent_domestic_rows_with_original_dates(self):
        prior=build_board([article(1,'삼성전자 AI 칩 협력')],{},NOW)
        fresh=build_board([article(2,'연준 기준금리 인상')],{},NOW)
        with patch('save_ticker_news.refresh',return_value=fresh):
            board=refresh_or_retain(prior,NOW)
        self.assertEqual(board['meta']['directCount'],1)
        self.assertEqual(board['meta']['macroCount'],1)
        retained=next(x for x in board['items'] if x['id']=='1')
        self.assertEqual(retained['publishedKST'],prior['items'][0]['publishedKST'])
        self.assertTrue(retained['retained'])


if __name__ == '__main__':
    unittest.main()
