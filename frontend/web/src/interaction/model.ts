import {createContext,useCallback,useContext,useEffect,useState,useSyncExternalStore} from 'react';
import type {Dispatch,SetStateAction} from 'react';
import type {FrontendPort,JsonValue,Slot,State,UiMessage} from '@display/core';
import {emptySlot} from '@display/core';
const Context=createContext<FrontendPort|null>(null);
export const DisplayProvider=Context.Provider;
export function useDisplay(){const port=useContext(Context);if(!port)throw Error('DisplayProvider is required');return port;}
import {action} from './action';
const empty=emptySlot();
export function useSlot(target:string):Slot {
 const port=useDisplay();
 const read=useCallback(()=>(port.snapshot() as State).targets[target]??empty,[port,target]);
 return useSyncExternalStore(port.subscribe,read,read);
}
export function useValue<T>(target:string,key:string,initial:T|(()=>T)):[T,Dispatch<SetStateAction<T>>]{
 const port=useDisplay();
 const [fallback]=useState(initial);
 const read=useCallback(()=>{
  const values=port.snapshot().targets?.[target]?.values;
  return values&&key in values?values[key] as unknown as T:fallback;
 },[port,target,key,fallback]);
 const value=useSyncExternalStore(port.subscribe,read,read);
 useEffect(()=>{
  if(!(key in (port.snapshot().targets?.[target]?.values??{}))){
   void port.dispatch(action('edit',target,{[key]:fallback}));
  }
 },[port,target,key,fallback]);
 const set=useCallback((next:SetStateAction<T>)=>{
  const saved=port.snapshot().targets?.[target]?.values?.[key];
  const before=saved===undefined?fallback:saved as unknown as T;
  const result=typeof next==='function'?(next as (v:T)=>T)(before):next;
  void port.dispatch(action('edit',target,{[key]:result}));
 },[port,target,key,fallback]);
 return [value,set];
}
// A page left and returned to is not a page that changed. Two pages reading the
// same target is not two questions. Both facts are remembered here so a shared
// read happens once per freshness window instead of once per mount.
const CACHE=new Map<string,{identity:string;at:number}>();
const INFLIGHT=new Map<string,string>();

export function useOperation<T>(target:string,operation:string,data:unknown={},enabled=true,automatic=true,hint:{fresh?:number}={}){
 const port=useDisplay(),slot=useSlot(target),identity=JSON.stringify(data);
 const fresh=hint.fresh??0, key=target+'\u0000'+operation;
 const invoke=useCallback((input?:unknown)=>{
  const body=input===undefined?JSON.parse(identity):input;
  const started=port.dispatch(action('invoke',target,body,operation));
  const mark=JSON.stringify(body);INFLIGHT.set(key,mark);
  void started.finally(()=>{if(INFLIGHT.get(key)===mark)INFLIGHT.delete(key);});
  return started;
 },[port,target,operation,identity,key]);
 useEffect(()=>{
  if(!(automatic&&enabled))return;
  if(fresh>0){
   const cached=CACHE.get(key);
   // Already answered for this exact input, or being answered right now.
   if(cached?.identity===identity&&Date.now()-cached.at<fresh)return;
   if(INFLIGHT.get(key)===identity)return;
  }
  void invoke();
  // A shared read keeps its value when the page leaves; a page-local one is
  // cleared so the next visit starts from the response, not from a leftover.
  return()=>{if(automatic&&!(fresh>0))void port.dispatch(action('reset',target,{}));};
 },[invoke,enabled,automatic,port,target,key,identity,fresh]);
 const status=slot.operations[operation]?.status??'idle';
 const valid=JSON.stringify(slot.operations[operation]?.result_input)===identity;
 const values=valid?slot.values[operation] as JsonValue[]|undefined:undefined;
 useEffect(()=>{if(values)CACHE.set(key,{identity,at:Date.now()});},[values,identity,key]);
 const requestId=slot.operations[operation]?.request_id??undefined;
 // The core marks an operation unavailable when its declared `requires` check
 // no longer matches the input it was approved for. That flag is the single
 // answer to "may this be invoked now", so views read it instead of rebuilding
 // the rule from the form.
 const available=(slot.operations[operation] as {available?:boolean}|undefined)?.available!==false;
 const message:UiMessage|undefined=requestId
  ? slot.messages.find(item=>item.correlation_id===requestId&&item.kind!=='problem')
  : undefined;
 return {
  value:values?.[0] as unknown as T|undefined,status,available,
  // True when the value on hand was produced by exactly the input the caller is
  // holding now. A view uses it to say "this conclusion is stale" without
  // inventing its own comparison rule.
  matched:valid,
  error:slot.problems.map(p=>p.title).join('；'),invoke,
  message,presentation:message?.presentation??undefined,
  kind:message?.kind,extensions:message?.extensions,links:message?.links,
  messages:slot.messages,
 };
}

export function useSession(target:string){
 const port=useDisplay();
 useEffect(()=>()=>{void port.dispatch(action('reset',target,{}));},[port,target]);
}
