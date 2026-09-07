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
    document.getElementById('combinedbody').innerHTML = (data.rows || []).map(r => `<tr>${cell(r.rank)}${stockCell(r)}${cell(r.condition)}${cell(r.entryState)}${cell(number(r.combinedScore))}${cell(number(r.currentPrice))}${cell(number(r.valueScore))}${cell(number(r.growthScore))}${cell(r.sectorRelation)}${cell(r.sourceDate)}</tr>`).join('') || '<tr><td colspan="10">같은 기준일의 진입·가치 또는 진입·성장 조건을 충족한 후보가 없습니다.</td></tr>';
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
      // Recompute filtered summaries from the same version-separated cohorts, never mix horizons.
      const groups = new Map();
      for (const row of rows) { const key = JSON.stringify([row.group,row.engineVersion,row.ruleVersion]); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(row); }
      const summary = [...groups.values()].map(items => {
        const ready = items.filter(r => r.horizons?.[horizon]?.returnPct != null);
        const values = ready.map(r => r.horizons[horizon].returnPct).sort((a,b) => a-b);
        const extras = ready.map(r => r.horizons[horizon].excessPct).filter(v => v != null);
        const avg = values.length ? values.reduce((a,b) => a+b,0)/values.length : null;
        const mid = values.length ? (values[Math.floor((values.length-1)/2)] + values[Math.floor(values.length/2)])/2 : null;
        const excess = extras.length ? extras.reduce((a,b) => a+b,0)/extras.length : null;
        const first = items[0];
        return `<tr>${cell(first.group)}<td title="${text(first.engineVersion)}">${text(first.ruleVersion)}<span class="sector">${text(first.engineVersion.slice(0,10))}</span></td>${cell(`${ready.length} / ${items.length}`)}${cell(items.length-ready.length)}${cell(new Set(ready.map(r=>r.ticker)).size)}${cell(pct(avg))}${cell(pct(mid))}${cell(pct(values.length ? values.filter(v=>v>0).length/values.length*100 : null))}<td>${text(pct(excess))}<span class="sector">${extras.length}건 비교</span></td></tr>`;
      });
      document.getElementById('performancesummary').innerHTML = summary.join('') || '<tr><td colspan="9">공개 확인된 추천이 쌓인 후 조건별 성과를 표시합니다.</td></tr>';
      rows.sort((a,b) => (b.horizons?.[horizon]?.returnPct ?? -Infinity) - (a.horizons?.[horizon]?.returnPct ?? -Infinity) || a.name.localeCompare(b.name,'ko'));
      let rank = 0;
      document.getElementById('performancerows').innerHTML = rows.map(r => {const result = r.horizons?.[horizon] || {};return `<tr>${cell(result.returnPct == null ? '—' : ++rank)}${stockCell(r)}${cell(r.group)}${cell(r.recommendationDate)}${cell(number(r.entryPrice))}${cell(r.entryDate || '대기')}${cell(result.exitDate || '대기')}${cell(pct(result.returnPct))}${cell(pct(r.currentReturnPct))}${cell(pct(r.maxDrawdownPct))}${cell(result.status || r.status)}</tr>`;}).join('') || '<tr><td colspan="11">기록 대기 · 과거 가격으로 추천일이나 성과를 만들어 넣지 않습니다.</td></tr>';
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
