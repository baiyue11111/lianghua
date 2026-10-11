// Data used by the existing lake presentation features.

export interface Profile {
  生成时间: string;
  结论: { 级别: string; 要点: string[]; 回测可用: boolean };
  总览: {
    日线行情: { 行数: number; 股票数: number; 区间: string; 落后交易日: number };
    股票主档: { 行数: number; 现存: number; 退市: number };
    估值因子: { 行数: number; 股票数: number; 区间: string };
    基本面因子: { 行数: number; 股票数: number; 区间: string };
  };
  年度覆盖: Record<string, { 交易日: number; 现存股日均?: number; 退市股日均?: number }>;
  日线字段: Record<string, { 有值率: number; 覆盖股数: number }>;
  估值因子字段: Record<string, number>;
  基本面因子字段: Record<string, Record<string, number>>;
  异常检查: string[];
  回测区间: { 区间: string; 行数: number; 股票数: number; 完整: boolean };
}

export interface ResearchInputs {
  summary: { first_date: string; last_date: string; rows: number; symbols: number; adjusted_close_rows: number; invalid_price_rows: number };
  duplicate_keys: number;
  price_basis_issues: { code: string; rows: number; first_date: string; last_date: string; median_vwap_close_ratio: number }[];
  trade_state_issues: Record<string, number>;
  freshness: { latest_date: string; latest_stock_count: number; tracked_stock_count: number; median_stock_date: string };
  unverified_code_changes: { old_code: string; new_code: string; effective_date: string }[];
  saved_factor_files: number;
  partition_sha256: Record<string, string>;
  ranking_status: string;
  requirements_before_ranking: string[];
}

export interface ClosedLoopTimelinePoint {
  sequence: number;
  at: string;
  breadth_5d: number;
  selection_day: boolean;
  signal_count: number;
  planned_count: number;
  fill_count: number;
  rejection_count: number;
  pending_count: number;
  cash: string;
  equity: string;
  position_count: number;
  positions: Record<string, string>;
  action: string;
}

export interface ClosedLoopTrade {
  sequence: number;
  order_id: string;
  at: string;
  instrument: string;
  side: "buy" | "sell";
  quantity: string;
  price: string;
  fee: string;
  commission: string;
  stamp_duty: string;
  transfer_fee: string;
}

export interface ClosedLoopReport {
  artifact_kind: "ENGINEERING_GOLDEN";
  validation: { engineering: "PASSED"; historical: "BLOCKED" | "PASSED"; historical_reason: string };
  strategy: {
    id: string;
    name: string;
    parameters: Record<string, number>;
    rules: string[];
  };
  run: {
    run_id: string;
    initial_cash: string;
    steps: number;
    first_at: string;
    last_at: string;
    events_sha256: string;
    events_file: string;
  };
  timeline: ClosedLoopTimelinePoint[];
  trades: ClosedLoopTrade[];
}

export interface ResearchDaily {
  as_of: string;
  today_completed: number;
  today_runs: number;
  today_families: number;
  today_failed: number;
  today_invalid: number;
  active: number;
  today_paused: number;
  today_resumed: number;
  strategies: {
    strategy_id: string; config_hash: string; policy_hash: string; run_id: string;
    day: string; state: string; transition: string | null; reason: string; assessment_current: boolean;
    sharpe: number | null; recent_return: number | null; drawdown: number | null;
    metrics: Record<string, number | null>;
  }[];
}

export interface StockItem {
  代码: string;
  名称: string;
  交易所: string;
  状态: string;
  首日: string | null;
  末日: string | null;
  最新收盘?: number | null;
  涨跌幅?: number | null;
  成交额亿?: number | null;
  行情日期?: string;
}

export interface KlineResp {
  dates: string[];
  candles: (number | null)[][];
  volumes: (number | null)[];
  区间?: string;
  error?: string;
}

export interface FetchJob {
  running: boolean;
  status: "idle" | "running" | "stopping" | "stopped" | "failed" | "succeeded" | string;
  done: number;
  total: number;
  rows: number;
  empty: number;
  failed: string[];
  current: string;
  finished_at: string | null;
  error: string | null;
  log: string[];
}

export interface ContinuousFetch {
  enabled: boolean;
  interval_seconds: number;
  last_run_at: string | null;
  next_run_at: string | null;
  latest_data_date: string | null;
  latest_data_stock_count: number | null;
  tracked_stock_count: number | null;
  median_stock_date: string | null;
  schedule_error: string | null;
  latest_data_error: string | null;
}

export interface MergeJob {
  running: boolean;
  output: string;
  finished_at: string | null;
}

export interface DoctorItem {
  id: string;
  级别: string;
  问题: string;
  可修复: boolean;
  fix: string | null;
  动作: string;
  范围: string;
  说明: string;
}

export interface DoctorJob {
  running: boolean;
  issue: string;
  stage: string;
  verdict: string | null;
  verdicts: Record<string, string>;
  merge_output: string;
  log: string[];
  finished_at: string | null;
  fetch: FetchJob;
}
