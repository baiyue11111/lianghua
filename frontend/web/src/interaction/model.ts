import {createContext,useCallback,useContext,useEffect,useState,useSyncExternalStore} from 'react';
import type {Dispatch,SetStateAction} from 'react';
import type {FrontendPort,JsonValue,Slot,State} from '@display/core';
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
export function useOperation<T>(target:string,operation:string,data:unknown={},enabled=true,automatic=true){
 const port=useDisplay(),slot=useSlot(target),identity=JSON.stringify(data);
 const invoke=useCallback((input?:unknown)=>port.dispatch(action('invoke',target,input===undefined?JSON.parse(identity):input,operation)),[port,target,operation,identity]);
 useEffect(()=>{
  if(automatic&&enabled)void invoke();
  return()=>{if(automatic)void port.dispatch(action('reset',target,{}));};
 },[invoke,enabled,automatic,port,target]);
 const status=slot.operations[operation]?.status??'idle';
 const valid=JSON.stringify(slot.operations[operation]?.result_input)===identity;
 const values=valid?slot.values[operation] as JsonValue[]|undefined:undefined;
 return {value:values?.[0] as unknown as T|undefined,status,error:slot.problems.map(p=>p.title).join('；'),invoke};
}

export function useSession(target:string){
 const port=useDisplay();
 useEffect(()=>()=>{void port.dispatch(action('reset',target,{}));},[port,target]);
}
