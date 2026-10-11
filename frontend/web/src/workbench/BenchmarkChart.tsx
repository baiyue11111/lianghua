import {useMemo} from 'react';
import {useValue,useOperation} from '../interaction/model';
import ReactECharts from 'echarts-for-react';
import type {RunDetail,BenchmarkSpec,BenchmarkSeries} from '@display/workbench/port';
import {benchmarkCurves} from '@display/workbench/curves';
import {label} from '@display/workbench/locale';

export default function BenchmarkChart({data,inspect}:{data:RunDetail|RunDetail[];inspect:(title:string,value:unknown)=>void}) {
  const runs=useMemo(()=>Array.isArray(data)?data:[data],[data]);
  const catalog=useOperation<BenchmarkSpec[]>('benchmark:catalog','workbench.benchmarks');
  const specs=catalog.value??[];
  const [identity,setIdentity]=useValue('benchmark','identity','');
  const chosen=identity||specs[0]?.id||'';
  const start=runs.filter(r=>r.equity.length).map(r=>r.equity[0].at).sort()[0]??'';
  const end=runs.filter(r=>r.equity.length).map(r=>r.equity.at(-1)!.at).sort().at(-1)??'';
  const result=useOperation<BenchmarkSeries>('benchmark:series','workbench.benchmark',{id:chosen,start,end},!!chosen&&!!start);
  const benchmark=result.value??null,failure=catalog.error||result.error;
  const plot=useMemo(()=>{
    if(!benchmark)return null;
    try{return {value:benchmarkCurves(runs.map(r=>({name:label(r.strategy_id)+' · '+r.run_id.slice(0,8),points:r.equity})),benchmark),error:''}}
    catch(e){return {value:null,error:String(e)}}
  },[runs,benchmark]);
  const error=failure||plot?.error;

  return <>
    <div className="benchmark-tools">
      <label>收益基准<select aria-label="收益基准" value={chosen} onChange={e=>{setIdentity(e.target.value)}}>{specs.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
      {benchmark&&<button onClick={()=>inspect('基准数据来源与快照',{...benchmark,points:undefined})}>来源与快照</button>}
    </div>
    {error?<p className="notice status-error" role="alert">基准对比不可用：{error}</p>:plot?.value?<>
      <ReactECharts option={{
        textStyle:{color:'#475569',fontFamily:"-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",fontSize:14},
        tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#d2d6dc',textStyle:{color:'#1d2939',fontSize:14}},
        legend:{type:'scroll',bottom:0,textStyle:{fontSize:14,color:'#475569'}},
        grid:{left:72,right:24,bottom:72,top:28},
        xAxis:{type:'time',axisLabel:{fontSize:13,color:'#667085'},axisLine:{lineStyle:{color:'#d0d5dd'}}},
        yAxis:{type:'value',scale:true,name:'共同起点 = 1',nameTextStyle:{fontSize:13,color:'#667085'},axisLabel:{fontSize:13,color:'#667085'},splitLine:{lineStyle:{color:'#eef0f3'}}},
        series:plot.value.series.map((s,i)=>({...s,lineStyle:i===plot.value!.series.length-1?{type:'dashed',width:2}:undefined})),
      }} style={{height:340}}/>
      <div className="benchmark-facts">
        <div><span>指数</span><strong>{benchmark?.name}</strong></div>
        <div><span>共同区间</span><strong>{plot.value.start} — {plot.value.end}</strong></div>
        <div><span>提供方</span><strong>{benchmark?.provider}</strong></div>
        <div><span>币种</span><strong>{label(benchmark?.currency??'')}</strong></div>
        <div><span>指数口径</span><strong>{label(benchmark?.return_type??'')}</strong></div>
        <div><span>缺失日线</span><strong>保留空缺</strong></div>
        <div><span>策略指标</span><strong>存档原值</strong></div>
      </div>
    </>:<div className="empty">{runs.some(r=>!r.equity.length)?'无权益序列':'读取基准'}</div>}
  </>;
}
