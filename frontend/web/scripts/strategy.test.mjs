import test from 'node:test';
import assert from 'node:assert/strict';
import {backtestInput,parameterValues} from '../../display/src/workbench/strategy-model.ts';
const spec={module:'strategies.test',defaults:{count:8,threshold:.5,order:'small',enabled:true},parameters:[
  {key:'count',label:'持股数量',type:'integer',default:8,minimum:1,maximum:30},
  {key:'threshold',label:'阈值',type:'number',default:.5,exclusive_minimum:0},
  {key:'order',label:'排序',type:'string',default:'small',options:['small','large']},
  {key:'enabled',label:'开关',type:'boolean',default:true},
]};
const input=(values=parameterValues(spec),capital='100000',start='2020-01-01',end='2020-12-31')=>backtestInput(spec,values,capital,start,end,'history');
test('schema values become typed strategy input; changing parameters, capital or dates changes request identity',()=>{
  assert.deepEqual(input().params,spec.defaults);
  const original=input();
  for(const changed of [input({...parameterValues(spec),count:'12'}),input(undefined,'200000'),input(undefined,undefined,'2020-02-01')])assert.notEqual(JSON.stringify(original),JSON.stringify(changed));
  assert.equal(spec.defaults.count,8);
});
test('invalid numeric, enum, date and capital values fail without guessing defaults',()=>{
  for(const values of [{count:''},{count:'1.5'},{count:'31'},{threshold:'0'},{order:'unknown'},{enabled:'true'}])assert.throws(()=>input({...parameterValues(spec),...values}));
  assert.throws(()=>input(undefined,'0'),/资金/);
  assert.throws(()=>input(undefined,undefined,'2020-02-30'),/日期/);
  assert.throws(()=>input(undefined,undefined,'2021-01-01','2020-01-01'),/早于/);
});



test('comparison only admits the same history, period, code, execution and unsampled dates',async()=>{
  const {comparisonIssue}=await import('../../display/src/workbench/strategy-model.ts');
  const first={input:input(),result:{data_hash:'data',code_hash:'code',execution_hash:'execution',equity:[{at:'2020-01-01',equity:100000},{at:'2020-01-02',equity:110000}]}};
  const changed=structuredClone(first);changed.input.params.count=12;changed.input.initial_cash=200000;
  assert.equal(comparisonIssue(first,changed),'');
  for(const key of ['data_hash','code_hash','execution_hash']) {
    const other=structuredClone(changed);other.result[key]='different';assert.notEqual(comparisonIssue(first,other),'');
    delete other.result[key];assert.match(comparisonIssue(first,other),/未提供/);
  }
  const other=structuredClone(changed);other.input.start='2020-01-02';assert.match(comparisonIssue(first,other),/时段/);
  other.input.start=first.input.start;other.result.equity.pop();assert.match(comparisonIssue(first,other),/日期/);
  other.result.equity=structuredClone(first.result.equity);other.result.equity_sampling={stride:2};assert.match(comparisonIssue(first,other),/抽样/);
});

test('a strategy unknown to frontend obtains its archive name and rules from the plugin catalog',async()=>{
  const {strategyGroups}=await import('../../display/src/workbench/research-model.ts');
  const plugin={strategy_id:'moving_average_cross',name:'双均线交叉',description:'快均线高于慢均线时持有',parameters:[],defaults:{},module:'strategies.moving_average_cross'};
  const run={id:'new',run_id:'new',strategy_id:plugin.strategy_id,metrics:{sharpe:null},qualification:'unverified'};
  const groups=strategyGroups([run],[plugin]);
  assert.equal(groups[0].name,plugin.name);
  assert.equal(groups[0].description,plugin.description);
  assert.equal(groups[0].metrics.sharpe,undefined);
});


test('catalog includes unused plugins, separates same names, and retains removed plugin archives',async()=>{
  const {strategyGroups,visibleStrategies,groupStatus}=await import('../../display/src/workbench/research-model.ts');
  const first={module:'plugins.first',strategy_id:'first',name:'相同名称',description:'首个',parameters:[],defaults:{}};
  const second={...first,module:'plugins.second',strategy_id:'second',description:'第二个'};
  const archive={id:'removed-run',run_id:'removed-run',strategy_id:'removed',metrics:{},qualification:'unverified'};
  const groups=strategyGroups([archive],[first,second]);
  assert.equal(groups.length,3);
  assert.deepEqual(groups.slice(0,2).map(item=>item.spec.module),['plugins.first','plugins.second']);
  assert.equal(groups[0].runs.length,0);
  assert.equal(groupStatus(groups[0]),'尚未回测');
  assert.equal(groups[2].spec,undefined);
  assert.equal(groupStatus(groups[2]),'仅存档');
  assert.equal(visibleStrategies(groups,'second','name').length,1);
});
