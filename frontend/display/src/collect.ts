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
 if(parts.length!==2||!name||!['latest','range','auto-on','auto-off','check','doctor','status'].includes(command))throw Error('需要 <market>:[start,end] 或 latest、range、auto-on、auto-off、check、doctor、status');
 if(range){
  if(values.start!==undefined||values.end!==undefined)throw Error('区间不能与 start、end 重复填写');
  values.start=range[1].length===4?range[1]+'-01-01':range[1];
  values.end=range[2].length===4?range[2]+'-12-31':range[2];
 }
 if(values.refresh&&!['range','doctor'].includes(command))throw Error('refresh 仅用于 range 或 doctor');
 if(['auto-on','auto-off','status'].includes(command)&&Object.keys(values).some(key=>key!=='selector'))throw Error(`${command} 不接受收集参数`);
 if(command==='latest'&&values.start!==undefined)throw Error('latest 自动确定起始日期');
 if(command==='range'&&(!values.start||!values.end))throw Error('range 需要 start 和 end');
 if(command!=='latest'&&(values.start===undefined)!==(values.end===undefined))throw Error('start 和 end 必须同时填写');
 if(signal.aborted)throw Error('会话已停止');
 if(command==='status')return await call('collection.automatic',{market:name});
 if(command==='auto-on'||command==='auto-off')return await call('collection.set_automatic',{market:name,enabled:command==='auto-on'});
 const request:Row={market:name,command};
 for(const field of ['data','codes','fields'])if(values[field]!==undefined)request[field]=items(values[field]);
 for(const field of ['start','end','refresh'])if(values[field]!==undefined)request[field]=values[field];
 if(values.start)day(values.start);
 if(values.end)day(values.end);
 if(values.start&&values.end&&values.start>values.end)throw Error('日期应有序');
 const result=await call('collection.run',request) as Row;
 if(result.outcome==='failed'){
  progress(result);
  throw Error(result.error??'收集失败');
 }
 return result as JsonValue;
};
