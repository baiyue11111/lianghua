// Tests only: turn fixed fixtures into standard backend messages. No production fake.
import {createDisplay} from '../../display/src/core';
import {features,outputSchema} from '../../display/src/features';
export {DisplayProvider} from '../src/interaction/model';
export function fixtureCore(port){
 return createDisplay({...features(),backend:{send:async request=>{
  const name=request.operation.split('.').at(-1),data=request.data;
  const args=name==='run'?[data.id,data.offset]:['preflight','backtest'].includes(name)?[data]:[];
  const result=await port[name](...args);
  return [{message_id:request.request_id+'-result',kind:'resource',type:'urn:test:'+name,schema:outputSchema(request.operation),correlation_id:request.request_id,data:result}];
 }}});
}
