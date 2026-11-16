import {useMemo} from 'react';
import ReactECharts from '../shared/chart';
import type {Run, RunDetail, StrategyCatalog, BenchmarkSpec, BenchmarkSeries} from '@display/research/port';
import {useValue, useOperation} from '../interaction/model';
import {strategyGroups} from '../research/groups';
import {attemptNames, configuration, resultMetrics} from '../research/model';
import {amount, day, errorMessage, number, record, security, status as statusText, text} from '../shared/format';
import {Card, DataTable, Empty, KV, Metric, Notice, PageHead, Tabs, Tag} from './ui';
import type {ViewId} from './ui';
import {firstError} from './summary';

type ResultTab = 'overview' | 'config' | 'trades' | 'limits';

// A benchmark and an equity curve share a calendar, not an array index. Slicing
// the benchmark positionally puts Monday's index level on top of a Tuesday
// equity point, and the two series drift apart by however many sessions differ.
// Match on the date, carry the benchmark across the equity calendar, and rebase
// from the first date both series actually cover.
//
// Every lookup is a binary search on a sorted key list rather than a cursor, so
// the result does not depend on the equity rows arriving in date order — an
// assumption nothing in the response actually promises.
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function dayOnOrBefore(keys: string[], target: string): number {
  let low = 0, high = keys.length - 1, found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (keys[mid] <= target) { found = mid; low = mid + 1; } else { high = mid - 1; }
  }
  return found;
}

function alignBenchmark(equity: {at: string; equity: number}[], bench?: BenchmarkSeries) {
  // A level at or below zero is not a comparable level, and rebasing onto it
  // would divide by zero.
  const points = (bench?.points ?? []).filter(point => Number.isFinite(point.close) && point.close > 0);
  if (!points.length || !equity.length) return [];
  const byDay = new Map<string, number>();
  for (const point of points) {
    const key = day(point.at);
    if (ISO_DAY.test(key)) byDay.set(key, point.close);
  }
  if (!byDay.size) return [];
  const keys = [...byDay.keys()].sort();
  const aligned = equity.map(item => {
    const key = day(item.at);
    if (!ISO_DAY.test(key)) return null;
    const at = dayOnOrBefore(keys, key);
    return at < 0 ? null : byDay.get(keys[at]) as number;
  });
  const start = aligned.findIndex(value => value != null);
  if (start < 0) return [];
  const base = aligned[start] as number;
  const equityBase = equity[start].equity;
  return aligned.map((value, index) => value == null || index < start ? null : (value / base) * equityBase);
}

function equityOption(detail: RunDetail, overlay: (number | null)[]) {
  const points = detail.equity.map(item => item.equity);
  return {
    animation: false, backgroundColor: 'transparent',
    grid: {left: 68, right: 18, top: 20, bottom: 30},
    tooltip: {trigger: 'axis', textStyle: {fontSize: 12}},
    legend: overlay.length ? {data: ['策略权益', '基准对照'], right: 10, top: 0, textStyle: {fontSize: 11}} : undefined,
    xAxis: {type: 'category' as const, data: detail.equity.map(item => day(item.at)),
      axisLine: {lineStyle: {color: '#e8e8ed'}}, axisTick: {show: false}, axisLabel: {color: '#a1a1a6', fontSize: 10}},
    yAxis: {type: 'value' as const, scale: true, axisLine: {show: false}, axisTick: {show: false},
      splitLine: {lineStyle: {color: '#f2f2f5'}}, axisLabel: {color: '#a1a1a6', fontSize: 10}},
    series: [
      {name: '策略权益', type: 'line' as const, showSymbol: false, data: points,
        lineStyle: {color: '#0071e3', width: 2}, areaStyle: {color: 'rgba(0,113,227,.08)'}},
      ...(overlay.length ? [{name: '基准对照', type: 'line' as const, showSymbol: false, data: overlay,
        lineStyle: {color: '#a1a1a6', width: 1.4, type: 'dashed' as const}}] : []),
    ],
  };
}

export default function Result({navigate}: {navigate: (view: ViewId) => void}) {
  const [strategyKey] = useValue('apple:selection', 'strategy', '');
  const [selected, setSelected] = useValue('apple:selection', 'run', '');
  const [tab, setTab] = useValue<ResultTab>('apple:result', 'tab', 'overview');
  const [offset, setOffset] = useValue('apple:result', 'offset', 0);
  const [benchId, setBenchId] = useValue('apple:result', 'bench', '');

  const archives = useOperation<{items: Run[]; total?: number}>('apple:archives', 'research.runs', {}, true, true, {fresh: 30_000});
  const plugins = useOperation<StrategyCatalog>('apple:catalog', 'backtest.strategies', {}, true, true, {fresh: 60_000});
  const benchmarks = useOperation<BenchmarkSpec[]>('apple:benchmarks', 'research.benchmarks', {}, true, true, {fresh: 60_000});
  const groups = useMemo(() => strategyGroups(archives.value?.items ?? [], plugins.value?.items ?? []),
    [archives.value, plugins.value]);
  const matched = groups.find(item => item.key === strategyKey);
  const group = matched ?? groups[0];
  // A stored selection that no longer exists must not quietly turn into a
  // different strategy: the heading would claim one thing and the data another.
  const orphaned = !!strategyKey && !matched;
  const runs = group?.runs ?? [];
  const names = attemptNames(runs);
  const runId = selected || runs[0]?.id || '';
  const detail = useOperation<RunDetail>('apple:result:detail', 'research.run', {id: runId, offset}, !!runId);
  const bench = useOperation<BenchmarkSeries>('apple:result:benchmark', 'research.benchmark',
    {id: benchId, start: '2020-01-01', end: new Date().toISOString().slice(0, 10)}, !!benchId);
  const value = detail.value;
  const metrics = value ? resultMetrics(value) : [];
  const error = firstError(archives.error, plugins.error, detail.error, bench.error);

  const trades = (value?.trades ?? []).map(row => {
    const item = record(row);
    return {
      at: day(item.trade_date), symbol: security(item.symbol),
      side: item.side === 'buy' ? '买入' : item.side === 'sell' ? '卖出' : statusText(item.side),
      shares: number(item.shares, 0), price: amount(item.price), gross: amount(item.gross_amount),
      fee: amount(item.commission), tax: amount(item.stamp_duty), transfer: amount(item.transfer_fee),
      state: statusText(item.status),
    };
  });
  const overlay = value ? alignBenchmark(value.equity, bench.value) : [];
  const option = value && value.equity.length ? equityOption(value, overlay) : null;
  const tradesTotal = value?.trades_total ?? trades.length;

  return <>
    <PageHead eyebrow="Result" title="回测结果与配置"
      subtitle={group?.spec ? (text(group.spec.name) ?? '策略') : groups.length ? '按存档分组' : '暂无策略'}
      actions={<>
        <Tag tone={value?.status === 'complete' ? 'green' : 'blue'}>{value ? statusText(value.status) : '读取中'} · 未验收</Tag>
        <button className="btn" type="button" onClick={() => navigate('strategies')}>返回策略列表</button>
        <button className="btn primary" type="button" disabled={!runId || detail.status === 'pending'}
          onClick={() => void detail.invoke()}>刷新结果</button>
      </>} />

    {!groups.length ? <Card><Empty busy={archives.status === 'pending'}>暂无回测存档，先到策略研究页运行一次回测</Empty></Card> : <>
      <div className="toolbar" style={{marginBottom: 18}}>
        {/* Every attempt is selectable. A control that lists six of forty while
            the label says "共 40 次" is an affordance that lies about its reach. */}
        <select className="input" aria-label="选择尝试" style={{width: 220}}
          value={runs.some(item => item.id === runId) ? runId : ''}
          onChange={event => { setSelected(event.target.value); setOffset(0); }}>
          {runs.map(item => <option key={item.id} value={item.id}>{names.get(item.id) ?? '尝试'}</option>)}
        </select>
        <span className="faint" style={{fontSize: 11}}>共 {archives.value?.total ?? runs.length} 次配置尝试</span>
      </div>

      {orphaned && <Notice>上次查看的策略已不在存档中，当前显示的是最近一组。</Notice>}

      <Tabs<ResultTab> label="结果分区" value={tab} onChange={setTab} options={[
        {value: 'overview', label: '概览'}, {value: 'config', label: '配置详情'},
        {value: 'trades', label: '成交记录'}, {value: 'limits', label: '数据限制'},
      ]} />

      {error && <Notice tone="error">{errorMessage(error)}</Notice>}

      {tab === 'overview' && <>
        <div className="grid g4" style={{marginBottom: 18}}>
          {metrics.map(item => <Card key={item.name}><Metric label={item.name} value={item.value} small /></Card>)}
          {!metrics.length && <Card><Metric label="结果" value="读取中" small /></Card>}
        </div>
        <div className="grid" style={{gridTemplateColumns: 'minmax(0, 1.6fr) minmax(0, 1fr)', alignItems: 'start'}}>
          <Card title="权益曲线" hint={value?.equity_sampling ? '序列经过抽样' : `${value?.equity.length ?? 0} 个观测`}
            extra={<select className="input" aria-label="基准对照" style={{width: 150}} value={benchId}
              onChange={event => setBenchId(event.target.value)}>
              <option value="">不对比基准</option>
              {(benchmarks.value ?? []).map(item => <option key={item.id} value={item.id}>{text(item.name) ?? '指数'}</option>)}
            </select>}>
            {option ? <ReactECharts option={option} style={{height: 300, width: '100%'}} notMerge />
              : <Empty busy={detail.status === 'pending'}>暂无权益序列</Empty>}
            {/* A missing benchmark line is a fact the reader has to know about.
                Hiding it leaves them comparing an absent series to nothing. */}
            {!!benchId && !!bench.value && !overlay.length &&
              <div className="faint" style={{fontSize: 11, paddingTop: 8}}>
                基准与权益曲线没有重合的交易日，对照线未绘制。
              </div>}
          </Card>
          <Card title="配置摘要">
            {value ? <KV items={configuration(value, group?.spec, plugins.value).slice(0, 8)} />
              : <Empty busy={detail.status === 'pending'}>读取中</Empty>}
          </Card>
        </div>

      </>}

      {tab === 'config' && <Card title="配置详情 · effective_config">
        {value ? <KV items={configuration(value, group?.spec, plugins.value)} />
          : <Empty busy={detail.status === 'pending'}>读取中</Empty>}
        <div className="faint" style={{marginTop: 14, fontSize: 11}}>
          参数与配置按展示模型转换为业务名称，机器标识、哈希和运行 ID 不进入视图。
        </div>
      </Card>}

      {tab === 'trades' && <Card flush title="成交记录" hint={`${tradesTotal} 笔`}
        extra={<div className="inline">
          <span className="faint" style={{fontSize: 11}}>
            {trades.length ? `第 ${offset + 1}–${offset + trades.length} 笔` : '本页无记录'}
          </span>
          <button className="btn sm" type="button" disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</button>
          <button className="btn sm" type="button" disabled={!value || trades.length < 50}
            onClick={() => setOffset(offset + 50)}>下一页</button>
        </div>}>
        <DataTable columns={[
          {label: '日期', render: row => <span className="num">{row.at}</span>},
          {label: '证券', render: row => row.symbol},
          {label: '买卖', render: row => row.side},
          {label: '数量', align: 'right', render: row => <span className="num">{row.shares}</span>},
          {label: '价格', align: 'right', render: row => <span className="num">{row.price}</span>},
          {label: '金额', align: 'right', render: row => <span className="num">{row.gross}</span>},
          {label: '佣金', align: 'right', render: row => <span className="num">{row.fee}</span>},
          {label: '印花税', align: 'right', render: row => <span className="num">{row.tax}</span>},
          {label: '过户费', align: 'right', render: row => <span className="num">{row.transfer}</span>},
          {label: '状态', render: row => row.state},
        ]} rows={trades} empty={detail.status === 'pending' ? '读取中…' : '暂无成交记录'} />
      </Card>}

      {tab === 'limits' && <Card title="数据限制与验收">
        {!value ? <Empty busy={detail.status === 'pending'}>读取中</Empty> : <>
          {value.limitations.length ? <ul style={{margin: 0, paddingLeft: 20, lineHeight: 2}}>
            {value.limitations.map((item, index) => <li key={index}>{text(item) ?? '存在尚未确认的数据限制'}</li>)}
          </ul> : <div className="muted">没有记录到数据限制。</div>}
          <div className="divider" />
          <KV items={[
            {name: '验收状态', value: statusText(value.qualification)},
            {name: '证据条目', value: value.evidence.length},
            {name: '成交总数', value: number(value.trades_total ?? value.trades.length, 0)},
          ]} />
        </>}
      </Card>}
    </>}
  </>;
}
