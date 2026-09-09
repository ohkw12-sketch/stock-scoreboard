const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../saveticker.js'),'utf8');

async function render(data,fail=false){
  const nodes={savetickerstatus:{},savetickerbody:{}};
  vm.runInNewContext(script,{document:{getElementById:id=>nodes[id]},Intl,Number,
    fetch:async()=>({ok:!fail,json:async()=>data}),console:{error(){}}});
  await new Promise(resolve=>setImmediate(resolve));
  return nodes;
}

test('renders direct and sector-linked Korean market news cards',async()=>{
  const n=await render({meta:{status:'갱신',updatedKST:'2026-09-10 15:45',range:'09.09–09.10',windowHours:36,
    authorCount:8,fetchedCount:100,directCount:1,sectorCount:1,selectionRule:'국장 관련만'},items:[
    {title:'삼성전자 협력',scope:'국장 직접',tone:'긍정',publishedKST:'2026-09-10 14:00',summary:'실제 협력 내용',
      marketImpact:'국내 반도체 직접 영향',sectors:['반도체'],source:'로이터',author:'오선',viewCount:1000,url:'https://saveticker.com/news/1'},
    {title:'브렌트유 상승',scope:'국내 섹터 영향',tone:'부정',publishedKST:'2026-09-10 13:00',summary:'유가 100달러 돌파',
      marketImpact:'정유와 항공 비용 영향',sectors:['정유·화학·운송'],source:'블룸버그',author:'오선',viewCount:2000,url:'https://saveticker.com/news/2'},
  ]});
  assert.match(n.savetickerstatus.textContent,/오선/);
  assert.match(n.savetickerbody.innerHTML,/국내 기업·시장 직접 뉴스/);
  assert.match(n.savetickerbody.innerHTML,/국내 업종에 영향이 큰 뉴스/);
  assert.match(n.savetickerbody.innerHTML,/기사 실제 내용/);
  assert.match(n.savetickerbody.innerHTML,/국장 연결/);
  assert.match(n.savetickerbody.innerHTML,/st-card positive/);
  assert.match(n.savetickerbody.innerHTML,/st-card negative/);
});

test('escapes article text and rejects unsafe links',async()=>{
  const n=await render({meta:{},items:[{title:'<img src=x>',scope:'국장 직접',summary:'<b>내용</b>',marketImpact:'영향',
    sectors:[],author:'오선',url:'javascript:alert(1)'}]});
  assert.doesNotMatch(n.savetickerbody.innerHTML,/<img|href="javascript:/);
  assert.match(n.savetickerbody.innerHTML,/&lt;img/);
});

test('load failure stays inside the new board',async()=>{
  const n=await render({},true);
  assert.match(n.savetickerstatus.textContent,/불러오기 실패/);
});
