import {useValue,useOperation} from '../interaction/model';
import type {Source,SyncInput} from '@display/workbench/port';
import {show} from '@display/workbench/format';

function sourceStatus(source:Source) {
  if(source.integrity_error)return '校验失败';
  if(source.job?.status==='running')return '同步中';
  if(source.job?.status==='failed')return source.snapshot?'失败 · 快照未更新':'同步失败';
  if(source.job?.status==='succeeded')return '同步完成';
  return source.snapshot?'已有快照':'未同步';
}

function SourceCard({source,open}:{source:Source;open:(source:Source)=>void}) {
  const target='source:'+source.id;
  const [input,setInput]=useValue<SyncInput>(target,'input',{codes:source.defaults.codes??[],start:source.defaults.start??'',end:source.defaults.end??'',keyword:source.defaults.keyword??''});
  const [interval,setIntervalSeconds]=useValue(target,'interval',source.schedule?.interval_seconds??60);
  const syncing=useOperation(target,'workbench.sync',{id:source.id,input},true,false);
  const [enabled,setEnabled]=useValue(target,'enabled',!source.schedule?.enabled);
  const scheduling=useOperation(target,'workbench.schedule',{id:source.id,enabled,interval_seconds:interval,input},true,false);
  const busy=syncing.status==='pending'||scheduling.status==='pending';
  const syncError=(syncing.error||scheduling.error||source.integrity_error)??(source.job?.status==='failed'?source.job.error:null);
  const sync=()=>syncing.invoke();
  function schedule(value:boolean){setEnabled(value);return scheduling.invoke({id:source.id,enabled:value,interval_seconds:interval,input});}
  return <section className="panel sync-card">
    <div className="top">
      <h2>{source.name}</h2>
      <div className="actions"><span className="pill">{source.frequency}</span><span className={'pill '+(source.job?.status==='failed'||source.integrity_error?'danger':'')}>{sourceStatus(source)}</span></div>
    </div>
    <div className="sync-data">
      <div><label>数据截至</label><strong>{source.snapshot?.data_as_of??'未知'}</strong></div>
      <div><label>采集时间</label><strong>{source.snapshot?.collected_at??'未采集'}</strong></div>
      <div><label>记录数</label><strong>{source.records}</strong></div>
      <div><label>最近完成</label><strong>{source.job?.finished_at??'未完成'}</strong></div>
    </div>

    <div className="sync-fields">
      <label>起始日期<input aria-label={`${source.name}起始日期`} type="date" value={input.start} onChange={e=>setInput({...input,start:e.target.value})}/></label>
      <label>截止日期<input aria-label={`${source.name}截止日期`} type="date" value={input.end} onChange={e=>setInput({...input,end:e.target.value})}/></label>
      {source.defaults.codes&&<label>查询代码<input aria-label={`${source.name}代码`} value={input.codes.join(',')} onChange={e=>setInput({...input,codes:e.target.value.split(',').map(s=>s.trim()).filter(Boolean)})}/></label>}
      {source.defaults.keyword!==undefined&&<label>标题关键字<input aria-label={`${source.name}关键字`} value={input.keyword} onChange={e=>setInput({...input,keyword:e.target.value})}/></label>}
    </div>

    {syncError&&<div className="source-errors" role="alert">{syncError}</div>}
    <div className="actions sync-actions">
      <button className="primary" disabled={busy||source.job?.status==='running'} onClick={()=>void sync()}>{busy?'提交中…':'同步数据'}</button>
      <button disabled={!source.snapshot||!!source.integrity_error} onClick={()=>open(source)}>查看记录</button>
    </div>

    <div className="schedule-row">
      <label>同步间隔（秒）<input aria-label={`${source.name}同步间隔`} type="number" min="30" value={interval} onChange={e=>setIntervalSeconds(Number(e.target.value))}/></label>
      <button disabled={busy||interval<30} onClick={()=>void schedule(!source.schedule?.enabled)}>{source.schedule?.enabled?'关闭定时同步':'启用定时同步'}</button>
      <span className={'pill '+(source.schedule?.error?'danger':'')}>{source.schedule?.error?'定时失败':source.schedule?.enabled?`每 ${source.schedule.interval_seconds} 秒`:'未开启'}</span>
    </div>
    {source.schedule?.error&&<p className="status-error" role="alert">{source.schedule.error}</p>}
    <details className="source"><summary>范围与快照标识</summary><p className="mono">{source.snapshot?.snapshot_id??'无快照'}</p><p>{show(source.snapshot?.scope)}</p></details>
  </section>;
}

export default function Sources({open}:{error:(s:string)=>void;open:(s:Source)=>void}) {
  const result=useOperation<Source[]>('sources','workbench.sources');
  const sources=result.value??[];
  const refresh=async()=>{await result.invoke();};
  return <>
    <div className="pagehead"><h1>数据同步</h1><div className="actions"><span className="pill">{sources.length} 类来源</span><button onClick={()=>void refresh()}>刷新状态</button></div></div>
    {result.error&&<div role="alert" className="notice status-error">{result.error}</div>}<section className="source-facts" aria-label="数据口径">
      <div><span>报价时间</span><strong>供应商最新</strong></div>
      <div><span>停市期间</span><strong>不更新</strong></div>
      <div><span>历史日线</span><strong>未复权</strong></div>
      <div><span>财报内容</span><strong>原文目录</strong></div>
      <div><span>财务数值</span><strong>未解析</strong></div>
      <div><span>报告期</span><strong>未知</strong></div>
    </section>
    <div className="sync-grid">{sources.map(s=><SourceCard key={s.id} source={s} open={open}/>)}</div>
  </>;
}
