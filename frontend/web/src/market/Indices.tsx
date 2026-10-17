import {currency,day,errorMessage,text} from '../shared/format';
import ReactECharts from 'echarts-for-react';
import {useOperation,useValue} from '../interaction/model';
import type {BenchmarkSpec,BenchmarkSeries} from '@display/workbench/port';
export default function Indices(){
  const catalog=useOperation<BenchmarkSpec[]>('market:indices','workbench.benchmarks');
  const [id,setId]=useValue('market:index','id','');
  const [start,setStart]=useValue('market:index','start','');
  const [end,setEnd]=useValue('market:index','end','');
  const selected=catalog.value?.find(item=>item.id===id);
  const series=useOperation<BenchmarkSeries>('market:index:series','workbench.benchmark',{id,start,end},!!selected&&!!start&&!!end&&start<=end);
  if(catalog.error)return <div className="notice status-error" role="alert">{errorMessage(catalog.error)}</div>;
  if(!catalog.value?.length)return null;
  return <section className="panel" data-od-id="indices"><div className="panelhead"><h2>指数</h2></div>
    <div className="config-form">
      <label>指数<select aria-label="指数" value={id} onChange={event=>setId(event.target.value)}><option value="">选择指数</option>{catalog.value.map(item=><option key={item.id} value={item.id}>{text(item.name)??"指数名称未提供"} · {currency(item.currency)}</option>)}</select></label>
      <label>起始日期<input aria-label="指数起始日期" type="date" value={start} onChange={event=>setStart(event.target.value)}/></label>
      <label>截止日期<input aria-label="指数截止日期" type="date" value={end} onChange={event=>setEnd(event.target.value)}/></label>
    </div>
    {start&&end&&start>end&&<div className="notice status-error" role="alert">起始日期不能晚于截止日期</div>}
    {series.error&&<div className="notice status-error" role="alert">{errorMessage(series.error)}</div>}
    {series.status==='pending'&&<div className="empty" role="status">读取中</div>}
    {series.value&&<><div className="panelhead"><h3>{text(series.value.name)??"指数名称未提供"} · 收盘价</h3><span>{text(series.value.provider)??"来源名称未提供"} · {currency(series.value.currency)}</span></div><ReactECharts option={{animation:false,textStyle:{fontSize:16},tooltip:{trigger:'axis',textStyle:{fontSize:16}},grid:{left:65,right:24,top:24,bottom:60},xAxis:{type:'category',axisLabel:{fontSize:14},data:series.value.points.map(point=>day(point.at))},yAxis:{type:'value',scale:true,axisLabel:{fontSize:14}},series:[{type:'line',showSymbol:false,data:series.value.points.map(point=>point.close),lineStyle:{color:'oklch(37% .055 245)'},areaStyle:{color:'oklch(94% .018 245)'}}]}} style={{height:320}}/></>}
  </section>;
}
