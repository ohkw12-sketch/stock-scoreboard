# stock-scoreboard 전체시장 순환매 엔진

기존 보드의 `p11`(순환) 데이터를 KOSPI+KOSDAQ 전 종목 구조로 계산하는 Python 엔진입니다. 기존 `data.json`은 **읽기만 하고 절대 수정하지 않습니다.** 모든 결과는 `test_output` 폴더에 따로 생성됩니다. 배포 기능도 없습니다.

## 가장 쉬운 테스트

1. 이미 Python 데이터 패키지가 있으면 `run_test.bat`를 더블클릭합니다. 처음 실행에서 패키지 오류가 나오면 `setup_windows.bat`를 한 번 실행합니다.
2. 끝나면 `test_output/data.test.json`을 확인합니다.

샘플 모드는 인터넷과 외부 시세 라이브러리가 없어도 실행되며, 같은 입력으로 항상 같은 결과를 만듭니다.

## 실제 전체시장 데이터로 실행

1. Python 3가 설치된 Windows에서 `setup_windows.bat`를 한 번만 더블클릭합니다.
2. 이후에는 `run_screener.bat`만 더블클릭합니다.
3. 결과는 `test_output/data.test.json`과 `test_output/p11.test.json`입니다.

실행 중 원본 `data.json`의 SHA-256을 앞뒤로 비교합니다. 조금이라도 바뀌면 오류로 종료합니다.

## 데이터 소스와 장애 대응

- 1순위 `pykrx`: KOSPI+KOSDAQ 종목 목록, KRX 업종 분류, 날짜별 전 종목 OHLCV/거래대금을 일괄 조회합니다. 종목별 수천 번 호출하지 않고 거래일별 약 80번 호출하는 구조입니다. 익명 접근이 차단된 환경에서는 즉시 다음 소스로 전환합니다.
- 캐시: 정상 수집한 전체 데이터를 `cache/krx_prices.csv.gz`에 저장합니다. 외부 호출 실패 시 마지막 정상 캐시로 계산합니다.
- 2차 대체 `FinanceDataReader + yfinance`: KRX 설명형 목록의 `Industry`와 `.KS`/`.KQ` 가격을 묶음 조회합니다. 화장품·반도체·2차전지·전력기기·전선·조선·방산 등 핵심 순환 테마는 회사명·업종·주요제품 키워드로 보정합니다.
- 재시도: 외부 호출은 기본 3회, 점증 대기 후 다음 소스로 넘어갑니다.
- 세부 테마 보정: `sector_overrides.example.csv`에 `종목코드,세부섹터`를 추가하면 KRX 대분류보다 우선 적용됩니다. 운영할 때는 파일명을 복사해 별도 관리해도 됩니다.

무료 비공식 데이터는 장기적으로 형식이나 접근 정책이 바뀔 수 있습니다. 실거래 주문·자동매매용이 아니라 보드 스크리닝용으로 설계했습니다.

## 계산 내용

- 시장 기준: 당일 KOSPI+KOSDAQ 구성 종목의 동일가중 수익률
- 섹터 상대강도: 섹터 동일가중 1일/3일/5일 수익률에서 시장 수익률 차감
- 거래대금 변화: 최근 3일 평균과 그 이전 10일 평균 비교
- 상승종목 비율: 섹터 내 당일 상승 종목 비율
- 선도주 강도: 섹터 상위 20% 종목의 5일 수익률과 시장 5일 수익률 차이
- 확산형/선도주 견인형: 상승 비율·거래대금과 선도주 강도를 독립 평가해 둘 다 포착
- 시작일: 최근 20~40영업일 창에서 종합점수·3일 RS와 확산/선도 조건이 이틀 지속된 최초일을 역산
- 단계: `①초기 ②확산 ③주도 ④눌림 ⑤재반등 ⑥후반 X조기이탈 X종료`
- 주기/위험: 과거 활성 구간 중앙값(부족하면 25일), 현재 경과, 위치 %, 낙폭·집중도·단기 약세 기반 위험 게이지
- 종목 관계: 종목 3일 수익률과 섹터 3일 수익률의 차이로 `선행/동행/후행`

상세 수치는 기존 화면이 무시해도 되는 추가 JSON 필드로 넣었습니다. 기존 화면이 요구하는 `rank/name/stage` 및 행 필드도 유지합니다.

## 명령줄 사용(선택)

```powershell
python rotation_screener.py --mode sample
python rotation_screener.py --config config.example.json
python -m unittest discover -s tests -v
```

## 파일 안내

- `rotation_screener.py`: 데이터 수집, 캐시, 계산, 단계 판정, JSON 생성
- `config.example.json`: 조회·캐시·출력 설정 예시
- `sector_overrides.example.csv`: 세부 섹터 수동 보정 예시
- `run_test.bat`: 오프라인 샘플 테스트
- `setup_windows.bat`: 최초 1회 환경 준비
- `run_screener.bat`: 실제 전체시장 실행
- `tests/test_engine.py`: 계산 및 p11 호환성 테스트
- `test_output/*.test.json`: 테스트 결과(실행 후 생성)

## 현재 보드에 적용할 때

이번 작업에서는 적용·배포하지 않습니다. 나중에 검증이 끝나면 `test_output/p11.test.json` 안의 `p11` 값만 기존 `data.json`의 `p11`에 교체할 수 있습니다.

