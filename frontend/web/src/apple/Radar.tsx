import {useCallback, useEffect, useState} from 'react';
import {useDisplay, useOperation} from '../interaction/model';
import {amount, errorMessage, missing, number} from '../shared/format';
import {Card, DataTable, Empty, Notice, PageHead, Tag} from './ui';
import type {Column} from './ui';

const target = 'apple:radar';
const operation = 'radar.scan';
const refreshMs = 60_000;

const statuses = {
  unavailable: {label: '缺少数据', tone: undefined},
  insufficient: {label: '样本不足', tone: 'amber'},
  stale: {label: '数据过期', tone: 'amber'},
  normal: {label: '正常', tone: undefined},
  surge: {label: '放量', tone: 'red'},
} as const;

interface Item {
  code: string;
  name: string;
  at: string | null;
  price: number | null;
  amount: number | null;
  volume: number | null;
  baseline: number | null;
  ratio: number | null;
  samples: number;
  status: keyof typeof statuses;
}

interface Scan {
  as_of: string | null;
  items: Item[];
}

const clock = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short', hourCycle: 'h23',
});

function time(value: string | null) {
  return value ? <time dateTime={value}>{clock.format(new Date(value))}</time> : missing;
}

const columns: Column<Item>[] = [
  {label: '代码', render: row => row.code},
  {label: '名称', render: row => row.name},
  {label: '状态', render: row => {
    const status = statuses[row.status];
    return <Tag tone={status.tone}>{status.label}</Tag>;
  }},
  {label: '成交额倍数', align: 'right', render: row => row.ratio == null ? missing : `${number(row.ratio)}×`},
  {label: '成交额', align: 'right', render: row => amount(row.amount)},
  {label: '样本数', align: 'right', render: row => number(row.samples, 0)},
  {label: '记录时间', render: row => time(row.at)},
];

export default function Radar() {
  const [query, setQuery] = useState('');
  const port = useDisplay();
  const scan = useOperation<Scan>(target, operation);
  const {invoke} = scan;
  const pending = scan.status === 'pending';
  const q = query.trim().toLowerCase();
  const rows = scan.value?.items.filter(row => row.code.toLowerCase().includes(q) || row.name.toLowerCase().includes(q)) ?? [];

  const refresh = useCallback(() => {
    if (port.snapshot().targets?.[target]?.operations?.[operation]?.status !== 'pending') void invoke();
  }, [invoke, port]);

  useEffect(() => {
    const timer = window.setInterval(refresh, refreshMs);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return <section data-od-id="radar-page">
    <PageHead title="雷达" actions={<>
      {scan.value && <span className="radar-date">数据截至 {time(scan.value.as_of)}</span>}
      <input className="input radar-search" type="search" aria-label="搜索证券代码或名称"
        placeholder="搜索代码或名称" value={query} onChange={event => setQuery(event.target.value)} />
      <button className="btn" type="button" disabled={pending} onClick={refresh}>刷新</button>
    </>} />
    {scan.error && <Notice tone="error">{errorMessage(scan.error)}</Notice>}
    <Card flush>
      {!scan.value ? <Empty busy={pending}>{scan.error ? '雷达数据读取失败' : '等待读取雷达数据'}</Empty>
        : <DataTable columns={columns} rows={rows} empty={q ? '没有匹配的证券' : '暂无雷达证券'} />}
    </Card>
  </section>;
}
