# 예상실적 보고서 수집 엔진

`forecast_report_engine.py`는 예상실적 원자료 수집기다. `forecast_integration.py`가 올해·내년 연간값이 모두 있고 불일치 검토가 아닌 종목만 기존 성장근거 계산에 연결한다. 연결 연간 회사 가이던스와 외부 전망이 함께 있으면 같은 기간의 매출·영업이익을 항목별로 비교해 발표일이 더 늦은 자료를 사용한다. 같은 날이거나 날짜가 불명확하면 회사 가이던스를 우선한다. 확정실적 기반 가치 원점수와 KIS 추정치 상향 이력은 바꾸지 않는다.

## 수집 범위

- 기본 기간: 실행 연도 1월 1일부터 실행일까지
- 기본 목록: 네이버 증권 국내종목 리서치 공개 요약, 네이버가 연결한 증권사 원문 PDF, 한경 컨센서스 기업분석 보고서
- 누락 보충: 보고서 원문까지 읽고도 현재 예상치가 없는 종목만 FnGuide 공개 집계 컨센서스에서 올해·내년 값을 확인한다. 개별 증권사 추정치와 섞지 않고 `외부 집계 컨센서스`로 표시한다.
- 추출 대상: 분기 및 연간 매출액·영업이익 예상치
- 중복 처리: 종목·증권사별 최근 보고서 2개까지만 분석하고 동일 증권사의 같은 기간 값은 최신 보고서를 사용한다.
- 금액 단위: 모든 추출값을 억원으로 변환한다. PDF에서 단위를 확인하지 못하면 수치를 저장하지 않는다. 메리츠 기업보고서의 정형 표만 명시적인 십억원 템플릿을 허용한다.
- 신선도: 보고서일로부터 90일을 초과한 값은 이력에는 남지만 현재 컨센서스에서 제외한다.
- PDF 처리: PyMuPDF의 빠른 텍스트 계층 분석을 먼저 하고, 텍스트에서 실적표를 찾지 못한 보고서만 pdfplumber 표 좌표 분석으로 다시 시도한다. 긴 보고서는 표지·초반부와 마지막 재무제표 페이지를 우선 확인한다.
- 표 검증: 보고서 안에서 페이지별 금액 단위가 달라도 각 페이지의 명시 단위를 우선한다. 손익계산서와 재무상태표가 나란히 있는 표는 손익계산서 열만 읽고, 동종사 비교표와 과거·현재·컨센서스가 반복되는 표는 현재 추정치를 확정할 수 없으면 제외한다.
- 원문 서버 보호: 한경 신규 PDF는 기본 1.5초 간격으로 제한한다. 네이버 원문은 최신 예상치가 없는 종목의 증권사별 최신 보고서만 소수 병렬로 조회한다. 서버가 HTTP 403 또는 429를 반환하면 해당 소스를 멈추고 성공분을 저장하며, 다음 `--incremental` 실행에서 이어받는다. 차단을 누락이나 변경 없음으로 기록하지 않는다.
- 목록 장애 복구: 목록 서버가 일시 차단되면 마지막으로 정상 저장한 `report_index.json`을 요청 기간에 맞게 다시 사용한다. 캐시 사용 여부와 원인은 `run_report.json`에 별도로 남긴다.
- 재판독 정정: 파서 개선 후 보고서를 다시 읽었을 때 과거 오검출이 확인되면 그 보고서의 이전 숫자를 자동 삭제한 뒤 새 결과로 교체한다.
- 요약문 검증: 같은 문장에 있는 기간별 매출·영업이익 쌍을 우선 사용한다. 매출이 없어도 회사 전체 기간이 직접 명시된 영업이익은 낮은 신뢰도로 보관한다. 사업부·제품·선대 기여액, 월간 실적, 상·하반기 합계는 연간 회사 실적으로 저장하지 않는다.
- 현재 컨센서스: 올해와 내년 예상만 포함하고, 끝난 분기와 내후년 이후 수치는 원자료에만 남긴다. 같은 기간의 최신 보고서보다 45일 넘게 오래된 값은 현재 컨센서스에서 제외한다. 증권사 3곳 이상일 때 중앙값에서 3배 넘게 벗어난 값은 원자료에 남기고 별도 검토 목록으로 보낸다.

## 실행

프로젝트의 Python 환경에 `requirements.txt`를 설치한 뒤 실행한다.

```powershell
python forecast_report_engine.py --start-date 2026-01-01 --end-date 2026-09-21
```

목록과 종목 수만 먼저 확인할 때:

```powershell
python forecast_report_engine.py --start-date 2026-01-01 --end-date 2026-09-21 --index-only
```

한 번 전체 수집한 뒤 새 보고서만 처리할 때:

```powershell
python forecast_report_engine.py --start-date 2026-01-01 --incremental
```

방금 정상 저장한 네이버 목록을 다시 내려받지 않고 파서만 재검증할 때:

```powershell
python forecast_report_engine.py --start-date 2026-01-01 --reuse-naver-index
```

Windows에서는 `setup_windows.bat`을 한 번 실행한 뒤 `run_forecast_engine.bat`을 실행하면 같은 증분 갱신을 수행한다.

컨센서스와 가이던스를 함께 갱신하고 검증할 때:

```powershell
python refresh_forecasts.py --reuse-naver-index
```

## 출력

기본 출력 위치는 `test_output/forecast_engine/`이며 Git에서 제외된다.

- `report_index.json`: 기간 내 정상 6자리 종목코드 보고서 목록
- `report_index.csv`: 목록을 엑셀 등에서 확인하기 위한 동일 내용
- `selected_reports.json`: 실제 PDF 분석 대상으로 선택한 보고서
- `naver_research_index.json`: 기간 내 네이버 증권 리서치 요약 목록
- `naver_research_index.csv`: 네이버 목록 확인용 표
- `naver_pdf_index.json`: 네이버 상세 페이지에서 확인한 증권사 원문 PDF 목록
- `naver_pdf_detail_audit.json`: 원문 주소와 종목코드 확인 결과
- `naver_pdf_extraction_audit.json`: 네이버 원문 PDF별 표 추출 결과
- `naver_pdf_failures.json`: 네이버 원문 주소·표 추출 실패
- `fnguide_consensus_audit.json`: 외부 집계 컨센서스 보충 결과
- `fnguide_consensus_failures.json`: 외부 집계에도 올해·내년 값이 없는 종목
- `forecast_observations.json`: 증권사·보고서·예상기간별 원자료
- `forecast_observations.csv`: 원자료 확인용 표
- `forecast_consensus.json`: 최근 90일 자료의 종목·기간별 중앙값
- `forecast_consensus.csv`: 중첩 출처 목록을 제외한 컨센서스 확인용 표
- `consensus_exclusions.json`: 오래된 상대 추정치와 다중 증권사 이상치 제외 기록
- `extraction_audit.json`: PDF별 단위와 추출 상태
- `naver_extraction_audit.json`: 리서치 요약별 추출 상태
- `extraction_failures.json`: PDF 다운로드·표 추출 실패
- `missing_tickers.json`: 원문을 판독했지만 예상 영업이익을 얻지 못한 종목
- `pending_tickers.json`: 원문 소스 차단으로 아직 판독하지 못해 다음 실행에서 재시도할 종목
- `current_consensus_gaps.json`: 과거 예상치는 있으나 현재 사용할 올해·내년 수치가 끝내 없는 종목
- `current_consensus_recovered.json`: 원문 PDF와 외부 집계 보충으로 새로 복구한 종목
- `run_report.json`: 전체 실행 결과와 커버리지

`forecast_consensus.json`은 최근 자료를 낸 증권사가 1곳이면 `개별 추정치`, 2곳이면 `참고 컨센서스`, 3곳 이상이면 `유효 컨센서스`로 표시한다. 두 증권사의 값이 3배 넘게 다르거나 흑자·적자가 엇갈리면 `불일치 검토`로 표시한다. FnGuide 보충값은 증권사 한 곳의 숫자로 세지 않고 `외부 집계 컨센서스`로 구분한다.

## 기사·다른 공식자료 보충

검증한 기사, 회사 가이던스, 다른 증권사 원문은 JSON 또는 CSV로 넣을 수 있다. 필수 필드는 다음과 같다.

```text
ticker,name,broker,report_date,period,operating_profit_krw_100m,source_url
```

선택 필드는 `sales_krw_100m`, `report_id`, `extraction_method`, `confidence`다. `period`는 `2026Q3` 또는 `2027FY` 형식을 사용한다. 출처 URL과 날짜가 없는 자료는 받지 않는다.

```powershell
python forecast_report_engine.py --incremental --supplements forecast_supplements.csv
```

보충자료도 증권사별 최신값 선택과 90일 신선도 규칙을 동일하게 적용한다.
형식은 `forecast_supplements.example.csv`에서 확인할 수 있다.
