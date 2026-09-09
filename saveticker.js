(()=>{
  const status=document.getElementById('savetickerstatus');
  const body=document.getElementById('savetickerbody');
  if(!status||!body)return;
  const e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const tone=v=>v==='긍정'?'positive':v==='부정'?'negative':v==='혼합'?'mixed':'watch';
  const fmt=v=>Number(v||0).toLocaleString('ko-KR');
  const safeUrl=v=>/^https:\/\/saveticker\.com\/news\/\d+$/.test(String(v||''))?v:'#';
  const card=x=>`<article class="st-card ${tone(x.tone)}"><div class="st-card-head"><span class="st-scope">${e(x.scope)}</span><span class="yt-count">${e(x.publishedKST?.slice(5))}</span></div><h4><a href="${e(safeUrl(x.url))}" target="_blank" rel="noopener noreferrer">${e(x.title)} ↗</a></h4><p class="st-actual"><strong>기사 실제 내용</strong> · ${e(x.summary)}</p><p class="st-impact"><strong>국장 연결</strong> · ${e(x.marketImpact)}</p><div class="st-tags">${(x.sectors||[]).map(v=>`<span>${e(v)}</span>`).join('')}<span>${e(x.tone)}</span>${x.isRumor?'<span>미확인 보도</span>':''}</div><div class="st-meta">${e(x.source)} · 작성자 ${e(x.author)} · 조회 ${fmt(x.viewCount)}</div></article>`;
  const section=(title,note,items)=>`<section class="st-section"><h3>${title}</h3><p class="st-section-note">${note}</p>${items.length?`<div class="st-grid">${items.map(card).join('')}</div>`:'<div class="st-empty">이 구분에 해당하는 주요뉴스가 없습니다.</div>'}</section>`;
  fetch('saveticker-market.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw new Error('load');return r.json()}).then(data=>{
    const meta=data.meta||{},items=data.items||[],direct=items.filter(x=>x.scope==='국장 직접'),sector=items.filter(x=>x.scope==='국내 섹터 영향');
    status.textContent=`SaveTicker · 작성자 오선 · ${meta.status||'자료 확인'} · ${meta.updatedKST||'기준시각 미확인'} KST · 매일 오후 3시 45분 전체 평가창과 함께 갱신`;
    body.innerHTML=`<div class="st-wrap"><div class="st-toolbar"><p><strong>국장 주요뉴스만 선별</strong><br>${e(meta.selectionRule||'국내 기업·시장 및 국내 업종 영향 뉴스만 표시')}</p><a href="https://saveticker.com/news" target="_blank" rel="noopener noreferrer">SaveTicker 뉴스 원문 ↗</a></div><div class="st-summary"><div class="st-stat"><span>확인 범위</span><strong>${e(meta.range||'—')}</strong><small>최근 ${e(meta.windowHours||36)}시간</small></div><div class="st-stat"><span>오선 작성</span><strong>${fmt(meta.authorCount)}건</strong><small>수집 ${fmt(meta.fetchedCount)}건 중</small></div><div class="st-stat"><span>국장 직접</span><strong>${fmt(meta.directCount)}건</strong><small>국내 기업·시장·제도</small></div><div class="st-stat"><span>섹터 영향</span><strong>${fmt(meta.sectorCount)}건</strong><small>영향 경로가 명확한 뉴스</small></div></div>${section('국내 기업·시장 직접 뉴스','국내 상장기업, 코스피·코스닥, 거래제도와 정책을 직접 언급한 기사입니다.',direct)}${section('국내 업종에 영향이 큰 뉴스','해외 뉴스 중 국내 반도체·디스플레이·정유·자동차·전력 업종으로 이어지는 경로가 뚜렷한 기사만 표시합니다.',sector)}<p class="st-foot">기사 내용은 SaveTicker의 오선 작성 원문을 짧게 요약합니다. ‘국장 연결’은 국내 종목을 추천하는 문구가 아니라 영향을 확인할 업종과 경로입니다. 비슷한 속보는 하나로 통합합니다.</p></div>`;
  }).catch(error=>{status.textContent='유튜브시황2 자료 불러오기 실패 · 다른 평가창과 무관';body.innerHTML='<div class="st-wrap"><div class="st-empty">SaveTicker 주요뉴스를 불러오지 못했습니다.</div></div>';console.error(error)});
})();
