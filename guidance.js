(() => {
  'use strict';
  const esc = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = n => Number.isFinite(n) ? (n/1e8).toLocaleString('ko-KR',{maximumFractionDigits:1}) : '—';
  const range = v => v ? (v.low===v.high?money(v.mid):`${money(v.low)}~${money(v.high)}`) : '—';
  const gap = n => Number.isFinite(n) ? `${n>0?'+':''}${(n*100).toFixed(1)}%` : '비교 보류';
  function render(data) {
    return `<p>${esc(data.policy)} · ${esc(data.status?.status)} · 확인 ${esc(data.status?.checked_at || '미확인')} · 조회 ${esc(data.status?.from||'미확인')}~${esc(data.status?.to||'미확인')}</p>`+
      (data.rows?.length ? '<div class="tablewrap"><table><thead><tr><th>종목 / Guidance</th><th>대상기간 / 기준</th><th>가이던스 우선 · 억원</th><th>컨센서스 · 억원</th><th>괴리율</th><th>발표 / 정정 / 출처</th></tr></thead><tbody>'+data.rows.map(r=>{
        let url; try { const u=new URL(r.source_url); if(u.protocol==='https:'&&['dart.fss.or.kr','kind.krx.co.kr'].includes(u.hostname))url=u.href; }catch{}
        return `<tr><td>${esc(r.name||r.ticker)}<br>Guidance 있음 ${esc((r.tags||[]).join(' / '))}</td><td>${esc(r.period)}<br>${esc(({consolidated:'연결',separate:'별도',managed_consolidated:'관리연결',subsidiary:'자회사 대상'})[r.basis]||'기준 확인 필요')}</td><td>매출 ${range(r.sales)}<br>영업이익 ${range(r.operating_profit)}<br>OPM ${r.opm?esc(r.opm.mid.toFixed(1))+'%':'—'}<br>${esc(Object.entries(r.changes||{}).map(([k,v])=>(k==='sales'?'매출 ':'영업이익 ')+v).join(' / '))}</td><td>매출 ${money(r.consensus?.sales)}<br>영업이익 ${money(r.consensus?.operating_profit)}<br>기준일 ${esc(r.consensus?.as_of||'미확인')}</td><td>매출 ${gap(r.guidance_vs_consensus_gap?.sales)}<br>영업이익 ${gap(r.guidance_vs_consensus_gap?.operating_profit)}<br>${esc(r.comparison_status)}</td><td>${esc(r.published_at)} · ${r.is_correction?'정정':'최초/신규 공시'}<br>${esc(r.fetch_status)}<br>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(r.source)} ${esc(r.receipt)}</a>`:'출처 확인 필요'}</td></tr>`;
      }).join('')+'</tbody></table></div>':'<p>표시할 검증 가이던스 없음 · 기존 컨센서스 그대로 사용 · 감점 없음</p>');
  }
  if(typeof module!=='undefined')module.exports={render};
  if(typeof document==='undefined')return;
  const host=document.getElementById('guidancebody');
  if(!host)return;
  fetch('guidance.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json()}).then(data=>{host.innerHTML=render(data)}).catch(()=>{host.textContent='Guidance 미수집/불러오기 실패 · 기존 컨센서스 그대로 사용 · 감점 없음'});
})();
