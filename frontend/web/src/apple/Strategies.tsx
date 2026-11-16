import {useMemo} from 'react';
import type {Run, StrategyCatalog} from '@display/research/port';
import {visibleStrategies} from '@display/research/research-model';
import {useOperation} from '../interaction/model';
import {strategyGroups} from '../research/groups';
import {errorMessage, percent, status as statusText, text} from '../shared/format';
import {go, LIST_PAGE, withList} from '../app/router';
import type {ListQuery, Route, SortKey} from '../app/router';
import {ARCHIVE_QUERY, ARCHIVE_WINDOW, statusMix, truncated} from './queries';
import {Card, DataTable, Empty, KV, Metric, Notice, PageHead, Pager, Segmented, Tag} from './ui';
import {firstError} from './summary';

type Group = ReturnType<typeof strategyGroups>[number];

const FACETS: {value: ListQuery['status']; label: string}[] = [
  {value: 'all', label: '全部'},
  {value: 'unverified', label: '有待验收'},
  {value: 'failed', label: '有失败'},
  {value: 'idle', label: '尚无尝试'},
];

// Only columns whose order is a fact about the data are offered. There is no
// "most recent" sort because the archive does not record when a run was made —
// a controller for an order the data cannot produce is a lie told in a dropdown.
const SORTS: {value: SortKey; label: string}[] = [
  {value: 'runs', label: '尝试数'},
  {value: 'name', label: '名称'},
  {value: 'total_return', label: '收益中位数'},
  {value: 'max_drawdown', label: '最大回撤中位数'},
];

function keeps(group: Group, status: ListQuery['status']): boolean {
  if (status === 'all') return true;
  if (status === 'idle') return group.runs.length === 0;
  if (status === 'failed') return group.runs.some(run => /fail|error/i.test(run.status));
  return group.runs.some(run => run.qualification === 'unverified');
}

function rank(groups: Group[], list: ListQuery): Group[] {
  // Filtering (and the metric sorts) come from the one implementation of "does
  // this strategy match what I typed": name, key, module, and run identities.
  const matches = visibleStrategies(groups, list.q, list.sort === 'runs' ? 'name' : list.sort,
    list.sort === 'runs' ? 'asc' : list.order);
  if (list.sort !== 'runs') return matches;
  return [...matches].sort((a, b) => {
    const delta = a.runs.length - b.runs.length;
    return (list.order === 'asc' ? delta : -delta) || a.name.localeCompare(b.name, 'zh-CN');
  });
}

export default function Strategies({list}: {list: ListQuery}) {
  const archives = useOperation<{items: Run[]; total?: number}>('apple:archives', 'research.runs', ARCHIVE_QUERY, true, true, {fresh: 30_000});
  const plugins = useOperation<StrategyCatalog>('apple:catalog', 'backtest.strategies', {}, true, true, {fresh: 60_000});

  const runs = useMemo(() => archives.value?.items ?? [], [archives.value]);
  const runTotal = archives.value?.total ?? runs.length;
  const catalog = plugins.value;
  const groups = useMemo(() => strategyGroups(runs, catalog?.items ?? []), [runs, catalog]);
  const shown = useMemo(() => rank(groups, list).filter(group => keeps(group, list.status)), [groups, list]);
  const pages = Math.max(1, Math.ceil(shown.length / LIST_PAGE));
  const page = Math.min(list.page, pages);
  const rows = shown.slice((page - 1) * LIST_PAGE, page * LIST_PAGE);
  const active = runs.filter(run => /running|pending/i.test(run.status));
  const unverified = runs.filter(run => run.qualification === 'unverified').length;
  const beyond = truncated(archives.value);
  const error = firstError(archives.error, plugins.error);
  const loading = archives.status === 'pending' || plugins.status === 'pending';

  const look = (change: Partial<ListQuery>) => go({name: 'strategies', list: withList(list, change)} as Route, true);
  const sortBy = (key: string) => look({
    sort: key as SortKey,
    order: list.sort === key && list.order === 'desc' ? 'asc' : 'desc',
    page: 1,
  });

  return <>
    <PageHead eyebrow="Strategies" title="策略研究"
      subtitle={`已登记 ${catalog?.items?.length ?? 0} 个策略 · 存档 ${runTotal} 次回测`}
      actions={<>
        <button className="btn" type="button" disabled={archives.status === 'pending'} onClick={() => void archives.invoke()}>刷新列表</button>
        <button className="btn primary" type="button" onClick={() => go({name: 'newRun', module: ''})}>新建回测</button>
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

    <div className="grid" style={{gridTemplateColumns: 'minmax(0, 2.1fr) minmax(0, 1fr)', alignItems: 'start'}}>
      <Card flush title="策略列表" hint={`${shown.length} 个策略`}
        extra={<div className="inline">
          <input className="input" aria-label="搜索策略" placeholder="搜名字、模块或运行编号" style={{width: 220}}
            value={list.q} onChange={event => look({q: event.target.value, page: 1})} />
          <select className="input" aria-label="排序" style={{width: 150}} value={list.sort}
            onChange={event => look({sort: event.target.value as SortKey, page: 1})}>
            {SORTS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </div>}>
        <div className="toolbar" style={{padding: '12px 20px 0', marginBottom: 0}}>
          <Segmented label="筛选策略" options={FACETS} value={list.status}
            onChange={value => look({status: value, page: 1})} />
          <span className="grow" />
          <span className="faint" style={{fontSize: 11}}>点一行进入策略详情</span>
        </div>
        <DataTable
          columns={[
            {label: '策略', sort: 'name', render: group => <button className="rowbtn" type="button"
              onClick={event => {event.stopPropagation(); openStrategy(group.key);}}>
              {group.spec ? (text(group.spec.name) ?? '策略名称未提供') : '已移除策略'}</button>},
            {label: '尝试', align: 'right', sort: 'runs', render: group => <span className="num">{group.runs.length}</span>},
            {label: '收益中位数', align: 'right', sort: 'total_return', render: group => <span className="num">
              {percent(group.metrics.total_return?.median)}</span>},
            {label: '回撤中位数', align: 'right', sort: 'max_drawdown', render: group => <span className="num">
              {percent(group.metrics.max_drawdown?.median)}</span>},
            {label: '状态', render: group => {
              const mix = statusMix(group.runs);
              if (!group.runs.length) return <span className="faint">尚无尝试</span>;
              return <span className="inline" style={{gap: 6}}>
                {!!mix.done && <Tag>{mix.done} 完成</Tag>}
                {!!mix.failed && <Tag tone="red">{mix.failed} 失败</Tag>}
                {!!mix.running && <Tag tone="blue">{mix.running} 运行中</Tag>}
              </span>;
            }},
          ]}
          rows={rows}
          sort={list.sort} order={list.order} onSort={sortBy}
          onRow={group => openStrategy(group.key)}
          empty={loading ? '读取策略中…' : list.q || list.status !== 'all' ? '没有符合筛选条件的策略' : '暂无策略'} />
        <Pager page={page} pages={pages} total={shown.length} unit="个策略"
          onPage={next => look({page: next})} />
      </Card>

      <div className="grid" style={{gap: 18}}>
        <Card title="运行状态" hint="存档回测">
          {!runs.length ? <Empty busy={archives.status === 'pending'}>暂无存档回测</Empty> : <>
            <KV items={[
              {name: '完成', value: statusMix(runs).done},
              {name: '运行中', value: active.length},
              {name: '失败', value: statusMix(runs).failed},
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
          ]} />
        </Card>
      </div>
    </div>
    {!!beyond && <Notice>{`列表只按存档窗口（最多 ${ARCHIVE_WINDOW} 条）计算，共 ${beyond} 条；超出部分不计入尝试数与中位数。`}</Notice>}
    {error && <Notice tone="error">{errorMessage(error)}</Notice>}
    <div className="faint" style={{marginTop: 14, fontSize: 11}}>
      策略分组按存档的配置归并；收益中位数只在同一策略的多次尝试之间计算，不跨策略求平均。
    </div>
  </>;
}

function openStrategy(key: string) {
  go({name: 'strategy', key});
}
