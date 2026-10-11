import {useEffect} from 'react';
import {useValue,useOperation,useDisplay} from '../interaction/model';
import {action} from '../interaction/action';
import type {Source,Snapshot,Evidence,Review} from '@display/workbench/port';
import {Json,Pages,RecordTable} from './shared';
export default function Audit({initial,onSync}:{error:(s:string)=>void;initial:string;onSync:()=>void}) {
  const [source]=useValue('audit','source',initial);
  const port=useDisplay();
  const setSource=(value:string)=>{void port.dispatch(action('edit','audit',{source:value,index:null,offset:0}));};
  useEffect(()=>{if(initial)void port.dispatch(action('edit','audit',{source:initial,index:null,offset:0}));},[port,initial]);
  const [offset,setOffset]=useValue('audit','offset',0);
  const [index,setIndex]=useValue<number|null>('audit','index',null);
  const [status,setStatus]=useValue<Review['status']>('audit','status','pending');
  const [note,setNote]=useValue('audit','note','');
  const list=useOperation<Source[]>('audit:sources','workbench.sources');
  const sources=list.value??[];
  const id=sources.find(s=>s.id===source&&!s.integrity_error)?.snapshot?.snapshot_id??'';
  const records=useOperation<Snapshot>('audit:snapshot','workbench.snapshot',{id,offset},!!id);
  const sample=useOperation<Evidence>('audit:evidence','workbench.evidence',{id,index:index??0},!!id&&index!==null);
  const review=useOperation<Review>('audit:review','workbench.review',{snapshot_id:id,record_index:index??0,status,note},true,false);
  const snapshot=records.value??null,evidence=index!==null?sample.value??null:null;
  const saving=review.status==='pending',saved=review.status==='succeeded';
  const failure=list.error||records.error||sample.error||review.error;
  const page=(value:number)=>setOffset(value),inspect=(value:number)=>{setIndex(value);};
  const save=()=>review.invoke(),setEvidence=()=>setIndex(null);
  const selected=sources.find(s=>s.id===source);
  return <><div className="pagehead"><h1>数据审核</h1><button disabled={!snapshot?.records.length} onClick={()=>snapshot&&void inspect(snapshot.offset+Math.floor(Math.random()*snapshot.records.length))}>随机抽样</button></div>{failure&&<div role="alert" className="notice status-error">{failure}</div>}<section className="panel"><div className="audit-controls"><label>数据来源<select aria-label="审核数据来源" value={source} onChange={e=>setSource(e.target.value)}><option value="">选择来源</option>{sources.map(s=><option key={s.id} value={s.id}>{s.name}{s.snapshot?'':' · 无快照'}</option>)}</select></label>{snapshot&&<span className="pill mono">快照 {snapshot.snapshot_id.slice(0,16)} · {snapshot.total} 条</span>}</div>
    {snapshot?<><RecordTable rows={snapshot.records} offset={snapshot.offset} onInspect={(_,index)=>void inspect(index)}/><Pages offset={snapshot.offset} total={snapshot.total} change={n=>void page(n)}/><details className="source"><summary>查询范围与采集时间</summary><Json value={{request:snapshot.request,scope:snapshot.scope,collected_at:snapshot.collected_at,data_as_of:snapshot.data_as_of}}/></details></>:<div className="empty-state"><h2>{selected?.integrity_error?'数据校验失败':'暂无可审计快照'}</h2>{selected?.integrity_error&&<p className="status-error" role="alert">{selected.integrity_error}</p>}<button onClick={onSync}>数据同步</button></div>}</section>
    {evidence&&<div className="backdrop" onClick={()=>setEvidence()}><section className="drawer" role="dialog" aria-modal="true" aria-label="数据样本审核" onClick={e=>e.stopPropagation()}><div className="drawer-head"><h2>实际数据样本审核</h2><button onClick={()=>setEvidence()}>关闭</button></div><div className="drawer-body"><p className="mono">{evidence.snapshot_id}</p><h3>记录 #{evidence.record_index+1}</h3><Json value={evidence.record}/><h3>查询与来源</h3><Json value={{request:evidence.request,collected_at:evidence.collected_at,scope:evidence.scope}}/><details><summary>原始响应</summary><Json raw value={typeof evidence.record.raw_page==='number'?evidence.raw[evidence.record.raw_page]:evidence.raw}/></details><h3>人工核对结论</h3><div className="review-form"><select aria-label="审核结论" value={status} onChange={e=>setStatus(e.target.value as Review['status'])}><option value="pending">待核对</option><option value="correct">正确</option><option value="anomaly">异常</option></select><textarea aria-label="审核备注" placeholder="审核备注" value={note} onChange={e=>setNote(e.target.value)}/><button className="primary" disabled={saving} onClick={()=>void save()}>{saving?'保存中…':'保存审核结论'}</button>{saved&&<span className="pill">已保存</span>}</div></div></section></div>}
  </>;
}
