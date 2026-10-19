// Display vocabulary only. API identifiers and stored evidence stay unchanged.
export const strategies:Record<string,{name:string;summary:string}>= {
  quality_trend:{name:'优质股趋势',summary:'先筛盈利、估值和成交额，再以120日涨幅为主、净资产收益率为辅排序，选3或5只股票。'},
  trend_pullback:{name:'趋势回调',summary:'在长期上涨趋势中，选择距近20个交易日最高价回落至少8%或12%的优质股；反弹至20日均线、跌破120日均线或达到持有期限时退出。'},
  era_leadership:{name:'强势行业龙头',summary:'选择连续处于强势区间的行业，再买其中120日涨幅较高的3只股票。市场仓位信号为零时仍可保留1只，目标仓位为三分之一。'},
  breadth_low_amount_reversal:{name:'市场广度·低成交额反转',summary:'市场广度五日均值达到60%才允许空仓开仓，低于45%清仓。通常在月末，从长期趋势向上的股票中取低成交额10%，再选20日涨幅最低的8只，每只目标仓位12.5%。入场时点以运行参数为准。'},
};
const vocabulary:Record<string,string>={
  CNY:'人民币',provider:'数据供应商',currency:'币种',return_type:'收益口径',points:'观测点',at:'日期',
  amount:'成交额',change_pct:'涨跌幅（%）',code:'证券代码',data_at:'数据时间',name:'名称',price:'价格',volume:'成交量',raw_page:'原始响应页索引',
  date:'日期',open:'开盘价',high:'最高价',low:'最低价',close:'收盘价',adjustflag:'复权标记',pctChg:'涨跌幅（%）',
  id:'标识',org_id:'机构标识',title:'标题',published_at:'公开时间',report_period:'报告期',url:'原文地址',
  order_id:'订单编号',symbol:'证券代码',side:'买卖方向',trade_date:'交易日期',shares:'股数',gross_amount:'成交金额',commission:'佣金',stamp_duty:'印花税',status:'状态',reason:'原因',
  run_id:'运行编号',strategy_id:'策略标识',limitations:'数据限制',metrics:'指标',manifest:'运行清单',manifest_file:'运行清单文件',file:'文件',sha256:'内容哈希',metrics_sha256:'指标文件哈希',manifest_sha256:'清单文件哈希',source_hashes:'来源文件哈希',config_hash:'配置哈希',code_version:'代码版本',research_tier:'研究级别',qualification:'验收状态',note:'说明',record:'记录',index:'记录索引',evidence:'证据',
  total_return:'累计收益率',max_drawdown:'最大回撤',sharpe:'夏普比率',observations:'观测数',cagr:'年化复合收益率',annualized_volatility:'年化波动率',initial_equity:'初始权益',final_equity:'期末权益',equity:'权益',cash:'现金',
  price_workbook:'价格工作簿',valuation_workbook:'估值工作簿',fundamental_workbook:'财务工作簿',
  snapshot_id:'快照标识',source_id:'来源标识',collected_at:'采集时间',data_as_of:'数据截至',scope:'查询范围',request:'查询参数',record_index:'记录索引',reviews:'审核记录',reviewed_at:'审核时间',
  codes:'查询代码',start:'起始日期',end:'截止日期',keyword:'标题关键字',category:'公告类别',pages:'页数',total:'总数',frequency:'频率',
  positions:'持股数量',drawdown:'回调阈值',max_hold_days:'最长持有交易日数',amount_quantile:'低成交额比例',amount_floor:'成交额门槛',entry_breadth:'开仓广度阈值',exit_breadth:'清仓广度阈值',entry_cadence:'入场时点',
  buy:'买入',sell:'卖出',filled:'已成交',rejected:'已拒绝',pending:'待核对',correct:'正确',anomaly:'异常',running:'执行中',succeeded:'成功',failed:'失败',unverified:'未验收',exploratory:'探索研究',strict:'严格研究',month_end:'月末',on_qualification:'满足条件时',
  NO_QUALIFIED_STRATEGY:'没有通过验收的策略',INVALID_UNADJUSTED_BASELINE:'未复权基线无效',
  SURVIVORSHIP_BIAS_CURRENT_UNIVERSE:'使用当前股票池，存在幸存者偏差',STATIC_INDUSTRY_MAPPING:'行业分类固定，未反映历史变化',
  CONSERVATIVE_SYNTHETIC_FUNDAMENTAL_AVAILABILITY_DATES:'财务数据可用日期由保守规则推算，非真实披露日期',CASH_DIVIDENDS_UNAVAILABLE:'缺少现金分红数据',
  NO_SHARE_COUNT_IN_THIS_PRICE_MATRIX_NO_INFERRED_CORPORATE_ACTIONS:'价格表没有股本数据，未推算送转等公司行动',SMOKE_SUBUNIVERSE_150_SYMBOLS_NOT_CANDIDATE_SELECTION:'仅用150只股票做流程检查，非完整选股结果',DUPLICATE_PRICE_ROWS_DEDUPED_100:'价格表有重复行，已按原流程去重（100行）',
  quality_trend_rank:'优质股趋势排序',trend_pullback_rank:'趋势回调排序',era_leadership_rank:'强势行业龙头排序',market_breadth_below_45pct:'市场广度低于45%，清仓',wait_for_60pct_entry:'等待市场广度达到60%',hold_until_month_end:'持有至月末',monthly_low_amount_20d_reversal:'月末低成交额20日反转选股',qualified_low_amount_20d_reversal:'条件达标时低成交额20日反转选股',
  'provider quote snapshot':'供应商报价快照',
  'configured query matches; document index only; report periods not inferred':'当前配置范围的查询结果；仅为原文目录，未推断报告期',
};
export function label(value:string):string {return strategies[value]?.name??vocabulary[value]??value}
export function localized(value:unknown):unknown {
  if(typeof value==='string')return label(value);
  if(Array.isArray(value))return value.map(localized);
  if(value!==null&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[label(k),localized(v)]));
  return value;
}
