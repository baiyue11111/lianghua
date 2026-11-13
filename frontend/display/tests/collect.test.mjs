import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
await build({entryPoints:[new URL('../src/collect.ts',import.meta.url).pathname],outfile:new URL('../dist/collect.mjs',import.meta.url).pathname,bundle:true,platform:'node',format:'esm'});
const {collect}=await import('../dist/collect.mjs');
const input={selector:'TEST:[2020-01-01,2020-01-02]'};
test('interaction submits one intent and shows the structured result',async()=>{
 const calls=[],events=[];
 const result=await collect({...input,codes:'A,C',fields:'close,amount'}, {signal:new AbortController().signal,progress:event=>events.push(event),call:async(op,data)=>{
  calls.push({op,data});return {market:'TEST',outcome:'succeeded',jobs:[]};
 }});
 assert.deepEqual(calls,[{op:'collection.run',data:{market:'TEST',command:'range',start:'2020-01-01',end:'2020-01-02',codes:['A','C'],fields:['close','amount']}}]);
 assert.equal(result.outcome,'succeeded');assert.equal(events.length,0);
});
test('failed securities remain visible before the command fails',async()=>{
 const events=[];
 await assert.rejects(collect(input,{signal:new AbortController().signal,progress:event=>events.push(event),call:async()=>({market:'TEST',outcome:'failed',error:'B: connection closed',failures:[{code:'B',error:'connection closed'}]})}),/B: connection closed/);
 assert.deepEqual(events[0].failures,[{code:'B',error:'connection closed'}]);
});
test('collection cancellation prevents submitting another intent',async()=>{
 const controller=new AbortController();controller.abort();let calls=0;
 await assert.rejects(collect(input,{signal:controller.signal,progress:()=>{},call:async()=>{calls++;}}),/会话已停止/);
 assert.equal(calls,0);
});
test('automatic controls submit one intent and return without a timer',async()=>{
 for(const [command,operation,data] of [
  ['auto-on','collection.set_automatic',{market:'TEST',enabled:true}],
  ['auto-off','collection.set_automatic',{market:'TEST',enabled:false}],
  ['status','collection.automatic',{market:'TEST'}],
 ]) {
  const calls=[];
  const result=await collect({selector:'TEST:'+command},{signal:new AbortController().signal,progress:()=>{},call:async(op,input)=>{
   calls.push({op,input});return {enabled:command==='auto-on',status:'idle'};
  }});
  assert.deepEqual(calls,[{op:operation,input:data}]);
  assert.equal(result.status,'idle');
 }
});
test('latest can use a completed-session cutoff',async()=>{
 const calls=[];
 await collect({selector:'TEST:latest',end:'2026-11-10'}, {signal:new AbortController().signal,progress:()=>{},call:async(operation,data)=>{
  calls.push({operation,data});return {outcome:'succeeded'};
 }});
 assert.deepEqual(calls,[{operation:'collection.run',data:{market:'TEST',command:'latest',end:'2026-11-10'}}]);
});
test('old foreground auto and interval options are rejected',async()=>{
 for(const values of [{selector:'TEST:auto'},{selector:'TEST:auto-on',interval:'1d'}]){
  await assert.rejects(collect(values,{signal:new AbortController().signal,progress:()=>{},call:async()=>{throw Error('must not call');}}));
 }
});
