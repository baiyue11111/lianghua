import {useMemo} from 'react';
import {useValue,useOperation} from '../interaction/model';
import BenchmarkChart from './BenchmarkChart';
import StrategyLab from './StrategyLab';
import type {Run,RunDetail,RecordData,StrategyCatalog} from '@display/workbench/port';
import {Pages,RecordTable} from './shared';
import {label,strategies} from '@display/workbench/locale';
import {groupStatus,strategyGroups,visibleStrategies} from '@display/workbench/research-model';
import type {ResearchMetric,ResearchOrder,ResearchSort,StrategyGroup} from '@display/workbench/research-model';

const metrics:[ResearchMetric,string][]=[
  ['total_return','累计收益'],
  ['max_drawdown','最大回撤'],
  ['sharpe','夏普'],
];

function metric(value:number,key:ResearchMetric) {
  return key==='sharpe'?value.toFixed(3):`${(value*100).toFixed(2)}%`;
}

function runMetric(run:Run,key:ResearchMetric) {
  const value=run.metrics[key];
  return typeof value==='number'&&Number.isFinite(value)?metric(value,key):'未提供';
}

function groupMetric(group:StrategyGroup,key:ResearchMetric) {
  const value=group.metrics[key]?.median;
  return typeof value==='number'&&Number.isFinite(value)?metric(value,key):'未提供';
}

export default function Research({inspect}:{error:(s:string)=>void;inspect:(title:string,value:unknown)=>void}) {
  const [current,setCurrent]=useValue('research','current','');
  const [activeGroup,setActiveGroup]=useValue('research','group','');
  const [offset,setOffset]=useValue('research','offset',0);
  const [search,setSearch]=useValue('research','search','');
  const [sort,setSort]=useValue<ResearchSort>('research','sort','name');
  const [order,setOrder]=useValue<ResearchOrder>('research','order','asc');
  const archives=useOperation<Run[]>('research:archives','workbench.runs');
  const plugins=useOperation<StrategyCatalog>('research:catalog','workbench.strategies');
  const selected=useOperation<RunDetail>('research:detail','workbench.run',{id:current,offset},!!current);
  const runs=useMemo(()=>archives.value??[],[archives.value]),catalog=plugins.value??null,catalogError=plugins.error||archives.error;
  const detail=selected.value??null,detailError=selected.error;
  const loading=archives.status==='pending'||plugins.status==='pending';
  const groups=useMemo(()=>strategyGroups(runs,catalog?.items),[runs,catalog]);
  const shown=useMemo(()=>visibleStrategies(groups,search,sort,order),[groups,search,sort,order]);
  const selectedGroup=groups.find(group=>group.key===activeGroup);
  const page=(value:number)=>setOffset(value);
  function openGroup(group:StrategyGroup){setActiveGroup(group.key);setCurrent(group.runs[0]?.id??'');setOffset(0);}
  function backToList(){setCurrent('');setActiveGroup('');}
  function trade(row:RecordData,index:number) {
    inspect('交易样本 · 实际存档记录',{
      run_id:detail?.run_id,index,record:row,evidence:detail?.evidence,
      source_hashes:detail?.source_hashes,qualification:detail?.qualification,
      note:'文件哈希标识当前存档。供应商原始行情及复权链未由这份存档证明；不能据此认定历史收益有效。',
    });
  }

  if(activeGroup&&selectedGroup) {
    return <>
      <div className="pagehead research-detail-head">
        <div>
          <button className="text research-back" onClick={backToList}>← 返回策略列表</button>
          <h1>{selectedGroup.name}</h1>
          <div className="runmeta"><span className="pill">存档 {selectedGroup.runs.length} 次</span><span className="pill warn">{groupStatus(selectedGroup,catalog!==null)}</span></div>
        </div>
        {selectedGroup.runs.length>0&&<label className="research-run-select">回测记录
          <select aria-label="选择回测记录" value={current} onChange={event=>{setCurrent(event.target.value)}}>
            {selectedGroup.runs.map(run=><option key={run.id} value={run.id}>{run.run_id}</option>)}
          </select>
        </label>}
      </div>
          <section className="panel">
            <div className="panelhead"><h2>策略规则说明</h2></div>
            <p className="research-rule">{selectedGroup.description??strategies[selectedGroup.key]?.summary}</p>
            <div className="panel-actions"><a className="text-link" href="/strategies.html" target="_blank" rel="noreferrer">策略规则与指标定义 ↗</a></div>
          </section>
      {catalogError&&<div className="lab-alert" role="alert">策略目录读取失败：{catalogError}</div>}
      {selectedGroup.spec&&catalog&&<StrategyLab key={selectedGroup.key} spec={selectedGroup.spec} availability={catalog.availability} inspect={inspect}/>}
      {!current?<section className="panel"><div className="empty">尚无回测记录</div></section>:detailError?<div className="empty" role="alert">详情读取失败：{detailError}</div>:!detail?<div className="empty">正在读取回测详情…</div>:<>
        <div className="detail">
          <section className="panel">
            <div className="runhead">
              <div>
                <h2>回测概览</h2>
                <p className="mono">{detail.run_id}</p>
                <div className="runmeta"><span className="pill">{label(detail.status)}</span></div>
              </div>
              <button onClick={()=>inspect('运行配置与来源',detail.manifest)}>配置与来源 ↗</button>
            </div>
            <div className="metrics">
              {metrics.map(([key,title])=><div className="metric" key={key}>
                <button onClick={()=>inspect('指标原始记录',{metrics:detail.metrics,file:detail.manifest_file,sha256:detail.metrics_sha256})}>{title} ↗</button>
                <strong>{runMetric(detail,key)}</strong>
              </div>)}
              <div className="metric"><span className="metric-label">观测数</span><strong>{detail.metrics.observations??'未提供'}</strong></div>
            </div>
          </section>
          <section className="panel">
            <div className="panelhead"><h2>存档权益曲线</h2>{detail.equity_sampling&&<span className="pill">图表抽样间隔 {detail.equity_sampling.stride} 行</span>}</div>
            <BenchmarkChart data={detail} inspect={inspect}/>
          </section>
          <section className="panel">
            <div className="panelhead"><h2>逐笔成交记录</h2><button disabled={!detail.trades.length} onClick={()=>{const index=Math.floor(Math.random()*detail.trades.length);trade(detail.trades[index],detail.offset+index)}}>随机抽样</button></div>
            <RecordTable rows={detail.trades} offset={detail.offset} onInspect={trade}/>
            {!!detail.trades_total&&<Pages offset={detail.offset} total={detail.trades_total} change={value=>void page(value)}/>}
          </section>
          <section className="panel">
            <div className="panelhead"><h2>数据限制与文件证据</h2></div>
            <div className="facts"><div>{detail.limitations.map(item=><p key={item} className="small">{label(item)}</p>)}</div><div className="mono evidence-list">{detail.evidence.map(item=><p key={item.file}>{item.file}<br/>{item.sha256}</p>)}</div></div>
          </section>
        </div>
      </>}
    </>;
  }

  return <>
    <div className="pagehead">
      <div><h1>策略研究</h1></div>
      <span className="pill">策略 {groups.length} · 回测 {runs.length} 次</span>
    </div>
    {catalogError&&<div className="lab-alert" role="alert">策略目录读取失败：{catalogError}</div>}
    <section className="panel research-panel">
      <div className="panelhead"><h2>策略概况</h2></div>
      <div className="strategy-tools">
        <input aria-label="搜索策略" className="search" placeholder="搜索策略名称、标识或运行编号" value={search} onChange={event=>setSearch(event.target.value)}/>
        <label className="research-sort">排序字段
          <select aria-label="策略排序" value={sort} onChange={event=>{
            const value=event.target.value as ResearchSort;
            setSort(value);setOrder(value==='name'?'asc':'desc');
          }}>
            <option value="name">名称</option>
            <option value="total_return">累计收益中位数</option>
            <option value="max_drawdown">最大回撤中位数</option>
            <option value="sharpe">夏普中位数</option>
          </select>
        </label>
        <label className="research-sort">顺序
          <select aria-label="排序顺序" value={order} onChange={event=>setOrder(event.target.value as ResearchOrder)}>
            <option value="asc">升序</option><option value="desc">降序</option>
          </select>
        </label>
      </div>
      <div className="tablewrap strategy-table-wrap">
          <table className="strategy-table">
          <thead><tr><th>策略</th><th>累计收益中位数</th><th>最大回撤中位数</th><th>夏普中位数</th><th>状态</th></tr></thead>
          <tbody>
            {loading?<tr><td colSpan={5} className="empty">正在读取策略目录…</td></tr>:shown.map(group=><tr key={group.key}>
              <td className="strategy-cell">
                <button className="strategy-open" onClick={()=>openGroup(group)}>{group.name}<span aria-hidden="true"> ↗</span></button>
                <span className="strategy-count">存档 {group.runs.length} 次</span>
              </td>
              {metrics.map(([key,title])=><td className="metric-cell" data-label={`${title}中位数`} key={key}>{groupMetric(group,key)}</td>)}
              <td data-label="状态"><span className="pill warn">{groupStatus(group,catalog!==null)}</span></td>
            </tr>)}
            {!loading&&shown.length===0&&<tr><td colSpan={5} className="empty">{groups.length?'没有符合条件的策略':'没有策略'}</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  </>;
}
