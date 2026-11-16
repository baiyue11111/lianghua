// Controlled browser surface: production components, real display core, no real transport.
import {createRoot} from 'react-dom/client';
import {createDisplay} from '../../display/src/core';
import {features,outputSchema} from '../../display/src/features';
import {action} from '../src/interaction/action';
import {DisplayProvider} from '../src/interaction/model';
import Workspace from '../src/Workspace';
import {DEFAULT_LIST, go} from '../src/app/router';
const spec={strategy_id:'test',module:'plugins.visual',name:'测试策略',parameters:[{key:'days',label:'观察周期',type:'integer',unit:'天'}],defaults:{days:20}};
const runs=Array.from({length:12},(_,i)=>({id:'internal-'+String(i).padStart(3,'0'),run_id:'internal-run-'+i,strategy_id:spec.strategy_id,status:'complete',qualification:'unverified',metrics:{total_return:i/100,max_drawdown:-.05,sharpe:.8},limitations:[],effective_config:{params:{days:i%2?30:20},market:{codes:['600000','000001']},start:'2024-01-02T00:00:00.123456+00:00',end:'2024-06-30T23:59:59.999999+00:00',initial_cash:'1000000'},config_hash:'b'.repeat(64)}));
const responses={'data.search':{items:[]},'data.bars':{dates:['2024-01-02','2024-01-03','2024-01-04'],candles:[[10,11,9,12],[11,10,9,12],[10,12,10,13]],volumes:[100,200,300]},'collection.automatic':{enabled:false},'collection.current':{running:false,status:'idle',done:0,total:0,rows:0,failed:[],log:[]},'research.runs':{items:runs},'backtest.strategies':{items:[spec]},'cli.backtest':{run_id:'internal-batch',intervals:[{name:'基准区间',result:{...runs[0],final_equity:'1100000'}},{name:'验证区间',result:{...runs[1],final_equity:'1010000'}}]},'research.benchmarks':[],'research.run':{...runs[0],manifest:{},trades:[{order_id:'b'.repeat(64),trade_date:'2024-01-02',symbol:'600000',side:'buy',shares:100,price:10,gross_amount:1000,commission:5,stamp_duty:0,transfer_fee:0,status:'filled'}],equity:[{at:'2024-01-02',equity:1000000},{at:'2024-06-30',equity:1100000}],offset:0}};
const modes=new Map(features().operations.map(item=>[item.operation,item.mode]));
const kinds=new Map([
 ['data.search','collection'],['data.bars','series'],['research.runs','collection'],['research.benchmarks','collection'],
 ['collection.sources','collection'],['collection.evidence','collection'],['backtest.strategies','collection'],
 ['data.check','document'],['collection.describe','document'],['collection.inspect','document'],
 ['data.research_inputs','document'],['backtest.prepare','document'],['cli.backtest','operation'],
]);
const roles=new Map([
 ['data.search','data-search'],['data.bars','kline'],['data.status','data-status'],['data.check','doctor'],
 ['collection.sources','sources'],['collection.current','fetch-job'],['collection.automatic','continuous-fetch'],
 ['research.runs','runs'],['research.run','run'],['research.benchmarks','benchmarks'],['research.benchmark','benchmark'],
 ['backtest.strategies','strategies'],['data.research_inputs','research-inputs'],['backtest.prepare','preflight'],
 ['backtest.run','backtest-result'],['cli.backtest','cli-backtest'],['cli.predict','cli-predict'],
 ['service.info','service-info'],['collection.describe','collection-describe'],['collection.inspect','collection-inspect'],
]);
const reply=(request,data)=>[{
 kind:kinds.get(request.operation)??'resource',
 message_id:request.request_id+'-reply',type:'urn:test',schema:outputSchema(request.operation),
 correlation_id:request.request_id,data,
 presentation:{title:request.operation,role:roles.get(request.operation)??request.operation.replace('.','-'),fields:[]},
 extensions:{'lianghua:operation':request.operation,'lianghua:resource':roles.get(request.operation)??request.operation.replace('.','-')},
}];
const core=createDisplay({...features(),backend:{send:async request=>{
 if(!Object.hasOwn(responses,request.operation)){
  // Read-only operations return an empty resource so the new screens render their empty states.
  if(modes.get(request.operation)==='write')throw new Error('Controlled fixture rejected operation');
  return reply(request,{});
 }
 return reply(request,responses[request.operation]);
}}});
// The scene is the address, because the address is what the app reads now.
function sceneRoute(scene){
 if(scene==='market')return {name:'market'};
 if(scene==='data')return {name:'data'};
 if(scene==='run')return {name:'newRun',module:'plugins.visual'};
 if(['result','config','trades'].includes(scene))return {name:'run',id:runs[0].id};
 if(scene==='detail')return {name:'strategy',key:spec.strategy_id};
 return {name:'strategies',list:DEFAULT_LIST};
}
async function show(){
 const scene=location.hash.slice(1)||'strategies';
 await core.dispatch(action('edit','apple:result',{tab:scene==='config'?'config':scene==='trades'?'trades':'overview',offset:0}));
 if(scene==='market')await core.dispatch(action('edit','apple:market',{stock:{代码:'600000',名称:'测试证券',交易所:'测试交易所',状态:'现存'},keyword:'',watch:[{代码:'600000',名称:'测试证券',交易所:'测试交易所',状态:'现存'}]}));
 go(sceneRoute(scene),true);
 createRoot(document.getElementById('root')).render(<DisplayProvider value={core}><Workspace/></DisplayProvider>);
}
void show();
