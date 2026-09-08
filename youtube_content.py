"""Local evidence ledger. Public captions are reviewed by the morning collector.

Raw captions stay in ignored cache; youtube_refresh publishes paraphrases only.
"""
from datetime import datetime
from pathlib import Path
import hashlib
import re

from growth_discovery import KST, cached_source_status, day, json_write, read_json
from growth_documents import _timestamp


def collect_youtube_content(config, now=None, *, reuse=False):
    now = now or datetime.now(KST)
    cache = Path(config['cache_dir']) / 'youtube'
    ledger_path, status_path = cache / 'content_ledger.json', cache / 'content_status.json'
    prior = read_json(ledger_path, [])
    previous_status = read_json(status_path, {'source': '검증 유튜브 발언 원문', 'status': '미수집'})
    if reuse:
        return prior, cached_source_status(previous_status, now, ledger_path.exists())
    input_path = Path(config.get('youtube_verified_transcripts_file', cache / 'verified_transcripts_input.json'))
    if not input_path.exists():
        return prior, dict(previous_status, status='원문검증대기', networkRequests=0,
                           problem='제공되거나 허용된 검증 자막 원문 없음; 가격 갱신은 신규 발언 갱신이 아님')
    payload = read_json(input_path, {})
    supplied = payload.get('videos') if isinstance(payload, dict) else None
    if not isinstance(supplied, list):
        raise ValueError('유튜브 원문 입력 videos 목록 없음')
    videos = {video['videoId']: video for video in prior}
    failures, changed = [], 0
    for index, video in enumerate(supplied):
        try:
            identity = str(video.get('videoId', ''))
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', identity):
                raise ValueError('유효한 영상 식별자 없음')
            if video.get('accessBasis') not in {'creator_permission', 'user_supplied', 'licensed', 'public_caption'}:
                raise ValueError('자막 확보 권한 근거 없음')
            text = str(video.get('transcript', '')).strip()
            if not text or not video.get('channel') or not video.get('title'):
                raise ValueError('자막 원문·채널·제목 누락')
            published = day(video.get('publishedAt'))
            if not published or published > str(now.date()):
                raise ValueError('영상 공개일 미확인 또는 미래 날짜')
            verification = video.get('verification') or {}
            if verification.get('status') != 'verified' or not verification.get('reviewer'):
                raise ValueError('발언 원문 검증 미완료')
            verified_at = _timestamp(verification.get('verifiedAt'), now)
            if day(verified_at) < published:
                raise ValueError('확인일이 공개일보다 이름')
            statements = video.get('statements', [])
            for statement in statements:
                quote = str(statement.get('quote', '')).strip()
                if not quote or quote not in text:
                    raise ValueError('발언 인용이 자막 원문에 없음')
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            old = videos.get(identity, {})
            changed += int(old.get('contentHash') != digest or old.get('statements') != statements)
            videos[identity] = dict(videoId=identity, url='https://www.youtube.com/watch?v=' + identity,
                                   title=video['title'], channel=video['channel'], publishedAt=published,
                                   transcript=text, contentHash=digest, statements=statements,
                                   accessBasis=video['accessBasis'], lastVerified=verified_at,
                                   verifiedBy=verification['reviewer'], status='원문확인')
        except (ValueError, TypeError, AttributeError) as exc:
            failures.append({'inputIndex': index, 'error': str(exc)})
            if isinstance(video, dict) and video.get('videoId') in videos:
                videos[video['videoId']]['status'] = '상태확인필요'
    result = sorted(videos.values(), key=lambda video: (video['publishedAt'], video['videoId']))
    status = dict(source='검증 유튜브 발언 원문', status='부분수집' if failures else '정상',
                  contentUpdatedKST=now.isoformat(timespec='seconds') if changed else previous_status.get('contentUpdatedKST'),
                  lastVerified=max((v['lastVerified'] for v in result if v['status']=='원문확인'), default=previous_status.get('lastVerified')),
                  processedAt=now.isoformat(timespec='seconds'), videoCount=len(result),
                  changedVideos=changed, failures=failures, networkRequests=0,
                  scope='제공된 검증 원문만; 채널 전체 신규 영상 자동 수집을 의미하지 않음')
    json_write(ledger_path, result)
    json_write(status_path, status)
    return result, status
