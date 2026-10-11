// Mounted regression tests with a test-only backend and no listening port.
import {build} from '../../display/node_modules/esbuild/lib/main.js';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';
const [playwrightEntry]=process.argv.slice(2);
if(!playwrightEntry)throw Error('Usage: node scripts/audit-ui.mjs <playwright index.mjs>');
const {chromium}=await import(pathToFileURL(resolve(playwrightEntry)).href);
const root=fileURLToPath(new URL('../../../',import.meta.url));
const fixture=`
import {createRoot} from '${root}frontend/web/node_modules/react-dom/client';
import Audit from '${root}frontend/web/src/workbench/Audit.tsx';
import Sources from '${root}frontend/web/src/workbench/Sources.tsx';
import {DisplayProvider} from '${root}frontend/web/src/interaction/model.ts';
import {createDisplay} from '${root}frontend/display/src/core.ts';
import {features,outputSchema} from '${root}frontend/display/src/features.ts';
const sources=['a','b'].map(id=>({id,name:'source '+id,defaults:{},snapshot:{snapshot_id:'snap-'+id,collected_at:'now',data_as_of:null,scope:{}},frequency:'daily',job:null,schedule:null,integrity_error:null,records:1}));
window.sent=[];
const core=createDisplay({...features(),backend:{send:async request=>{
 window.sent.push(request);const data=request.data;
 if(window.pause&&request.operation==='workbench.sources')await new Promise(resolve=>window.finish=resolve);
 const result=request.operation==='workbench.sources'?sources:request.operation==='workbench.snapshot'?{snapshot_id:data.id,source_id:data.id,records:[{name:'record'}],offset:data.offset,total:1,limit:25,request:{},scope:{},collected_at:'now',data_as_of:null}:request.operation==='workbench.evidence'?{snapshot_id:data.id,record_index:data.index,record:{name:'record'},raw:[],request:{},scope:{},collected_at:'now',reviews:[{status:'correct',note:'existing reviewed note'}]}:data;
 return [{message_id:request.request_id,kind:'resource',type:'urn:test',schema:outputSchema(request.operation),correlation_id:request.request_id,data:result}];
}}});
window.core=core;const app=createRoot(document.getElementById('root'));
window.renderAudit=initial=>app.render(<DisplayProvider value={core}><Audit initial={initial} error={()=>{}} onSync={()=>{}}/></DisplayProvider>);
window.renderSources=()=>app.render(<DisplayProvider value={core}><Sources error={()=>{}} open={()=>{}}/></DisplayProvider>);
window.refreshSlow=()=>{window.pause=true;void core.dispatch({action_id:'refresh',kind:'invoke',target:'sources',operation:'workbench.sources',data:{}});};
window.renderAudit('a');
`;
const bundle=await build({stdin:{contents:fixture,resolveDir:root+'frontend/web',loader:'jsx'},bundle:true,write:false,jsx:'automatic',alias:{'@display':root+'frontend/display/src'},define:{'process.env.NODE_ENV':'"development"'},loader:{'.css':'empty'}});
const browser=await chromium.launch({headless:true});
try {
 const page=await browser.newPage(),errors=[];page.setDefaultTimeout(5000);page.on('pageerror',error=>errors.push(error.message));
 await page.setContent('<div id="root"></div>');await page.addScriptTag({content:bundle.outputFiles[0].text});
 const sample=page.getByRole('button',{name:'随机抽样'});await sample.waitFor();await sample.click();await page.getByLabel('审核结论').waitFor();
 assert.equal(await page.getByLabel('审核结论').inputValue(),'correct');
 assert.equal(await page.getByLabel('审核备注').inputValue(),'existing reviewed note');
 await page.getByRole('button',{name:'保存审核结论'}).click();
 assert.deepEqual(await page.evaluate(()=>window.sent.find(r=>r.operation==='workbench.review').data),{snapshot_id:'snap-a',record_index:0,status:'correct',note:'existing reviewed note'});
 await page.evaluate(()=>window.renderAudit('b'));
 await page.waitForFunction(()=>document.querySelector('[aria-label="审核数据来源"]')?.value==='b');
 assert.equal(await page.getByRole('dialog').count(),0);
 await page.evaluate(()=>window.renderSources());await page.getByLabel('source a起始日期').waitFor();await page.getByLabel('source a起始日期').focus();
 await page.evaluate(()=>window.refreshSlow());
 await page.waitForFunction(()=>window.finish!==undefined);
 assert.equal(await page.getByLabel('source a起始日期').count(),1);
 assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('aria-label')),'source a起始日期');
 await page.evaluate(()=>{window.pause=false;window.finish()});
 assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('aria-label')),'source a起始日期');
 assert.deepEqual(errors,[]);await page.evaluate(()=>window.core.dispose());
 console.log('PASS: existing audit review preserved; selected source changes; same-input refresh retains forms and focus. No port opened.');
}finally{await browser.close();}
