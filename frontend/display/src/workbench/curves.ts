export interface StrategyCurve {name:string;points:{at:string;equity:number}[]}
export interface IndexCurve {name:string;points:{at:string;close:number}[]}
export function benchmarkCurves(runs:StrategyCurve[],benchmark:IndexCurve) {
  if(!runs.length||runs.some(r=>!r.points.length)||!benchmark.points.length)throw new Error('没有共同的观测区间');
  const starts=runs.map(r=>r.points[0].at).sort(),ends=runs.map(r=>r.points.at(-1)!.at).sort();
  const lower=starts.at(-1)!,end=ends[0];
  const common=runs[0].points.map(p=>p.at).filter(at=>at>=lower&&at<=end&&runs.every(r=>r.points.some(p=>p.at===at)));
  const start=common[0];
  if(!start||benchmark.points[0].at>end||benchmark.points.at(-1)!.at<start)throw new Error('没有共同的观测区间');
  const prices=new Map(benchmark.points.map(p=>[p.at,p.close]));
  const series=runs.map(run=>{
    const points=run.points.filter(p=>p.at>=start&&p.at<=end);
    for(const p of points){
      if(!prices.has(p.at))throw new Error(`基准缺少策略日期 ${p.at}，请核对交易日历与快照范围`);
      if(!Number.isFinite(p.equity)||p.equity<0)throw new Error('策略权益必须为正数');
    }
    if(points[0].equity<=0)throw new Error('策略初始权益必须为正数');
    return {name:run.name,type:'line' as const,showSymbol:false,data:points.map(p=>[p.at,p.equity/points[0].equity])};
  });
  const points=benchmark.points.filter(p=>p.at>=start&&p.at<=end);
  if(points.some(p=>!Number.isFinite(p.close)||p.close<=0))throw new Error('基准收盘价必须为正数');
  series.push({name:benchmark.name,type:'line',showSymbol:false,data:points.map(p=>[p.at,p.close/prices.get(start)!])});
  return {start,end,series};
}
