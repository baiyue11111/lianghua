import type {JsonValue,Operation,Options} from './core';
import {backtestInput} from './workbench/strategy-model';
import type {StrategySpec} from './workbench/port';
const object=(properties:Record<string,object>={},required=Object.keys(properties))=>({type:'object',properties,required,additionalProperties:false});
const text={type:'string'},integer={type:'integer',minimum:0},boolean={type:'boolean'};
const defs:Record<string,{input:object;mode?:'write';poll?:number;changes?:Operation['changes']}>={
 'workbench.strategies':{input:object()},'workbench.runs':{input:object()},
 'workbench.run':{input:object({id:text,offset:integer})},
 'workbench.benchmarks':{input:object()},'workbench.benchmark':{input:object({id:text,start:text,end:text})},
 'workbench.sources':{input:object(),poll:3000},
 'workbench.sync':{input:object({id:text,input:{type:'object'}}),mode:'write'},
 'workbench.schedule':{input:object({id:text,enabled:boolean,interval_seconds:{type:'integer',minimum:30},input:{type:'object'}}),mode:'write'},
 'workbench.job':{input:object({id:text}),poll:1000},
 'workbench.snapshot':{input:object({id:text,offset:integer})},
 'workbench.evidence':{input:object({id:text,index:integer}),changes:(_state,_target,_request,messages)=>{
   const record=messages[0]?.data as {reviews?:{status:string;note:string}[]};
   const review=record.reviews?.[0];
   return {audit:{status:review?.status??'pending',note:review?.note??''}};
 }},
 'workbench.reviews':{input:object()},'workbench.review':{input:object({snapshot_id:text,record_index:integer,status:{enum:['correct','pending','anomaly']},note:text}),mode:'write'},
 'lake.researchDaily':{input:object({asOf:text},[])},'lake.researchInputs':{input:object()},'lake.closedLoop':{input:object()},
 'lake.status':{input:object({rescan:boolean}),poll:3000},'lake.query':{input:object({q:text})},
 'lake.kline':{input:object({code:text,freq:text,adj:text,start:text,end:text},['code','freq','adj'])},
 'lake.startFetch':{input:{type:'object'},mode:'write'},'lake.fetchJob':{input:object(),poll:1000},
 'lake.stopFetch':{input:object(),mode:'write'},'lake.continuousFetch':{input:object(),poll:1000},
 'lake.setContinuousFetch':{input:object({enabled:boolean}),mode:'write'},
 'lake.startMerge':{input:object({dryRun:boolean}),mode:'write'},'lake.mergeJob':{input:object(),poll:1000},
 'lake.doctor':{input:object(),poll:3000},'lake.doctorFix':{input:object({id:text}),mode:'write'},'lake.doctorJob':{input:object(),poll:1000},
 'cli.ingest':{input:object({price:text,fundamentals:text,valuations:text,output:text,max_symbols:{anyOf:[integer,{type:'null'}]},price_part:{anyOf:[{type:'array',items:integer},{type:'null'}]}}),mode:'write'},
 'cli.backtest':{input:object({config:text}),mode:'write'},
 'cli.report':{input:object({artifact:text,output:text,builder:text,ready_timeout_ms:integer,timeout_ms:integer}),mode:'write'},
};
export const inputSchema=(operation:string)=>`urn:lianghua:ui:input:${operation}:v1`;
export const outputSchema=(operation:string)=>`urn:lianghua:ui:output:${operation}:v1`;
export function features():Pick<Options,'operations'|'schemas'> {
 const schemas:Record<string,object>={'urn:lianghua:ui:problem:v1':{type:'object'}};
 const operations:Operation[]=Object.entries(defs).map(([operation,definition])=>{
   schemas[inputSchema(operation)]=definition.input;
   // Existing feature data is JSON; transport/schema migration belongs to the backend adapter.
   schemas[outputSchema(operation)]={};
   return {operation,input_schema:inputSchema(operation),response_schemas:[outputSchema(operation),'urn:lianghua:ui:problem:v1'],mode:definition.mode??'read',poll:definition.poll,changes:definition.changes};
 });
 const prepare=(raw:JsonValue):JsonValue=>{
   const data=raw as unknown as {spec:StrategySpec;values:Record<string,string|boolean>;capital:string;start:string;end:string;dataset:string};
   const input=backtestInput(data.spec,data.values,data.capital,data.start,data.end,data.dataset);
   // Keep exact monetary text on the wire; business numeric validation remains unchanged.
   return {...input,initial_cash:data.capital.trim()} as unknown as JsonValue;
 };
 const form={type:'object',required:['spec','values','capital','start','end','dataset']};
 for(const operation of ['workbench.preflight','workbench.backtest']){
   schemas[inputSchema(operation)]={type:'object',required:['module','params','initial_cash','start','end','dataset_id'],properties:{module:text,params:{type:'object'},initial_cash:{type:'string',pattern:'^[0-9]+(\\.[0-9]+)?$'},start:text,end:text,dataset_id:text},additionalProperties:false};
   schemas[outputSchema(operation)]={type:'object'};
   operations.push({operation,input_schema:inputSchema(operation),response_schemas:[outputSchema(operation),'urn:lianghua:ui:problem:v1'],mode:'write',prepare,
     ...(operation==='workbench.backtest'?{requires:'workbench.preflight',changes:(state,target,request,messages):Record<string,Record<string,JsonValue>>=>{
       const completed={input:request.data,result:messages.find(m=>m.kind!=="problem")!.data};
       const comparison=state.targets[target+":comparison"]?.values??{};
       const result=completed.result as {id:string;run_id:string};
       const runs=(state.targets["research:archives"]?.values["workbench.runs"] as JsonValue[][]|undefined)?.[0]??[];
       return {[target+":comparison"]:{baseline:comparison.baseline??completed,latest:completed},
         "research:archives":{"workbench.runs":[[result as unknown as JsonValue,...runs.filter(run=>(run as {id?:string})?.id!==result.id)]]},
         research:{current:result.id}};
     }}:{ready:messages=>messages.some(m=>(m.data as {ready?:boolean})?.ready===true)}),
   });
 }
 // Exported by the feature, rather than added to the generic public contract.
 schemas['urn:lianghua:ui:form:v1']=form;
 return {operations,schemas};
}
