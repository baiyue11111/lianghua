import {useState} from 'react';
import type {Source} from '@display/research/port';
import {useOperation} from '../interaction/model';
import {useConsole} from './console';
import type {ConsoleModule} from './console';
import {day, errorMessage, number, text} from '../shared/format';
import {Card, DataTable, Notice, PageHead} from './ui';
import {firstError} from './summary';

const MARKET = '沪深';

function freshness(at: number | undefined): string {
  if (!at) return '等待状态推送';
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - at));
  return seconds < 5 ? '刚刚更新' : `${seconds} 秒前更新`;
}

function factValue(value: string | number | null): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number' && Number.isFinite(value)) {
    // Epoch seconds are the only numeric facts the console sends.
    return new Date(value * 1000).toLocaleString('zh-CN', {hour12: false});
  }
  return text(value) ?? '—';
}

function Lamp({level}: {level: ConsoleModule['level']}) {
  return <span className={`lamp ${level}`} aria-label={
    level === 'ok' ? '正常' : level === 'warn' ? '注意' : level === 'error' ? '异常' : '空闲'
  } />;
}

function ModuleCard({item, onRefresh, busy}: {
  item: ConsoleModule;
  onRefresh: () => void;
  busy: boolean;
}) {
  return <Card title={item.label}
    extra={<button className="btn sm" type="button" disabled={busy}
      aria-label={`刷新${item.label}`} onClick={onRefresh}>刷新</button>}
    hint={item.detail}>
    <div className="module-summary"><Lamp level={item.level} /> {item.summary}</div>
    {item.facts.length > 0 && <div className="module-facts">
      {item.facts.map(fact => <div key={fact.label} className="module-fact">
        <span className="lb">{fact.label}</span>
        <span className="num">{factValue(fact.value)}</span>
      </div>)}
    </div>}
    {item.events.length > 0 && <div className="module-events">
      {item.events.map((event, index) => <div className="event-row" key={`${event.at}-${index}`}>
        <span className="event-time">{event.at ? day(event.at) : '—'}</span>
        <span className={`event-dot ${event.level}`} />
        <span className="event-label">{event.label}</span>
        <span className="event-detail">{event.detail}</span>
      </div>)}
    </div>}
  </Card>;
}

export default function Data() {
  const [codes, setCodes] = useState('');
  const [selector, setSelector] = useState('沪深:status');
  const [command, setCommand] = useState<'status' | 'latest' | 'range' | 'check' | 'doctor'>('status');

  // One stream observes the whole system. The source registry is read on mount
  // only: it changes rarely and is not part of the live control loop.
  const consoleState = useConsole();
  const sources = useOperation<unknown>('apple:data:sources', 'collection.sources');
  const refresh = useOperation<unknown>('apple:system:refresh', 'system.console', {}, true, false);

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
  const busy = [refresh, setAuto, startJob, stopJob].some(item => item.status === 'pending');
  const error = firstError(sources.error, refresh.error, setAuto.error, startJob.error, stopJob.error);
  const sourceList: Source[] = (() => {
    const value = sources.value;
    if (Array.isArray(value)) return value as Source[];
    const items = (value as {items?: unknown} | undefined)?.items;
    return Array.isArray(items) ? items as Source[] : [];
  })();
  const selected = sourceList[0]?.id || '';
  const collection = state?.modules.find(item => item.key === 'collection');
  const facts = collection?.facts ?? [];
  const enabled = factValue(facts.find(fact => fact.label === '状态')?.value ?? null) === '运行中';
  const autoEnabled = collection?.summary.includes('已开启') ?? false;

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
    <PageHead eyebrow="System" title="系统总览"
      subtitle="各模块状态由后端持续推送；开关和命令只在明确操作后执行"
      actions={<span className="faint" style={{fontSize: 12}}>
        {state ? freshness(state.at) : consoleState.status === 'retrying' ? '重连中' : '连接中'}
      </span>} />

    <div className="console-cards">
      {state ? state.modules.map(item => <ModuleCard key={item.key} item={item}
        busy={busy} onRefresh={() => void refresh.invoke({})} />) :
        <Card title="连接中"><div className="muted" style={{padding: '24px 0'}}>
          {consoleState.status === 'retrying' ? '状态连接中断，正在重连…' : '正在连接状态流…'}
        </div></Card>}
    </div>

    <Card flush title="数据源" hint={`${sourceList.length} 个来源`}>
      <DataTable columns={[
        {label: '名称', render: item => text(item.name) ?? '来源名称未提供'},
        {label: '频率', render: item => text(item.frequency) ?? '未提供'},
        {label: '记录', align: 'right', render: item => number(item.records, 0)},
      ]} rows={sourceList} empty={sources.status === 'pending' ? '读取来源中…' : '暂无登记的数据源'} />
    </Card>

    <Card title="采集控制" hint="collection.console · 实时" className="console-control">
      {collection && <>
        <div className="inline">
          <label className="check"><input type="checkbox" role="switch" aria-label="自动采集"
            checked={autoEnabled} disabled={busy}
            onChange={event => void setAuto.invoke({market: MARKET, enabled: event.target.checked})} />自动续采</label>
          <button className="btn sm" type="button" disabled={busy || enabled}
            onClick={() => void startJob.invoke({mode: 'auto'})}>启动采集</button>
          {enabled && <button className="btn sm" type="button" disabled={busy}
            onClick={() => void stopJob.invoke()}>停止</button>}
        </div>
        {collection.detail && <div className="faint" style={{fontSize: 11, marginTop: 10}}>
          {collection.summary}
        </div>}
      </>}
      {error && <Notice tone="error">{errorMessage(error)}</Notice>}
    </Card>

    <details className="adv" style={{marginTop: 18}}>
      <summary>诊断与采集命令（开发入口）</summary>
      <div className="adv-body">
        <div className="inline" style={{marginBottom: 14}}>
          <input className="input" aria-label="证券代码列表" placeholder="证券代码，逗号分隔" style={{width: 190}}
            value={codes} onChange={event => setCodes(event.target.value)} />
          <input className="input" aria-label="CLI 选择器" placeholder="沪深:status" style={{width: 170}}
            value={selector} onChange={event => setSelector(event.target.value)} />
          <select className="input" aria-label="采集命令" value={command}
            onChange={event => setCommand(event.target.value as typeof command)}>
            <option value="status">状态</option><option value="latest">续采</option>
            <option value="range">区间</option><option value="check">检查</option>
            <option value="doctor">诊断</option>
          </select>
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
