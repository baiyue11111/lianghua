import type {JsonValue,Operation,Options} from './core';
import {collect} from './collect';
import {backtestInput} from './research/strategy-model';
import type {StrategySpec} from './research/port';
const object=(properties:Record<string,object>={},required=Object.keys(properties))=>({type:'object',properties,required,additionalProperties:false});
const text={type:'string'},integer={type:'integer',minimum:0},boolean={type:'boolean'};
const defs:Record<string,{input:object;mode?:'write';poll?:number;changes?:Operation['changes']}>={
 'backtest.strategies':{input:object()},'research.runs':{input:object()},
 'research.run':{input:object({id:text,offset:integer})},
 'research.benchmarks':{input:object()},'research.benchmark':{input:object({id:text,start:text,end:text})},
 'collection.sources':{input:object(),poll:3000},
 'collection.sync':{input:object({id:text,input:{type:'object'}}),mode:'write'},
 'collection.schedule':{input:object({id:text,enabled:boolean,interval_seconds:{type:'integer',minimum:30},input:{type:'object'}}),mode:'write'},
 'collection.job':{input:object({id:text}),poll:1000},
 'collection.snapshot':{input:object({id:text,offset:integer})},
 'collection.evidence':{input:object({id:text,index:integer}),changes:(_state,_target,_request,messages)=>{
   const record=messages[0]?.data as {reviews?:{status:string;note:string}[]};
   const review=record.reviews?.[0];
   return {audit:{status:review?.status??'pending',note:review?.note??''}};
 }},
 'data.research_inputs':{input:object()},
 'data.status':{input:object({rescan:boolean}),poll:3000},'data.search':{input:object({q:text})},
 'data.bars':{input:object({code:text,freq:text,adj:text,start:text,end:text},['code','freq','adj'])},
 'collection.start':{input:{type:'object'},mode:'write'},'collection.current':{input:object(),poll:1000},
 'collection.stop':{input:object(),mode:'write'},'collection.automatic':{input:object(),poll:1000},
 'collection.set_automatic':{input:object({enabled:boolean}),mode:'write'},
 'collection.merge':{input:object({dryRun:boolean}),mode:'write'},'collection.merge_job':{input:object(),poll:1000},
 'data.check':{input:object(),poll:3000},'collection.repair':{input:object({id:text}),mode:'write'},'collection.repair_job':{input:object(),poll:1000},
 'cli.ingest':{input:object({price:text,fundamentals:text,valuations:text,output:text,max_symbols:{anyOf:[integer,{type:'null'}]},price_part:{anyOf:[{type:'array',items:integer},{type:'null'}]}}),mode:'write'},
 'cli.backtest':{input:object({config:text}),mode:'write'},
 'collection.describe':{input:object()},
 'collection.inspect':{input:object({source_id:text,codes:{type:'array',items:text},start:text,end:text,fields:{type:'array',items:text}},['source_id'])},
 'collection.run':{input:{type:'object',required:['source_id'],properties:{source_id:text},additionalProperties:true},mode:'write'},
 'cli.collect':{input:object({selector:text,data:text,codes:text,fields:text,start:text,end:text,interval:text},['selector']),mode:'write'},
};
export const inputSchema=(operation:string)=>`urn:lianghua:ui:input:${operation}:v1`;
export const outputSchema=(operation:string)=>`urn:lianghua:ui:output:${operation}:v1`;
export function features():Pick<Options,'operations'|'schemas'> {
 const schemas:Record<string,object>={'urn:lianghua:ui:problem:v1':{type:'object'}};
 const operations:Operation[]=Object.entries(defs).map(([operation,definition])=>{
   schemas[inputSchema(operation)]=definition.input;
   // Existing feature data is JSON; transport/schema migration belongs to the backend adapter.
   schemas[outputSchema(operation)]={};
   return {operation,input_schema:inputSchema(operation),response_schemas:[outputSchema(operation),'urn:lianghua:ui:problem:v1'],mode:definition.mode??'read',poll:definition.poll,changes:definition.changes,...(operation==='cli.collect'?{execute:collect}:{})};
 });
 const prepare=(raw:JsonValue):JsonValue=>{
   const data=raw as unknown as {spec:StrategySpec;values:Record<string,string|boolean>;capital:string;start:string;end:string;dataset:string};
   const input=backtestInput(data.spec,data.values,data.capital,data.start,data.end,data.dataset);
   // Keep exact monetary text on the wire; business numeric validation remains unchanged.
   return {...input,initial_cash:data.capital.trim()} as unknown as JsonValue;
 };
 const form={type:'object',required:['spec','values','capital','start','end','dataset']};
 for(const operation of ['backtest.prepare','backtest.run']){
   schemas[inputSchema(operation)]={type:'object',required:['module','params','initial_cash','start','end','dataset_id'],properties:{module:text,params:{type:'object'},initial_cash:{type:'string',pattern:'^[0-9]+(\\.[0-9]+)?$'},start:text,end:text,dataset_id:text},additionalProperties:false};
   schemas[outputSchema(operation)]={type:'object'};
   operations.push({operation,input_schema:inputSchema(operation),response_schemas:[outputSchema(operation),'urn:lianghua:ui:problem:v1'],mode:'write',prepare,
     ...(operation==='backtest.run'?{requires:'backtest.prepare',changes:(state,target,request,messages):Record<string,Record<string,JsonValue>>=>{
       const completed={input:request.data,result:messages.find(m=>m.kind!=="problem")!.data};
       const comparison=state.targets[target+":comparison"]?.values??{};
       const result=completed.result as {id:string;run_id:string};
       const runs=(state.targets["research:archives"]?.values["research.runs"] as JsonValue[][]|undefined)?.[0]??[];
       return {[target+":comparison"]:{baseline:comparison.baseline??completed,latest:completed},
         "research:archives":{"research.runs":[[result as unknown as JsonValue,...runs.filter(run=>(run as {id?:string})?.id!==result.id)]]},
         research:{current:result.id}};
     }}:{ready:messages=>messages.some(m=>(m.data as {ready?:boolean})?.ready===true)}),
   });
 }
 // Exported by the feature, rather than added to the generic public contract.
 schemas['urn:lianghua:ui:form:v1']=form;
 return {operations,schemas};
}
