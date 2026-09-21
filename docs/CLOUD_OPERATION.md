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

`.github/workflows/cloud-refresh.yml`이 KST 평일 08:00/10:30/15:00에 실행된다.
실행 지연이 있더라도 원래 예약 슬롯을 유지하며 XKRX 휴장일에는 수집하지 않는다.
GitHub 예약은 정확한 시각의 실행을 보장하지 않는다.
수동 Actions 실행 `verify-state`는 키 없이 AI를 호출하지 않고, 암호화 원자료
복원·무결성·실제 프레임 로딩·회귀/화면 테스트만 검사한다. 이 모드도 RAW_STATE_KEY는 필요하다.
`refresh`는 AI 원문 검토, 결정적 계산, 검증, 원자료 보관, 게시, 게시 확인을 수행한다.
AI는 `.github/codex/cloud-review.md`에 따라 근거 입력/후보만 작성한다.
비공개 제공 원문은 암호화 보관만 하며 게시에는 짧은 재서술과 링크만 사용한다.
유튜브 자막 등 서버에서 접근하지 못한 소스는 실패로 기록하고 날짜를 보존한다.

필수 Actions secrets:
- RAW_STATE_KEY: 32바이트 난수를 base64 인코딩한 암호화 키
- DART_API_KEY, KIS_APP_KEY, KIS_APP_SECRET: 기존 읽기 전용 자료 수집 키
- OPENAI_API_KEY: 유효한 API 프로젝트 키. ChatGPT 구독과 API 사용료는 별도다.
- NAVER_CLIENT_ID, NAVER_CLIENT_SECRET: 선택적 뉴스 검색 연결

원격 `verify-state` 성공 → `refresh` 실제 실행·게시 검증 성공 → 기존 세 PC 예약
중지 → repository variable `CLOUD_AUTOMATION_ENABLED=true` 순서로 전환한다.
전환 전에는 변수 미설정으로 서버 예약을 막고 PC 예약을 유지한다. OPENAI_API_KEY가
없는 상태를 이관 완료로 보고하지 않는다. API 키는 채팅이나 파일에 붙여 넣지 않고
GitHub Settings → Secrets and variables → Actions에 직접 등록한다.

08:00은 전체 계산과 유튜브·삼프로 원문 검토, 10:30은 이슈 중간 확인만,
15:00은 전체 계산과 이슈 최종 검증을 한다. 이전 문서의 15:45 시각은 대체한다.
검증 실패 구역은 원래 자료/날짜를 유지하고 실패 상태를 기록한다.
기존 수동 `value-refresh.yml`도 같은 클라우드 절차로 연결한다.

## 복구

main과 raw-data를 각각 checkout하고 RAW_STATE_KEY를 안전하게 제공한 후:

```
python cloud_state.py restore --state-dir .cloud-state
python cloud_runner.py verify
```

과거 세대가 필요하면 raw-data 브랜치의 원하는 Git 커밋을 checkout한 상태로
복원한다. 공개 보드는 main에, 원자료는 raw-data에 따로 보관한다.
