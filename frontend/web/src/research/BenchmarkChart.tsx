import {useMemo} from 'react';
import {useValue,useOperation} from '../interaction/model';
import ReactECharts from 'echarts-for-react';
import type {RunDetail,BenchmarkSpec,BenchmarkSeries} from '@display/workbench/port';
import {benchmarkCurves} from '@display/workbench/curves';
import {day,errorMessage,text} from '../shared/format';
export default function BenchmarkChart({data,name}:{data:RunDetail;name:string}){
 const catalog=useOperation<BenchmarkSpec[]>('benchmark:catalog','workbench.benchmarks');
 const [id,setId]=useValue('benchmark','identity','');
 const start=day(data.equity[0]?.at),end=day(data.equity.at(-1)?.at);
 const result=useOperation<BenchmarkSeries>('benchmark:series','workbench.benchmark',{id,start,end},!!id&&!!data.equity.length);
 const plot=useMemo(()=>{
  if(!id)return {series:[{name,type:'line',showSymbol:false,data:data.equity.map(point=>[day(point.at),point.equity])}],error:''};
  if(!result.value)return {series:[],error:''};
  try{return {series:benchmarkCurves([{name,points:data.equity}],{...result.value,name:text(result.value.name)??'指数名称未提供'}).series,error:''};}
  catch{return {series:[],error:'所选基准与本次尝试的日期无法对齐'};}
 },[data,name,id,result.value]);
 const error=catalog.error||result.error;
 return <>
  <div className="benchmark-tools"><label>收益基准<select aria-label="收益基准" value={id} onChange={event=>setId(event.target.value)}><option value="">不比较</option>{catalog.value?.map((item,i)=><option key={item.id} value={item.id}>{text(item.name)??`指数 ${i+1}`}</option>)}</select></label></div>
  {error&&<div className="notice status-error" role="alert">{errorMessage(error)}</div>}
  {plot.error&&<div className="notice status-error" role="alert">{plot.error}</div>}
  {!data.equity.length?<div className="empty">权益记录未提供</div>:!plot.series.length?<div className="empty">{result.status==='pending'?'读取基准中':'基准曲线未提供'}</div>:<ReactECharts option={{
   animation:false,textStyle:{fontSize:16},tooltip:{trigger:'axis',textStyle:{fontSize:16}},legend:{type:'scroll',bottom:0,textStyle:{fontSize:16}},
   grid:{left:72,right:24,bottom:76,top:32},xAxis:{type:'time',axisLabel:{fontSize:14}},yAxis:{type:'value',scale:true,name:id?'共同起点 = 1':'权益',nameTextStyle:{fontSize:16},axisLabel:{fontSize:14}},
   series:plot.series.map(series=>({...series,areaStyle:{opacity:.06}})),
  }} style={{height:360}}/>}
 </>;
}
