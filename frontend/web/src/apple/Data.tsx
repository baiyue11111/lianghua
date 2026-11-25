import {useState} from 'react';
import type {Source} from '@display/research/port';
import {useOperation} from '../interaction/model';
import {useConsole} from './console';
import {day, errorMessage, number, text} from '../shared/format';
import {Card, DataTable, Metric, Notice, PageHead, Segmented, Tag} from './ui';
import {firstError} from './summary';

const MARKET = '沪深';

function clock(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '未安排';
  return new Date(value * 1000).toLocaleString('zh-CN', {hour12: false});
}

function freshness(at: number | undefined): string {
  if (!at) return '等待状态推送';
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - at));
  return seconds < 5 ? '刚刚更新' : `${seconds} 秒前更新`;
}

export default function Data() {
  const [sourceId, setSourceId] = useState('');
  const [codes, setCodes] = useState('');
  const [selector, setSelector] = useState('沪深:status');
  const [command, setCommand] = useState<'status' | 'latest' | 'range' | 'check' | 'doctor'>('status');

  // Observation comes from one stream. Only the source registry is still read
  // on mount: it changes rarely and it is not part of the live control loop.
  const consoleState = useConsole();
  const sources = useOperation<unknown>('apple:data:sources', 'collection.sources');

  // Commands remain discrete writes. Nothing here is auto-invoked.
  const setAuto = useOperation('apple:data:setauto', 'collection.set_automatic', {}, true, false);
  const startJob = useOperation('apple:data:start', 'collection.start', {}, true, false);
  const stopJob = useOperation('apple:data:stop', 'collection.stop', {}, true, false);
  const sync = useOperation<unknown>('apple:data:sync', 'collection.sync', {}, true, false);
  const schedule = useOperation<unknown>('apple:data:schedule', 'collection.schedule', {}, true, false);
  const job = useOperation<unknown>('apple:data:job', 'collection.job', {}, true, false);
  const snapshot = useOperation<unknown>('apple:data:snapshot', 'collection.snapshot', {}, true, false);
  const evidence = useOperation<unknown>('apple:data:evidence', 'collection.evidence', {}, true, false);
  const merge = useOperation<unknown>('apple:data:merge', 'collection.merge', {}, true, false);
  const repair = useOperation<unknown>('apple:data:repair', 'collection.repair', {}, true, false);
  const inspect = useOperation<unknown>('apple:data:inspect', 'collection.inspect', {}, true, false);
  const doctor = useOperation<unknown>('apple:data:doctor', 'collection.doctor', {}, true, false);
  const run = useOperation<unknown>('apple:data:run', 'collection.run', {}, true, false);
  const cliCollect = useOperation<unknown>('apple:data:clicollect', 'cli.collect', {}, true, false);

  const state = consoleState.value;
  const busy = [setAuto, startJob, stopJob].some(item => item.status === 'pending');
  const error = firstError(sources.error, setAuto.error, startJob.error, stopJob.error);
  const sourceList: Source[] = (() => {
    const value = sources.value;
    if (Array.isArray(value)) return value as Source[];
    const items = (value as {items?: unknown} | undefined)?.items;
    return Array.isArray(items) ? items as Source[] : [];
  })();
  const selected = sourceId || sourceList[0]?.id || '';

  const diagnostics = [
    {label: '同步来源', run: () => sync.invoke({id: selected, input: {}}), disabled: !selected},
    {label: '调度设置', run: () => schedule.invoke({id: selected, enabled: true, interval_seconds: 300, input: {}}), disabled: !selected},
    {label: '查询任务', run: () => job.invoke({id: selected}), disabled: !selected},
    {label: '读取快照', run: () => snapshot.invoke({id: selected, offset: 0}), disabled: !selected},
    {label: '审核证据', run: () => evidence.invoke({id: selected, index: 0}), disabled: !selected},
    {label: '试合并', run: () => merge.invoke({dryRun: true}), disabled: false},
    {label: '修复来源', run: () => repair.invoke({id: selected}), disabled: !selected},
    {label: '来源抽查', run: () => inspect.invoke({source_id: selected, codes: codes.split(/[\s,，]+/).filter(Boolean), start: '', end: ''}), disabled: !selected},
    {label: '来源诊断', run: () => doctor.invoke({source_id: selected, codes: codes.split(/[\s,，]+/).filter(Boolean), refresh: false}), disabled: !selected},
    {label: '采集命令', run: () => run.invoke({market: MARKET, command}), disabled: false},
    {label: 'CLI 采集', run: () => cliCollect.invoke({selector}), disabled: !selector.trim()},
  ];

  return <>
    <PageHead eyebrow="Data" title="数据中心"
      subtitle="运行状态由后端持续推送；开关和命令只在明确操作后执行"
      actions={<span className="faint" style={{fontSize: 12}}>
        {consoleState.status === 'live' ? '实时连接' : consoleState.status === 'retrying' ? '重连中' : '连接中'}
      </span>} />

    <div className="grid g4" style={{marginBottom: 18}}>
      <Card><Metric label="自动续采" value={state ? (state.enabled ? '已开启' : '未开启') : '—'}
        small foot={state?.scheduler ? '调度线程在运行' : '调度线程未运行'} /></Card>
      <Card><Metric label="当前任务" value={state?.running ? '运行中' : '空闲'} small
        foot={state?.records ? `已处理 ${number(state.records, 0)} 条` : '暂无新增'} /></Card>
      <Card><Metric label="下次续采" value={state ? clock(state.next_run_at) : '—'} small
        foot={state?.last_run_at ? `上次 ${day(state.last_run_at)}` : '尚无运行记录'} /></Card>
      <Card><Metric label="最近结果" value={state?.last_outcome ? text(state.last_outcome) ?? '—' : '—'}
        small foot={state ? freshness(state.at) : '等待状态'} /></Card>
    </div>

    <div className="grid" style={{gridTemplateColumns: 'minmax(0, 1.5fr) minmax(0, 1fr)', alignItems: 'start'}}>
      <Card title="采集控制台" hint="collection.console · 实时">
        {!state ? <div className="muted" style={{padding: '24px 0'}}>
          {consoleState.status === 'retrying' ? '状态连接中断，正在重连…' : '正在连接状态流…'}
        </div> : <>
          <div className="op-row" style={{marginBottom: 14}}>
            <span className="lb">当前状态</span>
            <span className="grow" />
            <Tag tone={state.running ? 'blue' : undefined}>{state.running ? state.phase ?? '运行中' : '空闲'}</Tag>
            {state.source_id && <span className="faint" style={{fontSize: 11}}>{state.source_id}</span>}
          </div>
          <div className="inline" style={{marginBottom: 14}}>
            <label className="check"><input type="checkbox" role="switch" aria-label="自动采集"
              checked={state.enabled} disabled={busy}
              onChange={event => void setAuto.invoke({market: MARKET, enabled: event.target.checked})} />自动续采</label>
            <button className="btn sm" type="button" disabled={busy || state.running}
              onClick={() => void startJob.invoke({mode: 'auto'})}>启动采集</button>
            {state.running && <button className="btn sm" type="button" disabled={busy}
              onClick={() => void stopJob.invoke()}>停止</button>}
            <span className="grow" />
            <span className="faint" style={{fontSize: 11}}>{freshness(state.at)}</span>
          </div>
          {state.error && <Notice tone="error">{state.error}</Notice>}
        </>}
        {error && <Notice tone="error">{errorMessage(error)}</Notice>}
      </Card>

      <Card flush title="数据源" hint={`${sourceList.length} 个来源`}>
        <DataTable columns={[
          {label: '名称', render: item => <button className="rowbtn" type="button" onClick={() => setSourceId(item.id)}>
            {text(item.name) ?? '来源名称未提供'}</button>},
          {label: '频率', render: item => text(item.frequency) ?? '未提供'},
          {label: '记录', align: 'right', render: item => number(item.records, 0)},
        ]} rows={sourceList} empty={sources.status === 'pending' ? '读取来源中…' : '暂无登记的数据源'} />
      </Card>
    </div>

    <details className="adv" style={{marginTop: 18}}>
      <summary>诊断与采集命令（开发入口）</summary>
      <div className="adv-body">
        <div className="inline" style={{marginBottom: 14}}>
          <input className="input" aria-label="证券代码列表" placeholder="证券代码，逗号分隔" style={{width: 190}}
            value={codes} onChange={event => setCodes(event.target.value)} />
          <input className="input" aria-label="CLI 选择器" placeholder="沪深:status" style={{width: 170}}
            value={selector} onChange={event => setSelector(event.target.value)} />
          <Segmented<'status' | 'latest' | 'range' | 'check' | 'doctor'> label="采集命令" value={command} onChange={setCommand}
            options={[
              {value: 'status', label: '状态'}, {value: 'latest', label: '续采'},
              {value: 'range', label: '区间'}, {value: 'check', label: '检查'}, {value: 'doctor', label: '诊断'},
            ]} />
        </div>
        <div className="ops-grid">
          {diagnostics.map(item => <div className="op-row" key={item.label}>
            <button className="btn sm" type="button" disabled={item.disabled || busy}
              onClick={() => void item.run()}>{item.label}</button>
          </div>)}
        </div>
        <div className="faint" style={{marginTop: 12, fontSize: 11}}>
          这些是排查入口，不参与实时监控；需要长期留存的结果请用 CLI。
        </div>
      </div>
    </details>
  </>;
}
