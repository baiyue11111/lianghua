import {useValue} from './interaction/model';
import Research from './research/Research';
import Chart from './market/Chart';
import Collection from './market/Collection';
import Indices from './market/Indices';
import './shared/app.css';
const nav=[{id:'market',label:'行情'},{id:'research',label:'策略研究'}] as const;
export default function Workspace(){
 const [view,setView]=useValue<'market'|'research'>('workspace','page','market');
 return <div className="wb"><div className="app">
  <aside className="sidebar" data-od-id="navigation"><div className="brand">量研</div><nav className="nav" aria-label="主导航">{nav.map(item=><button data-od-id={`nav-${item.id}`} key={item.id} className={view===item.id?'active':''} aria-current={view===item.id?'page':undefined} onClick={()=>setView(item.id)}>{item.label}</button>)}</nav></aside>
  <div className="workspace"><main className="main" data-od-id="workspace">{view==='market'?<><div className="pagehead"><h1 data-od-id="market-title">行情</h1></div><section className="panel market-chart" data-od-id="market-chart"><Chart/></section><Collection/><Indices/></>:<Research/>}</main></div>
 </div></div>;
}
