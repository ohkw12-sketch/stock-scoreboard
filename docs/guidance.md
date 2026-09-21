# Guidance 엔진

정식 저장소: `C:\Users\os\Documents\Codex\stock-scoreboard`

## 추가·변경 파일
- `guidance_engine.py`: 공식 공시 수집, 원문 표 파싱, 이력·최신 버전 선택, 컨센서스 변환과 비교, 독립 실행 명령.
- `guidance.js`: 가치성장 탭 안의 접이식 Guidance 표. 가이던스 우선 표시, 외부 컨센서스·기준일 병기.
- `forecast_integration.py`: 연결 연간 가이던스와 외부 전망 중 발표일이 더 늦은 수치를 성장근거에 적용. 같은 날·날짜 불명확 시 가이던스 우선.
- `refresh_forecasts.py`: 컨센서스·가이던스 순차 갱신, 검증, 게시 파일 승격.
- `preview_server.py`: Guidance 스크립트와 테스트 JSON의 미리보기 경로 추가.
- `config.example.json`: 임계값·NEW 표시 기간·컨센서스 회계기준 설정 추가.
- `tests/test_guidance.py`, `tests/test_guidance_ui.cjs`: 계산·이력·소스 오류·화면 테스트.
- `docs/guidance.md`: 사용 설명.

## 동작
공시명에서 공백을 제거한 후 `영업실적등에대한전망`을 탐지한다. OpenDART 공시검색을 90일 이하 구간으로 나누고 모든 페이지와 정정 이전 공시까지 조회한다. 공시 원본 ZIP을 내려받아 대상기간·단위·매출·영업이익 표를 파싱한다. KIND 공식 원문 파일도 보완 입력할 수 있다. KIND 전체 사이트 자동 검색은 구현 범위에 포함하지 않는다.

이력은 `(종목, 대상기간, 연결/별도, 공시번호)`로 중복 제거한다. 발표일과 공시번호 순으로 최신 값이 활성화되며 과거 원본은 이력에 남는다. 최신 표의 누락 항목을 이전 숫자로 채우지 않는다. 해석할 수 없는 정정공시는 실패 목록에 기록하고 이전 값에 확인 필요 표시를 붙인다.

금액은 원으로 정규화하고 표에서는 억원으로 표시한다. 범위는 low/high/mid로 저장한다. OPM은 매출·영업이익 범위에서 계산한 참고 비율이다. 명시적 숫자가 아닌 ‘이상’, 복잡한 문장, 모호한 표는 자동 추정하지 않는다.

괴리율은 `(가이던스 중간값 - 컨센서스) / 컨센서스`. 같은 종목·기간·회계기준·단위만 비교한다. 컨센서스 0/누락은 null이다. 음수 컨센서스는 공식 그대로 계산하되 부호 해석이 반대가 되므로 UP/RISK 자동 태그를 붙이지 않는다. 서로 다른 지표에서 UP/RISK가 동시에 나올 수 있다.

기본 +5% 초과 UP, -5% 미만 RISK. 최초 공시이고 발표 후 30일 이내면 NEW를 함께 표시한다. 정정본만 처음 수집한 경우 NEW로 표시하지 않는다. 분기·반기·연간은 서로 섞거나 임의 배분하지 않는다. 대상기간이 종료된 가이던스는 이력에만 남긴다.

컨센서스 캐시의 회계기준이 없으면 컨센서스 숫자와 날짜는 표시하되 비교를 보류한다. 공급자 기준을 확인한 경우에만 `guidance_consensus_basis`를 `consolidated` 또는 `separate`로 설정한다. 기본 null. 원본 캐시에 행별 basis가 있으면 그 값을 우선한다.

가이던스가 없다고 감점하지 않는다. 연결 연간 가이던스는 같은 기간 성장근거의 매출·영업이익을 우선하고, 나머지는 외부 컨센서스를 사용한다. 반기·분기·별도·자회사 가이던스를 연간 연결 수치로 바꾸지 않는다. 확정실적 가치 원점수에는 미래 전망을 넣지 않는다.

## 환경변수
`OPEN_DART_API_KEY`를 우선 사용한다. 없으면 기존 `DART_API_KEY` 환경변수 및 기존 Windows 사용자 환경변수 조회 함수를 재사용한다. 비밀값은 코드·결과·설명서에 넣지 않는다. 이 PC에서는 기존 키로 실제 조회가 성공했다.

## 실행 (저장소 폴더의 PowerShell)
```powershell
.venv\Scripts\python.exe guidance_engine.py --config config.example.json
.venv\Scripts\python.exe preview_server.py --port 8766
```
로컬 미리보기: http://127.0.0.1:8766/ → 성장 탭 → 공식 Guidance · 컨센서스 비교.

기본 조회는 오늘까지 400일이다. 이력은 `cache/guidance/history.json`, 보드 후보는 `test_output/guidance.json`. 기간 지정은 `--start 2026-01-01 --end 2026-09-21`. 실패 시 해당 기간을 다시 실행하면 미처리 공시를 재시도한다. 성공한 공시 원문은 캐시한다.

컨센서스 기본 입력은 `test_output/forecast_engine/forecast_consensus.json`이다. KIS CSV도 `--consensus 경로`로 계속 읽을 수 있다. 회계기준이 확인되지 않은 외부 수치는 화면에 표시하되 가이던스 괴리율은 보류한다.

KIND 보완 입력: `--kind-input kind-input.json`. JSON 배열 원소에는 `report_nm`, `rcept_no`, `rcept_dt`(YYYYMMDD), `stock_code`, `corp_name`, `source_url`(https://kind.krx.co.kr/...), `markup_path`(UTF-8 원문 파일)가 필요하다. DART 공시번호를 함께 사용하면 같은 공시 실패를 해소하고 중복 제거할 수 있다. 원문은 사용자가 확인한 공식 공시와 일치해야 한다.

검증된 `guidance.json`은 운영 정적 자산으로 배포한다. JSON 조회 실패는 가치성장 본표 렌더링에 영향을 주지 않는다. 클라우드 전체 갱신은 컨센서스·가이던스를 먼저 갱신한 뒤 보드를 계산하고, 암호화 원자료 상태에 필요한 JSON 이력을 보존한다.

## 검증
- 2026-09-21 전체 실행: 공식 가이던스 현재 84건·81종목, 수집 실패 0건.
- 외부 컨센서스 1,344개 현재 포인트 중 연간 전망 보유 417종목, 2026→2027 계산 가능 309종목.
- 전체 Python 328건, 화면 28건, 표시 계약 검사 통과.

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_guidance.py -v
.venv\Scripts\python.exe -m unittest discover -s tests -p test_kis_consensus.py
node --test tests/test_guidance_ui.cjs tests/test_ui_runtime.cjs
.venv\Scripts\python.exe board_contract.py
```

공식 참고: [OpenDART 공시정보 API](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001), [공시서류 원본](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019003), [KIND 실제 정정공시 표](https://kind.krx.co.kr/external/2025/12/22/000644/20251222000119/70957.htm).
