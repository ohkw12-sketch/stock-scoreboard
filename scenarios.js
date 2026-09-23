/* Independent scenario research UI. Source facts and model conditions are separate. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const num = (v, digits = 0) => finite(v) ? v.toLocaleString('ko-KR', {maximumFractionDigits:digits}) : '—';
  const percent = (v, digits = 1) => finite(v) ? `${v > 0 ? '+' : ''}${num(v,digits)}%` : '미확인';
  const eok = v => finite(v) ? `${num(v / 1e8, 1)}억` : '미확인';
  const mult = v => finite(v) ? `${num(v,1)}배` : '미확인';
  const color = v => finite(v) ? (v > 0 ? 'positive' : v < 0 ? 'negative' : '') : '';
  const pill = (text, kind='gray') => `<span class="pill ${esc(kind)}">${esc(text)}</span>`;
  const link = (url, title) => /^https:\/\//.test(url || '') ? `<a class="source-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(title)} ↗</a>` : `<span class="cell-note">출처 미확인</span>`;
  const empty = (title, body='') => `<div class="empty"><b>${esc(title)}</b>${esc(body)}</div>`;
  const heading = (eyebrow, title, description, extra='') => `<div class="page-head"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1><p class="description">${esc(description)}</p></div>${extra}</div>`;
  const section = (title, extra='') => `<div class="section-head"><h2>${esc(title)}</h2>${extra}</div>`;
  const stockLink = s => `<a class="stock-link" href="#stock/${esc(s.ticker)}">${esc(s.name)}<span class="stock-sub">${esc(s.sector)} · ${esc(s.ticker)}</span></a>`;
  const metric = (label, value, note='') => `<div class="card metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const coverageRow = (label, value) => `<div class="coverage-item"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
  const titles = {overview:'전체 흐름',scenarios:'세 가지 시나리오',sectors:'섹터 탐색',stocks:'종목 탐색',holdings:'보유 점검',audit:'검증과 기준',stock:'종목 상세'};
  let data;
  let routeRequest=0;
  const loadedStocks=new Set();
  const state = {scenario:'all',query:'',sector:'all',track:'all',eligible:false,page:1,sectorQuery:'',sectorPage:1};

  function candidate(s, key, watching=false) {
    const f=s.fundamentals,p=s.price,c=s.scenarios[key];
    const wait=c.waiting.length ? c.waiting.slice(0,2).join(' · ') : '다음 완료 거래일에도 조건 유지 여부 확인';
    return `<article class="candidate"><div class="candidate-head">${stockLink(s)}${pill(watching?'성장 확인 · 가격 대기':s.heatObservation?'과열 관찰':c.state,c.ready?'green':'amber')}</div>
      <p class="reason">${esc(key==='up'?(f.growthPath||f.track):f.track)}</p><div class="candidate-metrics"><span>내년 OP <b>${esc(percent(f.nextOPGrowthPct))}</b></span>${key==='up'?`<span>예상 OP 증가액 <b>${esc(eok(f.nextOPDelta))}</b></span><span>최근 분기 OP <b>${esc(percent(f.latestOPGrowthPct))}</b></span>`:''}<span>거래량 <b>${esc(mult(p.volumeRatio))}</b></span><span>20일 상대 <b class="${color(p.rs20)}">${esc(percent(p.rs20))}</b></span>${key==='range'?`<span>박스 위치 <b>${p.box?`${num(p.box.positionPct,1)}% · 하단 ${num(p.box.low)}원`:'상·하단 미확인'}</b></span>`:''}</div>
      <p class="wait">${esc(c.ready?'유지 확인':'대기')} · ${esc(wait)}</p></article>`;
  }

  function scenarioCard(key, compact=false) {
    const c=data.scenarios[key],ids=compact?c.tickers.slice(0,3):c.tickers;
    const watch=key==='up'&&compact?(c.watchTickers||[]).slice(0,2):[];
    return `<section class="card scenario-card ${key}"><div class="scenario-head"><div class="scenario-number">SCENARIO 0${['up','range','down'].indexOf(key)+1}</div><h3>${esc(c.title)}</h3><p>${esc(c.subtitle)}</p><div class="scenario-count"><strong>${num(c.shownReadyCount)}</strong><span>${esc(c.readyLabel)} / 표시 후보 ${c.tickers.length}개</span></div><p>전체 ${num(c.candidateCount)}개 연구 후보 중 ${num(c.readyCount)}개 ${esc(c.readyLabel)}${key==='up'?` · 기업 성장 우선후보 ${num((c.watchTickers||[]).length)}개`:''}</p></div>
      ${ids.map(t=>candidate(data.stocks[t],key)).join('')||empty('현재 표시할 후보가 없습니다','조건이 확인되면 이 영역에 나타납니다.')}
      ${watch.length?`<div class="watch-heading"><b>기업 성장 우선후보 미리보기</b><span>가격 상태와 무관하게 이익 증가액 순서로 선정</span></div>${watch.map(t=>candidate(data.stocks[t],key,true)).join('')}`:''}
      <div class="card-foot">${compact?`<a href="#scenarios" data-scenario="${key}">후보 ${c.tickers.length}개${key==='up'?` · 성장 우선후보 ${(c.watchTickers||[]).length}개`:''} 전체 보기 →</a>`:`<span class="muted">${esc(c.horizon)}</span>`}</div></section>`;
  }

  function growthLeaders() {
    const c=data.scenarios.up, ids=c.watchTickers||[];
    return `${section('가격 대기 · 기업 성장 우선후보', `<span class="muted">표시 ${num(ids.length)}개 · 전체 ${num(c.watchCount??ids.length)}개</span>`)}
      <div class="card growth-leaders"><p class="market-note">올해·내년 예상 영업이익과 최근 확정 분기 실적이 모두 강한 기업 중 가격 조건을 기다리는 종목입니다. 예상 영업이익 증가액, 최근 확정 증가액 순으로 살펴봅니다. 가격 조건은 순위에 반영하지 않습니다. 최대 20개, 동일 섹터 최대 3개입니다.</p>
      ${ids.length?`<div class="table-scroll"><table><thead><tr><th>기업</th><th class="num">내년 예상 OP 증가</th><th class="num">최근 분기 OP 증가</th><th class="num">최근 분기 마진</th><th>가격 상태</th></tr></thead><tbody>${ids.map((t,i)=>{const s=data.stocks[t],f=s.fundamentals,c=s.scenarios.up;return `<tr><td><span class="leader-rank">${i+1}</span>${stockLink(s)}</td><td class="num"><b>${eok(f.nextOPDelta)}</b><span class="cell-note">전년비 ${percent(f.nextOPGrowthPct)}</span></td><td class="num"><b>${eok(f.latestOPDelta)}</b><span class="cell-note">전년비 ${percent(f.latestOPGrowthPct)}</span></td><td class="num">${finite(f.latestMarginPct)?`${num(f.latestMarginPct,1)}%`:'미확인'}</td><td>${pill('가격 대기','amber')}<span class="cell-note">${esc(c.waiting.slice(0,2).join(' · ')||'다음 완료 거래일 확인')}</span></td></tr>`;}).join('')}</tbody></table></div>`:empty('현재 성장 우선후보가 없습니다','예상과 확정 실적을 함께 확인한 기업이 나올 때 표시합니다.')}</div>`;
  }

  function overview() {
    const m=data.market,c=data.coverage;
    const marketExplanation = m.disagreement?'최근 5일과 20일 방향이 다릅니다. 전환 가능성을 함께 확인하세요.':'전체 종목의 추세 분포로 읽은 보조 해석입니다. 세 시나리오는 모두 확인할 수 있습니다.';
    return heading('MARKET & EXPECTATIONS','시장 · 실적 · 시나리오','시장 방향 하나를 확정하지 않고, 기업의 이익 변화와 진입 위치를 나란히 확인합니다.',pill('독립 분석 · 비교 운영','blue'))+
      `<div class="market-layout"><section class="card market-hero"><div class="eyebrow">저장된 가격으로 읽은 시장</div><h2>${esc(m.label)}</h2><p>${esc(marketExplanation)}</p><div class="hero-bottom"><div><strong>${num(m.count)}</strong><span>추세 비교 종목</span></div><div><strong>${percent(m.return20Median)}</strong><span>20일 종목 수익률 중앙값</span></div><div><strong>${percent(m.return5Median)}</strong><span>5일 중앙값</span></div></div></section>
      <section class="card"><h3>상승 흐름이 얼마나 넓게 퍼졌나</h3>${[['20일선 위 종목',m.above20,''],['60일선 위 종목',m.above60,'green']].map(([label,value,kind])=>`<div class="breadth-row"><div class="breadth-label"><span>${label}</span><b>${num(value,1)}%</b></div><div class="bar ${kind}"><i style="width:${finite(value)?Math.max(0,Math.min(100,value)):0}%"></i></div></div>`).join('')}<p class="market-note">${esc(m.basis)}<br>${m.markets.map(x=>`${esc(x.name)} ${esc(x.label)} · ${num(x.count)}종목`).join(' / ')}</p></section></div>
      <div class="grid four section-space">${metric('가격 기준일',data.meta.priceDate,'생성 시각과 구분')}${metric('연간 OP 비교',`${num(c.annualPair)}종목`,'올해·내년 외부 전망 보유')}${metric('분기 OP 전망',`${num(c.quarterForecast)}종목`,'연간 전망과 별도 표시')}${metric('동일 증권사 전망 변경',`${num(c.matchedRevisions)}종목`,'같은 기간의 전후 추정치 비교')}</div>
      ${section('세 환경에서 각각 확인할 후보','<a href="#scenarios">전체 시나리오 →</a>')}<div class="grid three">${['up','range','down'].map(k=>scenarioCard(k,true)).join('')}</div>
      ${section('지금 함께 볼 변화','<a href="#sectors">섹터별 근거 →</a>')}<div class="grid two"><section class="card"><h3>가격과 실적의 속도를 구분합니다</h3><p class="market-note">미래 영업이익 증가율은 올해와 내년의 비교입니다. 전망 상향은 같은 증권사가 같은 연도에 제시한 수치를 바꿨을 때만 표시합니다. 연간 전망으로 3분기 서프라이즈를 추정하지 않습니다.</p></section><section class="card"><h3>정보가 없으면 ‘미확인’으로 남깁니다</h3><p class="market-note">${num(c.anyForecast)}개 종목에서 외부 전망을 연결했습니다. 전망이 없는 종목도 확정 실적의 회복 경로로 살펴봅니다. 수주 공시의 개수로 가점을 주지 않습니다.</p><a class="text-link" href="#audit">자료 범위와 미연결 항목 보기 →</a></section></div>`;
  }

  function scenarios() {
    const keys=state.scenario==='all'?['up','range','down']:[state.scenario];
    return heading('THREE SCENARIOS','하나의 정답 대신, 세 가지 준비','시장 판단과 무관하게 모든 시나리오를 볼 수 있습니다. 조건 충족은 해당 환경의 연구 조건이며 매수 확정이 아닙니다.')+
      `<div class="toolbar"><div class="segmented" aria-label="시나리오 선택">${[['all','모두 보기'],['up','상승'],['range','박스권'],['down','하락']].map(([k,t])=>`<button data-scenario="${k}" aria-pressed="${state.scenario===k}">${t}</button>`).join('')}</div></div>
      <div class="callout">진입 조건 충족 종목은 최대 5개 섹터에서 섹터당 3개를 표시합니다. 상승의 기업 성장 우선후보는 아래에서 따로 비교합니다. 화면 밖 종목과 보류 이유는 <a href="#stocks">종목 탐색</a>에서 확인할 수 있습니다.</div>
      <div class="grid ${keys.length===3?'three':''} section-space">${keys.map(k=>scenarioCard(k)).join('')}</div>
      ${keys.includes('up')?growthLeaders():''}
      ${section('판단을 바꿔야 하는 순간')}<div class="grid three"><div class="card"><h3>실적 근거가 바뀔 때</h3><p class="market-note">새 전망이 하향되거나 실제 이익이 기대를 뒷받침하지 못하면 기존 개선 가정을 재검토합니다.</p></div><div class="card"><h3>가격 조건이 무너질 때</h3><p class="market-note">지지선 종가 이탈과 돌파 실패를 구분해 확인합니다. 상승 중에도 긴 윗꼬리·종가 밀림은 진입 조건에서 제외합니다.</p></div><div class="card"><h3>확인할 자료가 없을 때</h3><p class="market-note">다음 발표 날짜와 기대치가 수집되지 않았다면 미확인으로 표시합니다. 예상 수치로 빈칸을 채우지 않습니다.</p></div></div>`;
  }

  function sectorRows() {
    const rows=data.sectors.filter(s=>!state.sectorQuery||s.name.includes(state.sectorQuery));
    const start=(state.sectorPage-1)*25,shown=rows.slice(start,start+25);
    return `<div class="table-scroll"><table><thead><tr><th>섹터</th><th>가격 흐름</th><th class="num">20일 시장 대비</th><th class="num">20일선 위</th><th class="num">전망 개선 / 비교 가능</th><th class="num">연구 조건 통과</th></tr></thead><tbody>${shown.map(s=>`<tr><td><button class="stock-link" data-sector="${esc(s.name)}">${esc(s.name)}</button><span class="stock-sub">${num(s.totalCount)}종목 · 추세 표본 ${num(s.count)}</span></td><td>${pill(s.label,s.label==='상승 우세'?'green':s.label==='하락 우세'?'red':'gray')}${s.disagreement?'<span class="cell-note">5일·20일 방향 엇갈림</span>':''}</td><td class="num ${color(s.relative20)}">${percent(s.relative20)}</td><td class="num">${num(s.above20,1)}%</td><td class="num">${num(s.improvedForecastCount)} / ${num(s.forecastCount)}</td><td class="num">${num(s.eligibleCount)}</td></tr>`).join('')}</tbody></table></div>${!shown.length?empty('검색 결과가 없습니다'):''}${pagination('sector',rows.length,state.sectorPage,25)}`;
  }

  function sectors() {
    const facts=data.sectors.filter(s=>s.industryFacts.length);
    return heading('SECTOR MAP','섹터의 가격 흐름과 이익 변화를 분리합니다','최근 20일 시장 대비 성과순입니다. 가격 흐름을 업황의 확정 판정으로 사용하지 않습니다. 섹터 이름을 누르면 해당 종목을 볼 수 있습니다.')+
      `<div class="toolbar"><label class="sr-only" for="sector-query">섹터 검색</label><input id="sector-query" placeholder="섹터 이름 검색" value="${esc(state.sectorQuery)}"></div><div class="card table-card" id="sector-results">${sectorRows()}</div>`+
      section('연결된 업황 자료')+`<div class="grid three">${facts.map(s=>`<section class="card"><h3>${esc(s.name)}</h3>${s.industryFacts.map(f=>`<div class="fact-line"><b>${esc(f.period)} · ${esc(f.kind)}</b>${finite(f.growthRate)?`전년 대비 ${percent(f.growthRate)}`:'수치 미확인'}<span class="cell-note">발표 ${esc(f.publishedAt)}</span>${link(f.url,f.source)}</div>`).join('')}<p class="market-note">산업 전체 통계입니다. 개별 회사의 매출·이익 기여율과 동일하지 않습니다.</p></section>`).join('')||empty('연결된 업황 자료 없음')}</div>`;
  }

  function filteredStocks() {
    const q=state.query.trim().toLowerCase();
    return Object.values(data.stocks).filter(s=>(!q||s.name.toLowerCase().includes(q)||s.ticker.includes(q))&&
      (state.sector==='all'||s.sector===state.sector)&&(state.track==='all'||s.fundamentals.track===state.track)&&(!state.eligible||s.eligible||s.heatObservation))
      .sort((a,b)=>Number(b.eligible)-Number(a.eligible)||a.name.localeCompare(b.name,'ko'));
  }

  function pagination(kind,total,page,size) {
    return `<div class="pagination"><span>${num(total)}개 중 ${total?num((page-1)*size+1):0}–${num(Math.min(page*size,total))}</span><div><button class="button" data-page="${page-1}" data-kind="${kind}" ${page<=1?'disabled':''}>이전</button><button class="button" data-page="${page+1}" data-kind="${kind}" ${page*size>=total?'disabled':''}>다음</button></div></div>`;
  }

  function stockRows() {
    const rows=filteredStocks(),start=(state.page-1)*40,shown=rows.slice(start,start+40);
    return `<div class="table-scroll"><table><thead><tr><th>종목</th><th>개선 경로</th><th class="num">올해 OP → 내년 OP</th><th class="num">내년 OP 증가율</th><th class="num">올해 예상 마진</th><th>판단 상태</th></tr></thead><tbody>${shown.map(s=>{const f=s.fundamentals;return `<tr><td>${stockLink(s)}</td><td>${esc(f.track)}<span class="cell-note">${f.forecastDates.length?`전망 발표 ${esc(f.forecastDates.at(-1))}`:'외부 전망 미확인'}</span></td><td class="num">${eok(f.annualOP)} → ${eok(f.nextOP)}</td><td class="num ${color(f.nextOPGrowthPct)}">${percent(f.nextOPGrowthPct)}<span class="cell-note">증가액 ${eok(f.nextOPDelta)}</span></td><td class="num">${finite(f.annualMarginPct)?num(f.annualMarginPct,1)+'%':'미확인'}</td><td>${pill(s.eligible?'연구 조건 통과':s.heatObservation?'과열 관찰':'보류',s.eligible?'blue':'amber')}<span class="cell-note">${esc(s.blockers.slice(0,2).join(' · ')||'시나리오별 가격 조건 확인')}</span></td></tr>`;}).join('')}</tbody></table></div>${!shown.length?empty('검색 결과가 없습니다','검색어 또는 필터를 변경해주세요.'):''}${pagination('stock',rows.length,state.page,40)}`;
  }

  function stocks() {
    return heading('COMPANY EXPLORER','숫자 뒤의 근거를 찾습니다','표시 후보 외의 전 종목도 검색할 수 있습니다. ‘연구 조건 통과’와 각 시나리오의 가격 조건 충족은 별개입니다.')+
      `<div class="toolbar"><label class="sr-only" for="stock-query">종목 검색</label><input id="stock-query" value="${esc(state.query)}" placeholder="종목명 또는 코드">
      <label class="sr-only" for="stock-sector">섹터</label><select id="stock-sector"><option value="all">모든 섹터</option>${data.sectors.map(s=>`<option value="${esc(s.name)}" ${s.name===state.sector?'selected':''}>${esc(s.name)}</option>`).join('')}</select>
      <label class="sr-only" for="stock-track">개선 경로</label><select id="stock-track">${['all','실적·전망 동반 개선','전망 개선 확인','실적 회복 확인','개선 근거 대기'].map(t=>`<option value="${t}" ${state.track===t?'selected':''}>${t==='all'?'모든 개선 경로':t}</option>`).join('')}</select>
      <label><input id="eligible-only" type="checkbox" ${state.eligible?'checked':''}>연구 후보만</label><button class="button" data-reset="stocks">필터 초기화</button></div>
      <div class="card table-card" id="stock-results">${stockRows()}</div>`;
  }

  function sparkline(s) {
    const pts=s.price.chart;
    if(pts.length<2)return empty('가격 이력 부족');
    const w=650,h=210,pad=25,values=pts.map(p=>p.close),box=s.price.box;
    const lo=Math.min(...values,...(box?[box.low]:[])),hi=Math.max(...values,...(box?[box.high]:[])),range=hi-lo||1;
    const y=v=>h-pad-(v-lo)/range*(h-pad*2),x=i=>pad+i/(pts.length-1)*(w-pad*2);
    const path=pts.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(p.close).toFixed(1)}`).join(' ');
    const support=box?.low??s.price.support;
    return `<svg class="chart" viewBox="0 0 ${w} ${h+25}" role="img" aria-label="${esc(s.name)} 최근 ${pts.length}거래일 가격 흐름. ${esc(pts[0].date)}부터 ${esc(pts.at(-1).date)}까지"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#dce6fa" stop-opacity=".65"/><stop offset="100%" stop-color="#fff" stop-opacity="0"/></linearGradient></defs>${[lo,(hi+lo)/2,hi].map(v=>`<line class="grid-line" x1="${pad}" x2="${w-pad}" y1="${y(v)}" y2="${y(v)}"/><text x="${pad}" y="${y(v)-6}">${num(v)}원</text>`).join('')}<path fill="url(#chart-fill)" d="${path} L${w-pad},${h-pad} L${pad},${h-pad} Z"/><path class="price-line" d="${path}"/>${finite(support)&&support>=lo&&support<=hi?`<line class="support-line" x1="${pad}" x2="${w-pad}" y1="${y(support)}" y2="${y(support)}"/>`:''}${box?`<line class="box-ceiling" x1="${pad}" x2="${w-pad}" y1="${y(box.high)}" y2="${y(box.high)}"/>`:''}<circle cx="${w-pad}" cy="${y(pts.at(-1).close)}" r="4" fill="#567cc5"/><text x="${pad}" y="${h+13}">${esc(pts[0].date)}</text><text x="${w-pad}" y="${h+13}" text-anchor="end">${esc(pts.at(-1).date)}</text></svg>`;
  }

  function forecastCell(point) {
    if(!point)return '<span class="muted">미확인</span>';
    return `<b>${eok(point.value)}</b><span class="cell-note">${esc(point.date)} · ${esc(point.source)}</span>${link(point.url,'선택값 원문')}<details class="small"><summary>참여 자료 ${point.sources.length}건</summary>${point.sources.map(x=>`<div class="cell-note">${eok(x.value)} · ${esc(x.date)} ${link(x.url,x.name)}</div>`).join('')}</details>`;
  }

  function stockDetail(ticker) {
    const s=data.stocks[ticker];
    if(!s)return heading('COMPANY','종목을 찾을 수 없습니다','종목 탐색에서 이름이나 코드를 확인해주세요.','<a class="button" href="#stocks">종목 탐색</a>');
    const f=s.fundamentals,p=s.price,year=Number(data.meta.priceDate.slice(0,4));
    const q3=f.forecasts.find(x=>x.period===`${year}Q3`),q4=f.forecasts.find(x=>x.period===`${year}Q4`);
    const relevantNotes=data.holdings.find(x=>x.ticker===ticker)?.assessment;
    return `<div class="page-head detail-head"><div><div class="eyebrow">COMPANY RESEARCH · ${esc(s.sector)}</div><h1>${esc(s.name)}<span class="ticker">${esc(s.ticker)}</span></h1><p class="description">${esc(f.track)} · ${esc(f.durability)}</p></div><a class="button" href="#stocks">← 종목 탐색</a></div>
      ${s.blockers.length?`<div class="callout amber"><b>현재 대기 이유</b> · ${esc(s.blockers.join(' / '))}</div>`:`<div class="callout">기업 개선·규모·유동성 연구 조건을 통과했습니다. 아래의 각 시나리오 가격 조건을 따로 확인하세요.</div>`}
      ${relevantNotes?`<div class="callout amber section-space"><b>숫자와 함께 봐야 할 확인 근거 · ${esc(relevantNotes.label)}</b><p>${esc(relevantNotes.fact)}</p><p>확인 ${esc(relevantNotes.checkedAt)} · ${relevantNotes.sources.map(x=>link(x.url,x.title)).join(' ')}</p></div>`:''}
      <div class="grid four section-space">${metric(`${year}년 예상 영업이익`,eok(f.annualOP),'최신 보유 전망')}${metric(`${year+1}년 예상 영업이익`,eok(f.nextOP),`올해 대비 ${percent(f.nextOPGrowthPct)}`)}${metric('이익 증가액',eok(f.nextOPDelta),'성장률의 낮은 기저를 함께 확인')}${metric('올해 예상 영업이익률',finite(f.annualMarginPct)?num(f.annualMarginPct,1)+'%':'미확인','매출·OP 항목별 선택값으로 계산')}</div>
      <div class="detail-top"><section class="card"><div class="chart-title"><div><strong>${num(p.close)}원</strong> <span class="${color(p.change1)}">${percent(p.change1)}</span><small class="cell-note">종가 ${esc(p.date)} · ${esc(p.source)}</small></div>${pill('60거래일 가격','blue')}</div>${sparkline(s)}<p class="small muted">수정주가를 현재 종가 기준으로 환산한 흐름 · 점선은 지지 추정선과 확인된 박스 상단 (차트 범위 안일 때)</p></section>
      <section class="card"><h3>이익 전망과 현재 가격의 관계</h3><div class="fact-line"><b>연간 이익 대비 시가총액</b>올해 P/OP ${mult(p.popCurrent)} → 내년 P/OP ${mult(p.popNext)}<span class="cell-note">PER와 다른 지표입니다. 낮은 배수만으로 적정가·저평가를 확정하지 않습니다.</span></div><div class="fact-line"><b>상반기 이후 남은 이익 부담</b>하반기 필요 OP ${eok(f.h2ImpliedOP)} · 상반기 대비 ${percent(f.h2RequiredVsH1Pct)}<span class="cell-note">연간 예상 OP − 확정 1·2분기 OP. 증권사의 별도 하반기 전망값은 아닙니다.</span></div><div class="fact-line"><b>직접 수집한 분기 전망</b>3분기 OP ${eok(q3?.op?.value)} / 4분기 OP ${eok(q4?.op?.value)}<span class="cell-note">분기와 연간값의 발표일이 달라 합계가 일치하지 않을 수 있습니다. 아래 원문 날짜를 확인하세요.</span></div><div class="fact-line"><b>가격 반응</b>20일 수익률 ${percent(p.r20)} · 시장 대비 ${percent(p.rs20)}<span class="cell-note">이익 전망이 주가에 얼마나 반영됐는지는 이 수치만으로 확정할 수 없습니다.</span></div></section></div>
      ${section('확정 실적과 외부 전망')}<div class="card table-card"><div class="table-scroll"><table><thead><tr><th>구분 / 기간</th><th>매출</th><th>영업이익</th><th class="num">영업이익률</th><th>자료 구분</th></tr></thead><tbody>${f.actuals.map(a=>`<tr><td><b>${esc(a.period)}</b></td><td>${eok(a.sales)}</td><td>${eok(a.op)}</td><td class="num">${num(a.marginPct,1)}%</td><td>${pill('확정','green')}<span class="cell-note">공시 ${esc(a.date)}</span>${link(a.url,'실적 공시')}${a.sources?.length>1?`<span class="cell-note">단독 분기 산출에 사용한 공시 ${a.sources.map((u,i)=>link(u,String(i+1))).join(' ')}</span>`:''}</td></tr>`).join('')}${f.forecasts.map(v=>`<tr><td><b>${esc(v.period)}</b></td><td>${forecastCell(v.sales)}</td><td>${forecastCell(v.op)}</td><td class="num">${v.sales?.value>0&&finite(v.op?.value)?num(v.op.value/v.sales.value*100,1)+'%':'미확인'}</td><td>${pill('예상','blue')}<span class="cell-note">항목별 발표일·출처 유지</span></td></tr>`).join('')}</tbody></table></div>${!f.actuals.length&&!f.forecasts.length?empty('연결된 실적·전망 자료가 없습니다','부정적 펀더멘털이라는 뜻은 아닙니다. 원자료 보완 대상입니다.'):''}</div>
      ${section('전망이 실제로 바뀌었는가',pill('동일 증권사 · 동일 연도','blue'))}<div class="card table-card">${f.revisions.length?`<div class="table-scroll"><table><thead><tr><th>증권사 / 대상기간</th><th>이전 OP</th><th>최신 OP</th><th class="num">변경률</th><th>원문</th></tr></thead><tbody>${f.revisions.map(r=>`<tr><td>${esc(r.broker)}<span class="stock-sub">${esc(r.period)}</span></td><td>${eok(r.before)}<span class="cell-note">${esc(r.beforeDate)}</span></td><td>${eok(r.after)}<span class="cell-note">${esc(r.date)}</span></td><td class="num ${color(r.changePct)}">${percent(r.changePct)}<span class="cell-note">증가액 ${eok(r.delta)}</span></td><td>${link(r.beforeUrl,'이전')} ${link(r.url,'최신')}</td></tr>`).join('')}</tbody></table></div>`:empty('비교 가능한 전후 추정치가 부족합니다','올해와 내년의 차이를 ‘전망 상향’으로 대신 표시하지 않습니다.')}</div>
      ${section('박스권의 실제 가격 위치')}<div class="card">${p.box?`<b>박스 하단 ${num(p.box.low)}원 · 상단 ${num(p.box.high)}원</b><p>현재 종가는 박스 폭의 하단에서 ${num(p.box.positionPct,1)}% 위치입니다. 하단 재확인 ${num(p.box.lowerTests)}회, 상단 반락 ${num(p.box.upperTests)}회 · 최근 하단 확인 ${esc(p.box.lastLowerTestDay)}.</p><p class="small muted">하단은 과거 거래 집중대에서 지지로 바뀐 추정 가격, 상단은 이후 반복된 가격 반락으로 추정했습니다.</p>`:empty('반복 확인된 박스 상·하단이 없습니다','매물대 위에 있다는 이유만으로 박스권 하단 후보로 보지 않습니다.')}</div>
      ${section('세 환경에서의 현재 조건')}<div class="grid three">${['up','range','down'].map(k=>{const c=s.scenarios[k];return `<section class="card"><h3>${esc(data.scenarios[k].title)}</h3><div class="section-space">${pill(c.state,c.ready?'green':'amber')}</div><ul class="criteria">${c.checks.map(x=>`<li class="${x.met?'met':'miss'}"><span aria-hidden="true">${x.met?'✓':'○'}</span><span>${esc(x.name)}<span class="cell-note">${esc(x.met?'확인됨':x.waiting)}</span></span></li>`).join('')}</ul><p class="small muted">${esc(c.waiting.length?c.waiting.join(' · '):'다음 완료 거래일에도 유지 여부 확인')}</p></section>`;}).join('')}</div>
      ${section('개선 이유를 확인할 원자료')}<div class="grid two">${s.evidence.map(e=>`<article class="card event-card">${pill(e.kind,'gray')}<h3>${esc(e.title)}</h3><p>${esc(e.fact||'원문 내용 확인 필요')}</p>${e.amount?`<p>계약 규모 ${eok(e.amount)} · 매출 대비 ${num(e.salesRatioPct,1)}%</p>`:''}<p class="market-note">${esc(e.interpretation)}</p>${link(e.url,`${e.source} · ${e.date}`)}</article>`).join('')||`<div class="card span-two">${empty('실적 개선 원인의 추가 원문이 필요합니다','이익 증가만으로 가동률·제품 믹스·신제품 효과를 단정하지 않습니다.')}</div>`}</div>
      ${relevantNotes?section('보유 종목에 대해 확인했던 근거')+`<article class="card event-card"><h3>${esc(relevantNotes.title)}</h3><p>${esc(relevantNotes.fact)}</p><p class="market-note">해석 · ${esc(relevantNotes.interpretation)}</p><p class="market-note">다음 확인 · ${esc(relevantNotes.watch)}</p><div class="source-list">${relevantNotes.sources.map(x=>link(x.url,x.title)).join('')}</div><span class="cell-note">평가 확인일 ${esc(relevantNotes.checkedAt)} · 현재 시점의 새 조사로 갱신된 것은 아닙니다.</span></article>`:''}
      ${section('다음 확인과 판단 변경 조건')}<div class="grid two"><section class="card"><h3>다음 확인</h3><ul class="audit-list">${s.nextChecks.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></section><section class="card"><h3>판단을 다시 검토할 조건</h3><ul class="audit-list">${s.invalidation.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></section></div>
      <details class="card section-space"><summary>자료 한계와 가격 계산 근거</summary><ul class="audit-list">${f.warnings.map(x=>`<li>${esc(x)}</li>`).join('')}<li>과거 거래 집중 추정대 ${p.resistanceZone?`${num(p.resistanceZone.low)}~${num(p.resistanceZone.high)}원 · ${esc(p.resistanceZone.formedDate)} 형성 · ${esc(p.resistanceZone.lastDate)}까지 거래 확인 ${num(p.resistanceZone.days)}일 · 과거 거래대금 비중 ${percent(p.resistanceZone.sharePct)}`:'미확인'}</li><li>저항 돌파 후 지지 전환 추정대 ${finite(p.support)?`${num(p.support)}원`:'미확인'} (${esc(p.supportBasis)}) · 확인된 돌파일 ${esc(p.supportFlipDay||'미확인')}</li><li>최근 큰 매물대 돌파일 ${esc(p.breakoutDay||'미확인')} · 해당일 거래량 ${mult(p.breakoutDayVolumeRatio)} · 단기 20일 스윙고점은 별도 참고 ${num(p.swingResistance)}원</li><li>최근 3일 거래량 ${num(p.volume3)} / 그 직전 14일 ${num(p.volumePrior14)} = ${mult(p.volumeRatio)}</li><li>보유한 과거 일봉 전체의 거래대금을 가격 구간에 나눠 추정했습니다. 실제 가격별 체결량이나 매물 소화 완료를 확인한 값은 아닙니다.</li><li>20일 평균 거래대금 ${eok(p.turnover20)} · ${esc(p.turnoverBasis)}</li><li>가격 수집 ${esc(p.fetchedAt)} · 거래일 ${esc(p.date)}</li></ul></details>`;
  }

  function holdings() {
    const items=[...data.holdings].sort((a,b)=>b.risks.length-a.risks.length);
    const riskCount=items.filter(h=>h.risks.length).length;
    return heading('PORTFOLIO REVIEW','보유 이유가 지금도 유효한가','수량과 평단은 입력값 그대로 유지합니다. 가격 약세와 기업 이익의 훼손을 구분해 점검하며, 부족한 근거를 임의로 채우지 않습니다.')+
      `<div class="grid four">${metric('보유 종목',`${items.length}개`,'승인된 보유 입력 유지')}${metric('우선 점검',`${riskCount}개`,'가격·실적·자료 부족 포함')}${metric('자료 가격 기준일',data.meta.priceDate,'보유 근거의 확인일과 별도')}${metric('평가금액 합계',eok(items.reduce((sum,h)=>sum+(h.marketValue||0),0)),'가격 확인 가능한 보유분')}</div>
      ${section('보유 종목별 점검')}<div class="grid two">${items.map(h=>{const s=data.stocks[h.ticker],n=h.assessment;return `<article class="card holding-card"><div class="holding-top">${s?stockLink(s):`<h3>${esc(h.name)}</h3>`}${pill(h.state,h.risks.length?'amber':'green')}</div><div class="holding-numbers"><div><small>수량 / 평단</small><strong>${num(h.qty)}주 / ${num(h.avg)}원</strong></div><div><small>평단 대비</small><strong class="${color(h.returnPct)}">${percent(h.returnPct)}</strong></div><div><small>보유 내 비중</small><strong>${num(h.weightPct,1)}%</strong></div></div><div class="risk-list">${h.risks.map(x=>pill(x,'amber')).join('')||pill('자동 점검 조건의 경고 없음','green')}</div><div class="review"><p><b>현재 자료에서 확인한 상태</b><br>${s?`${esc(s.fundamentals.track)} · 내년 OP ${eok(s.fundamentals.nextOP)} · 60일 고점 대비 ${percent(s.price.drawdown60)}`:'종목 원자료 보완 필요'}</p>${n?`<p><b>확인된 근거 · ${esc(n.checkedAt)}</b><br>${esc(n.fact)}</p><p><b>해석</b><br>${esc(n.interpretation)}</p><p><b>다음 확인</b><br>${esc(n.watch)}</p><div class="source-list">${n.sources.map(x=>link(x.url,x.title)).join('')}</div>`:'<p>별도로 검증된 설명 자료가 없습니다. 가격이 약하다는 이유만으로 실적 악화를 단정하지 않습니다.</p>'}</div></article>`;}).join('')}</div>`;
  }

  function audit() {
    const c=data.coverage,cmp=data.comparison,dates=data.meta.forecastPublicationDateRange;
    return heading('AUDIT & METHODOLOGY','무엇으로 판단했고, 무엇이 비어 있나','선정 차이와 투자 성과를 구분합니다. 실제로 보유한 자료 범위와 현재 판단 기준을 공개합니다.',pill(data.meta.version,'blue'))+
      `<div class="callout ${cmp.sameSnapshot?'':'amber'}"><b>${cmp.sameSnapshot?'동일 가격·재무 스냅샷 비교':'기존 화면과 기준 자료가 달라 증감 비교를 보류합니다'}</b><br>${esc(cmp.performanceStatus)}<br>${esc(cmp.pointInTime)}</div>
      <div class="grid two section-space"><section class="card"><h3>전 종목 자료 범위</h3>${[['요청 종목',c.requested],['가격 이력 보유',c.priceRows],['기준일 가격 확보',c.currentPrice],['확정 4개 분기 실적',c.actualFourQuarters],['외부 전망 연결',c.anyForecast],['올해·내년 OP 모두 확인',c.annualPair],['분기 OP 전망',c.quarterForecast],['같은 증권사 전후 추정치',c.matchedRevisions],['기업·규모·유동성 조건 통과',c.eligible]].map(([label,value])=>coverageRow(label,`${num(value)}종목`)).join('')}</section>
      <section class="card"><h3>어디에서 후보가 줄었나</h3><p class="market-note">종목 하나가 여러 조건에 걸릴 수 있어 아래 숫자는 합산하지 않습니다.</p>${Object.entries(c.blockerCounts).sort((a,b)=>b[1]-a[1]).map(([label,value])=>coverageRow(label,`${num(value)}종목`)).join('')}</section></div>
      ${section('사례별 누락 여부와 이유')}<div class="card table-card"><div class="table-scroll"><table><thead><tr><th>종목</th><th>기존 가치성장 노출</th><th>새 시나리오 노출</th><th>새 엔진의 현재 대기 이유</th></tr></thead><tbody>${cmp.cases.map(x=>{const s=data.stocks[x.ticker];return `<tr><td>${s?stockLink(s):esc(x.ticker)}</td><td>${x.inOld?'포함':'미포함'}</td><td>${x.inNew?'포함':'미포함'}</td><td>${esc(x.blockers.join(' · ')||(x.inNew?'시나리오별 상세 조건 참조':'연구 조건 통과 · 분산·표시 순서로 미노출'))}</td></tr>`;}).join('')}</tbody></table></div></div>
      <div class="grid two section-space"><section class="card"><h3>기존 가치성장과의 차이</h3>${coverageRow('기존 노출',`${cmp.oldCount}종목`)}${coverageRow('새 시나리오 중복 제거',`${cmp.newShownCount}종목`)}${coverageRow('공통 종목',`${cmp.overlap.length}종목`)}${cmp.sameSnapshot?coverageRow('새로 노출 / 기존만 노출',`${cmp.added.length} / ${cmp.removed.length}종목`):''}<p class="market-note">비교 대상은 기존 가치성장 표와 새 시나리오 표시 후보입니다. 관찰 후보까지 포함하므로 매수 성과의 우열을 뜻하지 않습니다.</p></section><section class="card"><h3>섹터 쏠림</h3><div class="table-scroll"><table><thead><tr><th>새 시나리오 섹터</th><th class="num">새 노출</th><th class="num">기존 노출</th></tr></thead><tbody>${Object.entries(cmp.newSectorCounts).sort((a,b)=>b[1]-a[1]).map(([name,count])=>`<tr><td>${esc(name)}</td><td class="num">${count}</td><td class="num">${cmp.oldSectorCounts[name]||0}</td></tr>`).join('')}</tbody></table></div><p class="market-note">각 시나리오는 최대 5개 섹터 × 3개 종목. 세 시나리오의 합집합은 이를 넘을 수 있습니다.</p></section></div>
      ${section('이번 분석에 적용한 원칙')}<div class="grid two"><section class="card"><h3>실적·정보</h3><ul class="audit-list"><li>${esc(data.meta.sourcePolicy)}</li><li>연간·분기 전망을 분리하고 항목마다 출처와 발표일을 유지합니다. 직접적인 분기 전망이 없으면 미확인으로 표시합니다.</li><li>실적·전망 동반 개선, 전망 개선, 확정 실적 회복 경로를 구분합니다. 모든 업종에 15% 마진을 일괄 적용하지 않습니다. 기존 보드의 기준은 유지됩니다.</li><li>수주·뉴스 건수, 기존 가치·성장 점수는 새 선정 순서에 사용하지 않습니다. 같은 사건은 한 근거로 묶습니다.</li><li>내년 이익 증가율과 같은 증권사의 전망 상향은 다른 지표입니다. 낮은 기저의 성장률로 순위를 높이지 않습니다.</li></ul></section>
      <section class="card"><h3>가격·선정 순서</h3><ul class="audit-list"><li>연간 매출 2,000억원, 20일 평균 거래대금 10억원, 60거래일 이력과 개선 근거를 확인합니다. 건설·바이오·제약 제외를 유지합니다.</li><li>반도체를 포함한 모든 연구 후보는 기업 실적 6~12개월 관점입니다. 단타 매수 신호를 만들지 않습니다.</li><li>보유한 과거 일봉 전체에서 거래대금이 뚜렷하게 몰린 가격대를 찾습니다. 20일 스윙고점과 이동평균선은 별도 참고값이며 매물대 기준이 아닙니다.</li><li>상승 조건은 그 가격대를 최근 3일 중 하루에 거래량 1.5배 이상, 고가권 종가로 넘었는지 확인합니다. 박스권은 돌파 후 지지로 바뀐 매물대를 하단으로 잡고 상단·하단 반응이 각각 두 차례 이상 반복됐는지 확인합니다. 현재 종가가 하단 위 3% 이내이면서 박스 폭의 하위 25%여야 합니다.</li><li>최근 3일 거래량과 그 직전 14일은 겹치지 않게 비교합니다. 1.5배 확인, 2배 강신호입니다. 일봉으로 나눈 거래대금은 실제 가격별 체결량이나 매물 소화 완료 판정이 아닙니다.</li><li>조건 충족 → 비과열 → 실적·전망 동반 개선 → 시나리오 조건 충족 수 → 마진 개선폭 → 시장 상대강도 순으로 정렬합니다. 합산 점수는 없습니다.</li><li>상승: 종가 > 20일선 > 60일선, 시장 대비 강세, 큰 매물대 종가 돌파, 거래량. 박스권: 반복 확인된 박스 하단 접근·하락 진정·고가권 종가. 하락: 상대 방어·60일선 유지·고점 대비 낙폭 15% 이내·이익 훼손 점검.</li><li>급등 후 종가 밀림은 제외. 과열 예외도 실적·전망 동반 개선, 거래량 2배, 확인된 돌파, 일중 상위 20% 마감이 함께 확인된 관찰 후보에만 적용합니다.</li></ul></section></div>
      ${section('미연결 자료와 날짜')}<section class="card"><ul class="audit-list">${c.limitations.map(x=>`<li>${esc(x)}</li>`).join('')}<li>전망 발표일 범위: ${esc(dates[0]||'미확인')} ~ ${esc(dates.at(-1)||'미확인')}. 생성 시각 ${esc(data.meta.generatedAt)}는 자료 발표일이 아닙니다.</li><li>기준일 가격 누락: ${c.missingStocks.map(t=>esc(data.stocks[t]?.name||t)).join(', ')||'없음'}.</li><li>관찰 이력: ${esc(data.meta.historyStatus)}. 앞으로 저장된 날짜별 자료로 검증합니다.</li></ul><details class="section-space"><summary>입력 스냅샷 식별자</summary><p class="small muted" style="overflow-wrap:anywhere">${esc(data.meta.snapshotId)}</p></details></section>`;
  }

  function renderView(view,ticker) {
    return ({overview,scenarios,sectors,stocks,holdings,audit}[view]||(()=>stockDetail(ticker)))();
  }

  async function route(focus=false) {
    if(!data)return;
    const request=++routeRequest;
    let [view,ticker]=(location.hash.slice(1)||'overview').split('/');
    if(!titles[view])view='overview';
    if(view==='stock'&&data.stocks[ticker]&&data.meta.detailBase&&!loadedStocks.has(ticker)){
      document.getElementById('content').innerHTML='<div class="loading"><span class="loading-dot"></span>종목의 실적·전망 원자료를 불러오고 있습니다.</div>';
      try{
        const r=await fetch(`${data.meta.detailBase}/${ticker.slice(0,2)}.json`,{cache:'force-cache'});
        if(!r.ok)throw new Error('상세 자료 없음');
        const detail=await r.json();
        if(detail.snapshotId!==data.meta.snapshotId||detail.engineHash!==data.meta.engineHash||!detail.stocks?.[ticker])throw new Error('상세 자료 기준 불일치');
        if(request!==routeRequest)return;
        for(const [t,s] of Object.entries(detail.stocks)){data.stocks[t]=s;loadedStocks.add(t);}
      }catch{
        if(request!==routeRequest)return;
        document.getElementById('content').innerHTML=empty('종목 상세 자료를 불러오지 못했습니다','기준 자료가 바뀌었거나 연결이 끊겼을 수 있습니다.')+'<button class="button" data-retry>자료 다시 불러오기</button>';
        return;
      }
    }
    document.getElementById('content').innerHTML=renderView(view,ticker);
    document.getElementById('view-name').textContent=titles[view];
    document.title=`${view==='stock'?(data.stocks[ticker]?.name||'종목 상세'):titles[view]} · 시나리오 리서치`;
    document.querySelectorAll('[data-view]').forEach(a=>{if(a.dataset.view===(view==='stock'?'stocks':view))a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
    if(focus){document.getElementById('content').focus({preventScroll:true});window.scrollTo({top:0,behavior:'instant'});}
  }

  async function load() {
    try {
      const response=await fetch('scenario-board.json',{cache:'no-store'});
      if(!response.ok)throw new Error(`HTTP ${response.status}`);
      const next=await response.json();
      if(!next.meta||!next.stocks||!['up','range','down'].every(k=>next.scenarios?.[k]))throw new Error('자료 구조 불일치');
      data=next;
      loadedStocks.clear();
      document.getElementById('version').textContent=data.meta.version;
      document.getElementById('data-strip').innerHTML=`<strong>가격 ${esc(data.meta.priceDate)}</strong><span class="separator">·</span>${esc(data.meta.dataMode)}<span class="separator">·</span>생성 ${esc(data.meta.generatedAt.slice(0,16).replace('T',' '))} KST<span class="separator">·</span>발표일은 종목별 원문에 표시`;
      route();
      try {const r=await fetch('scenario-refresh-status.json',{cache:'no-store'});if(r.ok){const status=await r.json();if(status.status==='실패·이전유지')document.getElementById('data-strip').insertAdjacentHTML('beforeend',' <strong> · 최근 계산 실패, 이전 자료 유지</strong>');}}catch{}
    } catch(error) {
      document.getElementById('data-strip').textContent='분석 자료 불러오기 실패 · 기존 평가창은 별도로 이용할 수 있습니다.';
      document.getElementById('content').innerHTML=empty('분석 자료를 불러오지 못했습니다','자료 생성 여부와 연결 상태를 확인한 후 다시 시도해주세요.')+'<p style="text-align:center"><button class="button primary" data-retry>다시 불러오기</button> <a class="button" href="index.html">기존 평가창</a></p>';
    }
  }

  if(typeof module!=='undefined'&&module.exports)module.exports={esc,num,percent,eok,link,forecastCell,setData:d=>{data=d;},renderView,filteredStocks,state};
  if(typeof document==='undefined')return;
  document.addEventListener('click',event=>{
    const target=event.target.closest('button,a');if(!target)return;
    if(target.hasAttribute('data-retry')){load();return;}
    const href=target.getAttribute('href');
    if(href?.startsWith('#')&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey){
      event.preventDefault();
      if(href==='#content'){document.getElementById('content').focus();return;}
      if(target.dataset.scenario)state.scenario=target.dataset.scenario;
      if(location.hash===href)route(true);else location.hash=href;
      return;
    }
    if(target.dataset.scenario){state.scenario=target.dataset.scenario;if(location.hash==='#scenarios'){event.preventDefault();route();}}
    if(target.dataset.sector){state.sector=target.dataset.sector;state.query='';state.track='all';state.eligible=false;state.page=1;location.hash='stocks';}
    if(target.dataset.page){if(target.dataset.kind==='stock'){state.page=Number(target.dataset.page);document.getElementById('stock-results').innerHTML=stockRows();}else{state.sectorPage=Number(target.dataset.page);document.getElementById('sector-results').innerHTML=sectorRows();}}
    if(target.dataset.reset==='stocks'){Object.assign(state,{query:'',sector:'all',track:'all',eligible:false,page:1});route();}
  });
  document.addEventListener('input',event=>{
    if(event.target.id==='stock-query'){state.query=event.target.value;state.page=1;document.getElementById('stock-results').innerHTML=stockRows();}
    if(event.target.id==='sector-query'){state.sectorQuery=event.target.value;state.sectorPage=1;document.getElementById('sector-results').innerHTML=sectorRows();}
  });
  document.addEventListener('change',event=>{
    if(event.target.id==='stock-sector')state.sector=event.target.value;
    else if(event.target.id==='stock-track')state.track=event.target.value;
    else if(event.target.id==='eligible-only')state.eligible=event.target.checked;
    else return;
    state.page=1;document.getElementById('stock-results').innerHTML=stockRows();
  });
  document.getElementById('global-search').addEventListener('submit',event=>{event.preventDefault();state.query=document.getElementById('global-query').value;state.sector='all';state.track='all';state.eligible=false;state.page=1;if(location.hash==='#stocks')route(true);else location.hash='stocks';});
  window.addEventListener('hashchange',()=>route(true));
  load();
})();
