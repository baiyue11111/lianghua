import Ajv from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import schema from './protocol.schema.json';
import type {JsonValue,UiAction,UiMessage,UiOperation,UiProblem,UiRequest,UiSlot,UiState,BackendPort} from './protocol';
export type {JsonValue,UiAction,UiMessage,UiRequest,UiState} from './protocol';
export type Slot=Required<UiSlot>;
export type State=Required<UiState> & {targets:Record<string,Slot>};
export type {FrontendPort,BackendPort} from './protocol';
export interface Operation extends UiOperation {
  requires?:string;
  // Feature code decides whether its successful check authorizes a write.
  ready?:(messages:UiMessage[])=>boolean;
  prepare?:(data:JsonValue)=>JsonValue;
  changes?:(state:State,target:string,request:UiRequest,messages:UiMessage[])=>Record<string,Record<string,JsonValue>>;
  poll?:number;
  repeat?:(messages:UiMessage[])=>boolean;
}
export interface Options {
  operations:Operation[];
  schemas:Record<string,object>;
  backend?:BackendPort;
  handlers?:Record<string,(message:UiMessage)=>void>;
  id?:()=>string;
  events?:Record<string,string>;
}
const ajv=new Ajv({strict:false,allErrors:true});
addFormats(ajv);
ajv.addSchema(schema);
const validators=new Map<string,ReturnType<typeof ajv.compile>>();
export function validateWire(model:string,value:unknown):boolean {
  // JSON cannot represent non-finite numbers, undefined, functions or prototypes.
  try {assertJson(value);}catch{return false;}
  let check=validators.get(model);
  if(!check){check=ajv.compile({$ref:`urn:lianghua:ui:v1#/$defs/${model}`});validators.set(model,check);}
  return !!check(value);
}
function assertJson(value:unknown):void {
  if(value===null||typeof value==='string'||typeof value==='boolean')return;
  if(typeof value==='number'&&Number.isFinite(value))return;
  if(Array.isArray(value)){value.forEach(assertJson);return;}
  if(typeof value==='object'&&Object.getPrototypeOf(value)===Object.prototype){Object.values(value as object).forEach(assertJson);return;}
  throw Error('非 JSON 数据');
}
function freeze<T>(value:T):T {
  if(value&&typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}
  return value;
}
export function emptySlot():Slot {return {revision:0,values:{},selection:null,query:{},messages:[],problems:[],operations:{}};}
function problem(code:string,title:string):UiProblem {return {type:'urn:lianghua:ui:problem',code,title,retryable:false};}
function object(value:JsonValue|undefined):Record<string,JsonValue> {
  if(value===null||typeof value!=='object'||Array.isArray(value))throw Error('输入必须是对象');
  return value;
}
// Pure reducer: no transport, file, timer, browser or process access.
export function update(slot:Slot,action:UiAction):Slot {
  switch(action.kind){
    case 'edit':return {...slot,values:{...slot.values,...object(action.data)},revision:slot.revision+1,problems:[]};
    case 'query':return {...slot,query:object(action.data),revision:slot.revision+1,problems:[]};
    case 'select':return {...slot,selection:action.data??null,revision:slot.revision+1,problems:[]};
    case 'reset':return {...emptySlot(),values:object(action.data),revision:slot.revision+1};
    case 'dismiss':return {...slot,problems:slot.problems.filter(p=>p.code!==action.data)};
    case 'invoke':return slot;
  }
}
export function createDisplay(options:Options) {
  const operations=new Map(options.operations.map(spec=>[spec.operation,spec]));
  if(operations.size!==options.operations.length)throw Error('重复操作注册');
  const registry=new Ajv({strict:false});addFormats(registry);
  for(const [id,body] of Object.entries(options.schemas))registry.addSchema(body,id);
  const checks=new Map(Object.keys(options.schemas).map(id=>[id,registry.getSchema(id)!]));
  const listeners=new Set<()=>void>();
  const pending=new Map<string,{id:string;revision:number}>();
  const approved=new Map<string,{revision:number;identity:string}>();
  const timers=new Map<string,ReturnType<typeof setTimeout>>();
  let state:State=freeze({revision:0,targets:{}}),closed=false,serial=0;
  const id=options.id??(()=>`request-${++serial}`);
  const key=(target:string,operation:string)=>target+'\0'+operation;
  const slot=(target:string):Slot=>state.targets[target]??emptySlot();
  function put(target:string,value:Slot){
    if(closed)return;
    const names=new Set(Object.keys(value.operations));
    for(const spec of operations.values())if(spec.requires&&value.operations[spec.requires])names.add(spec.operation);
    const blocked=[...pending.keys()].some(name=>name.startsWith(target+'\0'));
    const states={...value.operations};
    for(const name of names){
      const spec=operations.get(name);
      const available=!blocked&&(!spec?.requires||approved.get(key(target,spec.requires))?.revision===value.revision);
      states[name]={status:'idle',...states[name],available};
    }
    state=freeze({revision:state.revision+1,targets:{...state.targets,[target]:{...value,operations:states}}});
    for(const listener of listeners)listener();
  }
  function stop(target:string){
    for(const [k,timer] of timers)if(k.startsWith(target+'\0')){clearTimeout(timer);timers.delete(k);}
    for(const k of approved.keys())if(k.startsWith(target+'\0'))approved.delete(k);
  }
  function fail(target:string,code:string,title:string){put(target,{...slot(target),problems:[problem(code,title)]});}
  function accept(message:UiMessage,spec?:Operation){
    if(!validateWire('UiMessage',message))throw Error('消息不符合公共协议');
    if(spec&&!spec.response_schemas.includes(message.schema))throw Error('未注册响应 schema');
    const check=checks.get(message.schema);
    if(!check||!check(message.data))throw Error('响应正文不符合注册 schema');
    if(message.kind==='problem'&&!validateWire('UiProblem',message.data))throw Error('问题正文不符合协议');
    if(message.kind==='custom'&&!options.handlers?.[message.type])throw Error('未注册自定义类型');
  }
  function publish(target:string,messages:UiMessage[],spec?:Operation){
    messages.forEach(message=>accept(message,spec));
    const problems=messages.filter(m=>m.kind==='problem').map(m=>m.data as unknown as UiProblem);
    put(target,{...slot(target),messages:structuredClone(messages),problems,values:spec&&!problems.length?{...slot(target).values,[spec.operation]:messages.filter(m=>m.kind!=="problem").map(m=>m.data)}:slot(target).values});
    messages.forEach(message=>options.handlers?.[message.type]?.(message));
  }
  async function dispatch(action:UiAction):Promise<State>{
    if(closed)throw Error('session closed');
    if(!validateWire('UiAction',action))throw Error('动作不符合公共协议');
    const target=action.target;
    if(action.kind!=='invoke'){
      try{put(target,update(slot(target),action));if(['edit','query','select','reset'].includes(action.kind))stop(target);}
      catch(reason){fail(target,'invalid_input',String(reason));}
      return state;
    }
    const spec=operations.get(action.operation!);
    if(!spec){fail(target,'unsupported','未注册操作');return state;}
    const k=key(target,spec.operation),initial=slot(target);
    const timer=timers.get(k);if(timer){clearTimeout(timer);timers.delete(k);}
    if(spec.mode==='write'&&[...pending.keys()].some(value=>value.startsWith(target+'\0'))){fail(target,'pending','操作正在执行');return state;}
    let data:JsonValue;
    try {
      data=spec.prepare?spec.prepare(action.data??null):action.data??null;
      assertJson(data);
      const check=checks.get(spec.input_schema);
      if(!check||!check(data))throw Error('输入不符合注册 schema');
    }catch(reason){fail(target,'invalid_input',String(reason));return state;}
    const identity=JSON.stringify(data,(_name,value)=>value&&typeof value==='object'&&!Array.isArray(value)?Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b))):value);
    const check=spec.requires?approved.get(key(target,spec.requires)):undefined;
    if(spec.requires&&(check?.revision!==initial.revision||check?.identity!==identity)){fail(target,'check_required','请重新检查输入');return state;}
    if(!options.backend){fail(target,'not_connected','未连接');put(target,{...slot(target),operations:{...slot(target).operations,[spec.operation]:{...slot(target).operations[spec.operation],status:'failed',revision:initial.revision,input:action.data}}});return state;}
    const request:UiRequest={request_id:id(),operation:spec.operation,schema:spec.input_schema,data:structuredClone(data)};
    pending.set(k,{id:request.request_id,revision:initial.revision});
    put(target,{...slot(target),problems:[],operations:{...slot(target).operations,[spec.operation]:{...slot(target).operations[spec.operation],status:'pending',request_id:request.request_id,revision:initial.revision,input:action.data}}});
    const current=()=>!closed&&pending.get(k)?.id===request.request_id&&slot(target).revision===initial.revision;
    try {
      const messages=await options.backend.send(request);
      if(!current())return state;
      if(!Array.isArray(messages))throw Error('响应必须是消息列表');
      if(messages.some(message=>message.correlation_id!==request.request_id))throw Error('响应标识与请求不匹配');
      publish(target,messages,spec);
      if(spec.changes&&!slot(target).problems.length){
        for(const [name,values] of Object.entries(spec.changes(state,target,request,messages))){assertJson(values);put(name,{...slot(name),values:{...slot(name).values,...values}});}
      }
      const failed=slot(target).problems.length>0;
      if(!failed&&(!spec.ready||spec.ready(messages)))approved.set(k,{revision:initial.revision,identity});else approved.delete(k);
      put(target,{...slot(target),operations:{...slot(target).operations,[spec.operation]:{...slot(target).operations[spec.operation],status:failed?'failed':'succeeded',request_id:request.request_id,revision:initial.revision,input:action.data,...(!failed?{result_input:action.data}:{})}}});
      if(!failed&&spec.poll&&(!spec.repeat||spec.repeat(messages))){
        const old=timers.get(k);if(old)clearTimeout(old);
        timers.set(k,setTimeout(()=>{timers.delete(k);void dispatch(action);},spec.poll));
      }
    } catch(reason){
      if(current()){
        approved.delete(k);
        fail(target,String(reason).includes('未注册')?'unsupported':'failed',String(reason));
        put(target,{...slot(target),operations:{...slot(target).operations,[spec.operation]:{...slot(target).operations[spec.operation],status:'failed',request_id:request.request_id,revision:initial.revision,input:action.data}}});
      }
    }finally{
      if(pending.get(k)?.id===request.request_id){pending.delete(k);
        if(!closed){
          const status=slot(target).operations[spec.operation]?.status;
          put(target,status==='pending'?{...slot(target),operations:{...slot(target).operations,[spec.operation]:{...slot(target).operations[spec.operation],status:'idle',revision:slot(target).revision}}}:slot(target));
        }
      }
    }
    return state;
  }
  return {
    dispatch,snapshot:()=>state,
    subscribe(listener:()=>void){if(closed)throw Error('session closed');listeners.add(listener);return()=>listeners.delete(listener);},
    receive(message:UiMessage){
      if(closed)return;
      const target=options.events?.[message.type];
      if(!target)throw Error("未注册消息接收目标");
      publish(target,[message]);
    },
    dispose(){closed=true;listeners.clear();for(const timer of timers.values())clearTimeout(timer);timers.clear();pending.clear();approved.clear();},
  };
}
