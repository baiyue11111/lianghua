import {createRoot} from 'react-dom/client';
import {createDisplay} from '@display/core';
import {DisplayProvider,useOperation,useValue} from '../src/interaction/model';
import {action} from '../src/interaction/action';

let fieldRenders=0,recordsRenders=0;
const core=createDisplay({
 operations:[{operation:'find',mode:'read',input_schema:'urn:test:input',response_schemas:['urn:test:output']}],
 schemas:{'urn:test:input':{type:'object'},'urn:test:output':{type:'object'}},
 backend:{send:async request=>[{
  message_id:'fixture',correlation_id:request.request_id,kind:'collection',
  type:'urn:test:records',schema:'urn:test:output',data:{rows:[{equity:120}]},
 }]},
});
function Field(){
 const [value]=useValue('form','query','');
 fieldRenders++;
 return <p>查询：<span id="query">{value}</span></p>;
}
function Records(){
 const result=useOperation<{rows:{equity:number}[]}>('archive','find');
 recordsRenders++;
 return <p>权益：{result.value?.rows[0].equity??'读取中'}</p>;
}
const frame=()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve())));
async function run(){
 const output=document.querySelector('output')!;
 const lines:string[]=[];
 try{
  await core.dispatch(action('edit','form',{query:''}));
  await frame();
  if(!core.snapshot().targets.archive?.values.find)throw Error('未收到测试数据');
  const fieldBefore=fieldRenders,recordsBefore=recordsRenders;
  await core.dispatch(action('edit','task',{running:true}));await frame();
  if(fieldRenders!==fieldBefore||recordsRenders!==recordsBefore)throw Error('无关区域变化触发重绘');
  lines.push('通过：无关区域变化不重绘字段和记录');
  await core.dispatch(action('edit','form',{other:'changed'}));await frame();
  if(fieldRenders!==fieldBefore)throw Error('同区域其他字段变化触发重绘');
  lines.push('通过：同区域其他字段变化不重绘');
  await core.dispatch(action('edit','form',{query:'更新成功'}));await frame();
  if(document.querySelector('#query')?.textContent!=='更新成功'||fieldRenders<=fieldBefore)throw Error('相关字段没有更新');
  if(recordsRenders!==recordsBefore)throw Error('编辑字段触发归档重绘');
  lines.push('通过：相关字段更新可见，归档不重绘');
  output.textContent=lines.join('\n');
 }catch(error){output.textContent=lines.concat('失败：'+String(error)).join('\n')}
}
const root=createRoot(document.getElementById('root')!);
root.render(
 <DisplayProvider value={core}><h1>交互状态测试</h1><Field/><Records/>
  <button onClick={()=>void run()}>运行测试</button><output style={{display:'block',whiteSpace:'pre-wrap'}}/>
 </DisplayProvider>,
);
if(import.meta.hot)import.meta.hot.dispose(()=>{root.unmount();core.dispose()});
