# PC 없이 실행하는 스코어보드

## 원자료 보관

같은 GitHub 저장소의 `raw-data` 브랜치가 지속 원자료 저장소다.
`cloud_state.py`가 허용 목록의 가격·공시·컨센서스·검증 스냅샷과 AI 검토 원장을
파일별로 압축하고 AES-256-GCM으로 암호화한다. 공개 저장소에 원문을 노출하지 않는다.
키는 Actions secret `RAW_STATE_KEY`에만 제공하며 로그·저장소에 기록하지 않는다.
브라우저 쿠키, yfinance 인증 캐시, API 키, 임의 로컬 파일은 보관하지 않는다.
변경 없는 자료는 동일 암호화 객체를 재사용한다. 객체는 16MiB 단위여서 100MB
단일 Git 파일 제한을 피한다. `state.enc`가 현재 검증 세대를 가리킨다.
Git 이력과 과거 스냅샷은 유지하며 자동 삭제·강제 푸시는 하지 않는다.
이는 만료되는 Actions 캐시/90일 artifact와 별개다. 장기 이력 증가에 따라
실제 저장소 크기를 모니터링하고 10GB 권장 상한 전에 별도 보관 정책을 정해야 한다.
키가 사라지면 암호화 자료를 복호화할 수 없다. 키 교체는 기존 자료 전체를 다시
암호화한 뒤 진행해야 한다. 이관 시 PC 원본은 그대로 남긴다.

## 실행과 전환

사용자 변경 지시(2026-10-18): 서버 정기 예약 대신 모바일·PC 수동 실행을 사용한다.
서버 cron과 활성화 변수는 사용하지 않는다. 기존 PC 예약은 이 변경으로 중지하지 않는다.

모바일 Safari/Chrome 또는 PC 브라우저에서 GitHub에 로그인한 뒤 아래 주소를 연다.

https://github.com/ohkw12-sketch/stock-scoreboard/actions/workflows/manual-refresh.yml

**모바일·PC 전체 갱신 → Run workflow → main → Run workflow**로 한 번 실행한다.
작업은 GitHub 서버에서 계속되므로 실행 후 PC나 브라우저를 켜 둘 필요가 없다.
같은 실행 페이지에서 진행 상태와 성공/실패를 확인한다. 별도 앱 설치는 필요 없다.
이 주소를 휴대폰 홈 화면 또는 PC 즐겨찾기에 저장할 수 있다.

한 번의 실행으로 빠진 원문 검토, 전체 시장·순환·가치성장·보유·종합추천·성과 재계산,
검증, 기존 Cloudflare Pages 게시, 실제 공개 파일 대조까지 수행한다.
09:00 전에는 08:00 탐색 단계를, 이후에는 기존 코드의 15:00 최종 단계를 사용한다.
이는 단계 식별자이며 실행시각이나 가격시각을 과거로 바꾸지 않는다.
10:30–15:00에 눌러도 이슈만 갱신하는 중간 스캔으로 축소하지 않는다.
동시 실행은 같은 실행 그룹에서 직렬 처리한다. 휴장일에는 갱신·게시하지 않는다.
KRX 달력과 공식 임시휴장 공지 확인 실패도 게시를 차단한다.

수동 `cloud-refresh.yml`의 `verify-state`는 AI를 호출하지 않고 암호화 원자료 복원,
무결성, 실제 프레임 로딩, Python·화면 회귀 검증만 한다. 이 모드는 휴일에도 가능하다.
`refresh`는 AI 원문 검토부터 실제 갱신·게시까지 수행한다.
AI는 `.github/codex/cloud-review.md`에 따라 근거 입력과 후보만 작성한다.
확인되지 않은 자료는 이전 값과 원래 날짜를 보존하며 실패 사유를 남긴다.
보유수량·평단과 사용자 판단 필드는 자동 갱신 대상이 아니다.

필수 Actions secrets:
- RAW_STATE_KEY: 기존 암호화 키. 기존 키를 임의로 교체하지 않는다.
- DART_API_KEY, KIS_APP_KEY, KIS_APP_SECRET: 기존 읽기 전용 자료 수집 키.
- OPENAI_API_KEY: 유효한 API 프로젝트 키. ChatGPT 구독과 API 사용료는 별도다.
- NAVER_CLIENT_ID, NAVER_CLIENT_SECRET: 선택적 뉴스 검색 연결.

키 등록 주소:
https://github.com/ohkw12-sketch/stock-scoreboard/settings/secrets/actions

API 키는 채팅이나 공개 파일에 붙여 넣지 않고 위 페이지에 OPENAI_API_KEY라는
이름의 repository secret으로 직접 등록한다. 서버에는 기존 자료 수집 키와 암호화 키가
이미 등록되어 있어 이를 새로 만들 필요가 없다. AI 키가 없거나 결제가 비활성화되어
있으면 실제 전체 갱신은 실행할 수 없으며, 복원 검증만으로 전환 완료라고 하지 않는다.
키 등록 후 영업일에 실제 수동 refresh와 공개 파일 일치까지 확인해야 전환 완료다.

## 복구

main과 raw-data를 각각 checkout하고 RAW_STATE_KEY를 안전하게 제공한 후:

```
python cloud_state.py restore --state-dir .cloud-state
python cloud_runner.py verify
```

과거 세대가 필요하면 raw-data 브랜치의 원하는 Git 커밋을 checkout한 상태로
복원한다. 공개 보드는 main에, 원자료는 raw-data에 따로 보관한다.
