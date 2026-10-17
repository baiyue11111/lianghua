import {useOperation,useValue} from '../interaction/model';
import type {StrategyCatalog} from '@display/workbench/port';
import {batchRows} from './model';
import {errorMessage} from '../shared/format';
import Fields from '../shared/Fields';
export default function Backtest({refresh,catalog}:{refresh:()=>Promise<unknown>;catalog?:StrategyCatalog}){
 const [config,setConfig]=useValue('cli-form','config','');
 const [submitted,setSubmitted]=useValue('cli-form','submitted','');
 const operation=useOperation<unknown>('cli-result','cli.backtest',{config:submitted},true,false);
 const busy=operation.status==='pending',rows=batchRows(operation.value,catalog);
 async function run(){setSubmitted(config.trim());await operation.invoke({config:config.trim()});await refresh();}
 return <section className="panel" data-od-id="cli-backtest">
  <div className="panelhead"><h2>配置文件回测</h2></div>
  <form className="config-form" onSubmit={event=>{event.preventDefault();void run()}}>
   <label>服务器配置路径<input data-od-id="config-path" aria-label="服务器配置路径" type="text" autoComplete="off" required value={config} onChange={event=>setConfig(event.target.value)}/></label>
   <button data-od-id="run-backtest" className="primary" disabled={busy||!config.trim()}>{busy?'回测运行中':'运行回测'}</button>
  </form>
  {operation.error&&<div className="notice status-error" role="alert">{errorMessage(operation.error)}</div>}
  {operation.status==='succeeded'&&(rows.length?<div className="batch-results"><h3>区间结果</h3>{rows.map((row,i)=><section className="interval" key={i}><h3>{row.name}</h3><Fields items={row.fields}/><details><summary>区间配置</summary><Fields items={row.config}/></details></section>)}</div>:<div className="notice" role="status">回测已返回，区间结果未提供</div>)}
 </section>;
}
