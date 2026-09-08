# 유튜브시황1 · 삼프로 당일 시황

매일 KST 오후 3시 45분 전체 평가창 갱신 작업에서 공식 원문을 읽고 짧게 재서술한다. 별도 오전 예약 없이 기존 유튜브 시황과 함께 갱신·배포한다.
대상은 https://apps.3protv.com/news/list/1 의 당일 뉴스3+ 뉴스레터와 주요 방송 요약이다.
기사의 정확한 게시일과 본문을 실제로 읽는다. 검색 제목만으로 작성하지 않는다.
이날 모든 방송을 확인했다고 주장하지 않는다. 출연자별 견해가 다르면 함께 표시한다.

매크로(금리·환율·정책·경기·자금흐름)와 기업(수주·실적·투자·규제) 이슈를 선별하고
각 항목의 fact / view / watch를 작성한다. view에는 기사 또는 발언자 이름을 attribution에 표시한다.
watch와 overview는 편집 해석으로 표시한다. 출처에 없는 추천·확정 계약·실적을 만들지 않는다.
숫자와 발언은 출처와 대조하고 전체 기사나 자막을 공개 파일에 복제하지 않는다.
원문별 짧은 요약만 제공하고 링크를 유지한다. 기존 sampro-market.json의 구조를 따른다.

후보는 test_output/sections/sampro-market.json에 생성한다. 같은 날짜는 확인된 신규 기사로 보완하고
기존 다른 날짜 editions는 유지한다(최근 30개 날짜). 제목·본문이 검증된 경우에만 날짜를 올린다.
당일 기사 미게시/접근 실패면 기존 본문과 기준일을 그대로 유지하고 status에 이유를 기록한다.
이전 날짜 내용을 오늘 시황으로 바꾸지 않는다. checkedAt만 실제 확인 시각으로 기록한다.

검증 및 이 파일만 반영:

```
.venv/Scripts/python.exe sampro_validate.py test_output/sections/sampro-market.json --promote
.venv/Scripts/python.exe board_contract.py
node --check sampro.js
node --test tests/test_ui_runtime.cjs
```

검증 성공 후 sampro-market.json을 전체 갱신의 검증된 결과와 함께 커밋·기존 main에 푸시한다.
사용자는 실제 스코어보드 반영·배포를 승인했다. 대상은 ohkw12-sketch/stock-scoreboard와
https://stock-scoreboard.pages.dev/ 뿐이다. 다른 평가 결과와 기존 유튜브 시황을 변경하지 않는다.
실제 공개 sampro-market.json과 확정본이 일치하는지 확인한다.
신규 내용 없이 동일하면 불필요한 배포를 생략한다.
