import {useMemo} from 'react';
import type {Run, StrategyCatalog, StrategyParameter} from '@display/research/port';
import {useOperation, useValue} from '../interaction/model';
import {strategyGroups} from '../research/groups';
import {attemptNames} from '../research/model';
import {errorMessage, number, percent, status as statusText, text} from '../shared/format';
import {go, LIST_PAGE} from '../app/router';
import {ARCHIVE_QUERY, statusMix, truncated} from './queries';
import {Card, DataTable, Empty, KV, Metric, Notice, PageHead, Pager, Tag} from './ui';
import {firstError} from './summary';

// The strategy, not the run, is the object you browse: its definition, the
// parameters it runs with, and every attempt so far. A run is one row of this
// page opened on its own address.
export default function StrategyDetail({strategyKey}: {strategyKey: string}) {
  const [offset, setOffset] = useValue('apple:strategy', 'offset', 0);
  const archives = useOperation<{items: Run[]; total?: number}>('apple:archives', 'research.runs', ARCHIVE_QUERY, true, true, {fresh: 30_000});
  const plugins = useOperation<StrategyCatalog>('apple:catalog', 'backtest.strategies', {}, true, true, {fresh: 60_000});

  const groups = useMemo(() => strategyGroups(archives.value?.items ?? [], plugins.value?.items ?? []),
    [archives.value, plugins.value]);
  const group = groups.find(item => item.key === strategyKey);
  const loading = archives.status === 'pending' || plugins.status === 'pending';
  const error = firstError(archives.error, plugins.error);
  const beyond = truncated(archives.value);

  if (!group) {
    return <>
      <PageHead eyebrow="Strategy" title={loading ? '策略详情' : '策略不在存档中'}
        subtitle={loading ? '读取存档中' : `没有查到 ${text(strategyKey) ?? strategyKey} 的存档`}
        actions={<button className="btn" type="button" onClick={() => go({name: 'strategies', list: {q: '', status: 'all', sort: 'runs', order: 'desc', page: 1}})}>返回策略列表</button>} />
      {/* A strategy that vanished must say so. Falling back to "the first one"
          would put a different strategy's data under this address. */}
      {loading ? <Card><Empty busy>读取策略中…</Empty></Card>
        : <Card><Empty>这个地址对应的策略已不在目录或存档里，可能是被移除或改名的策略。</Empty></Card>}
      {error && <Notice tone="error">{errorMessage(error)}</Notice>}
    </>;
  }

  const name = group.spec ? (text(group.spec.name) ?? '策略名称未提供') : '已移除策略';
  const module = group.spec?.module ?? group.runs[0]?.module ?? '';
  const names = attemptNames(group.runs);
  const mix = statusMix(group.runs);
  const unverified = group.runs.filter(run => run.qualification === 'unverified').length;
  const latest = group.runs[0];
  const pages = Math.max(1, Math.ceil(group.runs.length / LIST_PAGE));
  const page = Math.min(Math.floor(offset / LIST_PAGE) + 1, pages);
  const rows = group.runs.slice((page - 1) * LIST_PAGE, page * LIST_PAGE);

  return <>
    <PageHead eyebrow="Strategy" title={name}
      subtitle={<>
        {text(group.spec?.description) ?? (group.spec ? '说明未提供' : '目录中已没有这个策略，以下为存档记录')}
        {module && <> · <span className="faint">{module}</span></>}
      </>}
      actions={<>
        <button className="btn" type="button" onClick={() => go({name: 'strategies', list: {q: '', status: 'all', sort: 'runs', order: 'desc', page: 1}})}>返回策略列表</button>
        <button className="btn" type="button" disabled={archives.status === 'pending'} onClick={() => void archives.invoke()}>刷新</button>
        <button className="btn primary" type="button" disabled={!module} onClick={() => go({name: 'newRun', module})}>以此策略新建回测</button>
      </>} />

    <div className="grid g4" style={{marginBottom: 18}}>
      <Card><Metric label="配置尝试" value={group.runs.length} foot="存档回测" /></Card>
      <Card><Metric label="收益中位数" value={percent(group.metrics.total_return?.median)} foot="仅本策略内比较" /></Card>
      <Card><Metric label="回撤中位数" value={percent(group.metrics.max_drawdown?.median)} foot="仅本策略内比较" /></Card>
      <Card><Metric label="未验收存档" value={unverified} foot={unverified ? '数据限制待人工确认' : '全部已确认'} /></Card>
    </div>

    <div className="grid" style={{gridTemplateColumns: 'minmax(0, 1.9fr) minmax(0, 1fr)', alignItems: 'start'}}>
      <Card flush title="配置尝试" hint={`${group.runs.length} 次`}>
        <DataTable
          columns={[
            {label: '尝试', render: run => <button className="rowbtn" type="button"
              onClick={event => {event.stopPropagation(); go({name: 'run', id: run.id});}}>
              {names.get(run.id) ?? '尝试'}</button>},
            {label: '累计收益', align: 'right', render: run => <span className="num">{percent(run.metrics.total_return)}</span>},
            {label: '最大回撤', align: 'right', render: run => <span className="num">{percent(run.metrics.max_drawdown)}</span>},
            {label: '夏普', align: 'right', render: run => <span className="num">{number(run.metrics.sharpe, 3)}</span>},
            {label: '状态', render: run => <Tag tone={/fail/i.test(run.status) ? 'red' : undefined}>{statusText(run.status)}</Tag>},
            {label: '数据限制', render: run => run.qualification === 'unverified'
              ? <Tag tone="amber">未验收</Tag> : <span className="faint">—</span>},
          ]}
          rows={rows}
          onRow={run => go({name: 'run', id: run.id})}
          empty={archives.status === 'pending' ? '读取存档中…' : '这个策略还没有运行过'} />
        <Pager page={page} pages={pages} total={group.runs.length} unit="次尝试"
          onPage={next => setOffset((next - 1) * LIST_PAGE)} />
      </Card>

      <div className="grid" style={{gap: 18}}>
        <Card title="策略定义" hint="来自策略目录">
          {group.spec ? <>
            <KV items={[
              {name: '名称', value: name},
              {name: '模块', value: module || '未提供'},
              {name: '参数项', value: group.spec.parameters.length},
            ]} />
            <div className="faint" style={{marginTop: 10, fontSize: 11, lineHeight: 1.7}}>
              {text(group.spec.description) ?? '说明未提供'}
            </div>
          </> : <Empty>目录中已没有这个策略，无法显示定义</Empty>}
        </Card>

        {/* Which attempt the values came from is a fact; "the latest" is not,
            because the archive records no write time. */}
        <Card title="参数" hint={latest ? `取自 ${names.get(latest.id) ?? '一次存档'}` : '目录默认值'}>
          {!group.spec?.parameters.length
            ? <Empty>该策略没有可调参数</Empty>
            : <ParameterTable fields={group.spec.parameters} values={latest?.parameters ?? {}} />}
        </Card>

        <Card title="存档状态" hint="本策略">
          <KV items={[
            {name: '完成', value: mix.done},
            {name: '运行中', value: mix.running},
            {name: '失败', value: mix.failed},
            {name: '未验收', value: unverified},
          ]} />
        </Card>
      </div>
    </div>

    {!!beyond && <Notice>{`策略列表只按存档窗口计算，共 ${beyond} 条，尝试数与中位数只覆盖这个窗口。`}</Notice>}
    {error && <Notice tone="error">{errorMessage(error)}</Notice>}
  </>;
}

function ParameterTable({fields, values}: {fields: StrategyParameter[]; values: Record<string, unknown>}) {
  const shown = (field: StrategyParameter) => {
    const value = field.key in values ? values[field.key] : field.default;
    if (typeof value === 'boolean') return value ? '开启' : '关闭';
    const label = text(value) ?? String(value ?? '');
    return `${label}${field.unit ? ` ${field.unit}` : ''}`;
  };
  return <DataTable columns={[
    {label: '参数', render: field => text(field.label) ?? field.key},
    {label: '取值', align: 'right', render: field => <span className="num">{shown(field)}</span>},
  ]} rows={fields} empty="没有可调参数" />;
}
