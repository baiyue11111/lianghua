import type {BacktestInput,BacktestResult,ParamValue,StrategySpec} from './port';

export type Completed={input:BacktestInput;result:BacktestResult};

export function comparisonIssue(first:Completed,second:Completed):string {
  if(first.input.module!==second.input.module)return '策略文件不同';
  if(first.input.start!==second.input.start||first.input.end!==second.input.end)return '回测时段不同';
  for(const [key,name] of [['data_hash','历史数据'],['code_hash','策略代码'],['execution_hash','执行规则']] as const) {
    if(!first.result[key]||!second.result[key])return `${name}身份未提供`;
    if(first.result[key]!==second.result[key])return `${name}不同`;
  }
  const dates=(item:Completed)=>item.result.equity.map(point=>point.at);
  if(JSON.stringify(dates(first))!==JSON.stringify(dates(second)))return '权益观测日期不同';
  if(first.result.equity_sampling&&first.result.equity_sampling.stride!==1||second.result.equity_sampling&&second.result.equity_sampling.stride!==1)return '权益序列经过抽样';
  if(!first.result.equity.length||!second.result.equity.length)return '缺少权益序列';
  return '';
}

export function backtestInput(spec:StrategySpec,values:Record<string,string|boolean>,capital:string,start:string,end:string,dataset:string):BacktestInput {
  const initial_cash=Number(capital);
  if(!capital.trim()||!Number.isFinite(initial_cash)||initial_cash<=0)throw new Error('投入资金必须大于 0');
  const date=(value:string)=>/^\d{4}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(value))&&new Date(value).toISOString().slice(0,10)===value;
  if(!date(start)||!date(end))throw new Error('请选择有效的开始与结束日期');
  if(start>end)throw new Error('结束日期不能早于开始日期');
  const params:Record<string,ParamValue>={};
  for(const field of spec.parameters) {
    const value=values[field.key];
    if(field.type==='boolean') {
      if(typeof value!=='boolean')throw new Error(`${field.label}必须为开关值`);
      params[field.key]=value;
    } else if(field.type==='number'||field.type==='integer') {
      const number=Number(value);
      if(typeof value!=='string'||!value.trim()||!Number.isFinite(number))throw new Error(`${field.label}必须为数字`);
      if(field.type==='integer'&&!Number.isInteger(number))throw new Error(`${field.label}必须为整数`);
      if(field.minimum!==undefined&&number<field.minimum)throw new Error(`${field.label}不能小于 ${field.minimum}`);
      if(field.maximum!==undefined&&number>field.maximum)throw new Error(`${field.label}不能大于 ${field.maximum}`);
      if(field.exclusive_minimum!==undefined&&number<=field.exclusive_minimum)throw new Error(`${field.label}必须大于 ${field.exclusive_minimum}`);
      params[field.key]=number;
    } else {
      if(typeof value!=='string')throw new Error(`${field.label}必须为文本`);
      params[field.key]=value;
    }
    if(field.options&&!field.options.includes(params[field.key]))throw new Error(`${field.label}不在可选范围内`);
  }
  return {module:spec.module,params,initial_cash,start,end,dataset_id:dataset};
}

export function parameterValues(spec:StrategySpec):Record<string,string|boolean> {
  return Object.fromEntries(spec.parameters.map(field=>{
    const value=spec.defaults[field.key];
    return [field.key,typeof value==='boolean'?value:String(value)];
  }));
}
