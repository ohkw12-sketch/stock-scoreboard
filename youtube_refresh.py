"""Build the morning YouTube edition from source-backed, reviewed captions.

Discovery and semantic review run in the scheduled Codex task (YOUTUBE_REFRESH.md).
This command validates the evidence, deduplicates videos/stocks and publishes no
raw captions. Afternoon market refreshes only update stock prices.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import re

from growth_discovery import KST, json_write, read_json
from youtube_content import collect_youtube_content

ROOT = Path(__file__).resolve().parent
SPEAKERS = {'김종효': 'kim', '박시동': 'park'}


def reviewed_statements(video):
    statements = video.get('statements', [])
    if not statements or video.get('status') != '원문확인':
        return []
    for item in statements:
        if (item.get('speaker') not in SPEAKERS or not item.get('summary')
                or item.get('kind') not in ('summary', 'recommendation')
                or not re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?', str(item.get('at', '')))
                or not item.get('quote') or item['quote'] not in video['transcript']):
            raise ValueError('발언자·요약·시각·원문 근거 검증 실패: ' + video['videoId'])
        if item['kind'] == 'recommendation' and (
                item['speaker'] != '김종효' or not item.get('stock') or not item.get('risk')
                or item.get('grade') not in ('핵심', '긍정', '조건부', '섹터')):
            raise ValueError('추천 발언자·종목·조건·등급 누락: ' + video['videoId'])
    return statements


def build_board(prior, ledger, scan, now):
    checked = datetime.fromisoformat(scan['checkedAt'])
    if checked.tzinfo is None or checked > now or checked.astimezone(KST).date() != now.date():
        raise ValueError('오늘 실제 조회한 출처 목록이 필요합니다.')
    if not isinstance(scan.get('sources'), list) or not scan['sources']:
        raise ValueError('조회 범위·실패 기록이 필요합니다.')
    start = now.date() - timedelta(days=10)
    selected = []
    for video in {v['videoId']: v for v in ledger}.values():
        if str(start) <= video['publishedAt'] <= str(now.date()) and reviewed_statements(video):
            selected.append(video)
    selected.sort(key=lambda v: (v['publishedAt'], v['videoId']))
    result = deepcopy(prior)
    pending = []
    for item in scan.get('videos', []):
        if (not re.fullmatch(r'[A-Za-z0-9_-]{11}', item.get('videoId', ''))
                or item.get('speaker') not in SPEAKERS or not item.get('title')):
            raise ValueError('발견 영상의 식별자·발언자·제목 오류')
        if item['videoId'] not in {v['videoId'] for v in selected}:
            pending.append({k: item.get(k) for k in ('videoId', 'speaker', 'title', 'channel', 'problem')})
    failures = [s for s in scan['sources'] if s.get('status') != '확인']
    state = {'status': '부분갱신' if pending or failures else '원문확인',
             'checkedAt': scan['checkedAt'], 'videoCount': len(selected),
             'pendingCount': len(pending), 'sources': scan['sources'],
             'scope': '등록된 조회 범위 내 확인 결과 · 모든 출연 영상 전수수집을 보장하지 않음'}
    result['contentStatus'] = state
    result['discovery'] = {'checkedAt': scan['checkedAt'], 'pending': pending}
    if not selected:
        state['status'] = '원문갱신실패·이전유지'
        result['meta']['status'] = '새 원문을 확보하지 못해 이전 내용과 기준일 유지'
        return result
    if prior.get('schemaVersion') != 2:
        result['previousEdition'] = {k: deepcopy(prior[k]) for k in ('meta', 'weeks', 'recommendations', 'expired')}
    weeks, candidates = {}, {}
    numbers = {'kim': 0, 'park': 0}
    for video in selected:
        date = datetime.fromisoformat(video['publishedAt']).date()
        monday = date - timedelta(days=date.weekday())
        week = weeks.setdefault(str(monday), {'label': f'{max(start, monday):%m.%d}–{min(now.date(), monday + timedelta(days=6)):%m.%d}', 'kim': [], 'park': []})
        for speaker, key in SPEAKERS.items():
            items = [s for s in video['statements'] if s['speaker'] == speaker]
            if not items:
                continue
            numbers[key] += 1
            number = chr(0x2460 + numbers[key] - 1) if numbers[key] <= 20 else str(numbers[key])
            week[key].append({'number': number, 'date': date.strftime('%m.%d'),
                              'title': video['title'], 'subtitle': video['channel'] + ' · 공개 자막 원문 확인',
                              'url': video['url'], 'points': [f"{s['at']} · {s['summary']}" for s in ( [s for s in items if s['kind']=='summary'] or items)]})
            for item in items:
                if item['kind'] != 'recommendation':
                    continue
                name = item['stock']
                existing = candidates.get(name)
                first = existing[0] if existing else date.strftime('%Y.%m.%d')
                candidates[name] = [first, name, item['grade'], date.strftime('%m.%d'), item['summary'], item['risk'], '가격 미확인']
                if existing and existing[3] == date.strftime('%m.%d'):
                    candidates[name][4] = existing[4] + ' / ' + item['summary']
                    candidates[name][5] = existing[5] + ' / ' + item['risk']
                    if existing[2] == '조건부':
                        candidates[name][2] = '조건부'
    prices = {r[1]: r[6] for r in prior.get('recommendations', []) if len(r) == 7}
    for name, row in candidates.items():
        if name in prices:
            row[6] = prices[name]
    result.update(schemaVersion=2, weeks=[weeks[k] for k in sorted(weeks)],
                  recommendations=sorted(candidates.values(), key=lambda r: (r[0], r[1])),
                  expired=f'{start:%m.%d} 이전 발언은 현재 후보에서 제외 · 원문 확인된 김종효 발언만 포함',
                  verifiedContent=[{k: v[k] for k in ('videoId', 'url', 'title', 'channel', 'publishedAt', 'status')} for v in selected])
    result['meta'].update(updatedKST=now.strftime('%Y-%m-%d %H:%M'),
                         contentThrough=max(v['publishedAt'] for v in selected),
                         range=f'{start:%Y.%m.%d}–{now:%m.%d}',
                         status='원문 기반 시황 갱신' + (' · 일부 영상 확인 대기' if pending or failures else ''),
                         summary=[{'label': '원문 확인', 'value': f'{len(selected)}편', 'detail': '최근 10일 전부터 오늘까지'},
                                  {'label': '김종효 후보', 'value': f'{len(candidates)}개', 'detail': '조건·위험 포함 · 중복 통합'},
                                  {'label': '본문 확인 대기', 'value': f'{len(pending)}편', 'detail': '제목만으로 추천하지 않음'}])
    result.setdefault('refreshStatus', {})['content'] = state
    return result


def validate_board(board):
    if not isinstance(board.get('weeks'), list) or not isinstance(board.get('recommendations'), list):
        raise ValueError('유튜브 화면 형식 오류')
    if any(len(row) != 7 for row in board['recommendations']):
        raise ValueError('추천 표 열 수 오류')
    if not board.get('contentStatus', {}).get('checkedAt'):
        raise ValueError('원문 조회 시각 없음')
    for week in board['weeks']:
        for key in ('kim', 'park'):
            for card in week[key]:
                if not re.fullmatch(r'https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}', card.get('url', '')):
                    raise ValueError('원본 영상 링크 없음')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, default=ROOT/'cache/youtube/verified_transcripts_input.json')
    parser.add_argument('--live', type=Path, default=ROOT/'youtube-market.json')
    parser.add_argument('--output', type=Path, default=ROOT/'test_output/youtube-market.test.json')
    parser.add_argument('--force', action='store_true', help='수동 복구 시 오전·동일일 제한 해제')
    args = parser.parse_args()
    now = datetime.now(KST)
    prior = read_json(args.live, {})
    if not args.force and (now.hour != 8 or prior.get('contentStatus', {}).get('checkedAt', '').startswith(str(now.date()))):
        print('오전 갱신 시간 외 또는 오늘 처리 완료 · 게시 파일 유지')
        return
    payload = read_json(args.input, {})
    ledger, status = collect_youtube_content({'cache_dir': ROOT/'cache', 'youtube_verified_transcripts_file': args.input}, now)
    if status.get('failures'):
        raise ValueError('원문 검증 실패: ' + str(status['failures']))
    board = build_board(prior, ledger, payload['discovery'], now)
    if board.get('schemaVersion') == 2:
        validate_board(board)
    json_write(args.output, board)
    print(board['contentStatus']['status'], '· 원문', board['contentStatus']['videoCount'], '편')


if __name__ == '__main__':
    main()
