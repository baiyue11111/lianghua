// Test-only port: browser checks exercise the production Research component.
import React from 'react';
import {flushSync} from 'react-dom';
import {fixtureCore,DisplayProvider} from './display.fixture';
import {createRoot} from 'react-dom/client';
import Research from '../src/workbench/Research';
const w=window;w.calls=[];w.error=[];
const plugin=id=>({module:`plugins.${id}`,strategy_id:id,name:id==='empty'?'零回测策略':'另一策略',description:'均线规则',parameters:[{key:'days',label:'周期',type:'integer',default:20,minimum:1}],defaults:{days:20}});
const archive={id:'old',run_id:'old',strategy_id:'removed',metrics:{},qualification:'unverified',status:'complete',limitations:[],equity:[],trades:[],evidence:[],source_hashes:{},manifest:{},trades_total:0};
w.port={benchmarks:async()=>[],strategies:async()=>{w.calls.push(['catalog']);return {items:[plugin('empty'),plugin('other')],availability:{ready:true,issues:[],datasets:[{id:'data',name:'测试数据',start:'2020-01-01',end:'2020-01-02'}]}}},runs:async()=>[archive],run:async(id)=>{w.calls.push(['archive',id]);return {...archive,...w.completed}},preflight:async(input)=>{w.calls.push(['check',input]);if(w.delay)await new Promise(resolve=>w.pending=resolve);return {ready:true,issues:[],effective_params:input.params,requirements:{fields:['close'],history_bars:20,frequency:'daily',price_basis:'adjusted'}}},backtest:async(input)=>{w.calls.push(['run',input]);return w.completed={...archive,id:'new',run_id:'new',strategy_id:input.module.split('.').at(-1),parameters:input.params,effective_config:input,config_hash:'config',code_hash:'code',data_hash:'data',execution_hash:'execution',equity:[{at:'2020-01-01',equity:Number(input.initial_cash)},{at:'2020-01-02',equity:Number(input.initial_cash)}]}}};
let active,core,serial=0;
w.root=createRoot(document.getElementById('root'));w.showError=value=>w.error.push(value);
w.render=(port=w.port)=>{
 const previous=core;
 if(active!==port){active=port;core=fixtureCore(port);serial++;}
 flushSync(()=>w.root.render(<DisplayProvider key={serial} value={core}><div className="wb"><Research error={w.showError} inspect={()=>{}}/></div></DisplayProvider>));
 if(previous&&previous!==core)previous.dispose();
};w.render();
