import test from 'node:test';
import assert from 'node:assert/strict';



test('Chinese display preserves original strategy and evidence data',async()=>{
  const {label,localized,strategies}=await import('../../display/src/workbench/locale.ts');
  const original={strategy_id:'quality_trend',side:'buy',price:10.25,limitations:['CASH_DIVIDENDS_UNAVAILABLE']};
  const serialized=JSON.stringify(original);
  assert.deepEqual(localized(original),{'策略标识':'优质股趋势','买卖方向':'买入','价格':10.25,'数据限制':['缺少现金分红数据']});
  assert.equal(JSON.stringify(original),serialized);
  assert.equal(label('future_strategy'), 'future_strategy');
  assert.equal(Object.keys(strategies).length,4);
});

test('audit table numbers use field precision while preserving integral quantities',async()=>{
  const {show}=await import('../../display/src/workbench/format.ts');
  assert.equal(show(12.35233999,'price'),'12.35');
  assert.equal(show('12.35234000','commission'),'12.35');
  assert.equal(show(100,'shares'),'100');
  assert.equal(show(1.2345678),'1.2346');
  assert.equal(show({price:12.3,commission:5.4,shares:100}),'{"价格":"12.30","佣金":"5.40","股数":"100"}');
});

test('strategy research groups same identifiers, searches archived runs, and sorts group medians',async()=>{
  const {strategyGroups,visibleStrategies,formatMetricRange,groupStatus}=await import('../../display/src/workbench/research-model.ts');
  const run=(id,strategy_id,metrics)=>({id,run_id:id,strategy_id,status:'NO_QUALIFIED_STRATEGY',qualification:'unverified',limitations:[],metrics,manifest_file:'',manifest_sha256:'',metrics_sha256:'',source_hashes:{},config_hash:null,note:''});
  const groups=strategyGroups([
    run('run-b','quality_trend',{total_return:.20,max_drawdown:-.10,sharpe:3}),
    run('run-a','quality_trend',{total_return:.10,max_drawdown:-.30,sharpe:1}),
    run('run-c','trend_pullback',{total_return:.08,max_drawdown:-.20,sharpe:.7}),
  ]);
  assert.equal(groups.length,2);
  assert.equal(groups[0].runs.length,2);
  assert.equal(groups[0].metrics.total_return.median.toFixed(2),'0.15');
  assert.equal(groups[0].metrics.total_return.min,.10);
  assert.equal(groups[0].metrics.total_return.max,.20);
  assert.equal(groups[0].metrics.total_return.count,2);
  assert.equal(formatMetricRange(groups[0].metrics.total_return,'total_return'),'10.00% – 20.00%');
  assert.equal(formatMetricRange({min:.1,max:.1,median:.1,count:2},'total_return'),'10.00%');
  assert.equal(formatMetricRange(undefined,'sharpe'),'未提供');
  assert.equal(groupStatus(groups[0]),'仅存档');
  assert.deepEqual(visibleStrategies(groups,'run-b','name').map(group=>group.name),['优质股趋势']);
  assert.deepEqual(visibleStrategies(groups,'','sharpe').map(group=>group.name),['优质股趋势','趋势回调']);
  assert.deepEqual(visibleStrategies(groups,'','sharpe','asc').map(group=>group.name),['趋势回调','优质股趋势']);
});

test('strategy research keeps unmeasured metrics last and shows all matches',async()=>{
  const {strategyGroups,visibleStrategies}=await import('../../display/src/workbench/research-model.ts');
  const run=(id,strategy_id,metrics)=>({id,run_id:id,strategy_id,status:'NO_QUALIFIED_STRATEGY',qualification:'unverified',limitations:[],metrics,manifest_file:'',manifest_sha256:'',metrics_sha256:'',source_hashes:{},config_hash:null,note:''});
  const runs=Array.from({length:21},(_,i)=>run(`run-${i}`,`strategy_${i+1}`,i===0?{}:{sharpe:i}));
  const groups=strategyGroups(runs);
  assert.equal(visibleStrategies(groups,'','sharpe').length,21);
  assert.equal(visibleStrategies(groups,'strategy_21','name').length,1);
  const missing=strategyGroups([run('measured','quality_trend',{sharpe:.3}),run('unmeasured','trend_pullback',{})]);
  assert.equal(visibleStrategies(missing,'','sharpe').at(-1).name,'趋势回调');
});
