'use strict';

// Pure JavaScript runtime unit tests. This small DOM double is deliberately not
// a browser: it checks rendering data/isolated failures, not CSS, layout or paint.
// Run: node --test tests/test_ui_runtime.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const researchScript = fs.readFileSync(path.join(root, 'research.js'), 'utf8');
const inlineScripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)].map(match => match[1]);
assert.ok(inlineScripts.length, 'The actual page must contain its initialization script');

const decode = value => String(value).replace(/<[^>]*>/g, '').replace(/&(?:amp|lt|gt|quot|#39);/g,
  entity => ({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"}[entity]));
const attributes = markup => Object.fromEntries([...markup.matchAll(/([\w-]+)="([^"]*)"/g)].map(match => [match[1], decode(match[2])]));

class ClassList {
  constructor(value = '') { this.values = new Set(value.split(/\s+/).filter(Boolean)); }
  contains(value) { return this.values.has(value); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  toggle(value, force) {
    const enabled = force === undefined ? !this.contains(value) : force;
    enabled ? this.add(value) : this.remove(value);
    return enabled;
  }
}

class Element {
  constructor(tag, attrs = {}, owner) {
    this.tagName = tag.toUpperCase();
    this.id = attrs.id || '';
    this.owner = owner;
    this.attributes = {...attrs};
    this.classList = new ClassList(attrs.class);
    this.dataset = {};
    for (const [name, value] of Object.entries(attrs)) {
      if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    }
    this.listeners = new Map();
    this._html = '';
    this._text = null;
    this.value = attrs.value || '';
    this.hidden = false;
    this.rows = [];
  }
  set innerHTML(value) {
    this._html = String(value);
    this._text = null;
    if (this.tagName === 'SELECT') {
      const options = [...this._html.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)];
      const selected = options.find(match => /\bselected\b/.test(match[1])) || options[0];
      this.value = selected ? attributes(selected[1]).value || decode(selected[2]) : '';
    }
    if (this.tagName === 'TBODY') {
      this.rows = [...this._html.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/g)].map(match => ({
        markup: match[0],
        cells: [...match[1].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)].map(cell => ({textContent: decode(cell[1])})),
      }));
    }
  }
  get innerHTML() { return this._html; }
  set textContent(value) { this._text = String(value); }
  get textContent() { return this._text === null ? decode(this._html) : this._text; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  fire(name, extra = {}) {
    for (const callback of this.listeners.get(name) || []) callback({type: name, preventDefault() {}, ...extra});
  }
  querySelector(selector) { return selector === '.sort-icon' ? this.sortIcon || null : null; }
  insertAdjacentHTML(position, markup) {
    assert.equal(position, 'beforeend');
    assert.match(markup, /sort-icon/, 'The DOM double only supports header sorting icons');
    this.sortIcon = new Element('span', {class: 'sort-icon'}, this.owner);
    this.sortIcon.textContent = '↕';
  }
  append(...rows) { this.rows = rows; this._html = rows.map(row => row.markup).join(''); }
}

class Table {
  constructor(markup, owner) {
    const bodyId = markup.match(/<tbody\b[^>]*\bid="([^"]+)"/)[1];
    this.bodyId = bodyId;
    this.headers = [...markup.matchAll(/<th\b([^>]*)>([\s\S]*?)<\/th>/g)].map(match => {
      const header = new Element('th', attributes(match[1]), owner);
      header.innerHTML = match[2];
      header.originalLabel = header.textContent;
      return header;
    });
    this.tBodies = [owner.getElementById(bodyId)];
  }
  querySelectorAll(selector) {
    if (selector === 'thead th') return this.headers;
    if (selector === 'th.sortable') return this.headers.filter(header => header.classList.contains('sortable'));
    throw new Error('Unsupported table selector: ' + selector);
  }
}

class Document {
  constructor(markup) {
    this.elements = new Map();
    for (const match of markup.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="[^"]+"[^>]*)>/gi)) {
      const attrs = attributes(match[2]);
      this.elements.set(attrs.id, new Element(match[1], attrs, this));
    }
    this.tabs = [...markup.matchAll(/<button\b([^>]*\bdata-target="[^"]+"[^>]*)>/g)].map(match => new Element('button', attributes(match[1]), this));
    this.panels = [...this.elements.values()].filter(element => element.classList.contains('panel'));
    for (const match of markup.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)) {
      this.getElementById(attributes(match[1]).id).innerHTML = match[2];
    }
    this.tables = [...markup.matchAll(/<table\b[^>]*>[\s\S]*?<\/table>/g)]
      .filter(match => /<tbody\b[^>]*\bid="/.test(match[0])).map(match => new Table(match[0], this));
  }
  getElementById(id) { return this.elements.get(id) || null; }
  querySelectorAll(selector) {
    if (selector === '.tab') return this.tabs;
    if (selector === '.panel') return this.panels;
    if (selector === '.board table') return this.tables;
    if (selector === '#performance th.sortable') return this.tables.filter(table => table.bodyId.startsWith('performance'))
      .flatMap(table => table.querySelectorAll('th.sortable'));
    // Dynamic YouTube filtering isn't exercised by these data-boundary tests.
    if (selector === '.yt-filter' || selector === '#ytRecoBody tr') return [];
    throw new Error('Unsupported document selector: ' + selector);
  }
  table(bodyId) { return this.tables.find(table => table.bodyId === bodyId); }
}

function fixtures() {
  const common = {ticker:'000001', name:'검증종목', sector:'검증산업', rank:1};
  return {
    'data.json': {
      meta: {title:'주식평가창', runId:'run-current', updatedKST:'2026-09-07 12:00', masterBasis:'2026-09-04 종가', note:'신뢰도는 예측 확률이 아닙니다.', sourceSummary:'검증 원자료'},
      p1: {status:'진입 정상', rows:[{...common, entryState:'진입가능', signal:'매수', currentPrice:100,
        entryZone:'90~100', confirmation:'지지 확인', invalidationPrice:90, stopPct:-10, growth1Y:'+20%',
        consensus:'자료 확인', consensusDate:'2026-08', valueMultiple:'8배', reason:'검증 근거'}]},
      p11: {status:'순환 정상', engine:{asOfDate:'2026-09-04', stockCount:1, sectorCount:1},
        sectors:[{rank:1,name:'검증산업',stage:'확산',score:60,rs1Pct:1,rs3Pct:2,rs5Pct:3,
          advanceRatioPct:60,rotationType:'확산',rotationStartDate:'2026-09-01',positionPct:40,riskGauge:20}],
        rows:[{...common,relation:'선행',marketState:'확산',signal:'관찰',marketDetail:'+3%',reason:'검증'}]},
      p2: {status:'가치 정상', method:'절대·섹터·정상화', rows:[{...common, typeRank:1,valueScore:75,
        normalizedPOP:8,sectorNormalizedPOP:10,normalizedPremiumPct:-20,normalizationAdjustmentPct:-5,
        confidence:'A',normalizationSourceBadge:'공시 실적'}]},
      growth: {status:'성장 정상',dataStatus:{status:'정상',news:{status:'정상'},verifiedDocuments:{status:'원문검증대기'}},
        rows:[{...common,growthRate:20,fundamentalScore:70,priceReflection:'미반영 가능',confidence:'보통',evidenceCount:2}],
        sectors:[{rank:1,sector:'검증산업',confidence:'보통',evidenceCount:3,stocks:[common]}]},
      p3: {status:'보유 정상',valuationBasis:'검증 종가',rows:[{...common,qty:10,avg:90,ret:'+11.11%',opGrowth:'+20%',
        valuePosition:'20% 할인',fairRange:'사용자 입력',drawdown3m:'-5%',judgment:'기존 판단',action:'보유',basis:'2026-09-04'}]},
    },
    'youtube-market.json': {meta:{status:'가격 갱신',updatedKST:'2026-09-01 08:00',range:'9월',priceBasis:'9/4 종가',summary:[]},
      weeks:[{label:'이번주',kim:[],park:[]}],recommendations:[],expired:'기간 경과',contentStatus:{status:'원문검증대기'}},
    'combined-recommendations.json': {status:'검증용 후보',candidateCount:1,sourceDate:'2026-09-04',
      generatedAt:'2026-09-07T12:00:00+09:00',ruleVersion:'combined-1.0',notice:'실험 규칙',
      rows:[{...common,condition:'진입 + 가치',entryState:'진입가능',combinedScore:70,currentPrice:100,
        valueScore:75,growthScore:null,sectorRelation:'선행',sourceDate:'2026-09-04'}]},
    'recommendation-performance.json': {status:'공개 확인된 추천 기록 대기',recordCount:0,priceDate:'2026-09-04',
      entryRule:'공개 다음 거래일 시가',returnBasis:'수정주가',costBps:0,benchmarkRule:'전체시장 동일비중',notice:'반복은 독립 표본이 아닙니다.',rows:[]},
    'refresh-status.json': {runId:'run-current',sections:{}},
  };
}

async function runtime({data = fixtures(), fail = {}} = {}) {
  const document = new Document(html);
  const errors = [], requests = [];
  const context = vm.createContext({document, window:{print() {}}, console:{error(...args) { errors.push(args); }},
    fetch: async url => {
      const name = String(url).split('?')[0];
      requests.push(name);
      if (fail[name] === 'network') throw new Error('Injected network failure');
      return {ok: fail[name] !== 'http', json: async () => {
        if (fail[name] === 'json') throw new SyntaxError('Injected invalid JSON');
        assert.ok(Object.hasOwn(data, name), 'Unexpected fetch; tests never access a real endpoint');
        return structuredClone(data[name]);
      }};
    }});
  for (const script of inlineScripts) vm.runInContext(script, context, {filename:'index.html:inline',timeout:1000});
  vm.runInContext(researchScript, context, {filename:'research.js',timeout:1000});
  // All fixture I/O resolves locally. One event-loop turn drains chained fetch,
  // json, render, catch and Promise.allSettled microtasks from both scripts.
  await new Promise(resolve => setImmediate(resolve));
  return {document, errors, requests, context};
}

function performanceRow(name, result = null, changes = {}) {
  return {ticker:'000001',name,sector:'검증산업',group:'진입',engineVersion:'engine-123456789',ruleVersion:'entry-1',
    recommendationDate:'2026-09-01',entryDate:null,entryPrice:null,currentReturnPct:null,maxDrawdownPct:null,
    status:'다음 거래일 대기',horizons:{'20':{returnPct:result,excessPct:null,exitDate:null,status:result==null?'기간 미도래':'완료'}},...changes};
}

test('actual page scripts render all independent boards and preserve locked headers', async () => {
  const {document, errors, requests} = await runtime();
  assert.equal(errors.length, 0);
  assert.equal(new Set(requests).size, 5);
  for (const id of ['p1body','p11body','p2body','growthbody','p3body','combinedbody']) assert.match(document.getElementById(id).innerHTML, /검증종목/);
  const contract = JSON.parse(fs.readFileSync(path.join(root, 'ui_contract.json'), 'utf8'));
  for (const table of Object.values(contract.tables)) {
    assert.deepEqual(document.table(table.tbodyId).headers.map(header => header.originalLabel), table.headers);
  }
  assert.match(document.getElementById('growthcoverage').textContent, /뉴스 검색큐: 정상/);
  assert.match(document.getElementById('growthcoverage').textContent, /검증 뉴스·IR 원문: 원문검증대기/);
  assert.match(document.getElementById('p5body').innerHTML, /이 기간에 확인·등록된 발언 없음/);
  assert.doesNotMatch(document.getElementById('p5body').innerHTML, /신규 공개 영상 없음/);
});

test('empty performance history shows waiting text without fabricated return or ranking', async () => {
  const {document, errors} = await runtime();
  assert.equal(errors.length, 0);
  assert.match(document.getElementById('performancerows').textContent, /기록 대기/);
  assert.match(document.getElementById('performancesummary').textContent, /공개 확인된 추천이 쌓인 후/);
  assert.doesNotMatch(document.getElementById('performancerows').innerHTML, /<td>0%<\/td>|<td>1<\/td>/);
});

test('null horizons stay unranked and do not enter win-rate denominator', async () => {
  const data = fixtures();
  data['recommendation-performance.json'].rows = [performanceRow('미도래'), performanceRow('상승',10),performanceRow('보합',0)];
  data['recommendation-performance.json'].recordCount = 3;
  const {document, errors} = await runtime({data});
  assert.equal(errors.length, 0);
  const rows = document.getElementById('performancerows').rows;
  assert.deepEqual(rows.map(row => row.cells[0].textContent), ['1','2','—']);
  assert.deepEqual(rows.map(row => row.cells[7].textContent), ['+10%','0%','—']);
  assert.equal(rows[2].cells[4].textContent, '—');
  const summary = document.getElementById('performancesummary').rows[0].cells;
  assert.equal(summary[2].textContent, '2 / 3');
  assert.equal(summary[3].textContent, '1');
  assert.equal(summary[5].textContent, '+5%');
  assert.equal(summary[7].textContent, '+50%');
});

test('all pending selected horizons show dashes, then filter renders exact available horizon', async () => {
  const data = fixtures();
  data['recommendation-performance.json'].rows = [performanceRow('종목',null,{horizons:{'5':{returnPct:3,excessPct:1,exitDate:'2026-09-07',status:'완료'},'20':{returnPct:null,status:'기간 미도래'}}})];
  const {document} = await runtime({data});
  let row = document.getElementById('performancerows').rows[0];
  assert.equal(row.cells[0].textContent, '—');
  assert.equal(row.cells[7].textContent, '—');
  assert.equal(document.getElementById('performancesummary').rows[0].cells[5].textContent, '—');
  const select = document.getElementById('performancehorizon');
  select.value='5'; select.fire('change');
  row = document.getElementById('performancerows').rows[0];
  assert.equal(row.cells[0].textContent, '1');
  assert.equal(row.cells[7].textContent, '+3%');
});

for (const failure of ['http','json','network']) {
  test(`core JSON ${failure} failure leaves combined, performance and YouTube rendering operational`, async () => {
    const {document} = await runtime({fail:{'data.json':failure}});
    assert.match(document.getElementById('note').textContent, /기본 평가자료를 불러오지 못했습니다/);
    assert.match(document.getElementById('combinedbody').innerHTML, /검증종목/);
    assert.match(document.getElementById('performancerows').innerHTML, /기록 대기/);
    assert.match(document.getElementById('p5body').innerHTML, /김종효/);
  });
}

test('YouTube JSON failure cannot abort entry, value or growth', async () => {
  const {document} = await runtime({fail:{'youtube-market.json':'json'}});
  assert.match(document.getElementById('p5status').textContent, /불러오기 실패/);
  for (const id of ['p1body','p2body','growthbody','combinedbody']) assert.match(document.getElementById(id).innerHTML, /검증종목/);
});

test('rotation render exception cannot abort value, growth or holdings', async () => {
  const data = fixtures();
  data['data.json'].p11.sectors[0].stage = null; // Actual stageClass throws inside renderP11.
  const {document, errors} = await runtime({data});
  assert.equal(errors.length, 1);
  assert.equal(errors[0][0], 'p11');
  assert.match(document.getElementById('p11status').textContent, /이 평가창 표시 오류/);
  for (const id of ['p2body','growthbody','p3body']) assert.match(document.getElementById(id).innerHTML, /검증종목/);
});

test('failed growth refresh labels retained results without hiding their rows', async () => {
  const data = fixtures();
  data['data.json'].growth.refreshState = {status:'실패·이전유지'};
  const {document, errors} = await runtime({data});
  assert.equal(errors.length, 0);
  assert.match(document.getElementById('growthstatus').textContent, /갱신 실패, 이전 자료 유지/);
  assert.match(document.getElementById('growthbody').innerHTML, /검증종목/);
});

test('matching refresh manifest overlays failed-section warnings on retained public rows only', async () => {
  const data = fixtures();
  data['refresh-status.json'].sections = {
    p1:{status:'계산완료'},p2:{status:'실패·이전유지'},growth:{status:'실패·이전유지'},
  };
  // The retained sections themselves deliberately have no new refreshState.
  const {document, errors} = await runtime({data});
  assert.equal(errors.length,0);
  for (const key of ['p2','growth']) {
    assert.match(document.getElementById(key+'status').textContent,/갱신 실패, 이전 자료 유지/);
    assert.match(document.getElementById(key+'body').innerHTML,/검증종목/);
  }
  assert.equal(document.getElementById('p1status').textContent,'진입 정상');
  assert.match(document.getElementById('combinedbody').innerHTML,/검증종목/);
});

test('unmatched or unavailable refresh manifest cannot relabel healthy boards', async () => {
  for (const mode of ['mismatch','http','json','network']) {
    const data = fixtures();
    data['refresh-status.json'] = {runId:mode==='mismatch'?'older-run':'run-current',
      sections:{p2:{status:'실패·이전유지'},growth:{status:'실패·이전유지'}}};
    const fail = mode==='mismatch'?{}:{'refresh-status.json':mode};
    const {document,errors} = await runtime({data,fail});
    assert.equal(errors.length,0,mode);
    assert.equal(document.getElementById('p2status').textContent,'가치 정상',mode);
    assert.equal(document.getElementById('growthstatus').textContent,'성장 정상',mode);
    assert.match(document.getElementById('p2body').innerHTML,/검증종목/,mode);
    assert.match(document.getElementById('combinedbody').innerHTML,/검증종목/,mode);
  }
});

test('research JSON/render failures remain contained in their own panels', async () => {
  const data = fixtures();
  data['recommendation-performance.json'].rows = [performanceRow('invalid',1,{engineVersion:null})];
  const {document} = await runtime({data,fail:{'combined-recommendations.json':'http'}});
  assert.match(document.getElementById('combinedstatus').textContent, /불러오기 실패/);
  assert.match(document.getElementById('performancestatus').textContent, /불러오기 실패/);
  for (const id of ['p1body','p2body','growthbody','p3body']) assert.match(document.getElementById(id).innerHTML, /검증종목/);
});

test('date filter resets sorting metadata to match the new default rows', async () => {
  const data = fixtures();
  data['recommendation-performance.json'].rows = [performanceRow('상승',10),performanceRow('하락',-3,{recommendationDate:'2026-09-02'})];
  const {document} = await runtime({data});
  const header = document.table('performancerows').headers[7];
  header.fire('click');
  assert.equal(header.getAttribute('aria-sort'),'ascending');
  assert.equal(document.getElementById('performancerows').rows[0].cells[7].textContent,'-3%');
  const select = document.getElementById('performancedate');
  select.value='2026-09-01'; select.fire('change');
  assert.equal(header.getAttribute('aria-sort'),'none');
  assert.equal(header.dataset.direction,'');
  assert.equal(header.sortIcon.textContent,'↕');
  assert.equal(document.getElementById('performancerows').rows.length,1);
  assert.equal(document.getElementById('performancerows').rows[0].cells[7].textContent,'+10%');
});
