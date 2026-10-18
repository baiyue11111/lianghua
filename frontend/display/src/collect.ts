// Collection intentions belong to the shared interaction core.
import type {JsonValue,Operation} from './core';
type Row=Record<string,any>;
const items=(value?:string):string[]=>{
 if(value===undefined)return [];
 const values=value.split(',').map(v=>v.trim());
 if(values.some(v=>!v))throw Error('列表项不能为空');
 return [...new Set(values)];
};
const day=(value:string)=>{
 if(!/^\d{4}-\d{2}-\d{2}$/.test(value)||new Date(value).toISOString().slice(0,10)!==value)throw Error('日期无效');
 return value;
};
export const collect:NonNullable<Operation['execute']>=async(raw,{call,progress,signal})=>{
 const values={...raw as Row};
 const parts=values.selector.split(':');
 const [name,intent]=parts;
 const range=/^\[\s*(\d{4}(?:-\d{2}-\d{2})?)\s*,\s*(\d{4}(?:-\d{2}-\d{2})?)\s*\]$/.exec(intent??'');
 const command=range?'range':intent;
 if(parts.length!==2||!['latest','range','auto','check','status'].includes(command))throw Error('需要 <market>:[start,end] 或 latest、range、auto、check、status');
 if(range){
  if(values.start!==undefined||values.end!==undefined)throw Error('区间不能与 start、end 重复填写');
  values.start=range[1].length===4?range[1]+'-01-01':range[1];
  values.end=range[2].length===4?range[2]+'-12-31':range[2];
 }
 if(command!=='auto'&&values.interval!==undefined)throw Error('interval 仅用于 auto');
 if(command==='status'&&Object.keys(values).some(key=>key!=='selector'))throw Error('status 不接受收集参数');
 if(['latest','auto'].includes(command)&&(values.start!==undefined||values.end!==undefined))throw Error('latest 和 auto 自动确定日期，请用 range 指定区间');
 if(command==='range'&&(!values.start||!values.end))throw Error('range 需要 start 和 end');
 if((values.start===undefined)!==(values.end===undefined))throw Error('start 和 end 必须同时填写');
 const duration=/^([1-9]\d*)(s|m|h|d)$/.exec(values.interval??'1d');
 if(!duration)throw Error('interval 需要正数和单位 s、m、h 或 d');
 const interval=Number(duration[1])*({s:1,m:60,h:3600,d:86400}[duration[2]]!)*1000;
 if(!Number.isSafeInteger(interval)||interval>2147483647)throw Error('interval 超出范围');
 const registry=await call('collection.describe',{}) as Row;
 const markets=registry.markets.filter((m:Row)=>[m.scope,m.name,...(m.aliases??[])].includes(name));
 if(markets.length!==1)throw Error('市场未唯一登记：'+name);
 const market=markets[0];
 if(typeof market.timezone!=='string'||!market.timezone)throw Error('市场未登记时区');
 if(command==='status')return {market:market.scope,sources:await call('collection.sources',{})};
 const spec=market.collect;
 if(!spec?.data)throw Error('市场未登记收集数据源');
 let types=items(values.data);
 if(!types.length)types=spec.default??[];
 if(types.length===1&&types[0]==='all')types=Object.keys(spec.data);
 if(!types.length||types.some(kind=>!spec.data[kind]))throw Error('数据类型未登记');
 const source=(id:string)=>{
  const found=registry.sources.find((s:Row)=>s.id===id);
  if(!found)throw Error('数据源未登记：'+id);
  return found;
 };
 const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:market.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
 if(values.start){day(values.start);day(values.end);if(values.start>values.end||values.end>today())throw Error('日期应有序且不晚于今天');}
 const requested=items(values.codes);
 const codes=async():Promise<string[]>=>{
  if(spec.codes)return spec.codes;
  const binding=spec.securities;
  if(!binding)throw Error('市场未登记证券目录，请提供 codes');
  source(binding.source);
  if(command!=='check'){
   const input:Row={source_id:binding.source,refresh:true};
   if(binding.exchanges!==undefined)input.exchanges=binding.exchanges;
   await call('collection.run',input);
  }
  let rows:Row[]=[],offset=0;
  while(true){
   const page=await call('collection.snapshot',{id:binding.source,offset}) as Row;
   rows.push(...page.records);offset+=page.records.length;
   if(offset>=page.total||!page.records.length)break;
  }
  const exchanges=new Set<string>();
  const result=rows.filter(row=>{
   if(Object.entries(binding.filters??{}).some(([field,allowed])=>!(allowed as unknown[]).includes(row[field])))return false;
   const code=row[binding.code];
   if(typeof code!=='string')throw Error('目录缺少证券代码');
   if(binding.pattern&&!new RegExp('^(?:'+binding.pattern+')$').test(code))return false;
   exchanges.add(code.split('.')[0]);
   const opened=row[binding.from],closed=row[binding.to];
   return !(opened&&opened>(values.end??today())||closed&&(values.start?closed<values.start:true));
  }).map(row=>row[binding.code]);
  if((binding.exchanges??[]).some((exchange:string)=>!exchanges.has(exchange)))throw Error('证券目录缺少登记的交易所');
  return [...new Set<string>(result)].sort();
 };
 const cycle=async():Promise<JsonValue>=>{
  const plans:Row[]=[],jobs:JsonValue[]=[];
  let marketCodes:string[]|undefined;
  for(const kind of types){
   if(signal.aborted)throw Error('会话已停止');
   const binding=spec.data[kind];source(binding.source);
   const selected=binding.codes??(requested.length?requested:(marketCodes??=await codes()));
   if(!selected.length)throw Error('区间内没有证券');
   let start=values.start,end=values.end;
   if(!start){
    const stored=await call('collection.inspect',{source_id:binding.source,codes:selected}) as Row;
    if(!stored.last_date)throw Error('没有已存数据，请用 range 指定 start 和 end');
    start=stored.last_date;end=today();
   }
   const request:Row={source_id:binding.source,start,end};
   if(values.fields)request.fields=items(values.fields);
   const size=spec.batch_size===undefined?selected.length:spec.batch_size;
   if(!Number.isSafeInteger(size)||size<1)throw Error('市场收集批量设置无效');
   for(let offset=0;offset<selected.length;offset+=size){
    if(signal.aborted)throw Error('会话已停止');
    const batch={...request,codes:selected.slice(offset,offset+size)};
    if(command==='check'){
     const result=await call('collection.inspect',batch) as Row;
     plans.push({data:kind,request:batch,coverage:result.coverage});
    }else{
     if(['latest','auto'].includes(command))Object.assign(batch,{force:true,refresh:true});
     jobs.push(await call('collection.run',batch));
     if(selected.length>size)progress({market:market.scope,data:kind,status:'running',completed:Math.min(offset+size,selected.length),total:selected.length});
    }
   }
  }
  return command==='check'?{market:market.scope,status:plans.every(p=>p.coverage.complete)?'complete':'incomplete',plans}:
   {market:market.scope,status:'succeeded',jobs};
 };
 do {
  const result=await cycle();
  if(command!=='auto')return result;
  progress(result);
  await new Promise<void>((resolve,reject)=>{
   const abort=()=>{clearTimeout(timer);reject(Error('会话已停止'));};
   const timer=setTimeout(()=>{signal.removeEventListener('abort',abort);resolve();},interval);
   signal.addEventListener('abort',abort,{once:true});
   if(signal.aborted)abort();
  });
 }while(!signal.aborted);
 throw Error('会话已停止');
};
