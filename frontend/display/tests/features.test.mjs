import test from 'node:test';
import assert from 'node:assert/strict';
import {createDisplay} from '../dist/core.mjs';
import {features,outputSchema} from '../dist/features.mjs';
const spec={module:'strategies.test',strategy_id:'test',defaults:{count:8},parameters:[{key:'count',label:'数量',type:'integer',default:8,minimum:1}]};
const form={spec,values:{count:'8'},capital:'9007199254740993.10',start:'2020-01-01',end:'2020-01-02',dataset:'verified'};
let serial=0;const invoke=(operation,data=form)=>({action_id:'a'+ ++serial,kind:'invoke',target:'strategy:strategies.test',operation,data});
const message=(r,data)=>({message_id:r.request_id+'-m',kind:'resource',type:'urn:test',schema:outputSchema(r.operation),correlation_id:r.request_id,data});
test('feature check and execution use identical input, preserve money text and register completed archive',async()=>{
 const requests=[];const core=createDisplay({...features(),backend:{send:async r=>{requests.push(r);return [message(r,r.operation.endsWith('preflight')?{ready:true}: {id:'new',run_id:'new',metrics:{}})]}}});
 await core.dispatch(invoke('workbench.preflight'));await core.dispatch(invoke('workbench.backtest'));
 assert.equal(requests.length,2);assert.deepEqual(requests[0].data,requests[1].data);assert.equal(requests[0].data.initial_cash,form.capital);
 assert.equal(core.snapshot().targets['research:archives'].values['workbench.runs'][0][0].run_id,'new');
 assert.equal(core.snapshot().targets.research.values.current,'new');
});
test('invalid numeric parameter is rejected before backend call',async()=>{
 let calls=0;const core=createDisplay({...features(),backend:{send:async()=>{calls++;return[]}}});
 await core.dispatch(invoke('workbench.preflight',{...form,values:{count:'1.5'}}));assert.equal(calls,0);assert.equal(core.snapshot().targets['strategy:strategies.test'].problems[0].code,'invalid_input');
});
test('a caller cannot execute changed input by omitting an edit action',async()=>{
 const requests=[];const core=createDisplay({...features(),backend:{send:async request=>{requests.push(request);return[message(request,{ready:true})]}}});
 await core.dispatch(invoke('workbench.preflight'));
 await core.dispatch(invoke('workbench.backtest',{...form,values:{count:'99'},dataset:'other'}));
 assert.equal(requests.length,1);
 assert.equal(core.snapshot().targets['strategy:strategies.test'].problems[0].code,'check_required');
});
test('opening existing evidence restores its review in the interaction model',async()=>{
 const core=createDisplay({...features(),backend:{send:async r=>[message(r,{reviews:[{status:'correct',note:'already checked'}]})]}});
 await core.dispatch({action_id:'sample',kind:'invoke',target:'audit:evidence',operation:'workbench.evidence',data:{id:'snapshot',index:0}});
 assert.equal(core.snapshot().targets.audit.values.status,'correct');assert.equal(core.snapshot().targets.audit.values.note,'already checked');
});
