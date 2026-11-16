import {useMemo} from 'react';
import type {ComponentProps,ReactNode} from 'react';
import ReactECharts from '../shared/chart';
import type {FieldHint,UiMessage} from '@display/core';
import {amount,day,errorMessage,missing,number,percent,security,status as statusText} from '../shared/format';
import {DataTable,Empty,KV,Notice,Tag} from './ui';
import type {Column} from './ui';
import {humanLabel,safeValue,summary} from './summary';

type Hint = FieldHint;
type ChartOption = ComponentProps<typeof ReactECharts>['option'];
type Renderer = 'notice' | 'table' | 'series' | 'status' | 'kv';

const LIST_KEYS = ['items', 'sources', 'records', 'rows', 'points', 'intervals', 'datasets', 'reviews', 'trades', 'strategies', 'benchmarks', 'runs', 'equity', 'days'];
const CODE_KEYS = new Set(['代码', 'code', 'symbol', 'ts_code', 'security', 'security_id']);
const NAME_KEYS = new Set(['名称', 'name', 'code_name', 'provider', 'strategy_name', 'market', 'frequency', 'currency', 'description']);
const PRICE_KEYS = new Set(['最新收盘', 'close', 'open', 'high', 'low', 'price', 'equity', 'cash', 'amount', 'gross_amount', 'final_equity', 'initial_cash', 'net_profit', 'revenue', 'eps', 'total_mv', 'float_mv', 'commission', 'stamp_duty', 'transfer_fee']);
const QUANTITY_KEYS = new Set(['volume', 'shares', 'rows', 'records', 'count', 'total', 'done', 'observations', 'trades_total', 'limit', 'offset', 'tracked_stock_count', 'latest_data_stock_count', 'history_bars']);
const DATE_KEYS = new Set(['date', 'at', 'start', 'end', 'ipodate', 'outdate', 'report_date', 'notice_date', 'update_date', 'available_at', 'collected_at', 'data_as_of', 'next_run_at', 'last_run_at', 'trade_date', 'latest_data_date', 'median_stock_date', '行情日期', '首日', '末日']);
const RATIO_KEYS = new Set(['total_return', 'return', 'returns', 'max_drawdown', 'roe', 'debt_ratio', 'turn', 'win_rate', 'accuracy']);
const NUMBER_KEYS = new Set(['pctchg', 'pettm', 'pbmrq', 'psttm', 'pcfncfttm', 'sharpe', 'ratio', 'precision']);
const STATUS_KEYS = new Set(['status', 'state', 'enabled', 'running', 'default', 'tradestatus', 'isst', 'retryable', 'available', 'ready', 'qualification', 'side', 'frequency', 'mode']);
const ID_KEYS = new Set(['id', 'run_id', 'request_id', 'message_id', 'correlation_id', 'sha256', 'file', 'path', 'root', 'dataset_id', 'source_id', 'storage_id', 'type', 'schema', 'module', 'config_hash', 'data_hash', 'code_hash', 'order_id', 'trace', 'evidence_ref', 'manifest_file', 'metrics_sha256', 'source_hashes', 'provenance', 'raw_page', 'adjustflag', 'pool']);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function decode(segment: string): string {
  return segment.replace(/~1/g, '/').replace(/~0/g, '~');
}

function encode(segment: string | number): string {
  return String(segment).replace(/~/g, '~0').replace(/\//g, '~1');
}

function makePath(parts: (string | number)[]): string {
  return '/' + parts.map(encode).join('/');
}

function pathParts(path: string): string[] {
  return path.split('/').filter(Boolean).map(decode);
}

function readPath(root: unknown, path: string): unknown {
  if (path === '/') return root;
  if (!path.startsWith('/')) return undefined;
  let current: unknown = root;
  for (const raw of pathParts(path)) {
    if (Array.isArray(current)) {
      const index = Number(raw);
      if (!Number.isInteger(index) || index < 0 || index >= current.length) return undefined;
      current = current[index];
    } else if (isRecord(current)) {
      if (!(raw in current)) return undefined;
      current = current[raw];
    } else {
      return undefined;
    }
  }
  return current;
}

function rowPath(path: string): string {
  const parts = pathParts(path);
  let index = -1;
  parts.forEach((part, position) => { if (/^\d+$/.test(part)) index = position; });
  const tail = index >= 0 ? parts.slice(index + 1) : parts.slice(-1);
  return tail.length ? makePath(tail) : '/';
}

function locateRows(data: unknown): {rows: unknown[]; prefix: (string | number)[]} {
  if (Array.isArray(data)) return {rows: data, prefix: []};
  if (isRecord(data)) {
    for (const key of LIST_KEYS) {
      const value = data[key];
      if (Array.isArray(value)) return {rows: value, prefix: [key]};
    }
  }
  return {rows: [], prefix: []};
}

function semantic(key: string, value: unknown): Omit<Hint, 'path'> {
  const lower = key.trim().toLowerCase();
  const base = {label: humanLabel(key)} as Omit<Hint, 'path'>;
  if (ID_KEYS.has(lower)) return {...base, role: 'identifier', format: 'plain', visible: false};
  if (CODE_KEYS.has(lower)) return {...base, role: 'security', format: 'code'};
  if (NAME_KEYS.has(lower)) return {...base, role: 'text', format: 'plain'};
  if (PRICE_KEYS.has(lower)) return {...base, role: 'price', format: 'currency', unit: 'CNY', precision: 2};
  if (QUANTITY_KEYS.has(lower)) return {...base, role: 'quantity', format: 'integer', precision: 0};
  if (DATE_KEYS.has(lower)) return {...base, role: 'date', format: 'date'};
  if (RATIO_KEYS.has(lower)) return {...base, role: 'ratio', format: 'percent', precision: 2};
  if (NUMBER_KEYS.has(lower)) return {...base, role: 'number', format: 'decimal', precision: 3, ...(lower === 'pctchg' ? {unit: '%'} : {})};
  if (STATUS_KEYS.has(lower)) return {...base, role: 'status', format: typeof value === 'boolean' ? 'boolean' : 'plain'};
  if (typeof value === 'boolean') return {...base, role: 'status', format: 'boolean'};
  if (typeof value === 'number' && Number.isFinite(value)) return {...base, role: 'number', format: 'decimal', precision: 2};
  if (typeof value === 'string') return {...base, role: 'text', format: 'plain'};
  return {...base, role: 'json', format: 'json'};
}

function inferFields(data: unknown): Hint[] {
  const located = locateRows(data);
  if (located.rows.length && isRecord(located.rows[0])) {
    return Object.entries(located.rows[0]).slice(0, 12).map(([key, value], index) => ({
      ...semantic(key, value), path: makePath([...located.prefix, 0, key]), order: index + 1,
    }));
  }
  if (isRecord(data)) {
    return Object.entries(data).slice(0, 12).map(([key, value], index) => ({
      ...semantic(key, value), path: makePath([key]), order: index + 1,
    }));
  }
  if (Array.isArray(data) && data.length) {
    return [{...semantic('值', data[0]), path: makePath([0]), order: 1}];
  }
  return [];
}

function visibleFields(fields: Hint[]): Hint[] {
  return fields.filter(field => field.visible !== false)
    .sort((left, right) => (left.order ?? 0) - (right.order ?? 0));
}

function fieldLabel(field: Hint): string {
  if (field.label) return field.label;
  const parts = pathParts(field.path);
  return humanLabel(parts[parts.length - 1] ?? '字段');
}

function numericField(field: Hint): boolean {
  return ['price', 'quantity', 'ratio', 'number'].includes(field.role ?? '') ||
    ['currency', 'integer', 'decimal', 'percent'].includes(field.format ?? '');
}

function marketOf(message: UiMessage | null | undefined): string | undefined {
  const value = message?.extensions?.['lianghua:market'];
  return typeof value === 'string' ? value : undefined;
}

function formatValue(value: unknown, field: Hint | undefined, market: string | undefined): ReactNode {
  if (value === null || value === undefined) return <span className="faint">—</span>;
  if (Array.isArray(value)) return `${value.length} 项`;
  if (isRecord(value)) return `${Object.keys(value).length} 个字段`;
  const role = field?.role ?? undefined;
  const format = field?.format ?? undefined;
  const unit = field?.unit ?? undefined;
  const precision = field?.precision ?? undefined;
  if (format === 'percent' || role === 'ratio') {
    const numeric = typeof value === 'number' ? value : Number(value);
    if (!Number.isFinite(numeric)) return '—';
    const tone = numeric > 0 ? ' up' : numeric < 0 ? ' down' : '';
    return <span className={`num${tone}`}>{percent(numeric)}</span>;
  }
  if (format === 'currency' || role === 'price') {
    const shown = amount(value);
    if (shown === missing) return '—';
    const cny = !market || market === '沪深' || market === 'CN_A' || unit === 'CNY';
    const symbol = cny ? '¥' : unit === 'USD' ? '$' : '';
    return <span className="num">{`${symbol}${shown}`}</span>;
  }
  if (format === 'integer' || role === 'quantity') {
    const shown = number(value, precision ?? 0);
    return shown === missing ? '—' : <span className="num">{shown}</span>;
  }
  if (format === 'decimal' || role === 'number' || unit === '%') {
    const shown = number(value, precision ?? (unit === '%' ? 2 : 3));
    return shown === missing ? '—' : <span className="num">{`${shown}${unit === '%' ? '%' : ''}`}</span>;
  }
  if (format === 'boolean' || typeof value === 'boolean') return value ? '开启' : '关闭';
  if (format === 'date' || role === 'date') {
    const shown = day(value);
    return shown === missing ? '—' : <span className="num">{shown}</span>;
  }
  if (format === 'code' || role === 'security') {
    const shown = security(value);
    return shown === missing ? safeValue(value) : <span className="num">{shown}</span>;
  }
  if (role === 'status') return statusText(value);
  return safeValue(value);
}

function kvItems(data: unknown, fields: Hint[], market: string | undefined): {name: string; value: ReactNode}[] {
  const used = visibleFields(fields.length ? fields : inferFields(data)).slice(0, 10);
  if (!used.length) return summary(data, 8).map(item => ({name: item.name, value: item.value}));
  return used.map(field => ({
    name: fieldLabel(field),
    value: formatValue(readPath(data, field.path), field, market),
  }));
}

function columnsFrom(fields: Hint[], rows: unknown[], market: string | undefined): Column<unknown>[] {
  const used = visibleFields(fields.length ? fields : inferFields(rows)).slice(0, 10);
  if (!used.length && rows.length && isRecord(rows[0])) {
    return Object.keys(rows[0]).slice(0, 8).map(key => ({
      label: humanLabel(key),
      render: (row: unknown) => formatValue(isRecord(row) ? row[key] : undefined, undefined, market),
    }));
  }
  if (!used.length) return [{label: '数据', render: (row: unknown) => formatValue(row, undefined, market)}];
  return used.map(field => ({
    label: fieldLabel(field),
    align: numericField(field) ? ('right' as const) : undefined,
    render: (row: unknown) => formatValue(readPath(row, rowPath(field.path)), field, market),
  }));
}

function seriesOption(data: unknown): ChartOption | null {
  const source = isRecord(data) ? data : {};
  const dates = Array.isArray(source.dates) ? (source.dates as unknown[]).map(value => day(value)) : [];
  const candles = Array.isArray(source.candles) ? source.candles as unknown[] : [];
  const volumes = Array.isArray(source.volumes) ? source.volumes as unknown[] : [];
  if (dates.length && candles.length) {
    const volumeData = volumes.map((value, index) => {
      const row = Array.isArray(candles[index]) ? candles[index] as unknown[] : [];
      const open = Number(row[0] ?? 0), close = Number(row[1] ?? 0);
      return {value, itemStyle: {color: close >= open ? '#d9302566' : '#16875d66'}};
    });
    return {
      animation: false, backgroundColor: 'transparent',
      grid: [{left: 56, right: 14, top: 12, height: '62%'}, {left: 56, right: 14, top: '75%', height: '14%'}],
      xAxis: [0, 1].map(index => ({type: 'category', data: dates, gridIndex: index,
        axisLine: {lineStyle: {color: '#e8e8ed'}}, axisTick: {show: false},
        axisLabel: index === 0 ? {show: false} : {color: '#a1a1a6', fontSize: 10}})),
      yAxis: [{scale: true, gridIndex: 0, axisLine: {show: false}, axisTick: {show: false},
        splitLine: {lineStyle: {color: '#f2f2f5'}}, axisLabel: {color: '#a1a1a6', fontSize: 10}},
        {scale: false, gridIndex: 1, axisLine: {show: false}, axisTick: {show: false},
          splitLine: {show: false}, axisLabel: {show: false}}],
      series: [
        {name: 'K 线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: candles,
          itemStyle: {color: '#d93025', color0: '#16875d', borderColor: '#d93025', borderColor0: '#16875d'}},
        {name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumeData},
      ],
    } as unknown as ChartOption;
  }
  const rawPoints: unknown[] = Array.isArray(source.points) ? source.points as unknown[]
    : Array.isArray(source.equity) ? source.equity as unknown[] : [];
  const points = rawPoints.filter((item): item is Record<string, unknown> => isRecord(item));
  const values = points.map(point => {
    const raw = point.close ?? point.equity ?? point.value;
    return typeof raw === 'number' ? raw : Number(raw);
  });
  if (!values.length || values.some(value => !Number.isFinite(value))) return null;
  return {
    animation: false, backgroundColor: 'transparent',
    grid: {left: 56, right: 16, top: 16, bottom: 28},
    tooltip: {trigger: 'axis', textStyle: {fontSize: 12}},
    xAxis: {type: 'category', data: points.map(point => day(point.at ?? point.date)),
      axisLine: {lineStyle: {color: '#e8e8ed'}}, axisTick: {show: false}, axisLabel: {color: '#a1a1a6', fontSize: 10}},
    yAxis: {type: 'value', scale: true, axisLine: {show: false}, axisTick: {show: false},
      splitLine: {lineStyle: {color: '#f2f2f5'}}, axisLabel: {color: '#a1a1a6', fontSize: 10}},
    series: [{name: '序列', type: 'line', showSymbol: false, data: values,
      lineStyle: {color: '#0071e3', width: 1.8}, areaStyle: {color: 'rgba(0,113,227,.08)'}}],
  } as unknown as ChartOption;
}

function pickRenderer(message: UiMessage | null | undefined): Renderer {
  if (!message) return 'notice';
  if (message.kind === 'problem') return 'notice';
  const role = message.presentation?.role ?? '';
  if (message.kind === 'series' || role === 'kline' || role === 'benchmark') return 'series';
  if (message.kind === 'collection' || /data-search|runs|benchmarks|sources|strategies|evidence|intervals|research-inputs/.test(role)) return 'table';
  if (/status|doctor|job|continuous-fetch|fetch-job|service-info|cli-predict|data-status/.test(role)) return 'status';
  return 'kv';
}

function NoticeView({message, busy, empty}: {message?: UiMessage | null; busy?: boolean; empty: string}) {
  if (!message) return <Empty busy={busy}>{empty}</Empty>;
  const data = isRecord(message.data) ? message.data : {};
  const issue = String(data.title ?? data.detail ?? '协议请求未完成');
  return <Notice tone={message.kind === 'problem' ? 'error' : undefined}>{errorMessage(issue)}</Notice>;
}

function KVView({data, fields, market}: {data: unknown; fields: Hint[]; market?: string}) {
  return <KV items={kvItems(data, fields, market)} />;
}

function StatusView({data, fields, market}: {data: unknown; fields: Hint[]; market?: string}) {
  const source = isRecord(data) ? data : {};
  const raw = source.status ?? source.state ?? (source.enabled === true ? 'running' : 'idle');
  const lower = String(raw).toLowerCase();
  const tone: 'red' | 'blue' | 'green' | undefined = /fail|error|invalid/.test(lower) ? 'red'
    : /running|pending|busy/.test(lower) ? 'blue'
      : /complete|succeeded|ready|valid/.test(lower) ? 'green' : undefined;
  return <>
    <div className="inline" style={{marginBottom: 12}}>
      <Tag tone={tone}>{statusText(raw)}</Tag>
      <span className="faint" style={{fontSize: 11}}>按通用 schema 渲染</span>
    </div>
    <KV items={kvItems(data, fields, market)} />
  </>;
}

function TableView({data, fields, market, busy, empty}: {data: unknown; fields: Hint[]; market?: string; busy?: boolean; empty: string}) {
  const {rows} = locateRows(data);
  if (!rows.length) return <Empty busy={busy}>{empty}</Empty>;
  return <DataTable columns={columnsFrom(fields, rows, market)} rows={rows} empty={empty} />;
}

function SeriesView({data, fields, market}: {data: unknown; fields: Hint[]; market?: string}) {
  const option = useMemo(() => seriesOption(data), [data]);
  if (!option) return <KV items={kvItems(data, fields, market)} />;
  return <ReactECharts option={option} className="chart" style={{height: 240, width: '100%'}} notMerge />;
}

type Props = {message?: UiMessage | null; busy?: boolean; empty?: string};

export function ResourceView({message, busy, empty = '暂无数据'}: Props) {
  const renderer = pickRenderer(message);
  if (!message || renderer === 'notice') return <NoticeView message={message} busy={busy} empty={empty} />;
  const data: unknown = message.data;
  const declared = message.presentation?.fields ?? [];
  const fields = declared.length ? declared : inferFields(data);
  const market = marketOf(message);
  if (renderer === 'table') return <TableView data={data} fields={fields} market={market} busy={busy} empty={empty} />;
  if (renderer === 'series') return <SeriesView data={data} fields={fields} market={market} />;
  if (renderer === 'status') return <StatusView data={data} fields={fields} market={market} />;
  return <KVView data={data} fields={fields} market={market} />;
}

