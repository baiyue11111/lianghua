import {useMemo, useState} from 'react';
import {features} from '@display/features';
import type {Source} from '@display/research/port';
import type {FetchJob, ContinuousFetch} from '@display/lib/market';
import {useOperation} from '../interaction/model';
import {day, errorMessage, number, status as statusText, text} from '../shared/format';
import {Card, DataTable, KV, Metric, Notice, PageHead, Progress, Segmented, Tag} from './ui';
import {OP_GROUPS, OPS} from './ops';
import {countOf, firstError, summary} from './summary';

const MARKET = '沪深';

export default function Data() {
  const [sourceId, setSourceId] = useState('');
  const [codes, setCodes] = useState('');
  const [selector, setSelector] = useState('沪深:status');
  const [command, setCommand] = useState<'status' | 'latest' | 'range' | 'check' | 'doctor'>('status');
  const [lastCommand, setLastCommand] = useState('');

  const status = useOperation<unknown>('apple:data:status', 'data.status');
  const check = useOperation<unknown>('apple:data:check', 'data.check');
  const inputs = useOperation<unknown>('apple:data:inputs', 'data.research_inputs');
  const sources = useOperation<unknown>('apple:data:sources', 'collection.sources');
  const describe = useOperation<unknown>('apple:data:describe', 'collection.describe');
  const current = useOperation<FetchJob>('apple:data:current', 'collection.current');
  const automatic = useOperation<ContinuousFetch>('apple:data:auto', 'collection.automatic', {market: MARKET});
  const mergeJob = useOperation<unknown>('apple:data:mergejob', 'collection.merge_job');
  const repairJob = useOperation<unknown>('apple:data:repairjob', 'collection.repair_job');

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
  const setAuto = useOperation('apple:data:setauto', 'collection.set_automatic', {}, true, false);
  const startJob = useOperation('apple:data:start', 'collection.start', {}, true, false);
  const stopJob = useOperation('apple:data:stop', 'collection.stop', {}, true, false);

  const registered = useMemo(() => new Set(features().operations.map(item => item.operation)), []);
  const covered = OPS.filter(item => registered.has(item.operation));
  const sourceList: Source[] = (() => {
    const value = sources.value;
    if (Array.isArray(value)) return value as Source[];
    const items = (value as {items?: unknown} | undefined)?.items;
    return Array.isArray(items) ? items as Source[] : [];
  })();
  const selected = sourceId || sourceList[0]?.id || '';
  const currentSource = sourceList.find(item => item.id === selected);
  const codeList = codes.split(/[\s,，]+/).map(item => item.trim()).filter(Boolean);
  const jobValue = current.value;
  const busy = [sync, schedule, job, snapshot, evidence, merge, repair, inspect, doctor, run, cliCollect, setAuto, startJob, stopJob]
    .some(item => item.status === 'pending');
  const error = firstError(sources.error, status.error, check.error, inputs.error, describe.error,
    current.error, automatic.error, mergeJob.error, repairJob.error);

  const commands = [
    {label: '同步来源', operation: 'collection.sync', run: () => sync.invoke({id: selected, input: {}}), disabled: !selected, result: sync},
    {label: '调度设置', operation: 'collection.schedule', run: () => schedule.invoke({id: selected, enabled: true, interval_seconds: 300, input: {}}), disabled: !selected, result: schedule},
    {label: '查询任务', operation: 'collection.job', run: () => job.invoke({id: selected}), disabled: !selected, result: job},
    {label: '读取快照', operation: 'collection.snapshot', run: () => snapshot.invoke({id: selected, offset: 0}), disabled: !selected, result: snapshot},
    {label: '审核证据', operation: 'collection.evidence', run: () => evidence.invoke({id: selected, index: 0}), disabled: !selected, result: evidence},
    {label: '试合并', operation: 'collection.merge', run: () => merge.invoke({dryRun: true}), disabled: false, result: merge},
    {label: '修复来源', operation: 'collection.repair', run: () => repair.invoke({id: selected}), disabled: !selected, result: repair},
    {label: '来源抽查', operation: 'collection.inspect', run: () => inspect.invoke({source_id: selected, codes: codeList, start: '', end: ''}), disabled: !selected, result: inspect},
    {label: '来源诊断', operation: 'collection.doctor', run: () => doctor.invoke({source_id: selected, codes: codeList, refresh: false}), disabled: !selected || !codeList.length, result: doctor},
    {label: '运行命令', operation: 'collection.run', run: () => run.invoke({market: MARKET, command}), disabled: false, result: run},
    {label: 'CLI 采集', operation: 'cli.collect', run: () => cliCollect.invoke({selector}), disabled: !selector.trim(), result: cliCollect},
  ];

  return <>
    <PageHead eyebrow="Data" title="数据中心"
      subtitle="来源、快照、体检、修复与调度 — 所有采集接口在此有入口"
      actions={<button className="btn" type="button" onClick={() => {
          [status, check, inputs, sources, describe, current, automatic, mergeJob, repairJob]
            .forEach(item => void item.invoke());
        }}>刷新数据</button>} />

    <div className="grid g4" style={{marginBottom: 18}}>
      <Card><Metric label="已登记数据源" value={sourceList.length || '—'} foot="来源注册表" /></Card>
      <Card><Metric label="研究数据集" value={countOf(inputs.value) ?? '—'} foot="可供回测使用" /></Card>
      <Card><Metric label="当前任务" value={jobValue ? statusText(jobValue.status) : '读取中'} small foot={jobValue?.running ? `${number(jobValue.done, 0)} / ${number(jobValue.total, 0)}` : '暂无运行中的任务'} /></Card>
      <Card><Metric label="最新行情" value={automatic.value?.latest_data_date ? day(automatic.value.latest_data_date) : '—'} small foot="自动续采" /></Card>
    </div>

    <div className="grid" style={{gridTemplateColumns: 'minmax(0, 1.25fr) minmax(0, 1fr)', alignItems: 'start'}}>
      <div className="grid" style={{gap: 18}}>
        <Card flush title="数据源" hint={`${sourceList.length} 个来源 · 已选 ${text(currentSource?.name) ?? selected ?? '无'}`}>
          <DataTable columns={[
            {label: '名称', render: item => <button className="rowbtn" type="button" onClick={() => setSourceId(item.id)}>
              {text(item.name) ?? '来源名称未提供'}</button>},
            {label: '频率', render: item => text(item.frequency) ?? '未提供'},
            {label: '记录', align: 'right', render: item => <span className="num">{number(item.records, 0)}</span>},
            {label: '调度', render: item => <Tag tone={item.schedule?.enabled ? 'green' : undefined}>
              {item.schedule?.enabled ? '已启用' : '未启用'}</Tag>},
            {label: '最新快照', render: item => item.snapshot ? day(item.snapshot.collected_at) : '—'},
          ]} rows={sourceList} empty={sources.status === 'pending' ? '读取来源中…' : '暂无登记的数据源'} />
          <div className="inline" style={{padding: '14px 20px', borderTop: '1px solid var(--border)'}}>
            <span className="faint" style={{fontSize: 11}}>来源说明 · collection.describe</span>
            <span className="muted" style={{fontSize: 11}}>{describe.value ? `${countOf(describe.value) ?? 0} 项说明` : '读取中'}</span>
          </div>
        </Card>

        <Card title="采集任务" hint="collection.current · collection.automatic">
          <div className="grid g2" style={{gap: 12}}>
            <Metric label="自动续采" value={automatic.value?.enabled ? '已开启' : '未开启'} small
              foot={automatic.value?.latest_data_date ? `最新行情 ${day(automatic.value.latest_data_date)}` : '行情日期未提供'} />
            <Metric label="任务进度" value={jobValue?.running ? `${number(jobValue.done, 0)} / ${number(jobValue.total, 0)}` : '空闲'} small
              foot={jobValue?.rows ? `新增 ${number(jobValue.rows, 0)} 行` : '暂无新增'} />
          </div>
          {jobValue?.running && <div style={{marginTop: 12}}><Progress value={jobValue.done} total={jobValue.total} /></div>}
          <div className="inline" style={{marginTop: 14}}>
            <label className="check"><input type="checkbox" role="switch" aria-label="自动采集"
              checked={automatic.value?.enabled ?? false} disabled={!automatic.value || busy}
              onChange={event => void setAuto.invoke({market: MARKET, enabled: event.target.checked})
                .then(() => automatic.invoke())} />自动续采</label>
            <button className="btn sm" type="button" disabled={busy || jobValue?.running}
              onClick={() => void startJob.invoke({mode: 'auto'}).then(() => current.invoke())}>启动采集</button>
            {jobValue?.running && <button className="btn sm" type="button" disabled={busy}
              onClick={() => void stopJob.invoke().then(() => current.invoke())}>停止</button>}
          </div>
          <div className="divider" />
          <div className="grid g2" style={{gap: 16}}>
            <div><div className="section-title">合并任务 · collection.merge_job</div><KV items={summary(mergeJob.value, 3)} /></div>
            <div><div className="section-title">修复队列 · collection.repair_job</div><KV items={summary(repairJob.value, 3)} /></div>
          </div>
        </Card>

        {/* Diagnostics are a developer surface. They stay available but
            collapsed: an operator should not have to read operation names to
            run a data check. */}
        <details className="adv">
          <summary>高级 · 诊断与采集命令</summary>
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
            {commands.map(item => <div className="op-row" key={item.operation}>
              <button className="btn sm" type="button" disabled={item.disabled || busy || item.result.status === 'pending'}
                onClick={() => { setLastCommand(item.operation); void item.run(); }}>{item.label}</button>
              <span className="op">{item.operation}</span>
              {item.result.error && <Tag tone="red">失败</Tag>}
              {item.result.value != null && <Tag tone="green">已返回</Tag>}
              <span className="lb">{item.result.error ? errorMessage(item.result.error) : ''}</span>
            </div>)}
          </div>
          <div className="faint" style={{marginTop: 12, fontSize: 11}}>
            诊断与抽查需要证券代码；快照与证据按上方选中的来源读取。
          </div>
          <div className="divider" />
          <div className="section-title">最近一次命令结果</div>
          {(() => {
            const latest = commands.find(item => item.operation === lastCommand);
            return latest?.result.value != null
              ? <><div className="muted" style={{fontSize: 12, marginBottom: 8}}>{latest.label}</div>
                <KV items={summary(latest.result.value, 6)} /></>
              : <div className="muted" style={{fontSize: 12}}>尚未执行命令</div>;
          })()}
          </div>
        </details>
      </div>

      <div className="grid" style={{gap: 18}}>
        <Card title="数据体检" hint="data.status · data.check">
          <div className="grid g2" style={{gap: 16}}>
            <div><div className="section-title">数据状态</div><KV items={summary(status.value, 6)} /></div>
            <div><div className="section-title">完整性检查</div><KV items={summary(check.value, 6)} /></div>
          </div>
          {status.error && <Notice tone="error">{errorMessage(status.error)}</Notice>}
          {check.error && <Notice tone="error">{errorMessage(check.error)}</Notice>}
        </Card>

        <Card title="研究数据集" hint="data.research_inputs">
          <KV items={summary(inputs.value, 8)} />
          <div className="faint" style={{marginTop: 12, fontSize: 11}}>
            原始数据以 Parquet long-format 存放，样本覆盖现役与退市证券，避免生存者偏差。
          </div>
        </Card>

        <details className="adv">
          <summary>高级 · 接口覆盖（{covered.length} / {registered.size}）</summary>
          <div className="adv-body">
          {OP_GROUPS.map(group => <div className="op-group" key={group}>
            <h3>{group}<Tag tone="blue">{OPS.filter(item => item.group === group).length}</Tag></h3>
            {OPS.filter(item => item.group === group).map(item => <div className="op-row" key={item.operation}>
              <span className={`dot${registered.has(item.operation) ? '' : ' off'}`} />
              <span className="op">{item.operation}</span>
              <span className="lb">{item.label}</span>
              <Tag tone={item.mode === 'write' ? 'amber' : undefined}>{item.mode === 'write' ? '写' : '读'}</Tag>
            </div>)}
          </div>)}
          <div className="faint" style={{fontSize: 11}}>
            清单来自交互核心注册表；圆点表示该 operation 已在核心注册并在本套界面有入口。
          </div>
          </div>
        </details>
      </div>
    </div>
    {error && <Notice tone="error">{errorMessage(error)}</Notice>}
  </>;
}
