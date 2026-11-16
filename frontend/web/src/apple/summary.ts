import {record, text} from '../shared/format';

const labels: Record<string, string> = {
  id: '标识', name: '名称', code: '代码', status: '状态', root: '项目位置',
  enabled: '已启用', running: '运行中', done: '已完成', total: '总数', rows: '新增行',
  records: '记录数', error: '异常', start: '起始日期', end: '截止日期',
  market: '市场', source_id: '数据源', provider: '来源', currency: '币种',
  interval_seconds: '调度间隔（秒）', next_run_at: '下次运行', last_run_at: '上次运行',
  latest_data_date: '最新行情日期', tracked_stock_count: '跟踪证券数',
  median_stock_date: '行情日期中位数', latest_data_stock_count: '最新行情证券数',
  collected_at: '采集时间', data_as_of: '数据截至', snapshot_id: '快照',
  total_return: '累计收益', max_drawdown: '最大回撤', sharpe: '夏普比率',
  observations: '观测数', offset: '起点', limit: '条数', dry_run: '试运行',
  ready: '预检通过', issues: '问题项', frequency: '频率', defaults: '默认参数',
  integrity_error: '完整性异常', schedule_error: '调度异常', latest_data_error: '行情异常',
  job: '任务', schedule: '调度', snapshot: '快照', availability: '可用数据集',
  datasets: '数据集', items: '条目', points: '观测点', reviews: '审核',
};

export function humanLabel(key: string): string {
  return labels[key] ?? (text(key) && /[\u3400-\u9fff]/.test(key) ? key : key.replace(/_/g, ' '));
}

/** Only business-facing, human-readable values reach the view. */
export function safeValue(value: unknown): string {
  if (value === null || value === undefined) return '未提供';
  if (typeof value === 'boolean') return value ? '开启' : '关闭';
  if (typeof value === 'number') return Number.isFinite(value) ? new Intl.NumberFormat('en-US', {maximumFractionDigits: 4}).format(value) : '未提供';
  if (typeof value === 'string') {
    const shown = text(value);
    if (shown && !/^[a-f\d]{16,}$/i.test(shown)) return shown;
    if (/^\d{4}-\d{2}-\d{2}/.test(shown ?? '')) return (shown ?? '').slice(0, 19).replace('T', ' ');
    return /^(?:CNY|USD|HKD|EUR|JPY|CN_A|daily|complete|idle|running|succeeded|failed|pending|valid|invalid|unverified|ready)$/i.test(value) ? value : '—';
  }
  if (Array.isArray(value)) return `${value.length} 项`;
  if (typeof value === 'object') return `${Object.keys(value as Record<string, unknown>).length} 个字段`;
  return '未提供';
}

/** Flatten one protocol response into a short, readable key/value list. */
export function summary(value: unknown, max = 8): {name: string; value: string}[] {
  const source = Array.isArray(value) ? {条目: `${value.length} 项`} : record(value);
  return Object.entries(source)
    .filter(([, item]) => item !== null && item !== undefined)
    .slice(0, max)
    .map(([key, item]) => ({name: humanLabel(key), value: safeValue(item)}));
}

export function firstError(...values: (string | undefined)[]): string | undefined {
  return values.find(value => !!value);
}

export function countOf(value: unknown): number | undefined {
  if (Array.isArray(value)) return value.length;
  const data = record(value);
  for (const key of ['items', 'sources', 'datasets', 'records', 'points']) {
    if (Array.isArray(data[key])) return (data[key] as unknown[]).length;
  }
  return undefined;
}
