// Market display data consumed by the web interface.

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
