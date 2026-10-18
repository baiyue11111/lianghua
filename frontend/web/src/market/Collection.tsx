import type {ContinuousFetch,FetchJob} from '@display/lib/market';
import {useOperation} from '../interaction/model';
import {errorMessage,number,status} from '../shared/format';
export default function Collection(){
 const schedule=useOperation<ContinuousFetch>('market:schedule','collection.automatic');
 const task=useOperation<FetchJob>('market:task','collection.current');
 const toggle=useOperation('market:toggle','collection.set_automatic',{},true,false);
 const start=useOperation('market:start','collection.start',{},true,false);
 const stop=useOperation('market:stop','collection.stop',{},true,false);
 const value=schedule.value,job=task.value;
 const busy=toggle.status==='pending'||start.status==='pending'||stop.status==='pending';
 const errors=[schedule.error,task.error,toggle.error,start.error,stop.error,value?.schedule_error,value?.latest_data_error,job?.error].filter(Boolean);
 async function change(enabled:boolean){await toggle.invoke({enabled});await schedule.invoke();}
 async function collect(){await start.invoke({mode:'auto'});await task.invoke();}
 return <section className="collection" data-od-id="collection" aria-label="行情采集">
  <div className="collection-controls">
   <label className="check"><input data-od-id="auto-collection" type="checkbox" role="switch" aria-label="自动采集" checked={value?.enabled??false} disabled={!value||busy} onChange={event=>void change(event.target.checked)}/>自动采集</label>
   <button data-od-id="manual-collection" disabled={!job||job.running||busy} onClick={()=>void collect()}>手动采集</button>
   {job?.running&&<button disabled={busy} onClick={()=>void stop.invoke().then(()=>task.invoke())}>停止采集</button>}
   <span role="status">{job?status(job.status):'读取采集状态中'}</span>
   {job?.running&&<span className="value">{number(job.done,0)} / {number(job.total,0)}</span>}
   {job&&job.status!=='idle'&&job.rows>0&&<span>新增 {number(job.rows,0)} 行</span>}
  </div>
  {!!errors.length&&<div className="notice status-error" role="alert">{errorMessage(errors[0])}</div>}
  {!!job?.failed.length&&<div className="notice status-error">{job.failed.length} 个采集任务失败</div>}
 </section>;
}
