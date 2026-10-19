import type {Run,StrategySpec,StrategyCatalog} from '@display/research/port';
import {label} from '@display/research/locale';
import {runConfig} from './groups';
import {amount,currency,day,missing,number,percent,record,security,status,text} from '../shared/format';
export type Field={name:string;value:string};
export function parameters(run:Run,spec?:StrategySpec):Field[]{
 const values=record(runConfig(run).params),fields:Field[]=[];
 for(const key of Object.keys(values)){
  const meta=spec?.parameters.find(item=>item.key===key),name=text(meta?.label);
  const value=values[key];
  const shown=typeof value==='boolean'?(value?'开启':'关闭'):typeof value==='number'?number(value,6):typeof value==='string'&&['integer','number'].includes(meta?.type??'')&&/^-?\d+(\.\d+)?$/.test(value)?number(value,6):typeof value==='string'&&text(value)&&(/[\u3400-\u9fff]/.test(label(value))||/^\d+$/.test(value))?(text(label(value))??'参数值未提供可读名称'):'参数值未提供可读名称';
  const unit=text(meta?.unit);
  fields.push({name:name??'参数名称未提供',value:shown+(unit&&shown!==missing?' '+unit:'')});
 }
 return fields.length?fields:[{name:'参数',value:missing}];
}
export function configuration(run:Run,spec?:StrategySpec,catalog?:StrategyCatalog):Field[]{
 const config=runConfig(run),market=record(config.market),resolved=record(config.market_resolution);
 const codes=[...new Set([market.codes,config.symbols].flatMap(value=>Array.isArray(value)?value:[]).map(security))];
 const dataset=catalog?.availability?.datasets?.find(item=>item.id===config.dataset_id);
 const scope=codes.length?codes.join('、'):market.index?'指数范围（名称未提供）':market.scope?'市场范围（名称未提供）':missing;
 const fields:Field[]=[{name:'证券范围',value:scope},{name:'起始日期',value:day(config.start)},{name:'截止日期',value:day(config.end)},{name:'初始资金',value:amount(config.initial_cash)},{name:'币种',value:currency(resolved.currency)},{name:'数据集',value:text(dataset?.name)??missing}];
 if(Array.isArray(market.filters)&&market.filters.length)fields.push({name:'筛选条件',value:`${market.filters.length} 项（名称未提供）`});
 return [...fields,...parameters(run,spec)];
}
export function attemptNames(runs:Run[],previous:Record<string,string>={}):Map<string,string>{
 const names=new Map(Object.entries(previous));
 let next=Math.max(0,...[...names.values()].map(value=>Number(value.replace('尝试 ',''))||0));
 for(const run of [...runs].sort((a,b)=>a.id.localeCompare(b.id,'en')))if(!names.has(run.id))names.set(run.id,`尝试 ${++next}`);
 return names;
}
export const resultMetrics=(run:Run):Field[]=>[
 {name:'累计收益',value:percent(run.metrics.total_return)},
 {name:'最大回撤',value:percent(run.metrics.max_drawdown)},
 {name:'夏普比率',value:number(run.metrics.sharpe,3)},
 {name:'观测数',value:number(run.metrics.observations,0)},
];
export function batchRows(value:unknown,catalog?:StrategyCatalog):{name:string;fields:Field[];config:Field[]}[]{
 const batch=record(value);
 if(!Array.isArray(batch.intervals))return [];
 return batch.intervals.map((raw,i)=>{
  const interval=record(raw),result=record(interval.result),config=record(result.effective_config),metrics=record(result.metrics);
  const spec=catalog?.items.find(item=>item.module===config.module||item.strategy_id===result.strategy_id);
  return {name:text(interval.name)??`区间 ${i+1}`,config:configuration({effective_config:config} as unknown as Run,spec,catalog),fields:[
   {name:'状态',value:result.status?status(result.status):'完成'},
   {name:'起始日期',value:day(config.start??result.first_at)},
   {name:'截止日期',value:day(config.end??result.last_at)},
   {name:'初始资金',value:amount(config.initial_cash)},
   {name:'期末权益',value:amount(result.final_equity)},
   {name:'累计收益',value:percent(metrics.total_return)},
   {name:'最大回撤',value:percent(metrics.max_drawdown)},
   {name:'成交笔数',value:number(result.fills,0)},
   {name:'夏普比率',value:number(metrics.sharpe,3)},
   {name:'期末现金',value:amount(result.final_cash)},
   {name:'观测数',value:number(result.steps,0)},
   {name:'待处理订单数',value:number(result.pending_orders,0)},
   ...Object.entries(record(result.positions)).map(([symbol,quantity])=>({name:`持仓 ${security(symbol)}`,value:number(quantity,6)})),
  ]};
 });
}
