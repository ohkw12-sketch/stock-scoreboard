# 이슈 확산 운영

정식 저장소와 stock-scoreboard.pages.dev만 사용한다. 새 공개 데이터는
`issue-spread.json`이며 기존 data.json의 프1·순환·가치·성장·보유를 변경하지 않는다.
화면은 `index.html`, `issue-spread.js`, `issue-spread.css`로 구성한다.

## 실행 순서

예약은 Codex의 기존 두 예약을 08:00/15:00 전체 갱신으로 정리하고 10:30만 추가한다.
GitHub Actions에 같은 cron을 추가하지 않는다. 한국시간 기준이며 예약 실행자는 첫 단계로
`issue_spread.trading_day(datetime.now(KST))`를 확인한다. 휴장이면 조회·분석·파일 갱신·알림
없이 종료한다. 달력 범위 밖/확인 실패도 진행하지 않는다. 임시휴장은 KRX 공식 공지로
확인하고 실행을 중단한다. 프로그램의 XKRX 달력과 공식 공지가 다르면 공식 공지가 우선이다.

1. 08:00: 밤사이 미국장, 대형 뉴스, DART/KIND/SEC, 기업 IR과 유튜브시황2의
   삼프로TV·오선의 미국증시를 검색·원문 검토한다. 영상 미확보는 누락으로 남긴다.
   유튜브는 detection에만 넣는다. 이를 통해 새 이슈와 직접 관련 제품·고객·수주를
   찾고 KOSPI/KOSDAQ 업종·제품 목록 및 기존 추천 전체와 대조한다.
2. 신뢰도 검증 순서는 DART → KIND → SEC → 기업IR → Reuters → 연합뉴스 → 증권사 리포트.
   제목만 읽은 자료, 추측, 확인되지 않은 계약금액·실적 전망은 evidence에 넣지 않는다.
   부정/취소 공시는 기존 긍정 근거를 무효로 바꾼다. 같은 사건 보도는 중복 근거로 세지 않는다.
3. 10:30: 이번 시각의 국내 가격·등락·5일 조정수익률·20일선 이격·거래대금을
   수집한다. 거래대금 배율은 최근 20거래일 **같은 경과시각** 평균 대비 배율이며
   일일 전체 거래대금과 비교하지 않는다. 데이터 공급 시각, 출처 URL, 기업행동 확인을
   함께 기록한다. 부분 누락 종목은 대체 소스로 재시도하고 누락 이름·이유를 기록한다.
4. 15:00: 오전 후보 및 새 후보의 2차/3차 확산과 2026Q3~2027Q2 실적·컨센서스 상향·
   수주·고객·CAPEX 연결을 다시 원문으로 검증한다. 실제로 덜 오른 종목만 최종선정 가능하다.
5. 실행자가 검증 원자료를 Git 제외 `cache/issue-input.json`에 아래 형식으로 쓴다.
   수집은 예약 실행자의 연결 도구/공식 소스 검토가 담당하고 Python은 사실을 만들어내거나
   뉴스 본문을 자동 해석하지 않는다. 원자료 생성이 없으면 자동으로 수집실패가 된다.
6. 08:00/15:00은 `refresh_all.py`의 기존 실행이 이슈 계산을 함께 호출한다.
   10:30만 `python issue_spread.py --slot 10:30`을 별도로 실행한다.
   후보는 `test_output/issue-spread.test.json`에서 확인한다.
7. 게시가 승인된 예약은 검증 후 `python issue_spread.py --slot 08:00 --promote`
   (해당 시각으로 변경)로 이슈 파일만 승격한다. 전체 실행에 시간이 오래 걸리면
   승격 직전에 장중 인용값을 다시 수집한다. 검증 실패는 이전 내용과 원래 기준일을
   보존하고 실패 상태만 명시한다. 기존 전체 보드는 기존 promote_sections.py를 사용한다.
   미배포 기능 코드와 다른 작업의 미커밋 변경을 임의로 함께 게시하지 않는다.

## 원자료 계약

최상위: `asOf`(시간대 포함 ISO 실행 시각), `issues`(배열), `missing`(누락 목록).
이슈: `id`(안정된 사건 ID), `name`, `detection`, `evidence`, `leaders`, `candidates`.
`detection`은 채널·원문 링크·공개일·짧은 탐지 요약만 포함하며 점수 0이다.

evidence 한 건의 필수 필드:

```json
{
  "id": "동일사건-종목-사실종류",
  "ticker": "000000",
  "source": "DART",
  "url": "https://dart.fss.or.kr/원문주소",
  "publishedAt": "2026-09-10T07:00:00+09:00",
  "verifiedAt": "2026-09-10T07:55:00+09:00",
  "reviewer": "원문 검토자",
  "status": "verified",
  "polarity": "positive",
  "kind": "contract",
  "period": "2026Q3",
  "summary": "원문으로 확인한 사실의 짧은 재서술"
}
```

kind: contract/policy/customer/guidance/product/earnings/consensus/capex.
period는 원문에 해당 분기 연결이 확인된 경우만 기입한다. 위 예시는 구조 설명이며 실제 종목이 아니다.
status=verified이고 최근 7일 안에 원문을 재검증한 양의 사실만 점수에 사용한다.

candidates: ticker(6자리), name, sector(KRX 업종과 사업보고서로 검증), evidenceIds, quote.
leaders: name, market(KR 또는 US 등), quote.
quote: status=verified, source, url, asOf, adjustmentChecked=true,
changePct, return5dPct, turnoverRatio, distance20Pct. 숫자는 백분율 단위(3=3%)이다.
국내 시세는 당일 30분 이내, 해외 선행주는 18시간 이내만 유효하다.
누락값은 null로 두고 확인했다고 표시하지 않는다.

## 점수와 단계

- 이슈 20: 계약·정책·고객·가이던스 확인 종류당 5점, 최대 20.
- 주도주 20: 유효 시세 +2% 이상, 같은 시각 거래대금 1.5배 이상.
- 확산 20: 직접 관련 사실이 있는 후보 2개 이상 동반 양의 등락·거래대금 1.5배.
  동반 종목당 5점, 최대 20. 중복 티커 제외.
- 펀더멘털 30: 요청 기간에 연결되는 실적·컨센서스·계약·CAPEX의 서로 다른 종류당
  10점, 최대 30. 날짜만 같은 무관한 사실로 채우지 않는다.
- 진입 10: 당일 +0~5% 및 거래대금 확인 시 10점. +5~10%면 5점.
  하루 +10%, 5일 +25%, 20일선 이격 +20% 중 하나라도 충족하면 0점·WATCH·추격주의.
- 08:00 탐색. 10:30 주도주와 본인 가격 확인 시 확인, 직접 관련 2종목 이상이면 확산.
- 15:00 확산 + 직접수혜 + 펀더멘털 20점 이상 + 총점 75점 이상 + 진입 10점일 때만 최종선정.
- 최종선정과 당일 기존 진입가능/순환 진입적합/절대·상대 저평가를 대조해
  한 영역 추가 통과 DOUBLE, 두 영역 이상 CORE. 단순 이름 중복은 통과가 아니다.
  조건4를 별도 확인하지 않은 경우 조건4 PASS라고 주장하지 않는다.

## 확인

`python -m unittest discover -s tests -p test_issue_spread.py -v`
`node --test tests/test_issue_ui.cjs tests/test_ui_runtime.cjs`
`python board_contract.py`

`python preview_server.py --port 8765`로 로컬 보드를 열고 이슈 확산 탭을 선택한다.
가격/컨센서스 전수 수집 테스트는 원자료 수집 실행 시 수행한다. 기능 테스트의 가상 종목을
실제 issue-spread.json에 게시하지 않는다. 이 기능에는 실시간 구독 계약이나 주문 기능이 없다.
