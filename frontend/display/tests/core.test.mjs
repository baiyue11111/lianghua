import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createDisplay,validateWire} from '../dist/core.mjs';
const samples=JSON.parse(readFileSync(new URL('./wire.json',import.meta.url)));
for(const item of samples)test('wire: '+item.name,()=>assert.equal(validateWire(item.model,item.value),item.valid));
const spec={operation:'save',input_schema:'urn:test:input',response_schemas:['urn:test:out'],mode:'write'};
const msg=(request,data={},kind='resource')=>({protocol:'lianghua.ui',version:1,message_id:request.request_id+'-m',correlation_id:request.request_id,kind,type:'urn:test:type',schema:'urn:test:out',data});
const action=(kind='invoke',operation='save',data={})=>({action_id:'a',target:'form',kind,operation,data});
const options={operations:[spec,{...spec,operation:'find',mode:'read'}],schemas:{'urn:test:input':{type:'object'},'urn:test:out':{} }};
test('not connected fails explicitly without a fake result',async()=>{const core=createDisplay(options);await core.dispatch(action());assert.equal(core.snapshot().targets.form.problems[0].code,'not_connected');});
test('edit invalidates checks; failed guard sends nothing',async()=>{
 let sent=0;const core=createDisplay({...options,operations:[{...spec,operation:'check'},{...spec,requires:'check'}],backend:{send:async r=>{sent++;return[msg(r,{ready:true})]}}});
 await core.dispatch(action('invoke','save'));assert.equal(sent,0);
 await core.dispatch(action('invoke','check'));await core.dispatch(action());assert.equal(sent,2);
 await core.dispatch(action('edit',undefined,{amount:'12.50'}));await core.dispatch(action());assert.equal(sent,2);
});
test('older reads never overwrite new query',async()=>{
 const replies=[];const core=createDisplay({...options,backend:{send:r=>new Promise(resolve=>replies.push([r,resolve]))}});
 const first=core.dispatch(action('invoke','find',{q:'old'})),second=core.dispatch(action('invoke','find',{q:'new'}));
 replies[1][1]([msg(replies[1][0],{value:'new'})]);await second;replies[0][1]([msg(replies[0][0],{value:'old'})]);await first;
 assert.equal(core.snapshot().targets.form.messages[0].data.value,'new');
});
test('duplicate pending writes do not send twice',async()=>{
 let resolve,request,sent=0;const core=createDisplay({...options,backend:{send:r=>{sent++;request=r;return new Promise(r=>resolve=r)}}});
 const first=core.dispatch(action());await core.dispatch(action());assert.equal(sent,1);resolve([msg(request)]);await first;
});
test('failure is visible and is never retried',async()=>{let n=0;const core=createDisplay({...options,backend:{send:async()=>{n++;throw Error('offline')}}});await core.dispatch(action());assert.equal(n,1);assert.equal(core.snapshot().targets.form.operations.save.status,'failed');});
test('eight message kinds are delivered; custom handler must be registered',async()=>{
 for(const kind of ['resource','collection','series','document','operation','event','problem','custom']){
 const core=createDisplay({...options,handlers:{'urn:test:type':()=>{}},backend:{send:async r=>[msg(r,kind==='problem'?{type:'urn:test',code:'bad',title:'Bad'}:{amount:'9007199254740993.10'},kind)]}});
 await core.dispatch(action());assert.equal(core.snapshot().targets.form.messages[0].kind,kind);
 }
 const core=createDisplay({...options,backend:{send:async r=>[msg(r,{},'custom')]}});await core.dispatch(action());assert.equal(core.snapshot().targets.form.problems[0].code,'unsupported');
});
test('dispose stops notifications, polling and late replies',async()=>{
 let finish,r,n=0;const core=createDisplay({...options,backend:{send:x=>{r=x;return new Promise(y=>finish=y)}}});const unsubscribe=core.subscribe(()=>n++);
 const pending=core.dispatch(action());const before=n;unsubscribe();core.dispose();finish([msg(r)]);await pending;assert.equal(n,before);await assert.rejects(core.dispatch(action()),/closed/);
});
test('unsolicited events require explicit target binding',()=>{
 const core=createDisplay({...options,events:{'urn:test:type':'form'},handlers:{'urn:test:type':()=>{}}});
 core.receive({...msg({request_id:'push'}, {status:'running'},'event'),correlation_id:null});assert.equal(core.snapshot().targets.form.messages[0].data.status,'running');
});
test('a failed check never authorizes execution',async()=>{
 let sent=0;const core=createDisplay({...options,operations:[{...spec,operation:'check',ready:messages=>messages[0].data.ready===true},{...spec,requires:'check'}],backend:{send:async r=>{sent++;return[msg(r,{ready:false})]}}});
 await core.dispatch(action('invoke','check'));await core.dispatch(action());assert.equal(sent,1);assert.equal(core.snapshot().targets.form.problems[0].code,'check_required');
});
test('input and output schemas, response correlation and snapshot immutability are enforced',async()=>{
 for(const response of [(r)=>[{...msg(r),correlation_id:'wrong'}],(r)=>[{...msg(r),schema:'urn:other'}],(r)=>[{...msg(r),data:()=>1}]]){
  const core=createDisplay({...options,backend:{send:async r=>response(r)}});await core.dispatch(action());assert.equal(core.snapshot().targets.form.operations.save.status,'failed');
 }
 const core=createDisplay(options);await core.dispatch(action('edit',undefined,{amount:'12.50'}));
 assert.throws(()=>{core.snapshot().targets.form.values.amount='0'});assert.equal(core.snapshot().targets.form.values.amount,'12.50');
});
test('polling stops after failure and disposal',async()=>{
 let sent=0;const core=createDisplay({...options,operations:[{...spec,mode:'read',poll:5}],backend:{send:async r=>{if(++sent===2)throw Error('offline');return[msg(r,{task:'running'})]}}});
 await core.dispatch(action());await new Promise(resolve=>setTimeout(resolve,35));assert.equal(sent,2);core.dispose();
 let calls=0;const next=createDisplay({...options,operations:[{...spec,mode:'read',poll:5}],backend:{send:async r=>{calls++;return[msg(r)]}}});
 await next.dispatch(action());next.dispose();await new Promise(resolve=>setTimeout(resolve,20));assert.equal(calls,1);
});
test('push routes through the registered receiver without another public action channel',()=>{
 const core=createDisplay({...options,events:{'urn:test:type':'form'}});
 core.receive({...msg({request_id:'push'},{amount:'12.50'},'event'),correlation_id:null});
 assert.equal(core.snapshot().targets.form.messages[0].data.amount,'12.50');
 assert.throws(()=>core.receive({...msg({request_id:'push'}),type:'urn:unknown'}),/未注册/);
});
test('the core exposes execution availability and invalidates it on edit',async()=>{
 const core=createDisplay({...options,operations:[{...spec,operation:'check'},{...spec,requires:'check'}],backend:{send:async r=>[msg(r,{ready:true})]}});
 await core.dispatch(action('invoke','check'));assert.equal(core.snapshot().targets.form.operations.save.available,true);
 await core.dispatch(action('edit',undefined,{field:'changed'}));assert.equal(core.snapshot().targets.form.operations.save.available,false);
});
