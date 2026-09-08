import copy
from datetime import datetime, timedelta
import unittest
import pandas as pd
from growth_discovery import KST
from youtube_refresh import build_board, validate_board
from refresh_all import refresh_youtube_prices

NOW = datetime(2026, 9, 8, 8, 20, tzinfo=KST)


class YouTubeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.prior = {'meta': {'updatedKST':'2026-09-01 08:00', 'priceBasis':'가격 미확인'},
                      'weeks':[], 'recommendations':[], 'expired':''}
        self.scan = {'checkedAt': NOW.isoformat(), 'sources':[{'query':'김종효', 'status':'확인'}], 'videos':[]}

    def video(self, date='2026-09-07', identity='abcdefghijk', speaker='김종효'):
        return dict(videoId=identity, url='https://www.youtube.com/watch?v='+identity,
                    title='실제 원문', channel='원본 채널', publishedAt=date, status='원문확인',
                    transcript='추가 성장이 확인돼야 합니다.', statements=[dict(kind='recommendation',
                    speaker=speaker, at='6:10', quote='추가 성장이 확인돼야 합니다.',
                    summary='성장 확인 뒤 접근', stock='확인종목', grade='조건부', risk='성장 미확정')])

    def test_rolling_window_excludes_old_and_future_and_deduplicates(self):
        video = self.video()
        b=build_board(self.prior,[self.video('2026-08-28','oldoldoldol'), self.video('2026-09-09','futurfuturf'),video,video], self.scan,NOW)
        self.assertEqual(b['contentStatus']['videoCount'],1)
        self.assertEqual(len(b['recommendations']),1)
        self.assertEqual(b['meta']['range'],'2026.08.29–09.08')
        validate_board(b)

    def test_failed_content_preserves_original_date_and_rows(self):
        old=copy.deepcopy(self.prior)
        old['recommendations']=[['2026.08.25','기존종목','조건부','08.25','기존','조건','가격 미확인']]
        b=build_board(old,[],self.scan,NOW)
        self.assertEqual(b['recommendations'],old['recommendations'])
        self.assertEqual(b['meta']['updatedKST'],old['meta']['updatedKST'])
        self.assertIn('실패',b['contentStatus']['status'])

    def test_title_only_video_never_creates_recommendation(self):
        self.scan['videos']=[dict(videoId='pendingpend', speaker='김종효',title='신규 종목 급등',channel='채널',problem='자막 없음')]
        b=build_board(self.prior,[self.video()],self.scan,NOW)
        self.assertEqual(len(b['recommendations']),1)
        self.assertEqual(b['contentStatus']['status'],'부분갱신')
        self.assertEqual(b['contentStatus']['pendingCount'],1)

    def test_park_cannot_become_kim_stock_pick(self):
        with self.assertRaises(ValueError):
            build_board(self.prior,[self.video(speaker='박시동')],self.scan,NOW)

    def test_quote_must_exist_in_caption(self):
        v=self.video();v['statements'][0]['quote']='없는 발언'
        with self.assertRaises(ValueError):build_board(self.prior,[v],self.scan,NOW)

    def test_stale_scan_cannot_claim_today(self):
        self.scan['checkedAt']=(NOW-timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):build_board(self.prior,[self.video()],self.scan,NOW)

    def test_same_day_duplicate_keeps_conditions_from_both_appearances(self):
        first=self.video(); second=self.video(identity='lmnopqrstuv')
        second['statements'][0]['risk']='비중 관리'
        b=build_board(self.prior,[first,second],self.scan,NOW)
        self.assertIn('성장 미확정',b['recommendations'][0][5])
        self.assertIn('비중 관리',b['recommendations'][0][5])

    def test_afternoon_prices_do_not_overwrite_morning_content_state(self):
        b=build_board(self.prior,[self.video()],self.scan,NOW)
        prices=pd.DataFrame([dict(ticker='123456',name='확인종목',date=pd.Timestamp('2026-09-08'),close=12000)])
        updated=refresh_youtube_prices(b,prices)
        for field in ('weeks','verifiedContent','contentStatus','discovery'):
            self.assertEqual(updated[field],b[field])
        self.assertEqual(updated['meta']['updatedKST'],b['meta']['updatedKST'])
        self.assertEqual(updated['meta']['status'],b['meta']['status'])
        self.assertIn('12,000',updated['recommendations'][0][6])


if __name__ == '__main__':unittest.main()
