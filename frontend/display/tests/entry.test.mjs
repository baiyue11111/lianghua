import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {createInterface} from 'node:readline';
import {build} from 'esbuild';
import {createDisplay} from '../dist/core.mjs';
import {features,outputSchema} from '../dist/features.mjs';
const root=new URL('../../../',import.meta.url).pathname;
await build({entryPoints:[new URL('../../web/src/interaction/action.ts',import.meta.url).pathname],outfile:new URL('../dist/web-action.mjs',import.meta.url).pathname,bundle:true,platform:'node',format:'esm'});
const {action}=await import('../dist/web-action.mjs');
test('Python CLI and Web actions use the same core, requests and state sequence',async()=>{
 const pythonAction=JSON.parse(execFileSync(root+'.venv/bin/python',['-c',"from frontend.cli import build_parser,to_action; import json; print(json.dumps(to_action(build_parser().parse_args(['run','--config','x.json'])).to_dict()))"],{cwd:root}));
 const data=pythonAction.data;
 const requests=[];
 const reply=r=>[{protocol:'lianghua.ui',version:1,message_id:r.request_id+'-m',kind:'resource',type:'urn:test',schema:outputSchema(r.operation),correlation_id:r.request_id,data:{amount:'9007199254740993.10',input:r.data}}];
 const web=createDisplay({...features(),backend:{send:async request=>{requests.push(request);return reply(request);}}});
 const child=spawn('node',[root+'frontend/display/runtime/cli.mjs','--connected'],{stdio:['pipe','pipe','pipe']});
 const lines=createInterface({input:child.stdout})[Symbol.asyncIterator]();
 const send=frame=>child.stdin.write(JSON.stringify(frame)+'\n');
 const expectedActions=[action('edit','cli',{note:'测试'}),action('invoke','cli',data,'cli.run'),action('reset','cli',{})];
 const incomingActions=[{...expectedActions[0],action_id:'cli-edit'},pythonAction,{...expectedActions[2],action_id:'cli-reset'}];
 try {
  for(let i=0;i<expectedActions.length;i++){
   const expected=await web.dispatch(expectedActions[i]);send({kind:'action',action:incomingActions[i]});
   let frame=JSON.parse((await lines.next()).value);
   if(frame.kind==='request'){assert.deepEqual(frame.request,requests.at(-1));send({kind:'response',request:frame.request,messages:reply(frame.request)});frame=JSON.parse((await lines.next()).value);}
   assert.equal(frame.kind,'state');assert.deepEqual(frame.state,expected);
  }
 }finally{web.dispose();send({kind:'close'});child.stdin.end();await new Promise(resolve=>child.once('close',resolve));}
 assert.equal(child.exitCode,0);
});
