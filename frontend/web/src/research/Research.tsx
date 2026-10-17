import {useMemo} from 'react';
import {useValue,useOperation} from '../interaction/model';
import type {Run,RunDetail,StrategyCatalog} from '@display/workbench/port';
import {strategyGroups} from './groups';
import {attemptNames,configuration,parameters,resultMetrics} from './model';
import {errorMessage,percent,status,text} from '../shared/format';
import Fields from '../shared/Fields';
import {Pages,RecordTable} from '../shared/Records';
import BenchmarkChart from './BenchmarkChart';
import Backtest from './Backtest';

export default function Research(){
 const [groupId,setGroupId]=useValue('research','group','');
 const [current,setCurrent]=useValue('research','current','');
 const [offset,setOffset]=useValue('research','offset',0);
 const [page,setPage]=useValue('research','attemptPage',0);
 const [search,setSearch]=useValue('research','search','');
 const [tab,setTab]=useValue('research','tab','overview');
 const [backtest,setBacktest]=useValue('research','backtest',false);
 const [savedNames,setSavedNames]=useValue<Record<string,string>>('research','names',{});
 const archives=useOperation<{items:Run[]}>('research:archives','workbench.runs');
 const plugins=useOperation<StrategyCatalog>('research:catalog','workbench.strategies');
 const selected=useOperation<RunDetail>('research:detail','workbench.run',{id:current,offset},!!current);
 const runs=archives.value?.items??[],catalog=plugins.value;
 const groups=useMemo(()=>strategyGroups(runs,catalog?.items),[runs,catalog]);
 const group=groups.find(item=>item.key===groupId),names=attemptNames(group?.runs??[],savedNames);
 const archivedName=(key:string)=>`已移除策略 ${groups.filter(item=>!item.spec).map(item=>item.key).sort().indexOf(key)+1}`;
 const name=group?.spec?text(group.spec.name)??'策略名称未提供':group?archivedName(group.key):'策略名称未提供';
 const error=archives.error||plugins.error;
 function openGroup(id:string){const item=groups.find(group=>group.key===id)!;setSavedNames(Object.fromEntries(attemptNames(item.runs,savedNames)));setGroupId(id);setCurrent('');setPage(0);}
 function openRun(run:Run){setCurrent(run.id);setOffset(0);setTab('overview');}
 const groupStatus=group?.spec?(group.runs.length?'未验收':'尚无尝试'):plugins.error?'目录状态未知':'策略已移除';
 if(group&&current){
  const detail=selected.value;
  return <>
   <div className="pagehead"><div><button className="text" onClick={()=>setCurrent('')}>返回配置尝试</button><h1 data-od-id="attempt-title">{name} · {names.get(current)??'尝试'}</h1></div><div className="actions"><span className="pill">未验收</span><button disabled={selected.status==='pending'} onClick={()=>void selected.invoke()}>刷新结果</button></div></div>
   <nav className="result-tabs" aria-label="尝试结果"><button aria-pressed={tab==='overview'} onClick={()=>setTab('overview')}>概览</button><button aria-pressed={tab==='config'} onClick={()=>setTab('config')}>配置详情</button><button aria-pressed={tab==='trades'} onClick={()=>setTab('trades')}>成交记录</button></nav>
   {selected.error?<div className="notice status-error" role="alert">{errorMessage(selected.error)}</div>:!detail?<div className="empty" role="status">读取结果中</div>:<>
    {tab==='overview'&&<>
     <section className="panel" data-od-id="attempt-overview"><div className="panelhead"><h2>回测结果</h2><span>{status(detail.status)}</span></div><Fields items={resultMetrics(detail)}/></section>
     <section className="panel" data-od-id="equity-chart"><div className="panelhead"><h2>权益曲线</h2></div><BenchmarkChart data={detail} name={`${name} · ${names.get(current)??'尝试'}`}/></section>
     {!!detail.limitations.length&&<section className="panel"><div className="panelhead"><h2>数据限制</h2></div><ul className="limitations">{detail.limitations.map((item,i)=><li key={i}>{text(item)&&/[\u3400-\u9fff]/.test(item)?text(item):'存在尚未确认的数据限制'}</li>)}</ul></section>}
    </>}
    {tab==='config'&&<section className="panel" data-od-id="configuration"><div className="panelhead"><h2>配置详情</h2></div><Fields items={configuration(detail,group.spec,catalog)}/></section>}
    {tab==='trades'&&<section className="panel" data-od-id="trades"><div className="panelhead"><h2>成交记录</h2></div><RecordTable rows={detail.trades}/><Pages offset={detail.offset} total={detail.trades_total??detail.trades.length} change={setOffset}/></section>}
   </>}
  </>;
 }
 if(group){
  return <>
   <div className="pagehead"><div><button className="text" onClick={()=>{setGroupId('');setCurrent('')}}>返回策略列表</button><h1 data-od-id="strategy-title">{name}</h1></div><span className="pill">{groupStatus}</span></div>
   {error&&<div className="notice status-error" role="alert">{errorMessage(error)}</div>}
   <section className="panel" data-od-id="attempts"><div className="panelhead"><h2>配置尝试</h2><span>{group.runs.length} 次</span></div>
    {!group.runs.length?<div className="empty">尚无配置尝试</div>:<div className="attempt-list">{group.runs.slice(page,page+10).map(run=>{
     const fields=configuration(run,group.spec,catalog),params=parameters(run,group.spec);
     return <article className="attempt-card" key={run.id} data-od-id={`attempt-${names.get(run.id)?.replace('尝试 ','')}`}>
      <div className="attempt-head"><h3>{names.get(run.id)}</h3><span>累计收益 <strong className="value">{percent(run.metrics.total_return)}</strong></span><button aria-label={`查看${names.get(run.id)}`} onClick={()=>openRun(run)}>查看结果</button></div>
      <Fields items={[...params,...fields.slice(0,4)]}/>
      <div className="attempt-status">{status(run.status)} · 未验收</div>
     </article>;
    })}</div>}
    <Pages offset={page} total={group.runs.length} size={10} change={setPage}/>
   </section>
  </>;
 }
 const shown=groups.filter(group=>!search.trim()||(text(group.spec?.name)??'已移除策略').includes(search.trim()));
 return <>
  <div className="pagehead"><h1 data-od-id="research-title">策略研究</h1><div className="actions"><button onClick={()=>void archives.invoke()}>刷新列表</button><button aria-expanded={backtest} onClick={()=>setBacktest(!backtest)}>{backtest?'关闭配置文件回测':'配置文件回测'}</button></div></div>
  {error&&<div className="notice status-error" role="alert">{errorMessage(error)}</div>}
  <section className="panel" data-od-id="strategy-list"><div className="panelhead"><h2>策略</h2><input aria-label="搜索策略" placeholder="搜索策略名称" value={search} onChange={event=>setSearch(event.target.value)}/></div>
   <div className="strategy-list">{shown.map(group=><article className="strategy-row" key={group.key}>
    <button className="strategy-open" onClick={()=>openGroup(group.key)}>{group.spec?(text(group.spec.name)??'策略名称未提供'):archivedName(group.key)}</button>
    <span>{group.runs.length} 次尝试</span><span>收益中位数 <strong className="value">{percent(group.metrics.total_return?.median)}</strong></span><span>{group.spec?(group.runs.length?'未验收':'尚无尝试'):plugins.error?'目录状态未知':'策略已移除'}</span>
   </article>)}</div>
   {!shown.length&&<div className="empty">{archives.status==='pending'||plugins.status==='pending'?'读取策略中':'没有策略'}</div>}
  </section>
  {backtest&&<Backtest catalog={catalog} refresh={()=>archives.invoke()}/>}
 </>;
}
