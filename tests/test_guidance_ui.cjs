const {test}=require('node:test');
const assert=require('node:assert/strict');
const {render}=require('../guidance.js');
test('missing guidance explicitly keeps consensus',()=>{assert.match(render({status:{status:'설정필요'},rows:[]}),/감점 없음/)});
test('escaping and independent guidance fields',()=>{
 const html=render({rows:[{name:'<script>',period:'2026',basis:'consolidated',sales:{low:1e10,high:1.2e10,mid:1.1e10},tags:['NEW','UP'],consensus:{sales:1e10,as_of:'2026-01-01'},guidance_vs_consensus_gap:{sales:.1},source_url:'javascript:alert(1)',published_at:'2026-01-02',is_correction:true}]});
 assert.match(html,/&lt;script&gt;/);assert.match(html,/100~120/);assert.match(html,/\+10.0%/);assert.match(html,/정정/);assert.ok(!html.includes('javascript:'));
});
