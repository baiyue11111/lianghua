// Stdio adapter: the same interaction model used by the browser. No business imports.
import {createInterface} from 'node:readline';
import {createDisplay,validateWire} from './display/src/core.ts';
import {features} from './display/src/features.ts';
const waits=new Map();
const output=value=>process.stdout.write(JSON.stringify(value)+'\n');
const backend=process.argv.includes('--connected')?{send:request=>new Promise((resolve,reject)=>{
 waits.set(request.request_id,{resolve,reject});output({kind:'request',request});
})}:undefined;
const core=createDisplay({...features(),backend,progress:state=>output({kind:'state',state})});
const input=createInterface({input:process.stdin,crlfDelay:Infinity});
function close(){core.dispose();for(const wait of waits.values())wait.reject(Error('session closed'));waits.clear();input.close();}
let running=false;
input.on('line',async line=>{
 try {
  const frame=JSON.parse(line);
  if(!validateWire('UiFrame',frame))throw Error('Invalid bridge frame');
  if(frame.kind==='close'){close();return;}
  if(frame.kind==='response'){
   const wait=waits.get(frame.request?.request_id);
   if(!wait)throw Error('Unknown response request');
   waits.delete(frame.request.request_id);wait.resolve(frame.messages);return;
  }
  if(frame.kind!=='action'||!frame.action||running)throw Error('Expected one action at a time');
  running=true;
  const state=await core.dispatch(frame.action);
  output({kind:'state',state});running=false;
 }catch(reason){process.stderr.write(String(reason)+'\n');process.exitCode=1;close();}
});
input.on('close',()=>{core.dispose();for(const wait of waits.values())wait.reject(Error('session closed'));waits.clear();});
process.once('SIGINT',close);process.once('SIGTERM',close);
