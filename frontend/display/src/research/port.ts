// Business contracts only: no provider name or HTTP assumption in components.
export type RecordData = Record<string, unknown>;
export interface Run {
  id:string; run_id:string; strategy_id:string; status:string; qualification:string;
  limitations:string[]; metrics:Record<string,number|null>; manifest_file:string;
  manifest_sha256:string; metrics_sha256:string; source_hashes:Record<string,string>;
  config_hash:string|null; note:string;
}
export interface RunDetail extends Run {
  manifest:RecordData; trades:RecordData[]; trades_total?:number;
  equity:{at:string;equity:number}[]; equity_sampling?:{total:number;stride:number};
  evidence:{file:string;sha256:string}[]; offset:number; limit:number;
}
export interface SyncInput { codes:string[]; start:string; end:string; keyword:string }
export interface Job {
  job_id:string; source_id:string; status:'running'|'succeeded'|'failed'; request:SyncInput;
  error:string|null; started_at:string; finished_at:string|null; snapshot_id:string|null; records:number;
}
export interface Source {
  id:string; name:string; frequency:string; defaults:Partial<SyncInput>;
  job:Job|null; schedule:{enabled:boolean;interval_seconds:number;error:string|null;next_at:number|null}|null; integrity_error:string|null; records:number;
  snapshot:{snapshot_id:string;collected_at:string;data_as_of:string|null;scope:RecordData}|null;
}
export interface Snapshot {
  snapshot_id:string;source_id:string;collected_at:string;data_as_of:string|null;
  request:RecordData;scope:RecordData;records:RecordData[];offset:number;limit:number;total:number;
}
export interface Review { snapshot_id:string;record_index:number;status:'correct'|'pending'|'anomaly';note:string;reviewed_at:string }
export interface Evidence {
  snapshot_id:string;collected_at:string;request:RecordData;record_index:number;
  record:RecordData;raw:RecordData[];scope:RecordData;reviews:Review[];
}
export type ParamValue=string|number|boolean;
export interface StrategyParameter {
  key:string;label:string;type:'integer'|'number'|'string'|'boolean';default:ParamValue;
  minimum?:number;maximum?:number;exclusive_minimum?:number;unit?:string;options?:ParamValue[];
}
export interface StrategySpec {
  module:string;strategy_id:string;name:string;description:string;
  parameters:StrategyParameter[];defaults:Record<string,ParamValue>;
}
export interface BacktestIssue {code:string;message:string}
export interface StrategyCatalog {
  items:StrategySpec[];
  availability:{ready:boolean;issues:BacktestIssue[];datasets:{id:string;name:string;start?:string;end?:string}[]};
}
export interface BacktestInput {
  module:string;params:Record<string,ParamValue>;initial_cash:number|string;start:string;end:string;dataset_id:string;
}
export interface Preflight {
  ready:boolean;effective_params:Record<string,ParamValue>;
  requirements:{fields:string[];history_bars:number;frequency:string;price_basis:string};
  config_hash:string;issues:BacktestIssue[];
}
export interface BacktestResult extends RunDetail {
  parameters:Record<string,ParamValue>;effective_config:RecordData;code_hash:string;data_hash:string;execution_hash:string;
}

export interface BenchmarkSpec {id:string;name:string;source_id:string;code:string;provider:string;currency:string;return_type:string}
export interface BenchmarkSeries extends BenchmarkSpec {snapshot_id:string;collected_at:string;start:string;end:string;points:{at:string;close:number}[]}
