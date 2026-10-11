import {label} from './locale.ts';
import type {Run,StrategySpec} from './port';

export type ResearchMetric='total_return'|'max_drawdown'|'sharpe';
export type ResearchSort='name'|ResearchMetric;
export type ResearchOrder='asc'|'desc';
export type MetricSummary={min:number;max:number;median:number;count:number};
export type StrategyGroup={key:string;name:string;spec?:StrategySpec;description?:string;runs:Run[];metrics:Partial<Record<ResearchMetric,MetricSummary>>};

const metricKeys:ResearchMetric[]=['total_return','max_drawdown','sharpe'];
const keyOf=(value:string)=>value.normalize('NFKC').trim().replace(/\s+/g,' ').toLocaleLowerCase();

function summarize(values:number[]):MetricSummary|undefined {
  if(!values.length)return undefined;
  const sorted=[...values].sort((a,b)=>a-b), middle=Math.floor(sorted.length/2);
  const median=sorted.length%2?sorted[middle]:(sorted[middle-1]+sorted[middle])/2;
  return {min:sorted[0],max:sorted[sorted.length-1],median,count:sorted.length};
}

export function formatMetricRange(summary:MetricSummary|undefined,key:ResearchMetric):string {
  if(!summary)return '未提供';
  const format=(value:number)=>key==='sharpe'?value.toFixed(3):`${(value*100).toFixed(2)}%`;
  const first=format(summary.min),last=format(summary.max);
  return first===last?first:`${first} – ${last}`;
}

export function groupStatus(group:StrategyGroup,catalogReady=true):string {
  if(!catalogReady)return '目录状态未知';
  if(!group.spec)return '仅存档';
  if(!group.runs.length)return '尚未回测';
  const values=[...new Set(group.runs.map(run=>run.qualification))];
  return values.length===1&&values[0]==='unverified'?'未验收':values.length===1?label(values[0]):'状态不一';
}

export function strategyGroups(runs:Run[],catalog:StrategySpec[]=[]):StrategyGroup[] {
  const groups=new Map<string,StrategyGroup>();
  for(const spec of catalog)groups.set(spec.strategy_id,{key:spec.strategy_id,name:spec.name,spec,description:spec.description,runs:[],metrics:{}});
  for(const run of runs){
    const key=run.strategy_id;
    const group=groups.get(key)??{key,name:label(key),runs:[],metrics:{}};
    group.runs.push(run);groups.set(key,group);
  }
  return [...groups.values()].map(group=>({
    ...group,
    metrics:Object.fromEntries(metricKeys.flatMap(key=>{
      const values=group.runs.map(run=>run.metrics[key]).filter((value):value is number=>typeof value==='number'&&Number.isFinite(value));
      const summary=summarize(values);
      return summary?[[key,summary]]:[];
    })) as StrategyGroup['metrics'],
  }));
}

export function visibleStrategies(groups:StrategyGroup[],query:string,sort:ResearchSort,order:ResearchOrder=sort==='name'?'asc':'desc'):StrategyGroup[] {
  const needle=keyOf(query);
  const matches=groups.filter(group=>!needle||[group.name,group.key,group.spec?.module??'',...group.runs.flatMap(run=>[run.strategy_id,run.run_id])]
    .some(value=>keyOf(value).includes(needle)));
  const byName=(a:StrategyGroup,b:StrategyGroup)=>a.name.localeCompare(b.name,'zh-CN');
  matches.sort((a,b)=>{
    if(sort==='name')return byName(a,b)*(order==='asc'?1:-1);
    const left=a.metrics[sort]?.median,right=b.metrics[sort]?.median;
    if(left===undefined)return right===undefined?byName(a,b):1;
    if(right===undefined)return -1;
    return (order==='asc'?left-right:right-left)||byName(a,b);
  });
  return matches;
}
