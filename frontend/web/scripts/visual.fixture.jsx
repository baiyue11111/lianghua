// Controlled browser surface: production components, real display core, no real transport.
import {createRoot} from 'react-dom/client';
import {createDisplay} from '../../display/src/core';
import {features,outputSchema} from '../../display/src/features';
import {action} from '../src/interaction/action';
import {DisplayProvider} from '../src/interaction/model';
import Workspace from '../src/Workspace';
const spec={strategy_id:'test',module:'plugins.visual',name:'测试策略',parameters:[{key:'days',label:'观察周期',type:'integer',unit:'天'}],defaults:{days:20}};
const runs=Array.from({length:12},(_,i)=>({id:'internal-'+String(i).padStart(3,'0'),run_id:'internal-run-'+i,strategy_id:spec.strategy_id,status:'complete',qualification:'unverified',metrics:{total_return:i/100,max_drawdown:-.05,sharpe:.8},limitations:[],effective_config:{params:{days:i%2?30:20},market:{codes:['600000','000001']},start:'2024-01-02T00:00:00.123456+00:00',end:'2024-06-30T23:59:59.999999+00:00',initial_cash:'1000000'},config_hash:'b'.repeat(64)}));
const responses={'lake.query':{items:[]},'lake.kline':{dates:['2024-01-02','2024-01-03','2024-01-04'],candles:[[10,11,9,12],[11,10,9,12],[10,12,10,13]],volumes:[100,200,300]},'lake.continuousFetch':{enabled:false},'lake.fetchJob':{running:false,status:'idle',done:0,total:0,rows:0,failed:[],log:[]},'workbench.runs':{items:runs},'workbench.strategies':{items:[spec]},'cli.backtest':{run_id:'internal-batch',intervals:[{name:'基准区间',result:{...runs[0],final_equity:'1100000'}},{name:'验证区间',result:{...runs[1],final_equity:'1010000'}}]},'workbench.benchmarks':[],'workbench.run':{...runs[0],manifest:{},trades:[{order_id:'b'.repeat(64),trade_date:'2024-01-02',symbol:'600000',side:'buy',shares:100,price:10,gross_amount:1000,commission:5,stamp_duty:0,transfer_fee:0,status:'filled'}],equity:[{at:'2024-01-02',equity:1000000},{at:'2024-06-30',equity:1100000}],offset:0}};
const core=createDisplay({...features(),backend:{send:async request=>{
 if(!Object.hasOwn(responses,request.operation))throw new Error('Controlled fixture rejected operation');
 return [{kind:'resource',message_id:request.request_id+'-reply',type:'urn:test',schema:outputSchema(request.operation),correlation_id:request.request_id,data:responses[request.operation]}];
}}});
async function show(){
 const scene=location.hash.slice(1)||'attempts';
 await core.dispatch(action('edit','workspace',{page:scene==='market'?'market':'research'}));
 await core.dispatch(action('edit','research',{group:['research','batch'].includes(scene)?'':'test',backtest:scene==='batch',current:['result','config','trades'].includes(scene)?runs[0].id:'',tab:scene==='config'?'config':scene==='trades'?'trades':'overview',names:Object.fromEntries(runs.map((run,i)=>[run.id,`尝试 ${i+1}`]))}));
 if(scene==='batch'){await core.dispatch(action('edit','cli-form',{config:'/controlled/config.yaml',submitted:'/controlled/config.yaml'}));await core.dispatch(action('invoke','cli-result',{config:'/controlled/config.yaml'},'cli.backtest'));}
 if(scene==='market')await core.dispatch(action('edit','chart',{stock:{代码:'600000',名称:'测试证券',交易所:'测试交易所',状态:'现存'},keyword:''}));
 createRoot(document.getElementById('root')).render(<DisplayProvider value={core}><Workspace/></DisplayProvider>);
}
void show();
