import {renderToString} from 'react-dom/server';
import {createDisplay} from '../../display/src/core';
import {features,outputSchema} from '../../display/src/features';
import {action} from '../src/interaction/action';
import {DisplayProvider} from '../src/interaction/model';
import {nodes} from './capture';
import Workspace from '../src/Workspace';
import Indices from '../src/market/Indices';
import Chart from '../src/market/Chart';
import Collection from '../src/market/Collection';
import Backtest from '../src/research/Backtest';
import Research from '../src/research/Research';
import {RecordTable} from '../src/shared/Records';
import Fields from '../src/shared/Fields';
import BenchmarkChart from '../src/research/BenchmarkChart';
export * from '../src/research/model';
export * from '../src/shared/format';
export {strategyGroups as originalGroups} from '../../display/src/research/research-model';
export {strategyGroups,runConfig} from '../src/research/groups';
export function fixture(responses={}){
  const calls=[];
  const core=createDisplay({...features(),backend:{send:async request=>{
    calls.push({operation:request.operation,data:request.data});
    const value=responses[request.operation];
    if(value instanceof Error)throw value;
    return [{message_id:request.request_id+'-response',kind:'resource',type:'urn:test',schema:outputSchema(request.operation),correlation_id:request.request_id,data:typeof value==='function'?await value(request.data):value??{}}];
  }}});
  const edit=(target,data)=>core.dispatch(action('edit',target,data));
  const read=(target,operation,data={})=>core.dispatch(action('invoke',target,data,operation));
  function render(name,props={}){
    nodes.length=0;
    const Component={Workspace,Chart,Collection,Backtest,Research,Indices,RecordTable,Fields,BenchmarkChart}[name];
    const html=renderToString(<DisplayProvider value={core}><Component {...props}/></DisplayProvider>);
    return {html,nodes:[...nodes]};
  }
  return {core,calls,edit,read,render};
}
