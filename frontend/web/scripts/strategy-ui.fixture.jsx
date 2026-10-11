// UI test fixture only. These records never enter the application data path.
import React from 'react';
import {flushSync} from 'react-dom';
import {fixtureCore,DisplayProvider} from './display.fixture';
import {createRoot} from 'react-dom/client';
import StrategyLab from '../src/workbench/StrategyLab';
const w=window;
w.calls=[];w.pending=null;w.blocked=false;w.nextData='data';w.nextExecution='execution';w.count=0;
const spec={module:'strategies.test',strategy_id:'test',name:'测试策略',description:'',defaults:{count:8,order:'small',enabled:true,note:''},parameters:[{key:'count',label:'持股数量',type:'integer',default:8,minimum:1,maximum:20},{key:'order',label:'排序',type:'string',default:'small',options:['small','large']},{key:'enabled',label:'开关',type:'boolean',default:true},{key:'note',label:'备注',type:'string',default:''}]};
const other={module:'strategies.cross',strategy_id:'cross',name:'另一类策略',description:'',defaults:{fast_window:20,slow_window:60,exposure:1},parameters:[{key:'fast_window',label:'快线周期',type:'integer',default:20,minimum:1},{key:'slow_window',label:'慢线周期',type:'integer',default:60,minimum:2},{key:'exposure',label:'资金比例',type:'number',default:1,minimum:0,maximum:1}]};
const port={strategies:async()=>({items:[spec,other],availability:{ready:true,issues:[],datasets:[{id:'verified',name:'测试数据',start:'2020-01-01',end:'2020-01-02'}]}}),preflight:async(input)=>{w.calls.push(['check',input]);if(w.delay)await new Promise(resolve=>w.pending=resolve);return {ready:!w.blocked,effective_params:input.params,requirements:{fields:['close'],history_bars:input.params.count,frequency:'daily',price_basis:'adjusted'},config_hash:JSON.stringify(input),issues:w.blocked?[{code:'history',message:'历史数据未验收'}]:[]}},backtest:async(input)=>{w.calls.push(['run',input]);if(w.delay)await new Promise(resolve=>w.pending=resolve);const id=String(++w.count);return {id,run_id:id,strategy_id:'test',qualification:'qualified',metrics:{total_return:input.params.count/100,max_drawdown:-.1,sharpe:null},parameters:input.params,effective_config:input,config_hash:JSON.stringify(input),code_hash:'code',data_hash:w.nextData,execution_hash:w.nextExecution,equity:[{at:'2020-01-01',equity:Number(input.initial_cash)},{at:'2020-01-02',equity:Number(input.initial_cash)*(1+input.params.count/100)}]}}};
w.spec=spec;w.other=other;w.availability={ready:true,issues:[],datasets:[{id:'verified',name:'测试数据',start:'2020-01-01',end:'2020-01-02'}]};
let active,core,serial=0;
w.root=createRoot(document.getElementById('root'));
w.render=(selected=port,strategy=spec)=>{
 const previous=core;
 if(active!==selected){active=selected;core=fixtureCore(selected);serial++;}
 flushSync(()=>w.root.render(<DisplayProvider key={serial} value={core}><div className="wb"><StrategyLab key={strategy.module} spec={strategy} availability={w.availability} inspect={(title,value)=>{w.inspect={title,value}}}/></div></DisplayProvider>));
 if(previous&&previous!==core)previous.dispose();
};w.port=port;w.render();
