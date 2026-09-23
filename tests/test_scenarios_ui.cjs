'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ui = require('../scenarios.js');

test('untrusted source text is escaped; non-https links never become anchors',()=>{
  assert.equal(ui.esc('<img onerror="run()">'),'&lt;img onerror=&quot;run()&quot;&gt;');
  assert.ok(!ui.link('javascript:alert(1)','bad').includes('<a'));
  assert.ok(ui.link('https://example.com/?a="x"','<fake>').includes('&lt;fake&gt;'));
});

test('missing numbers remain unknown while verified zero and negative OP survive',()=>{
  assert.equal(ui.eok(null),'미확인');
  assert.equal(ui.percent(undefined),'미확인');
  assert.equal(ui.eok(0),'0억');
  assert.equal(ui.eok(-100000000),'-1억');
  assert.ok(!ui.percent(null).includes('0%'));
});

test('forecast cells retain dates, source scope and underlying broker values',()=>{
  const rendered=ui.forecastCell({value:1e10,date:'2026-08-01',source:'개별 추정치',url:'https://example.com/source',
    sources:[{value:1e10,date:'2026-08-01',name:'A증권',url:'https://example.com/source'}]});
  assert.ok(rendered.includes('2026-08-01'));
  assert.ok(rendered.includes('개별 추정치'));
  assert.ok(rendered.includes('A증권'));
  assert.ok(rendered.includes('noopener noreferrer'));
});

test('all views render the real dataset without NaN or undefined', {skip:!fs.existsSync(path.join(__dirname,'../test_output/scenario-board.test.json'))},()=>{
  const data=JSON.parse(fs.readFileSync(path.join(__dirname,'../test_output/scenario-board.test.json'),'utf8'));
  ui.setData(data);
  for(const view of ['overview','scenarios','sectors','stocks','holdings','audit','stock']){
    const html=ui.renderView(view,'222800');
    assert.ok(html.length>500,view);
    assert.ok(!html.includes('undefined'),view);
    assert.ok(!html.includes('NaN'),view);
  }
  const html=ui.renderView('stock','222800');
  assert.ok(html.includes('2026Q3'));
  assert.ok(html.includes('2026Q4'));
  assert.ok(html.includes('동일 증권사'));
});

test('search and sector filters find stocks outside displayed candidate caps', {skip:!fs.existsSync(path.join(__dirname,'../test_output/scenario-public.test.json'))},()=>{
  ui.setData(JSON.parse(fs.readFileSync(path.join(__dirname,'../test_output/scenario-public.test.json'),'utf8')));
  Object.assign(ui.state,{query:'심텍',sector:'all',eligible:false,track:'all'});
  assert.ok(ui.filteredStocks().some(s=>s.ticker==='222800'));
  Object.assign(ui.state,{query:'007810'});
  assert.deepEqual(ui.filteredStocks().map(s=>s.ticker),['007810']);
  Object.assign(ui.state,{query:'',sector:'unlikely-sector'});
  assert.deepEqual(ui.filteredStocks(),[]);
});

function browserRuntime(fetch, hash='#overview'){
  const elements=new Map(),listeners=new Map();
  const element=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',addEventListener(){},focus(){},insertAdjacentHTML(_where,text){this.innerHTML+=text;}});return elements.get(id);};
  const context={fetch,location:{hash},document:{getElementById:element,querySelectorAll:()=>[],addEventListener:(name,fn)=>listeners.set(name,fn)},window:{addEventListener(){},scrollTo(){}},console};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../scenarios.js'),'utf8'),context);
  return {context,elements,listeners,element};
}

test('failed initial load offers retry without fabricated empty-market findings',async()=>{
  const runtime=browserRuntime(async()=>{throw new Error('network failure');});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(runtime.element('content').innerHTML,/분석 자료를 불러오지 못했습니다/);
  assert.match(runtime.element('content').innerHTML,/다시 불러오기/);
  assert.ok(!runtime.element('content').innerHTML.includes('현재 표시할 후보가 없습니다'));
});

test('internal company links update the route explicitly, including embedded browsers',()=>{
  const runtime=browserRuntime(async()=>{throw new Error('offline');});
  let prevented=false;
  const target={dataset:{},hasAttribute:()=>false,getAttribute:()=> '#stock/222800'};
  runtime.listeners.get('click')({target:{closest:()=>target},preventDefault(){prevented=true;}});
  assert.equal(runtime.context.location.hash,'#stock/222800');
  assert.equal(prevented,true);
});

test('mismatched detail generations fail closed instead of mixing estimates',async()=>{
  const board={meta:{version:'test',priceDate:'2026-09-21',generatedAt:'2026-09-23T02:00:00+09:00',detailBase:'scenario-stocks/abc',snapshotId:'A',engineHash:'1'},stocks:{'222800':{name:'심텍'}},scenarios:{up:{},range:{},down:{}}};
  const runtime=browserRuntime(async url=>({ok:true,json:async()=>url==='scenario-board.json'?board:url.includes('22.json')?{snapshotId:'B',engineHash:'1',stocks:{'222800':{}}}:{} }), '#stock/222800');
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(runtime.element('content').innerHTML,/상세 자료를 불러오지 못했습니다/);
});
