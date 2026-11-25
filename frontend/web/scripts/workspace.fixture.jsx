import {renderToString} from 'react-dom/server';
import {createDisplay} from '../../display/src/core';
import {features,outputSchema} from '../../display/src/features';
import {action} from '../src/interaction/action';
import {DisplayProvider} from '../src/interaction/model';
import {ConsoleContext} from '../src/apple/console';
import {nodes} from './capture';
import Workspace from '../src/Workspace';
export * from '../src/research/model';
export * from '../src/shared/format';
export {strategyGroups as originalGroups} from '../../display/src/research/research-model';
export {strategyGroups,runConfig} from '../src/research/groups';
export * from '../src/app/router';
export function fixture(responses={},consoleSnapshot){
  const calls=[];
  const core=createDisplay({...features(),backend:{send:async request=>{
    calls.push({operation:request.operation,data:request.data});
    const value=responses[request.operation];
    if(value instanceof Error)throw value;
    return [{
      message_id:request.request_id+'-response',kind:'resource',type:'urn:test',schema:outputSchema(request.operation),
      correlation_id:request.request_id,data:typeof value==='function'?await value(request.data):value??{},
      presentation:{title:request.operation,role:request.operation.replace('.','-'),fields:[]},
      extensions:{'lianghua:operation':request.operation},
    }];
  }}});
  const edit=(target,data)=>core.dispatch(action('edit',target,data));
  const read=(target,operation,data={})=>core.dispatch(action('invoke',target,data,operation));
  function render(){
    nodes.length=0;
    const html=renderToString(<ConsoleContext.Provider value={consoleSnapshot}><DisplayProvider value={core}><Workspace/></DisplayProvider></ConsoleContext.Provider>);
    return {html,nodes:[...nodes]};
  }
  return {core,calls,edit,read,render};
}
