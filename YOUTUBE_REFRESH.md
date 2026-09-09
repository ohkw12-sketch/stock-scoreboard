# 유튜브 오후 통합 갱신 운영

매일 한국시간 오후 3시 45분, 이 저장소와 https://stock-scoreboard.pages.dev/ 만 사용한다.
전체시장 갱신과 함께 원문·시황·추천을 새로 검토한다. 유튜브 후보를 먼저 검증·반영하고 전체시장 갱신으로 신규 추천의 가격까지 갱신한 뒤 함께 배포한다.
자막 해석은 예약된 Codex 작업이 담당하고 `youtube_refresh.py`가 근거 검사와 화면 생성을 담당한다.
수동 원문 파일이 생기기를 기다리며 끝내지 말고 아래 수집·검토를 직접 수행한다.

## 수집

1. YouTube에서 김종효, 김종효 김구라, 박시동을 각각 검색하고 최근 게시 순으로 확인한다.
   김구라 경제연구소(@greegura)의 주식 회차를 반드시 포함한다.
   매일경제TV(@MKeconomy_TV), TomatoTV(@TomatoTV_Official), 시동위키(@sidongtv),
   매불쇼(@maebulshow)와 검색에서 발견한 다른 원본 출연 채널도 확인한다.
   오늘부터 10일 전까지의 날짜를 포함한다. 쇼츠·재편집 중복보다 원본 회차를 우선한다.
   검색 결과는 전수 출연 보장이 아니므로 실제 확인 범위와 미확인 영상을 기록한다.
2. 영상 페이지를 연 뒤 제목이 목적 영상과 일치하고 본문이 로드됐는지 확인한다.
   더보기를 펼쳐 정확한 공개일을 읽는다. 상대 날짜만으로 날짜를 확정하지 않는다.
   브라우저의 `content.exportYouTubeTranscript()`로 공개 자막을 확보하고 읽는다.
   로딩 전 실패하면 페이지 확인 후 한 번 재시도한다. 실제 자막이 없거나 접근이 실패하면
   제목과 링크만 대기 목록에 기록한다. 로그인·유료 제한을 우회하지 않는다.
3. 원문 전체의 문맥을 읽고 김종효의 긍정·조건부 후보, 제외·경계 의견과 매수 조건을 구분한다.
   진행자 질문·시청자 보유주·다른 출연자의 의견은 김종효 추천으로 간주하지 않는다.
   박시동은 시장·정책·산업 요약만 작성한다. 자동 자막의 종목명 오자는 공식 종목명과 대조한다.
   주간과 각 인물의 영상은 오래된 날짜부터 정렬하고 인물별 순번을 이어서 표시한다.
   새 발언이 기존 후보를 부정하면 해당 후보를 ledger의 이전 recommendations에서 제외하거나
   최신 조건을 반영한다. 원래 자막은 수정하지 않으며 정정 사유를 남긴다.

## 입력과 검증

`cache/youtube/verified_transcripts_input.json` (비공개·Git 제외):

```json
{
  "videos": [{
    "videoId": "실제11자ID", "title": "원본 제목", "channel": "채널명",
    "publishedAt": "YYYY-MM-DD", "accessBasis": "public_caption",
    "transcript": "확보한 원문 전체",
    "verification": {"status":"verified", "reviewer":"Codex · 원문 문맥 검토", "verifiedAt":"현재 ISO 시간 +09:00"},
    "statements": [{"kind":"summary", "speaker":"김종효", "at":"6:19", "quote":"원문에 정확히 존재하는 근거 구간", "summary":"짧은 한국어 재서술"},
      {"kind":"recommendation", "speaker":"김종효", "at":"6:35", "quote":"정확한 근거 구간", "summary":"추천 근거 재서술", "stock":"공식 종목명", "grade":"조건부", "risk":"발언의 조건·위험"}]
  }],
  "discovery": {"checkedAt":"현재 ISO 시간 +09:00", "sources":[{"query":"실제 조회 채널·검색어", "status":"확인 또는 실패", "scope":"범위와 누락 사유"}],
    "videos":[{"videoId":"미확인11자ID", "speaker":"김종효", "title":"제목", "channel":"채널", "problem":"원문 확인 대기 사유"}]}
}
```

`statements.kind`는 summary / recommendation, grade는 핵심 / 긍정 / 조건부 / 섹터.
근거 구간은 검증을 위해 로컬에만 저장한다. 공개 파일에는 재서술·시각·링크만 싣는다.
조건은 추천 행과 시황 요약 모두에 유지한다. 직접 종목명이 없는 테마로 종목을 추측하지 않는다.

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe youtube_refresh.py
.venv/Scripts/python.exe save_ticker_news.py --output test_output/saveticker-market.test.json
.venv/Scripts/python.exe -m unittest discover -s tests -p test_youtube_refresh.py
.venv/Scripts/python.exe board_contract.py
.venv/Scripts/python.exe promote_sections.py --youtube-only
```

동일일 실패 복구나 명시적 수동 재실행인 경우 `youtube_refresh.py --force`를 사용한다.
당일 재실행 전 마지막 공개본과 비교해 신규 내용·실패 복구가 있을 때만 갱신한다.
확보한 원문이 0개면 이전 본문과 날짜를 유지하고 실패 상태만 기록한다.
부분 성공은 확인된 원문만 반영하고 누락을 별도 대기 목록에 표시한다.
이전 9월 1일 수동 시황은 previousEdition 보관본으로 분리되어 있다.

## 게시

변경된 공개 파일과 필요한 코드 수정만 검증 후 main에 커밋·푸시한다.
`cache/`의 자막 원문·입력·원장은 커밋하지 않는다. 다른 평가창의 공개 JSON은 수정하지 않는다.
기존 Git 연결 Cloudflare Pages 배포 후 실제 youtube-market.json의 내용 해시를 로컬과 비교한다.
조회 시각만 바뀌고 내용 변화가 없으면 불필요한 배포와 알림은 생략한다.
실패·완료·새로운 추천 변화·사용자 조치 필요 시에만 간결하게 알린다.
