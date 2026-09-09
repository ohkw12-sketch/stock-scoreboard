const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../issue-spread.js'),'utf8');
async function render(data,fail=false){
  const nodes={issuestatus:{},issuebody:{}};
  vm.runInNewContext(script,{document:{getElementById:id=>nodes[id]},Intl,Date,
    fetch:async()=>({ok:!fail,json:async()=>data})});
  await new Promise(resolve=>setImmediate(resolve));
  return nodes;
}
test('empty board explicitly waits without fabricated candidates',async()=>{
  const n=await render({status:'첫 검증 실행 대기',issues:[]});
  assert.match(n.issuestatus.textContent,/첫 검증/);
  assert.match(n.issuebody.innerHTML,/아직 확인된 이슈가 없습니다/);
});
test('load failure remains isolated',async()=>{
  assert.match((await render({},true)).issuestatus.textContent,/불러오기 실패/);
});
test('issue content is escaped and source protocols are restricted',async()=>{
  const n=await render({status:'검증완료',issues:[{name:'<img src=x onerror=alert(1)>',strength:90,
    leaders:[],candidates:[],evidence:[{url:'javascript:alert(1)',source:'DART'}]}]});
  assert.doesNotMatch(n.issuebody.innerHTML,/<img|href="javascript:/);
  assert.match(n.issuebody.innerHTML,/&lt;img/);
  assert.match(n.issuebody.innerHTML,/실적연결도/);
});
