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
        self.assertEqual(selected[0]['scope'], '국내 섹터 영향')
        self.assertEqual(selected[0]['sectors'], ['정유·화학·운송'])

    def test_board_summarizes_actual_article_content_and_keeps_source_link(self):
        items = [article(1, '한화오션, 태국 호위함 우선협상대상자 선정', views=7000)]
        details = {'1': {'source': 'SaveTicker', 'content': [
            {'type': 'text', 'content': '한화오션은 태국 해군 차세대 호위함 사업의 우선협상대상자로 선정됐습니다.'},
            {'type': 'text', 'content': '최종 계약 체결 여부와 사업 규모는 후속 협상에서 정해집니다.'},
        ]}}
        board = build_board(items, details, NOW)
        validate_board(board)
        self.assertIn('우선협상대상자', board['items'][0]['summary'])
        self.assertEqual(board['items'][0]['url'], 'https://saveticker.com/news/1')
        self.assertEqual(board['meta']['directCount'], 1)

    def test_failed_refresh_retains_previous_board(self):
        previous = build_board([article(1, '삼성전자, AI 칩 협력')], {}, NOW)
        with patch('save_ticker_news.refresh', side_effect=TimeoutError):
            retained = refresh_or_retain(previous, NOW)
        self.assertEqual(retained['items'], previous['items'])
        self.assertEqual(retained['collection']['status'], '실패·이전유지')


if __name__ == '__main__':
    unittest.main()
