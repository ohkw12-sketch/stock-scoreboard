/* Independent data and render boundaries: a research error cannot replace existing boards. */
(() => {
  'use strict';
  const text = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => value == null ? '—' : Number(value).toLocaleString('ko-KR', {maximumFractionDigits:2});
  const pct = value => value == null ? '—' : `${value > 0 ? '+' : ''}${number(value)}%`;
  const cell = value => `<td>${text(value)}</td>`;
  const stockCell = row => `<td><strong>${text(row.name)}</strong><span class="sector">${text(row.sector || row.ticker)}</span></td>`;
  async function read(path) { const r = await fetch(`${path}?v=${Date.now()}`, {cache:'no-store'}); if (!r.ok) throw new Error(path); return r.json(); }
  function combined(data) {
    document.getElementById('combinedstatus').textContent = `${data.status} · 전체 조합 후보 ${number(data.candidateCount)}개`;
    document.getElementById('combinedbasis').textContent = `가격 ${data.sourceDate || '미확인'} · 산출 ${data.generatedAt || '미확인'} · ${data.ruleVersion || ''} · ${data.notice || ''}`;
    document.getElementById('combinedbody').innerHTML = (data.rows || []).map(r => `<tr>${cell(r.rank)}${stockCell(r)}<td>${text(r.condition)}<span class="sector">${text(r.matchedPattern?.label || '')}</span></td>${cell(r.entryState)}<td>${text(number(r.combinedScore))}<span class="sector">${text(r.matchedPattern ? `${r.matchedPattern.horizon}일 · ${r.matchedPattern.sampleCount}종목 · 중앙 ${pct(r.matchedPattern.medianReturnPct)}` : '')}</span></td>${cell(number(r.currentPrice))}${cell(number(r.valueScore))}${cell(number(r.growthScore))}${cell(r.sectorRelation)}${cell(r.sourceDate)}</tr>`).join('') || '<tr><td colspan="10">완료된 과거 성과와 일치하는 최근 후보가 아직 없습니다.</td></tr>';
    const patterns = document.getElementById('feedbackpatterns');
    if (patterns) patterns.innerHTML = (data.feedback?.patterns || []).slice(0,20).map(p => `<tr>${cell(p.rank)}${cell(p.label)}${cell(`${p.horizon}거래일`)}${cell(p.sampleCount)}${cell(p.recommendationDays)}${cell(pct(p.meanReturnPct))}${cell(pct(p.medianReturnPct))}${cell(pct(p.winRatePct))}${cell(`${p.status} · 저장본 ${p.archiveSampleCount}종목`)}</tr>`).join('') || '<tr><td colspan="9">같은 기간의 평가 완료 표본이 쌓이면 표시합니다.</td></tr>';
    setupTableSorting();
  }
  function performance(data) {
    document.getElementById('performancestatus').textContent = `${data.status} · 기록 ${number(data.recordCount)}건 · 가격 ${data.priceDate || '미확인'}`;
    document.getElementById('performancebasis').textContent = `${data.entryRule} · ${data.returnBasis} · 왕복비용 ${number(data.costBps)}bp`;
    document.getElementById('performancenote').textContent = `${data.benchmarkRule} · ${data.notice}`;
    const dateSelect = document.getElementById('performancedate');
    dateSelect.innerHTML = '<option value="all">전체</option>' + [...new Set((data.rows || []).map(r => r.recommendationDate))].sort().reverse().map(d => `<option value="${text(d)}">${text(d)}</option>`).join('');
    function render() {
      document.querySelectorAll('#performance th.sortable').forEach(header => {
        header.dataset.direction=''; header.setAttribute('aria-sort','none');
        const icon=header.querySelector('.sort-icon'); if(icon){icon.textContent='↕';icon.classList.remove('active');}
      });
      const horizon = Number(document.getElementById('performancehorizon').value);
      const date = dateSelect.value;
      const rows = (data.rows || []).filter(r => date === 'all' || r.recommendationDate === date);
      const outcome = r => horizon === 0 ? {returnPct:r.currentReturnPct, exitDate:r.latestDate, status:r.status} : (r.horizons?.[horizon] || {});
      // Recompute filtered summaries from the same version-separated cohorts, never mix horizons.
      const groups = new Map();
      for (const row of rows) { const key = row.group; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(row); }
      const summary = [...groups.values()].map(items => {
        const ready = items.filter(r => outcome(r).returnPct != null);
        const values = ready.map(r => outcome(r).returnPct).sort((a,b) => a-b);
        const extras = ready.map(r => outcome(r).excessPct).filter(v => v != null);
        const avg = values.length ? values.reduce((a,b) => a+b,0)/values.length : null;
        const mid = values.length ? (values[Math.floor((values.length-1)/2)] + values[Math.floor(values.length/2)])/2 : null;
        const excess = extras.length ? extras.reduce((a,b) => a+b,0)/extras.length : null;
        const first = items[0];
        return {avg, html:`<tr>${cell(first.group)}${cell('프로젝트별 최초 추천 · 당시 버전 보존')}${cell(`${ready.length} / ${items.length}`)}${cell(items.length-ready.length)}${cell(new Set(ready.map(r=>r.ticker)).size)}${cell(pct(avg))}${cell(pct(mid))}${cell(pct(values.length ? values.filter(v=>v>0).length/values.length*100 : null))}<td>${text(pct(excess))}<span class="sector">${extras.length}건 비교</span></td></tr>`};
      });
      document.getElementById('performancesummary').innerHTML = summary.sort((a,b)=>(b.avg??-Infinity)-(a.avg??-Infinity)).map(s=>s.html).join('') || '<tr><td colspan="9">추천 기록 대기</td></tr>';
      rows.sort((a,b) => (outcome(b).returnPct ?? -Infinity) - (outcome(a).returnPct ?? -Infinity) || a.name.localeCompare(b.name,'ko'));
      let rank = 0;
      document.getElementById('performancerows').innerHTML = rows.map(r => {const result = outcome(r);return `<tr>${cell(result.returnPct == null ? '—' : ++rank)}${stockCell(r)}<td>${text(r.group)}<span class="sector">당시 ${text(r.rank ?? '—')}위 · ${text((r.features||[]).join(' / '))}</span></td><td>${text(r.recommendationDate)}<span class="sector">${text(r.recordBasis || '')}</span></td>${cell(number(r.entryPrice))}${cell(r.entryDate || '대기')}${cell(result.exitDate || '대기')}${cell(pct(result.returnPct))}${cell(pct(r.currentReturnPct))}${cell(pct(r.maxDrawdownPct))}${cell(result.status || r.status)}</tr>`;}).join('') || '<tr><td colspan="11">확인 가능한 추천 기록 대기</td></tr>';
      setupTableSorting();
    }
    dateSelect.addEventListener('change', render);
    document.getElementById('performancehorizon').addEventListener('change', render);
    render();
  }
  Promise.allSettled([
    read('combined-recommendations.json').then(combined).catch(() => {document.getElementById('combinedstatus').textContent='종합추천 자료 불러오기 실패 · 기존 평가창은 유지합니다.';}),
    read('recommendation-performance.json').then(performance).catch(() => {document.getElementById('performancestatus').textContent='성과검증 자료 불러오기 실패 · 기존 평가창은 유지합니다.';})
  ]);
})();
