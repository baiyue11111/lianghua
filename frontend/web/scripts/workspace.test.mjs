import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from '../../display/node_modules/esbuild/lib/main.js';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const dir=await mkdtemp(join(tmpdir(),'lianghua-components-'));
const out=join(dir,'fixture.mjs');
const bundle=await build({entryPoints:[resolve('scripts/workspace.fixture.jsx')],bundle:true,write:false,platform:'node',format:'esm',jsx:'transform',jsxFactory:'capture',jsxFragment:'Fragment',inject:[resolve('scripts/capture.jsx')],alias:{'@display':resolve('../display/src')},loader:{'.css':'empty'},mainFields:['module','main'],external:['node:*'],banner:{js:"import {createRequire} from 'node:module';const require=createRequire(import.meta.url);"}});
await writeFile(out,bundle.outputFiles[0].text);
const {fixture,strategyGroups,runConfig,originalGroups,amount,day,parameters,configuration,attemptNames,errorMessage,go,routeNow,DEFAULT_LIST}=await import(pathToFileURL(out));
test.after(()=>rm(dir,{recursive:true,force:true}));
const find=(tree,label)=>{const node=(tree.nodes.find(n=>n.props['aria-label']===label)??tree.nodes.find(n=>n.children.flat().includes(label)));assert.ok(node,label);return node.props;};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const run=(id,strategy_id,config={})=>({id,run_id:id,strategy_id,status:'complete',qualification:'unverified',limitations:[],metrics:{total_return:.1},manifest_file:'',manifest_sha256:'',metrics_sha256:'',source_hashes:{},config_hash:id,effective_config:config});

test('catalog module and strategy IDs group the same history without changing stored records',()=>{
 const spec={strategy_id:'signal',module:'plugins.example',name:'策略',defaults:{},parameters:[]};
 const runs=[run('a','plugins.example',{start:'2020-01-01',end:'2021-01-01',initial_cash:'100.00'}),run('b','signal'),run('c','old',{module:'plugins.example'}),run('d','unknown')];
 const before=JSON.stringify(runs),groups=strategyGroups(runs,[spec]);
 assert.equal(originalGroups(runs,[spec]).length,4);assert.equal(groups.length,2);assert.deepEqual(groups[0].runs.map(r=>r.id),['a','b','c']);assert.equal(groups[0].runs[0],runs[0]);assert.equal(JSON.stringify(runs),before);assert.equal(runConfig(runs[0]).initial_cash,'100.00');
 const ambiguous=strategyGroups([run('a','shared')],[{...spec,module:'shared'},{...spec,strategy_id:'other',module:'shared'}]);
 assert.equal(ambiguous.find(g=>g.key==='shared').runs.length,1);
});

test('the market page reads a selected security and renders its K-line', async()=>{
 const stock={代码:'sh.600000',名称:'测试证券',交易所:'上海',状态:'现存'};
 const f=fixture({'data.search':{items:[stock]},'data.bars':{dates:['2020-01-01'],candles:[[1,2,1,2]],volumes:[100]}});
 try{
  go({name:'market'});
  await f.edit('apple:market',{watch:[stock]});
  find(f.render(),'搜索股票代码或名称').onChange({target:{value:' test '}});
  assert.equal(f.core.snapshot().targets['apple:market'].values.keyword,' test ');
  await f.read('apple:market:search','data.search',{q:'test'});
  const item=f.render().nodes.find(node=>node.props.className==='rail-item');
  assert.ok(item);
  item.props.onClick();
  const bars={code:stock.代码,freq:'日',adj:'不复权'};
  await f.read('apple:market:bars','data.bars',bars);
  const tree=f.render();
  assert.match(tree.html,/测试证券/);
  const option=tree.nodes.find(node=>node.props.option).props.option;
  assert.equal(option.series[0].type,'candlestick');
  assert.deepEqual(option.xAxis[0].data,['2020-01-01']);
  assert.deepEqual(f.calls,[{operation:'data.search',data:{q:'test'}},{operation:'data.bars',data:bars}]);
  assert.ok(!tree.nodes.some(node=>node.children.includes('分时')));
 }finally{f.core.dispose()}
});

test('the data page submits collection writes only after user events', async()=>{
 const f=fixture({'collection.automatic':{enabled:false},'collection.current':{running:false,status:'idle'},'collection.set_automatic':{enabled:true},'collection.start':{status:'running'}});
 try{
  go({name:'data'});
  assert.equal(find(f.render(),'自动采集').disabled,true);
  assert.equal(f.calls.length,0);
  await f.read('apple:data:auto','collection.automatic',{market:'沪深'});
  await f.read('apple:data:current','collection.current');
  assert.match(f.render().html,/空闲/);
  find(f.render(),'自动采集').onChange({target:{checked:true}});
  await settle();
  find(f.render(),'启动采集').onClick();
  await settle();
  assert.deepEqual(f.calls.filter(call=>['collection.set_automatic','collection.start'].includes(call.operation)),[
   {operation:'collection.set_automatic',data:{market:'沪深',enabled:true}},
   {operation:'collection.start',data:{mode:'auto'}},
  ]);
 }finally{go({name:'market'});f.core.dispose()}
});

test('the run page sends one trimmed server configuration path after submission', async()=>{
 const f=fixture({'cli.backtest':{run_id:'batch',intervals:[]}});
 try{
  go({name:'newRun',module:'plugins.example'});
  const tree=f.render();
  assert.equal(f.calls.length,0);
  assert.equal(find(tree,'按文件运行回测').disabled,true);
  find(tree,'配置文件路径').onChange({target:{value:' /server/config.json '}});
  assert.equal(f.calls.length,0);
  function hasConfigInput(child) {
   return child?.props?.['aria-label']==='配置文件路径'
    || (child?.props?.children != null && [child.props.children].flat().some(hasConfigInput));
  }
  const serverForm=f.render().nodes.find(node=>node.type==='form'&&node.children.some(hasConfigInput));
  assert.ok(serverForm);
  serverForm.props.onSubmit({preventDefault(){}});
  await settle();
  assert.deepEqual(f.calls,[{operation:'cli.backtest',data:{config:'/server/config.json'}}]);
 }finally{go({name:'market'});f.core.dispose()}
});

test('the sidebar lists destinations, and creating or opening a run is not one',()=>{
 const f=fixture();try{
  const tree=f.render();
  const nav=tree.nodes.filter(n=>n.props['data-od-id']?.startsWith('nav-'));
  // A destination is a place you can be without first choosing an object.
  // "新建回测" is an action and "回测结果" is whatever you last opened, so
  // neither can be a peer of 行情 — they live at #/runs/new and #/runs/<id>.
  assert.deepEqual(nav.map(n=>n.props['data-od-id']),['nav-market','nav-strategies','nav-data']);
  assert.match(tree.html,/行情/);assert.match(tree.html,/数据中心/);
  assert.doesNotMatch(tree.html,/数据审核|数据同步|营销/);
  assert.equal(f.calls.length,0);
 }finally{f.core.dispose()}
});

test('every screen has an address, and a bad address resolves to something real',async()=>{
 const {parse,href,DEFAULT_LIST}=await import(pathToFileURL(out)).then(m=>m.router??m);
 const list={...DEFAULT_LIST};
 assert.deepEqual(parse('#/strategies'),{name:'strategies',list});
 assert.deepEqual(parse('#/strategies/plugins.example?'),{name:'strategy',key:'plugins.example'});
 assert.deepEqual(parse('#/runs/attempt-1'),{name:'run',id:'attempt-1'});
 assert.deepEqual(parse('#/runs/new?module=plugins.example'),{name:'newRun',module:'plugins.example'});
 // Round trip: what a page writes is what a page can read back.
 for(const route of [{name:'market'},{name:'strategies',list:{...list,q:'均线',page:3}},{name:'strategy',key:'a b'},{name:'run',id:'x/y'},{name:'newRun',module:'m'},{name:'data'}]){
  assert.deepEqual(parse(href(route)),route);
 }
 // An unknown or truncated address is not a blank screen.
 assert.deepEqual(parse('#/nonsense'),{name:'market'});
 assert.deepEqual(parse('#/strategies/'),{name:'strategies',list});
 assert.equal(parse('#/runs/').name,'market');
});

test('the strategy list is sorted, searchable and paged by facts the archive can prove',async()=>{
 const router=await import(pathToFileURL(out));
 const spec=(id,name)=>({strategy_id:id,module:'plugins.'+id,name,description:'规则',defaults:{},parameters:[]});
 const catalog={items:[spec('alpha','甲策略'),spec('beta','乙策略')]};
 const runs=[run('a1','alpha'),run('a2','alpha'),run('a3','alpha'),run('b1','beta')];
 const f=fixture({'research.runs':{items:runs,total:4},'backtest.strategies':catalog});
 try{
  await f.read('apple:archives','research.runs',{limit:1000});await f.read('apple:catalog','backtest.strategies');
  // Default order is attempt count, descending: the strategy you actually use
  // is the one at the top, and the order is a stated fact, not an accident.
  router.go({name:'strategies',list:{...router.DEFAULT_LIST}},true);
  let tree=f.render();
  assert.ok(tree.html.indexOf('甲策略')<tree.html.indexOf('乙策略'),'more attempts first');
  assert.match(tree.html,/点一行进入策略详情/);
  assert.doesNotMatch(tree.html,/最近一次/,'no column claims a recency the archive cannot prove');

  // Searching by name narrows the list without losing the other strategy's data.
  router.go({name:'strategies',list:{...router.DEFAULT_LIST,q:'乙'}},true);
  tree=f.render();
  assert.doesNotMatch(tree.html,/甲策略/);assert.match(tree.html,/乙策略/);
  assert.match(tree.html,/1 个策略/);

  // An address that matches nothing says so instead of showing everything.
  router.go({name:'strategies',list:{...router.DEFAULT_LIST,q:'不存在的策略'}},true);
  assert.match(f.render().html,/没有符合筛选条件的策略/);
  router.go({name:'market'},true);
 }finally{router.go({name:'market'},true);f.core.dispose()}
});

test('a screen addressed at an object that is not there says so, and shows nothing else',async()=>{
 const router=await import(pathToFileURL(out));
 const spec={strategy_id:'alpha',module:'plugins.alpha',name:'甲策略',description:'规则',defaults:{},parameters:[]};
 const f=fixture({'research.runs':{items:[run('a1','alpha')],total:1},'backtest.strategies':{items:[spec]}});
 try{
  await f.read('apple:archives','research.runs',{limit:1000});await f.read('apple:catalog','backtest.strategies');
  // A stale or wrong strategy address must not resolve to "the first strategy":
  // that would put another object's numbers under this address.
  router.go({name:'strategy',key:'gone'},true);
  let html=f.render().html;
  assert.match(html,/策略不在存档中/);assert.doesNotMatch(html,/甲策略/);
  assert.match(html,/返回策略列表/);

  router.go({name:'run',id:'gone'},true);
  html=f.render().html;
  assert.match(html,/这次回测不在存档中/);assert.doesNotMatch(html,/甲策略/);

  // The real object still opens, at its own address.
  router.go({name:'strategy',key:'alpha'},true);
  html=f.render().html;
  assert.match(html,/甲策略/);assert.match(html,/配置尝试/);
  router.go({name:'market'},true);
 }finally{router.go({name:'market'},true);f.core.dispose()}
});

test('the strategy detail paginates distinct attempts and opens the correct archive', async()=>{
 const spec={strategy_id:'signal',module:'plugins.example',name:'测试策略',description:'规则',defaults:{},parameters:[]};
 const runs=Array.from({length:21},(_,index)=>run('attempt-'+String(index+1).padStart(2,'0'),index%2?spec.strategy_id:spec.module));
 const f=fixture({'research.runs':{items:runs},'backtest.strategies':{items:[spec]}});
 try{
  await f.read('apple:archives','research.runs',{limit:2000});
  await f.read('apple:catalog','backtest.strategies');
  go({name:'strategy',key:spec.strategy_id});
  const first=f.render();
  assert.match(first.html,/测试策略/);
  assert.match(first.html,/尝试 20</);
  assert.doesNotMatch(first.html,/尝试 21<|attempt-01|attempt-02/);
  find(first,'下一页').onClick();
  const second=f.render();
  assert.match(second.html,/尝试 21</);
  find(second,'尝试 21').onClick({stopPropagation(){}});
  assert.deepEqual(routeNow(),{name:'run',id:runs[20].id});
 }finally{go({name:'market'});f.core.dispose()}
});

test('format and parameter names preserve data; unknown metadata stays explicit',()=>{
 assert.equal(amount('1000000'),'1,000,000');assert.equal(amount('10000000000000001.99'),'10,000,000,000,000,001.99');assert.equal(amount('-0.005'),'-0.01');assert.equal(amount(null),'未提供');assert.equal(day('2024-06-30T23:59:59.999999+00:00'),'2024-06-30');assert.equal(day('2024-02-30'),'未提供');
 const spec={parameters:[{key:'window',label:'观察周期',type:'integer',unit:'天'}]};
 const value=run('secret','module',{params:{window:20,hidden_key:4},market:{codes:['600000','000001']},start:'2024-01-01',end:'2024-12-31',initial_cash:'1000000'}),before=JSON.stringify(value);
 assert.deepEqual(parameters(value,spec),[{name:'观察周期',value:'20 天'},{name:'参数名称未提供',value:'4'}]);assert.equal(configuration(value,spec)[0].value,'600000、000001');assert.equal(JSON.stringify(value),before);
 const names=Object.fromEntries(attemptNames([value]));assert.equal(attemptNames([run('aaa','module'),value],names).get('secret'),'尝试 1');
});

test('the run result displays its equity, stored configuration and paged trade data',async()=>{
 const spec={strategy_id:'signal',module:'plugins.example',name:'测试策略',description:'规则',defaults:{},parameters:[]};
 const archive=run('attempt-one',spec.strategy_id,{start:'2019-01-01',end:'2020-12-31',initial_cash:'100000.00'});
 const orderId='f'.repeat(64);
 const trade={order_id:orderId,symbol:'600000',side:'buy',trade_date:'2020-01-02T00:00:00.000001+00:00',shares:11600,price:34.27,gross_amount:397532,commission:99.38,stamp_duty:0,transfer_fee:3.98,status:'filled'};
 const equity=[{at:'2019-01-01',equity:100000},{at:'2020-12-31',equity:110000}];
 const f=fixture({
  'research.runs':{items:[archive]},'backtest.strategies':{items:[spec]},
  'research.run':({offset})=>({...archive,manifest:{},equity,evidence:[],trades_total:51,
   trades:offset===0?Array.from({length:50},()=>trade):[{...trade,side:'sell'}]}),
  'research.benchmark':{points:[{at:'2019-01-01',close:100},{at:'2020-12-31',close:120}]},
 });
 try{
  await f.read('apple:archives','research.runs',{limit:2000});
  await f.read('apple:catalog','backtest.strategies');
  go({name:'run',id:archive.id});
  await f.read('apple:result:detail','research.run',{id:archive.id,offset:0});
  const page=f.render();
  const chart=page.nodes.find(node=>node.props.option).props.option;
  assert.deepEqual(chart.series[0].data,[100000,110000]);
  find(page,'基准对照').onChange({target:{value:'test-index'}});
  await f.read('apple:result:benchmark','research.benchmark',{id:'test-index',start:'2019-01-01',end:'2020-12-31'});
  const comparison=f.render().nodes.find(node=>node.props.option).props.option;
  assert.deepEqual(comparison.series[1].data,[100000,120000]);
  find(f.render(),'配置详情').onClick();
  assert.match(f.render().html,/100,000/);
  assert.match(f.render().html,/2019-01-01/);
  find(f.render(),'成交记录').onClick();
  const trades=f.render();
  assert.deepEqual(trades.nodes.filter(node=>node.type==='th').map(node=>node.children[0]),
   ['日期','证券','买卖','数量','价格','金额','佣金','印花税','过户费','状态']);
  assert.match(trades.html,/397,532/);
  assert.match(trades.html,/买入/);
  assert.ok(!trades.html.includes(orderId));
  find(trades,'下一页').onClick();
  const inputs={id:archive.id,offset:50};
  await f.read('apple:result:detail','research.run',inputs);
  assert.deepEqual(f.calls.at(-1),{operation:'research.run',data:inputs});
  assert.match(f.render().html,/卖出/);
  assert.equal(find(f.render(),'下一页').disabled,true);
 }finally{go({name:'market'});f.core.dispose()}
});

test('technical errors are classified without leaking original text',async()=>{
 const secret='a'.repeat(64),raw='Traceback /private/service/plugins.example.py sha256 '+secret;
 const f=fixture({'research.runs':new Error(raw)});
 try{
  go({name:'strategies',list:DEFAULT_LIST});
  assert.doesNotMatch(errorMessage(raw),new RegExp(secret+'|Traceback|plugins|/private'));
  await f.read('apple:archives','research.runs',{limit:2000});
  const page=f.render();
  assert.match(page.html,/操作失败|服务请求失败/);
  assert.doesNotMatch(page.html,new RegExp(secret+'|Traceback|plugins|/private'));
 }finally{go({name:'market'});f.core.dispose()}
});

test('the market has one detail pane and keeps collection controls on the data page',()=>{
 const f=fixture();
 try{
  go({name:'market'});
  const tree=f.render();
  assert.equal(tree.nodes.filter(node=>node.props['data-od-id']==='market-detail').length,1);
  assert.doesNotMatch(tree.html,/collection\.start|collection\.set_automatic|启动采集/);
 }finally{f.core.dispose()}
});

test('recorded security selections remain visible in configuration summaries',()=>{
 const value=run('stored','signal',{symbols:['600009','000002']});
 assert.equal(configuration(value)[0].value,'600009、000002');
 const combined=run('both','signal',{market:{codes:['600009']},symbols:['600009','000002']});
 assert.equal(configuration(combined)[0].value,'600009、000002');
});

test('an archived run still fetches its own detail on open',async()=>{
 // The run list carries headers only; equity, trades and the effective config
 // come from research.run. Gating that request on whether the run is already in
 // the cached archive page left a known run showing "读取中" forever.
 const {readFile}=await import('node:fs/promises');
 const source=await readFile(new URL('../src/apple/Result.tsx',import.meta.url),'utf8');
 const detail=source.split('\n').find(line=>line.includes("'apple:result:detail'")&&line.includes('research.run'));
 assert.ok(detail,'run detail operation must exist');
 assert.ok(!/!known|\bknown\b/.test(detail),'detail must not be gated on archive membership: '+detail);
 assert.match(detail,/,\s*!!runId\s*\)\s*;/, 'only the enabled flag is passed; automatic stays at its default: '+detail);
});

test('benchmark window follows the equity curve instead of a fixed 2020 start',async()=>{
 // A fixed start truncated the comparison line for every run whose backtest
 // began earlier, although the index dataset reaches back to 2003.
 const {readFile}=await import('node:fs/promises');
 const source=await readFile(new URL('../src/apple/Result.tsx',import.meta.url),'utf8');
 assert.ok(!source.includes("'2020-01-01'"),'benchmark start must not be hard-coded');
 assert.ok(/start:\s*day\(equity\[0\]\.at\)/.test(source),'benchmark start must come from the first equity day');
 assert.ok(/end:\s*day\(equity\[equity\.length - 1\]\.at\)/.test(source),'benchmark end must come from the last equity day');
});

test('kline drag and wheel pan the window instead of zooming it',async()=>{
 // A horizontal swipe should walk through time, matching the trading-chart
 // convention. The inside dataZoom defaults (wheel=zoom, no zoomLock) made the
 // gesture feel like a resize, so pin the pan-only configuration.
 const {readFile}=await import('node:fs/promises');
 const source=await readFile(new URL('../src/apple/Market.tsx',import.meta.url),'utf8');
 const lines=source.split('\n');
 const start=lines.findIndex(line=>line.includes("type: 'inside'")&&line.includes('xAxisIndex'));
 assert.ok(start>=0,'inside dataZoom must exist');
 const block=lines.slice(start,start+3).join('\n');
 for(const flag of ['zoomLock: true','moveOnMouseMove: true','moveOnMouseWheel: true','zoomOnMouseWheel: false'])
   assert.ok(block.includes(flag),'missing pan flag: '+flag);
});

test('market kline reads the stored span and has no range picker',async()=>{
 // The chart shows every stored session. The old 250-session default and the
 // "涨跌区间" date inputs are gone; index cards use each benchmark's own span.
 const {readFile}=await import('node:fs/promises');
 const source=await readFile(new URL('../src/apple/Market.tsx',import.meta.url),'utf8');
 assert.ok(!source.includes('benchStart'),'range picker state must be gone');
 assert.ok(!source.includes('涨跌区间'),'range picker label must be gone');
 assert.ok(!source.includes('type="date"'),'no date inputs remain on the market page');
 // data.bars must not be pinned to a window: no start/end in its request.
 const barsCall=source.split('\n').find(line=>line.includes("'data.bars'"));
 assert.ok(barsCall&&!barsCall.includes('start')&&!barsCall.includes('end'),'bars request must not pass a window');
});
