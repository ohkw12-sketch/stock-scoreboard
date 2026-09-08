(() => {
  'use strict';
  const e = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const link = source => `<a href="${e(source.url)}" target="_blank" rel="noopener noreferrer">${e(source.title)} ↗</a>`;
  const status = document.getElementById('samprostatus');
  const body = document.getElementById('samprobody');
  fetch('sampro-market.json', {cache:'no-store'}).then(r => {if(!r.ok) throw new Error('load');return r.json();}).then(data => {
    const editions = [...data.editions].sort((a,b)=>b.date.localeCompare(a.date));
    const today = new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul'}).format(new Date());
    status.textContent = `삼프로TV · 자료 기준 ${editions[0].date} · ${data.status} · 매일 오후 3시 45분 전체 평가창과 함께 갱신` + (editions[0].date < today ? ' · 오늘 새 원문 미반영, 이전 기준일 자료입니다.' : '');
    body.innerHTML = `<div class="sp-wrap"><div class="sp-toolbar"><label>날짜별 시황 <select id="samprodate">${editions.map(d=>`<option value="${e(d.date)}">${e(d.date)}</option>`).join('')}</select></label><a href="https://apps.3protv.com/news/list/1" target="_blank" rel="noopener noreferrer">삼프로 공식 뉴스룸 ↗</a></div><p class="sp-note">공식 뉴스레터·방송 요약에서 확인한 내용을 짧게 정리합니다. ‘앞으로 확인할 점’과 종합 정리는 편집 해석이며, 출연자 의견은 이름을 표시합니다.</p><div id="samproedition"></div></div>`;
    const draw = () => {
      const d=editions.find(x=>x.date===document.getElementById('samprodate').value);
      const sourceMap=Object.fromEntries(d.sources.map(s=>[s.id,s]));
      document.getElementById('samproedition').innerHTML=`<header class="sp-intro"><span>${e(d.date)} · 종합 정리</span><h2>${e(d.headline)}</h2><p>${e(d.overview)}</p></header>${['매크로','기업'].map(group=>`<section><h3>${group==='매크로'?'매크로 경제 · 시장의 방향':'개별 기업 · 실적에 연결되는 이슈'}</h3><div class="sp-grid">${d.items.filter(x=>x.category===group).map(x=>`<article class="sp-card"><h4>${e(x.title)}</h4><dl><dt>무슨 일이 있었나</dt><dd>${e(x.fact)}</dd><dt>어떻게 바라보나</dt><dd>${e(x.view)}</dd><dt>앞으로 확인할 점 <small>편집 해석</small></dt><dd>${e(x.watch)}</dd></dl><footer>${e(x.attribution)}<br>${link(sourceMap[x.source])}${(x.additionalSources||[]).map(id=>' · '+link(sourceMap[id])).join('')}</footer></article>`).join('')}</div></section>`).join('')}<p class="sp-note">원문 게시: ${d.sources.map(s=>`${link(s)} · ${e(s.publishedAt.replace('T',' ').replace('+09:00',' KST'))}`).join('<br>')}<br>확인 시각: ${e(d.updatedAt)} · 이날 모든 방송을 망라한 요약은 아닙니다.</p>`;
    };
    document.getElementById('samprodate').addEventListener('change',draw);draw();
  }).catch(error=>{status.textContent='삼프로 시황을 불러오지 못했습니다. 잠시 후 다시 열어 주세요.';console.error(error);});
})();
