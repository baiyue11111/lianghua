import type { RecordData } from '@display/workbench/port';
import {show} from '@display/workbench/format';
import {label,localized} from '@display/workbench/locale';
export function Json({value,raw=false}:{value:unknown;raw?:boolean}) {
  if(raw)return <pre className="evidence">{JSON.stringify(value,null,2)}</pre>;
  return <><pre className="evidence">{JSON.stringify(localized(value),null,2)}</pre><details><summary>查看原始字段与标识（未翻译）</summary><pre className="evidence">{JSON.stringify(value,null,2)}</pre></details></>;
}
export function RecordTable({rows,offset=0,onInspect}:{rows:RecordData[];offset?:number;onInspect:(row:RecordData,index:number)=>void}) {
  if(!rows.length) return <div className="empty">当前查询没有记录</div>;
  const keys=[...new Set(rows.flatMap(Object.keys))];
  return <div className="tablewrap"><table><thead><tr>{keys.map(k=><th key={k} title={k}>{label(k)}</th>)}<th>审核</th></tr></thead><tbody>{rows.map((row,i)=><tr key={offset+i}>{keys.map(k=><td key={k}>{k==='url'&&typeof row[k]==='string'?<a href={row[k] as string} target="_blank" rel="noreferrer">原文 ↗</a>:show(row[k],k)}</td>)}<td><button onClick={()=>onInspect(row,offset+i)}>核对 ↗</button></td></tr>)}</tbody></table></div>;
}
export function Pages({offset,total,change}:{offset:number;total:number;change:(offset:number)=>void}) {
  return <div className="table-bottom actions"><button disabled={offset===0} onClick={()=>change(Math.max(0,offset-50))}>上一页</button><span>{offset+1}–{Math.min(offset+50,total)} / {total} 条</span><button disabled={offset+50>=total} onClick={()=>change(offset+50)}>下一页</button></div>;
}
