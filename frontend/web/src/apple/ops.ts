import type {ViewId} from './ui';

export type OpGroup = '行情与基准' | '采集与调度' | '策略与回测' | '服务与 CLI';
export interface OpDef {
  operation: string;
  label: string;
  group: OpGroup;
  mode: 'read' | 'write';
  page: ViewId;
}

/** All 35 operations registered by the interaction core, mapped to a screen entry. */
export const OPS: OpDef[] = [
  // 行情与基准 · 7
  {operation: 'research.benchmarks', label: '基准指数目录', group: '行情与基准', mode: 'read', page: 'market'},
  {operation: 'research.benchmark', label: '指数收盘序列', group: '行情与基准', mode: 'read', page: 'market'},
  {operation: 'data.search', label: '证券检索', group: '行情与基准', mode: 'read', page: 'market'},
  {operation: 'data.bars', label: 'K 线与成交量', group: '行情与基准', mode: 'read', page: 'market'},
  {operation: 'data.status', label: '数据状态', group: '行情与基准', mode: 'read', page: 'data'},
  {operation: 'data.check', label: '数据体检', group: '行情与基准', mode: 'read', page: 'data'},
  {operation: 'data.research_inputs', label: '研究数据集', group: '行情与基准', mode: 'read', page: 'data'},
  // 采集与调度 · 19
  {operation: 'collection.sources', label: '数据源清单', group: '采集与调度', mode: 'read', page: 'data'},
  {operation: 'collection.sync', label: '同步数据源', group: '采集与调度', mode: 'write', page: 'data'},
  {operation: 'collection.schedule', label: '调度设置', group: '采集与调度', mode: 'write', page: 'data'},
  {operation: 'collection.job', label: '采集任务', group: '采集与调度', mode: 'read', page: 'data'},
  {operation: 'collection.snapshot', label: '采集快照', group: '采集与调度', mode: 'read', page: 'data'},
  {operation: 'collection.evidence', label: '快照审核证据', group: '采集与调度', mode: 'read', page: 'data'},
  {operation: 'collection.start', label: '启动采集', group: '采集与调度', mode: 'write', page: 'market'},
  {operation: 'collection.current', label: '当前采集任务', group: '采集与调度', mode: 'read', page: 'market'},
  {operation: 'collection.stop', label: '停止采集', group: '采集与调度', mode: 'write', page: 'market'},
  {operation: 'collection.automatic', label: '自动续采状态', group: '采集与调度', mode: 'read', page: 'market'},
  {operation: 'collection.set_automatic', label: '自动续采开关', group: '采集与调度', mode: 'write', page: 'market'},
  {operation: 'collection.merge', label: '合并数据', group: '采集与调度', mode: 'write', page: 'data'},
  {operation: 'collection.merge_job', label: '合并任务', group: '采集与调度', mode: 'read', page: 'data'},
  {operation: 'collection.repair', label: '修复数据', group: '采集与调度', mode: 'write', page: 'data'},
  {operation: 'collection.repair_job', label: '修复任务', group: '采集与调度', mode: 'read', page: 'data'},
  {operation: 'collection.describe', label: '来源说明', group: '采集与调度', mode: 'read', page: 'data'},
  {operation: 'collection.inspect', label: '来源抽查', group: '采集与调度', mode: 'read', page: 'data'},
  {operation: 'collection.doctor', label: '来源诊断', group: '采集与调度', mode: 'write', page: 'data'},
  {operation: 'collection.run', label: '采集命令', group: '采集与调度', mode: 'write', page: 'data'},
  // 策略与回测 · 5
  {operation: 'backtest.strategies', label: '策略目录', group: '策略与回测', mode: 'read', page: 'strategies'},
  {operation: 'research.runs', label: '回测存档', group: '策略与回测', mode: 'read', page: 'strategies'},
  {operation: 'research.run', label: '单次回测结果', group: '策略与回测', mode: 'read', page: 'result'},
  {operation: 'backtest.prepare', label: '回测预检', group: '策略与回测', mode: 'write', page: 'run'},
  {operation: 'backtest.run', label: '运行回测', group: '策略与回测', mode: 'write', page: 'run'},
  // 服务与 CLI · 4
  {operation: 'service.info', label: '服务信息', group: '服务与 CLI', mode: 'read', page: 'market'},
  {operation: 'cli.backtest', label: '配置文件回测', group: '服务与 CLI', mode: 'write', page: 'strategies'},
  {operation: 'cli.predict', label: '下一交易日预测', group: '服务与 CLI', mode: 'read', page: 'strategies'},
  {operation: 'cli.collect', label: 'CLI 采集', group: '服务与 CLI', mode: 'write', page: 'data'},
];

export const OP_GROUPS: OpGroup[] = ['行情与基准', '采集与调度', '策略与回测', '服务与 CLI'];
export const OP_TOTAL = OPS.length;
