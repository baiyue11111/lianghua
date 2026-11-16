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
const {fixture,strategyGroups,runConfig,originalGroups,amount,day,parameters,configuration,attemptNames,errorMessage}=await import(pathToFileURL(out));
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

test('market search selection uses the real source query and K-line hooks',async()=>{
 const stock={代码:'test-code',名称:'测试证券',交易所:'测试交易所',状态:'现存',首日:null,末日:null};
 const f=fixture({'data.search':{items:[stock]},'data.bars':{dates:['2020-01-01'],candles:[[1,2,1,2]],volumes:[100]}});
 try{
 let tree=f.render('Chart');find(tree,'搜索股票代码或名称').onChange({target:{value:'test'}});await settle();
 await f.read('chart:search','data.search',{q:'test'});tree=f.render('Chart');
 const hit=tree.nodes.find(n=>n.props.className==='kline-hit');assert.ok(hit);hit.props.onClick();await settle();
 await f.read('chart:series','data.bars',{code:stock.代码,freq:'日',adj:'不复权'});tree=f.render('Chart');
 assert.match(tree.html,/测试证券/);assert.match(tree.html,/K 线根数/);assert.equal(tree.nodes.find(n=>n.props.option).props.option.series[0].type,'candlestick');
 assert.deepEqual(f.calls.map(c=>c.operation),['data.search','data.bars']);assert.equal(f.calls[1].data.code,stock.代码);
 assert.ok(!tree.nodes.some(n=>n.children.includes('分时')));
 }finally{f.core.dispose()}
});

test('collection renders real status; only user events submit write requests',async()=>{
 const f=fixture({'collection.automatic':{enabled:false,last_run_at:null,next_run_at:null,schedule_error:null},'collection.current':{running:false,status:'idle',done:0,total:0,rows:0,failed:[],log:[],finished_at:null,error:null},'collection.set_automatic':{enabled:true},'collection.start':{status:'running'}});
 try{
 let tree=f.render('Collection');assert.equal(f.calls.length,0);assert.equal(find(tree,'自动采集').disabled,true);
 await f.read('market:schedule','collection.automatic');await f.read('market:task','collection.current');tree=f.render('Collection');assert.match(tree.html,/空闲/);
 await find(tree,'自动采集').onChange({target:{checked:true}});await settle();
 await find(f.render('Collection'),'手动采集').onClick();await settle();
 assert.deepEqual(f.calls.filter(c=>['collection.set_automatic','collection.start'].includes(c.operation)),[{operation:'collection.set_automatic',data:{market:'沪深',enabled:true}},{operation:'collection.start',data:{mode:'auto'}}]);
 }finally{f.core.dispose()}
});

test('CLI submits exactly one server path, renders every interval result, refreshes archives after success or failure',async()=>{
 const complete={run_id:'batch',intervals:[{name:'基准区间',result:{id:'a',effective_config:{initial_cash:'1000000',start:'2024-01-01',end:'2024-06-30'},final_equity:'1010000',metrics:{total_return:.01}}},{name:'验证区间',result:{id:'b'}}]};
 const f=fixture({'cli.backtest':complete});let refreshed=0;const props={refresh:async()=>{refreshed++}};
 try{
 let tree=f.render('Backtest',props);assert.equal(f.calls.length,0);find(tree,'服务器配置路径').onChange({target:{value:' /server/config.json '}});await settle();
 tree=f.render('Backtest',props);await tree.nodes.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await settle();
 assert.deepEqual(f.calls,[{operation:'cli.backtest',data:{config:'/server/config.json'}}]);assert.equal(refreshed,1);
 tree=f.render('Backtest',props);assert.doesNotMatch(tree.html,/>batch<|&quot;|<pre|run_id/);assert.match(tree.html,/区间结果/);assert.match(tree.html,/基准区间/);assert.match(tree.html,/验证区间/);assert.match(tree.html,/1,010,000/);assert.match(tree.html,/2024-06-30/);
 }finally{f.core.dispose()}
 const failed=fixture({'cli.backtest':new Error('interval failed')});
 try{await failed.edit('cli-form',{config:'/server/bad.json'});const tree=failed.render('Backtest',props);await tree.nodes.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await settle();assert.match(failed.render('Backtest',props).html,/区间回测失败/);assert.equal(refreshed,2)}finally{failed.core.dispose()}
});

test('the sidebar lists destinations, and creating or opening a run is not one',()=>{
 const f=fixture();try{
  const tree=f.render('Workspace');
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
  let tree=f.render('Workspace');
  assert.ok(tree.html.indexOf('甲策略')<tree.html.indexOf('乙策略'),'more attempts first');
  assert.match(tree.html,/点一行进入策略详情/);
  assert.doesNotMatch(tree.html,/最近一次/,'no column claims a recency the archive cannot prove');

  // Searching by name narrows the list without losing the other strategy's data.
  router.go({name:'strategies',list:{...router.DEFAULT_LIST,q:'乙'}},true);
  tree=f.render('Workspace');
  assert.doesNotMatch(tree.html,/甲策略/);assert.match(tree.html,/乙策略/);
  assert.match(tree.html,/1 个策略/);

  // An address that matches nothing says so instead of showing everything.
  router.go({name:'strategies',list:{...router.DEFAULT_LIST,q:'不存在的策略'}},true);
  assert.match(f.render('Workspace').html,/没有符合筛选条件的策略/);
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
  let html=f.render('Workspace').html;
  assert.match(html,/策略不在存档中/);assert.doesNotMatch(html,/甲策略/);
  assert.match(html,/返回策略列表/);

  router.go({name:'run',id:'gone'},true);
  html=f.render('Workspace').html;
  assert.match(html,/这次回测不在存档中/);assert.doesNotMatch(html,/甲策略/);

  // The real object still opens, at its own address.
  router.go({name:'strategy',key:'alpha'},true);
  html=f.render('Workspace').html;
  assert.match(html,/甲策略/);assert.match(html,/配置尝试/);
  router.go({name:'market'},true);
 }finally{router.go({name:'market'},true);f.core.dispose()}
});

test('strategy detail exposes readable historical configuration while preserving stored identity',async()=>{
 const spec={strategy_id:'signal',module:'plugins.example',name:'测试策略',description:'规则',defaults:{},parameters:[]};
 const runs=[run('attempt-one','plugins.example',{start:'2020-01-01',end:'2020-12-31',initial_cash:'100000.00'}),run('attempt-two','signal',{start:'2021-01-01',end:'2021-12-31',initial_cash:'200000.00'})];
 const f=fixture({'research.runs':{items:runs},'backtest.strategies':{items:[spec]},'research.run':{...runs[0],manifest:{},trades:[],equity:[],evidence:[],offset:0,limit:50}});
 try{await f.read('research:archives','research.runs');await f.read('research:catalog','backtest.strategies');let tree=f.render('Research',{inspect(){}});find(tree,'测试策略').onClick();await settle();
 await f.read('research:detail','research.run',{id:'attempt-one',offset:0});
 // The production component renders the stored configuration fields.
 tree=f.render('Research',{inspect(){}});assert.doesNotMatch(tree.html,/attempt-one|attempt-two/);assert.match(tree.html,/100,000/);assert.match(tree.html,/200,000/);assert.match(tree.html,/2021-12-31/);
 }finally{f.core.dispose()}
});

test('index controls come only from the real catalog and query one selected series',async()=>{
 const item={id:'index-fixture',name:'测试指数',currency:'CNY',code:'index-code',provider:'测试源'};
 const f=fixture({'research.benchmarks':[item],'research.benchmark':{...item,points:[{at:'2020-01-01',close:100},{at:'2020-01-02',close:110}]}});
 try{
 assert.doesNotMatch(f.render('Indices').html,/选择指数/);await f.read('market:indices','research.benchmarks');let tree=f.render('Indices');assert.match(tree.html,/测试指数/);
 find(tree,'指数').onChange({target:{value:item.id}});find(tree,'指数起始日期').onChange({target:{value:'2020-01-01'}});find(tree,'指数截止日期').onChange({target:{value:'2020-01-02'}});await settle();
 await f.read('market:index:series','research.benchmark',{id:item.id,start:'2020-01-01',end:'2020-01-02'});tree=f.render('Indices');
 assert.match(tree.html,/收盘价/);assert.deepEqual(f.calls[1],{operation:'research.benchmark',data:{id:item.id,start:'2020-01-01',end:'2020-01-02'}});
 const option=tree.nodes.find(n=>n.props.option).props.option;assert.deepEqual(option.series[0].data,[100,110]);
 }finally{f.core.dispose()}
});

test('readability: idle collection has no maintenance wall or raw technical error',async()=>{
 const f=fixture({'collection.automatic':{enabled:false},'collection.current':{running:false,status:'idle',done:0,total:0,rows:0,failed:[],log:[]}});
 try{await f.read('market:schedule','collection.automatic');await f.read('market:task','collection.current');const tree=f.render('Collection');assert.match(tree.html,/空闲/);assert.doesNotMatch(tree.html,/任务进度|上次运行|下次运行|最新行情|0 \/ 0/);}finally{f.core.dispose()}
});

test('readability: no machine identities; duplicate attempts keep distinct human names and original requests',async()=>{
 const machine='a'.repeat(64),spec={strategy_id:'signal',module:'plugins.example',name:'测试策略',description:'规则',parameters:[{key:'window',label:'观察周期',type:'integer',unit:'天'}],defaults:{}};
 const runs=[run(machine,'plugins.example',{params:{window:20},start:'2024-01-02T00:00:00.123456+00:00',end:'2024-06-30T23:59:59.999999+00:00',initial_cash:'1000000'}),run('internal-order-second','signal',{params:{window:20},initial_cash:'1000000'})];
 const f=fixture({'research.runs':{items:runs},'backtest.strategies':{items:[spec]}});
 try{await f.read('research:archives','research.runs');await f.read('research:catalog','backtest.strategies');find(f.render('Research'),'测试策略').onClick();await settle();let tree=f.render('Research');
 assert.doesNotMatch(tree.html, new RegExp(machine+'|internal-order-second|plugins\\.example|123456|999999'));
 assert.match(tree.html,/观察周期/);assert.match(tree.html,/20 天/);assert.match(tree.html,/1,000,000/);assert.match(tree.html,/尝试 1/);assert.match(tree.html,/尝试 2/);
 find(tree,'查看尝试 1').onClick();await settle();tree=f.render('Research');assert.doesNotMatch(tree.html,/测试尝试|查看尝试 2/);
 assert.equal(f.core.snapshot().targets.research.values.current,machine);
 }finally{f.core.dispose()}
});

test('format and parameter names preserve data; unknown metadata stays explicit',()=>{
 assert.equal(amount('1000000'),'1,000,000');assert.equal(amount('10000000000000001.99'),'10,000,000,000,000,001.99');assert.equal(amount('-0.005'),'-0.01');assert.equal(amount(null),'未提供');assert.equal(day('2024-06-30T23:59:59.999999+00:00'),'2024-06-30');assert.equal(day('2024-02-30'),'未提供');
 const spec={parameters:[{key:'window',label:'观察周期',type:'integer',unit:'天'}]};
 const value=run('secret','module',{params:{window:20,hidden_key:4},market:{codes:['600000','000001']},start:'2024-01-01',end:'2024-12-31',initial_cash:'1000000'}),before=JSON.stringify(value);
 assert.deepEqual(parameters(value,spec),[{name:'观察周期',value:'20 天'},{name:'参数名称未提供',value:'4'}]);assert.equal(configuration(value,spec)[0].value,'600000、000001');assert.equal(JSON.stringify(value),before);
 const names=Object.fromEntries(attemptNames([value]));assert.equal(attemptNames([run('aaa','module'),value],names).get('secret'),'尝试 1');
});

test('trade reading order is Chinese business data; identifiers never become columns or details',()=>{
 const secret='f'.repeat(64),row={order_id:secret,symbol:'600000',side:'buy',trade_date:'2024-01-02T00:00:00.000001+00:00',shares:11600,price:34.27,gross_amount:397532,commission:99.38,stamp_duty:0,transfer_fee:3.98,status:'filled'};
 const f=fixture();try{
 const tree=f.render('RecordTable',{rows:[row]});const headings=tree.nodes.filter(n=>n.type==='th').map(n=>n.children[0]);assert.deepEqual(headings,['日期','证券','买卖','数量','价格','金额','佣金','印花税','过户费','状态']);
 assert.doesNotMatch(tree.html,new RegExp(secret+'|order_id|transfer_fee|000001|&quot;|<pre'));assert.match(tree.html,/397,532/);assert.match(tree.html,/11,600/);assert.match(tree.html,/买入/);assert.match(tree.html,/2024-01-02/);
 }finally{f.core.dispose()}
});

test('result configuration and chart use human names; tabs do not render entire attempts list',async()=>{
 const secret='b'.repeat(64),spec={strategy_id:'signal',module:'plugins.example',name:'真实中文策略',parameters:[{key:'n',label:'观察周期',type:'integer',unit:'天'}],defaults:{}};
 const archive=run(secret,'plugins.example',{params:{n:20},start:'2024-01-01',end:'2024-01-02',initial_cash:'1000000',market:{codes:['600000']}});
 const detail={...archive,manifest:{sha256:secret},equity:[{at:'2024-01-01',equity:1000000},{at:'2024-01-02',equity:1010000}],trades:[],limitations:['数据尚未验收'],offset:0,limit:50};
 const second={...archive,id:'internal-second',run_id:'internal-second'};
 const f=fixture({'research.runs':{items:[archive,second]},'backtest.strategies':{items:[spec]},'research.run':request=>({...detail,id:request.id,run_id:request.id})});
 try{
 await f.read('research:archives','research.runs');await f.read('research:catalog','backtest.strategies');find(f.render('Research'),'真实中文策略').onClick();await settle();find(f.render('Research'),'查看尝试 1').onClick();await settle();
 await find(f.render('Research'),'刷新结果').onClick();await settle();let tree=f.render('Research');
 assert.equal(f.calls.at(-1).data.id,secret);assert.doesNotMatch(tree.html,new RegExp(secret+'|plugins\\.example|<pre|JSON|查看结果'));
 const chart=tree.nodes.find(n=>n.props.option).props.option;assert.equal(chart.series[0].name,'真实中文策略 · 尝试 1');assert.ok(chart.xAxis.axisLabel.fontSize>=14);assert.ok(chart.legend.textStyle.fontSize>=16);
 find(tree,'配置详情').onClick();await settle();tree=f.render('Research');assert.match(tree.html,/观察周期/);assert.match(tree.html,/20 天/);assert.match(tree.html,/1,000,000/);assert.doesNotMatch(tree.html,new RegExp(secret+'|module|config_hash|<pre|&quot;'));
 find(tree,'返回配置尝试').onClick();await settle();find(f.render('Research'),'查看尝试 2').onClick();await settle();await find(f.render('Research'),'刷新结果').onClick();await settle();assert.equal(f.calls.at(-1).data.id,second.id);assert.match(f.render('Research').html,/尝试 2/);
 }finally{f.core.dispose()}
});

test('strategy home defers CLI form and attempts paginate ten at a time',async()=>{
 const spec={strategy_id:'signal',module:'plugins.example',name:'测试策略',parameters:[],defaults:{}};
 const runs=Array.from({length:52},(_,i)=>run('internal-'+String(i).padStart(3,'0'),'signal'));
 const f=fixture({'research.runs':{items:runs},'backtest.strategies':{items:[spec]}});
 try{
 await f.read('research:archives','research.runs');await f.read('research:catalog','backtest.strategies');let tree=f.render('Research');assert.equal(tree.nodes.filter(n=>n.type==='form').length,0);assert.doesNotMatch(tree.html,/服务器配置路径/);
 find(tree,'配置文件回测').onClick();await settle();assert.ok(find(f.render('Research'),'服务器配置路径'));find(f.render('Research'),'测试策略').onClick();await settle();tree=f.render('Research');assert.equal(tree.nodes.filter(n=>n.props.className==='attempt-card').length,10);assert.doesNotMatch(tree.html,/internal-/);
 }finally{f.core.dispose()}
});

test('technical errors are classified without leaking original text anywhere',async()=>{
 const secret='a'.repeat(64),raw='Traceback /private/service/plugins.example.py sha256 '+secret;
 assert.doesNotMatch(errorMessage(raw),new RegExp(secret+'|Traceback|plugins|/private'));
 const f=fixture({'research.runs':new Error(raw)});
 try{await f.read('research:archives','research.runs');const tree=f.render('Research');assert.match(tree.html,/操作失败/);assert.doesNotMatch(tree.html,new RegExp(secret+'|Traceback|plugins|/private'));}finally{f.core.dispose()}
});

test('responsive contract: readable sizes and non-wrapping values, mobile cards instead of wide core tables',async()=>{
 const {readFile}=await import('node:fs/promises');const css=await readFile(new URL('../src/shared/app.css',import.meta.url),'utf8');
 assert.doesNotMatch(css,/font-size:\s*(?:[0-9]|1[0-5])px|\.small\b/);assert.match(css,/\.value\{white-space:nowrap/);assert.match(css,/@media\(max-width:700px\)/);assert.match(css,/\.trades thead\{display:none\}/);assert.match(css,/\.fields dd\.value\{min-width:max-content\}/);
 const f=fixture();try{const tree=f.render('Workspace');
  // One navigation model: each page is linked exactly once.
  for(const id of ['market','strategies','data'])assert.equal((tree.html.match(new RegExp(`data-od-id="nav-${id}"`,'g'))||[]).length,1);
  // The reading page owns one detail pane and no operating controls; collection
  // starts and schedules belong to the data page.
  assert.ok(tree.html.indexOf('data-od-id="market-detail"')>0);
  assert.doesNotMatch(tree.html,/collection\.start|collection\.set_automatic|data-od-id="collection"/);
 }finally{f.core.dispose()}
});

test('recorded security selections remain visible in configuration summaries',()=>{
 const value=run('stored','signal',{symbols:['600009','000002']});
 assert.equal(configuration(value)[0].value,'600009、000002');
 const combined=run('both','signal',{market:{codes:['600009']},symbols:['600009','000002']});
 assert.equal(configuration(combined)[0].value,'600009、000002');
});

test('generic resource renderer dispatches by kind and semantic field hints',()=>{
 const f=fixture();
 try{
  const table=f.render('ResourceView',{message:{
   message_id:'m-1',kind:'collection',type:'urn:test',schema:'urn:test',correlation_id:null,
   data:{items:[{代码:'sh.600000',名称:'测试证券',最新收盘:10.21}]},
   presentation:{title:'证券检索',role:'data-search',fields:[
    {path:'/items/0/代码',label:'代码',role:'security',format:'code',order:1,visible:true},
    {path:'/items/0/名称',label:'名称',role:'text',format:'plain',order:2,visible:true},
    {path:'/items/0/最新收盘',label:'最新收盘',role:'price',format:'currency',unit:'CNY',precision:2,order:3,visible:true},
   ]},extensions:{'lianghua:market':'沪深'},
  }});
  assert.match(table.html,/代码/);assert.match(table.html,/最新收盘/);
  assert.match(table.html,/sh\.600000/);assert.match(table.html,/¥10\.21/);

  const series=f.render('ResourceView',{message:{
   message_id:'m-2',kind:'series',type:'urn:test',schema:'urn:test',correlation_id:null,
   data:{dates:['2024-01-02'],candles:[[10,11,9,12]],volumes:[100]},
   presentation:{title:'行情序列',role:'kline',fields:[]},
  }});
  const option=series.nodes.find(node=>node.props.option).props.option;
  assert.equal(option.series[0].type,'candlestick');

  const problem=f.render('ResourceView',{message:{
   message_id:'m-3',kind:'problem',type:'urn:test',schema:'urn:test',correlation_id:null,
   data:{title:'配置或参数无效',detail:'bad input'},presentation:{title:'协议请求未完成',role:'problem',fields:[]},
  }});
  assert.match(problem.html,/配置或参数无效/);
 }finally{f.core.dispose()}
});
