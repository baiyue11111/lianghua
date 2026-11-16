import {useMemo} from 'react';
import type {Run, StrategyCatalog} from '@display/research/port';
import {useValue, useOperation} from '../interaction/model';
import {strategyGroups} from '../research/groups';
import {attemptNames} from '../research/model';
import {errorMessage, percent, status as statusText, text} from '../shared/format';
import {Card, DataTable, Empty, KV, Metric, Notice, PageHead, Tag} from './ui';
import {firstError} from './summary';
import type {ViewId} from './ui';

export default function Strategies({navigate}: {navigate: (view: ViewId) => void}) {
  const [search, setSearch] = useValue('apple:strategies', 'search', '');
  const [, setStrategy] = useValue('apple:selection', 'strategy', '');
  const [, setRun] = useValue('apple:selection', 'run', '');

  const archives = useOperation<{items: Run[]; total?: number}>('apple:archives', 'research.runs', {}, true, true, {fresh: 30_000});
  const plugins = useOperation<StrategyCatalog>('apple:catalog', 'backtest.strategies', {}, true, true, {fresh: 60_000});

  const runs = useMemo(() => archives.value?.items ?? [], [archives.value]);
  // A list may be windowed; the count a reader sees is the archive's, not the page's.
  const runTotal = archives.value?.total ?? runs.length;
  const catalog = plugins.value;
  const groups = useMemo(() => strategyGroups(runs, catalog?.items ?? []), [runs, catalog]);
  const shown = groups.filter(group => !search.trim() ||
    (text(group.spec?.name) ?? '已移除策略').includes(search.trim()));
  const names = attemptNames(runs);
  const active = runs.filter(run => /running|pending/i.test(run.status));
  const unverified = runs.filter(run => run.qualification === 'unverified').length;
  const error = firstError(archives.error, plugins.error);

  function open(groupKey: string) {
    setStrategy(groupKey);
    setRun('');
    navigate('result');
  }
  function openRun(groupKey: string, run: Run) {
    setStrategy(groupKey);
    setRun(run.id);
    navigate('result');
  }

  return <>
    <PageHead eyebrow="Strategies" title="策略研究"
      subtitle={`已登记 ${catalog?.items?.length ?? 0} 个策略 · 存档 ${runTotal} 次回测`}
      actions={<>
        <button className="btn" type="button" disabled={archives.status === 'pending'} onClick={() => void archives.invoke()}>刷新列表</button>
        <button className="btn primary" type="button" onClick={() => navigate('run')}>新建回测</button>
      </>} />

    {/* No blended median across strategies: different attempt counts and
        different date windows make a cross-strategy average an average of
        incomparable units. Counts and the segment itself carry the meaning. */}
    <div className="grid g4" style={{marginBottom: 18}}>
      <Card><Metric label="已登记策略" value={catalog?.items?.length ?? '—'} foot="策略目录" /></Card>
      <Card><Metric label="配置尝试" value={runTotal} foot="存档回测" /></Card>
      <Card><Metric label="运行中" value={active.length} foot={active.length ? '实时状态' : '当前无进行中的回测'} /></Card>
      <Card><Metric label="未验收存档" value={unverified} foot="数据限制待人工确认" /></Card>
    </div>

    <div className="grid" style={{gridTemplateColumns: 'minmax(0, 1.65fr) minmax(0, 1fr)', alignItems: 'start'}}>
      <div className="grid" style={{gap: 18}}>
      <Card flush title="策略列表" hint={`${shown.length} 个分组`}
        extra={<input className="input" aria-label="搜索策略" placeholder="搜索策略名称" style={{width: 180}}
          value={search} onChange={event => setSearch(event.target.value)} />}>
        <DataTable
          columns={[
            {label: '策略', render: group => <button className="rowbtn" type="button" onClick={() => open(group.key)}>
              {group.spec ? (text(group.spec.name) ?? '策略名称未提供') : '已移除策略'}</button>},
            {label: '尝试', align: 'right', render: group => <span className="num">{group.runs.length}</span>},
            {label: '收益中位数', align: 'right', render: group => <span className="num">
              {percent(group.metrics.total_return?.median)}</span>},
            {label: '最近一次收益', align: 'right', render: group => group.runs.length
              ? <span className="num">{percent(group.runs[0].metrics.total_return)}</span>
              : <span className="faint">—</span>},
            {label: '最近状态', render: group => group.runs.length
              ? <Tag tone={/fail/i.test(group.runs[0].status) ? 'red' : undefined}>{statusText(group.runs[0].status)}</Tag>
              : <span className="faint">尚无尝试</span>},
          ]}
          rows={shown}
          empty={archives.status === 'pending' || plugins.status === 'pending' ? '读取策略中…' : '没有匹配的策略'} />
        <div className="inline" style={{padding: '14px 20px', borderTop: '1px solid var(--border)'}}>
          <span className="faint" style={{fontSize: 11}}>点击策略名查看配置尝试、回测结果与成交记录</span>
        </div>
      </Card>

      <Card flush title="最近配置尝试" hint="点击直接查看结果">
        <DataTable columns={[
          {label: '尝试', render: run => <button className="rowbtn" type="button"
            onClick={() => openRun(groups.find(item => item.runs.some(entry => entry.id === run.id))?.key ?? '', run)}>
            {names.get(run.id) ?? '尝试'}</button>},
          {label: '累计收益', align: 'right', render: run => <span className="num">{percent(run.metrics.total_return)}</span>},
          {label: '状态', render: run => <Tag tone={/fail/i.test(run.status) ? 'red' : undefined}>{statusText(run.status)}</Tag>},
          {label: '数据限制', render: run => run.qualification === 'unverified'
            ? <Tag tone="amber">未验收</Tag> : <span className="faint">—</span>},
        ]} rows={runs.slice(0, 6)} empty="暂无配置尝试" />
      </Card>
      </div>

      <div className="grid" style={{gap: 18}}>
        <Card title="运行状态" hint="存档回测">
          {!runs.length ? <Empty>暂无存档回测</Empty> : <>
            <KV items={[
              {name: '完成', value: runs.filter(item => /complete|succeeded|success/i.test(item.status)).length},
              {name: '运行中', value: active.length},
              {name: '失败', value: runs.filter(item => /fail|error/i.test(item.status)).length},
              {name: '未验收', value: unverified},
            ]} />
            {!!active.length && <div style={{marginTop: 12}}>{active.slice(0, 3).map(item => <div key={item.id} className="op-row">
              <span>{statusText(item.status)}</span><span className="lb">存档回测</span>
            </div>)}</div>}
          </>}
        </Card>

        <Card title="数据集" hint="回测输入">
          <KV items={[
            {name: '可用数据集', value: catalog?.availability?.datasets?.length ?? '未提供'},
            {name: '目录状态', value: catalog?.availability?.ready ? '就绪' : '待检查'},
            {name: '问题项', value: catalog?.availability?.issues?.length ?? 0},
            {name: '最近尝试', value: runs.length ? (names.get(runs[0].id) ?? '尝试') : '暂无'},
          ]} />
        </Card>
      </div>
    </div>
    {error && <Notice tone="error">{errorMessage(error)}</Notice>}
    <div className="faint" style={{marginTop: 14, fontSize: 11}}>
      策略分组按存档的配置归并；收益中位数只在同一策略的多次尝试之间计算，不跨策略求平均。
    </div>
  </>;
}
