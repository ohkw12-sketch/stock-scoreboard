# 유튜브시황2 · SaveTicker 오선 국장 주요뉴스

매일 KST 오후 3시 45분 전체 평가창 갱신에서 `https://saveticker.com/news`의 공개 뉴스 자료를 확인한다. 작성자가 `오선`인 최근 기사만 대상으로 하며 다음 두 구분만 게시한다.

- 국장 직접: 국내 상장기업, 코스피·코스닥, 한국거래소, 국내 금융·산업 정책을 직접 다룬 기사
- 국내 섹터 영향: 해외 뉴스라도 국내 반도체, 디스플레이, 정유·화학·운송, 자동차·수출, 전력기기·냉각, 2차전지, 수출·관세로 이어지는 경로가 명확한 기사

비슷한 속보는 하나로 합치며 `카더라`, 소식통 인용, 검토 단계는 미확인 보도로 표시한다. 기사 제목만으로 종목을 추천하지 않는다. 기사 실제 내용의 짧은 요약과 국내 시장에 연결되는 업종·경로를 따로 표시한다.

수집 실패 시 이미 검증해 게시한 이전 자료를 유지하고 실패 상태를 표시한다. 첫 수집부터 실패한 경우 빈 화면에 수집 실패 상태를 표시한다.

```powershell
.venv\Scripts\python.exe save_ticker_news.py
.venv\Scripts\python.exe -m unittest tests.test_save_ticker_news
node --test tests/test_saveticker_ui.cjs
```
