import {useValue,useOperation,useSlot,useSession} from '../interaction/model';
import ReactECharts from 'echarts-for-react';
import type {BacktestInput,BacktestResult,Preflight,StrategyCatalog,StrategySpec} from '@display/workbench/port';
import {backtestInput,comparisonIssue,parameterValues} from '@display/workbench/strategy-model';
import type {Completed} from '@display/workbench/strategy-model';

const metrics=[['total_return','累计收益'],['max_drawdown','最大回撤'],['sharpe','夏普']] as const;
const metric=(run:BacktestResult,key:string)=>{
  const value=run.metrics[key];
  return typeof value==='number'&&Number.isFinite(value)?key==='sharpe'?value.toFixed(3):`${(value*100).toFixed(2)}%`:'未提供';
};

export default function StrategyLab({spec,availability,inspect}:{spec:StrategySpec;availability:StrategyCatalog['availability'];inspect:(title:string,value:unknown)=>void}) {
  const target='strategy:'+spec.module;
  useSession(target);
  const first=availability.datasets[0];
  const [values,setValues]=useValue<Record<string,string|boolean>>(target,'parameters',()=>parameterValues(spec));
  const [capital,setCapital]=useValue(target,'capital','100000'),[start,setStart]=useValue(target,'start',first?.start??''),[end,setEnd]=useValue(target,'end',first?.end??''),[dataset,setDataset]=useValue(target,'dataset',first?.id??'');
  const [baseline,setBaseline]=useValue<Completed|null>(target+':comparison','baseline',null),[latest,setLatest]=useValue<Completed|null>(target+':comparison','latest',null);
  const form={spec,values,capital,start,end,dataset};
  const checking=useOperation<Preflight>(target,'workbench.preflight',form,true,false);
  const running=useOperation<BacktestResult>(target,'workbench.backtest',form,true,false);
  const slot=useSlot(target);
  const currentCheck=slot.operations['workbench.preflight']?.revision===slot.revision?checking.value??null:null;
  const busy=checking.status==='pending'?'check':running.status==='pending'?'run':null;
  const failure=checking.error||running.error;
  const preflight=()=>checking.invoke(),run=()=>running.invoke();
  let input:BacktestInput|null=null,invalid='';
  try{input=backtestInput(spec,values,capital,start,end,dataset)}catch(reason){invalid=(reason as Error).message}
  const identity=input?JSON.stringify({...input,initial_cash:capital.trim()}):'';
  const completed=baseline&&latest&&baseline.result.run_id!==latest.result.run_id?[baseline,latest]:latest?[latest]:baseline?[baseline]:[];
  const comparison=completed.length===2?comparisonIssue(completed[0],completed[1]):'';
  const repeated=completed.length===2&&JSON.stringify(completed[0].input)===JSON.stringify(completed[1].input);
  const issueList=currentCheck?.issues??availability.issues??[];
  return <section className="panel strategy-lab" aria-label="参数回测">
    <div className="panelhead"><h2>参数回测</h2><span className={`pill ${currentCheck?.ready?'':'warn'}`}>{busy==='run'?'正在回测':busy==='check'?'正在检查':currentCheck?currentCheck.ready?'可以运行':'运行已阻止':'待检查'}</span></div>
    {failure&&<div className="lab-alert" role="alert">{failure}</div>}
    <>
      <div className="lab-layout">
        <form className="lab-form" onSubmit={event=>{event.preventDefault();void preflight()}}>
          <fieldset disabled={busy!==null}>
            <div className="lab-fields">
              <label>投入资金<input aria-label="投入资金" type="number" min="0" step="any" required value={capital} onChange={event=>setCapital(event.target.value)}/></label>
              <label>历史数据<select aria-label="回测历史数据" value={dataset} onChange={event=>setDataset(event.target.value)}>{!availability.datasets.length&&<option value="">无已验收数据</option>}{availability.datasets.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
              <label>开始日期<input aria-label="回测开始日期" type="date" required value={start} onChange={event=>setStart(event.target.value)}/></label>
              <label>结束日期<input aria-label="回测结束日期" type="date" required value={end} onChange={event=>setEnd(event.target.value)}/></label>
            </div>
            <div className="lab-section-head"><h3>策略参数</h3><button type="button" className="text" onClick={()=>{setValues(parameterValues(spec))}}>恢复默认</button></div>
            <div className="lab-fields">{spec.parameters.map(field=><label key={field.key} className={field.type==='boolean'?'lab-toggle':''}>
              <span>{field.label}{field.unit?` (${field.unit})`:''}</span>
              {field.type==='boolean'?<input aria-label={field.label} type="checkbox" checked={values[field.key]===true} onChange={event=>setValues({...values,[field.key]:event.target.checked})}/>:
                field.options?<select aria-label={field.label} value={String(values[field.key])} onChange={event=>setValues({...values,[field.key]:event.target.value})}>{field.options.map(option=><option key={String(option)} value={String(option)}>{String(option)}</option>)}</select>:
                <input aria-label={field.label} type={field.type==='string'?'text':'number'} step={field.type==='integer'?1:'any'} min={field.minimum} max={field.maximum} required={field.type!=='string'} value={String(values[field.key])} onChange={event=>setValues({...values,[field.key]:event.target.value})}/>
              }
            </label>)}</div>
          </fieldset>
          {invalid&&<p className="lab-validation">{invalid}</p>}
          <div className="lab-actions"><button type="submit" disabled={!input||busy!==null}>{busy==='check'?'正在检查…':'检查参数与数据'}</button><button className="primary" type="button" onClick={()=>void run()} disabled={!slot.operations['workbench.backtest']?.available||busy!==null}>{busy==='run'?'正在回测…':baseline?'运行修改参数':'运行基准'}</button></div>
        </form>
        <div className="lab-state" aria-live="polite">
          <h3>{currentCheck?currentCheck.ready?'检查通过':'暂不能回测':'运行检查'}</h3>
          {issueList.length>0&&<ul className="lab-issues">{issueList.map(issue=><li key={issue.code}>{issue.message}</li>)}</ul>}
          {currentCheck&&<>
            <dl className="lab-facts"><div><dt>历史长度</dt><dd>{currentCheck.requirements.history_bars} 根日线</dd></div><div><dt>价格口径</dt><dd>{currentCheck.requirements.price_basis==='adjusted'?'复权价格':currentCheck.requirements.price_basis}</dd></div><div><dt>数据字段</dt><dd>{currentCheck.requirements.fields.join(' · ')}</dd></div></dl>
            <button className="text" onClick={()=>inspect('本次参数与数据检查',{input,preflight:currentCheck})}>有效参数与配置身份 ↗</button>
          </>}
          {!currentCheck&&!issueList.length&&<div className="empty">填写参数后检查</div>}
        </div>
      </div>
      {completed.length>0&&<div className="lab-results">
        <div className="panelhead"><h2>回测对比</h2><div className="actions">{identity!==JSON.stringify((latest??baseline)?.input)&&<span className="pill warn">当前参数未运行</span>}<button onClick={()=>{setBaseline(latest);setLatest(null)}} disabled={!latest||baseline?.result.run_id===latest.result.run_id}>设为新基准</button></div></div>
        <div className="lab-result-cards">{completed.map((item,index)=><article className="lab-result-card" key={item.result.run_id}>
          <div className="lab-result-head"><h3>{index===0?'基准':repeated?'同参数复跑':'修改后'}</h3><span className="pill">{item.result.qualification==='qualified'?'已验收':item.result.qualification==='unverified'?'未验收':item.result.qualification}</span></div>
          <div className="lab-result-metrics">{metrics.map(([key,name])=><div key={key}><span>{name}</span><strong>{metric(item.result,key)}</strong></div>)}</div>
          <div className="lab-run-facts"><span>资金 {Number(item.input.initial_cash).toLocaleString('zh-CN')}</span><span>{item.input.start} — {item.input.end}</span></div>
          <dl className="lab-parameter-record">{spec.parameters.map(field=><div key={field.key}><dt>{field.label}</dt><dd>{typeof item.result.parameters[field.key]==='boolean'?item.result.parameters[field.key]?'开启':'关闭':String(item.result.parameters[field.key])}</dd></div>)}</dl>
          <button className="text" onClick={()=>inspect('回测参数与来源',{request:item.input,run_id:item.result.run_id,parameters:item.result.parameters,effective_config:item.result.effective_config,config_hash:item.result.config_hash,code_hash:item.result.code_hash,data_hash:item.result.data_hash,execution_hash:item.result.execution_hash})}>参数与运行身份 ↗</button>
        </article>)}</div>
        {comparison?<div className="lab-alert" role="status">不能直接比较：{comparison}</div>:<ReactECharts option={{
          color:['#6b7280','#007aff'],tooltip:{trigger:'axis'},legend:{bottom:0},grid:{left:75,right:30,top:30,bottom:65},
          xAxis:{type:'time'},yAxis:{type:'value',scale:true,name:'初始资金 = 1'},
          series:completed.map((item,index)=>({name:index===0?'基准':repeated?'同参数复跑':'修改后',type:'line',showSymbol:false,data:item.result.equity.map(point=>[point.at,point.equity/Number(item.input.initial_cash)])})),
        }} style={{height:350}}/>}
      </div>}
    </>
  </section>;
}
