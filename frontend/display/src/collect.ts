// Parse user input; the collection module resolves and executes market intentions.
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
 if(parts.length!==2||!['latest','range','auto','check','doctor','status'].includes(command))throw Error('需要 <market>:[start,end] 或 latest、range、auto、check、doctor、status');
 if(range){
  if(values.start!==undefined||values.end!==undefined)throw Error('区间不能与 start、end 重复填写');
  values.start=range[1].length===4?range[1]+'-01-01':range[1];
  values.end=range[2].length===4?range[2]+'-12-31':range[2];
 }
 if(values.refresh&&!['range','doctor'].includes(command))throw Error('refresh 仅用于 range 或 doctor');
 if(command!=='auto'&&values.interval!==undefined)throw Error('interval 仅用于 auto');
 if(command==='status'&&Object.keys(values).some(key=>key!=='selector'))throw Error('status 不接受收集参数');
 if(['latest','auto'].includes(command)&&(values.start!==undefined||values.end!==undefined))throw Error('latest 和 auto 自动确定日期，请用 range 指定区间');
 if(command==='range'&&(!values.start||!values.end))throw Error('range 需要 start 和 end');
 if((values.start===undefined)!==(values.end===undefined))throw Error('start 和 end 必须同时填写');
 const duration=/^([1-9]\d*)(s|m|h|d)$/.exec(values.interval??'1d');
 if(!duration)throw Error('interval 需要正数和单位 s、m、h 或 d');
 const interval=Number(duration[1])*({s:1,m:60,h:3600,d:86400}[duration[2]]!)*1000;
 if(!Number.isSafeInteger(interval)||interval>2147483647)throw Error('interval 超出范围');
 const request:Row={market:name,command:command==='auto'?'latest':command};
 for(const field of ['data','codes','fields'])if(values[field]!==undefined)request[field]=items(values[field]);
 for(const field of ['start','end','refresh'])if(values[field]!==undefined)request[field]=values[field];
 if(values.start){day(values.start);day(values.end);if(values.start>values.end)throw Error('日期应有序');}
 const cycle=async():Promise<JsonValue>=>{
  if(signal.aborted)throw Error('会话已停止');
  const result=await call('collection.run',request) as Row;
  if(command==='auto'||result.status==='failed')progress(result);
  if(result.status==='failed')throw Error(result.error??'收集失败');
  return result;
 };
 do {
  const result=await cycle();
  if(command!=='auto')return result;
  await new Promise<void>((resolve,reject)=>{
   const abort=()=>{clearTimeout(timer);reject(Error('会话已停止'));};
   const timer=setTimeout(()=>{signal.removeEventListener('abort',abort);resolve();},interval);
   signal.addEventListener('abort',abort,{once:true});
   if(signal.aborted)abort();
  });
 }while(!signal.aborted);
 throw Error('会话已停止');
};
