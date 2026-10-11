import {useCallback} from 'react';
import {useValue} from '../interaction/model';
import {BarChart3,Database,RefreshCw,CandlestickChart} from 'lucide-react';
import type {Source} from '@display/workbench/port';
import Research from './Research';
import Sources from './Sources';
import Audit from './Audit';
import ChartView from '../sections/ChartView';
import {Json} from './shared';
import './workbench.css';
type View='research'|'audit'|'sync'|'chart';
const nav=[{id:'research',label:'策略研究',icon:BarChart3},{id:'audit',label:'数据审核',icon:Database},{id:'sync',label:'数据同步',icon:RefreshCw},{id:'chart',label:'行情 K 线',icon:CandlestickChart}] as const;
export default function Workbench() {
  const [view,setView]=useValue<View>('workspace','view','research'),[message,setMessage]=useValue('workspace','message',''),[initial,setInitial]=useValue('workspace','initial','');
  const [inspection,setInspection]=useValue<{title:string;value:unknown}|null>('workspace','inspection',null);
  const error=useCallback((s:string)=>setMessage(s),[setMessage]);
  function open(s:Source){setInitial(s.id);setView('audit')}
  return <div className="wb"><div className="app"><aside className="sidebar"><div className="brand"><span className="brandmark"><i/><i/><i/></span>量研 <span className="small muted">工作台</span></div><nav className="nav" aria-label="工作区">{nav.map(n=><button key={n.id} className={view===n.id?'active':''} aria-current={view===n.id?'page':undefined} onClick={()=>{setView(n.id);setMessage('')}}><n.icon size={18}/>{n.label}</button>)}</nav></aside><div className="workspace"><header className="toolbar"><div className="crumb"><span className="muted">量化研究 /</span>{nav.find(n=>n.id===view)?.label}</div><div className="toolbar-end"><span className="pill">交互工作台</span><button className="text" onClick={()=>setView('sync')}>数据来源 ↗</button></div></header><main className="main">{message&&<div className="notice status-error" role="alert">{message}<button className="text" onClick={()=>setMessage('')}>关闭提示</button></div>}{view==='research'&&<Research error={error} inspect={(title,value)=>setInspection({title,value})}/>} {view==='sync'&&<Sources error={error} open={open}/>} {view==='audit'&&<Audit error={error} initial={initial} onSync={()=>setView('sync')}/>} {view==='chart'&&<div className="legacy-terminal"><ChartView/></div>}</main></div></div>{inspection&&<div className="backdrop" onClick={()=>setInspection(null)}><section className="drawer" role="dialog" aria-modal="true" aria-label={inspection.title} onClick={e=>e.stopPropagation()}><div className="drawer-head"><h2>{inspection.title}</h2><button onClick={()=>setInspection(null)}>关闭</button></div><div className="drawer-body"><Json value={inspection.value}/></div></section></div>}</div>;
}
