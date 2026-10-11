import {useOperation} from "../interaction/model";
import type {ResearchInputs} from "@display/lib/lake";
import ResearchDailyView from "./ResearchDailyView";
import ClosedLoopView from "./ClosedLoopView";

export default function ResearchView() {
  const result=useOperation<ResearchInputs>('research:inputs','lake.researchInputs');
  const report=result.value??null,error=result.error,loading=result.status==='pending';
  const refresh=()=>result.invoke();
  function download() {
    if (!report) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "research-input-audit.json";
    link.click();
    URL.revokeObjectURL(url);
  }
  return <div className="flex-1 overflow-auto px-6 py-6 sm:px-8">
    <ClosedLoopView inputs={report} />
    <ResearchDailyView />
    <details className="border border-[#333] p-4"><summary className="cursor-pointer text-sm">数据准备与审计详情</summary>
    <div className="flex flex-wrap items-start justify-between gap-4 border-b border-[#2e2e2e] pb-5">
      <div><div className="tag-label">RESEARCH · 第一阶段</div>
        <h1 className="mt-2 text-2xl">10万元策略验证</h1>
        <p className="mt-2 max-w-3xl text-sm text-[#a0a0a0]">先核对数据与资金账，再计算收益。旧回测尚未通过复权验收，历史收益、回撤与夏普已停止作为策略结论。</p>
      </div>
      <div className="flex gap-2 text-sm">
        <button disabled={loading} onClick={() => void refresh()} className="border border-[#00aaff] px-3 py-2 disabled:opacity-40">{loading ? "正在扫描…" : "重新扫描"}</button>
        <button disabled={!report || loading} onClick={download} className="border border-[#444] px-3 py-2 disabled:opacity-40">导出审计记录</button>
      </div>
    </div>
    {error && <p role="alert" className="mt-4 text-[#fb2c36]">检查失败：{error}。当前不能判断数据是否通过。</p>}
    {!report && !error && <p className="mt-6">正在读取真实行情分区并核对文件哈希…</p>}
    {report && <>
      <div className="mt-6 grid gap-3 md:grid-cols-3">
        <Card title="行情范围" value={`${report.summary.symbols.toLocaleString()} 只`} detail={`${report.summary.first_date} 至 ${report.summary.last_date} · ${report.summary.rows.toLocaleString()} 行；全A股历史覆盖尚未验收`} />
        <Card title="最晚日期的覆盖" value={`${report.freshness.latest_stock_count} / ${report.freshness.tracked_stock_count} 只`} detail={`最晚行情 ${report.freshness.latest_date}；股票末日的中位数 ${report.freshness.median_stock_date}`} />
        <Card title="已保存复权因子文件" value={`${report.saved_factor_files.toLocaleString()} 份`} detail="文件数量代表采集进度；全部股票、全部日期的复权连续性仍须验收" />
      </div>
      <section className="mt-6 border border-[#493a20] bg-[#17130c] p-5">
        <h2 className="text-lg text-[#ffc107]">回测尚未具备发布结果的条件</h2>
        <p className="mt-2 text-sm text-[#c0c0c0]">数据检查 → 公司行动与资金账验收 → 全量策略回测 → 可复现结果与逐笔交易</p>
        <p className="mt-2 text-sm text-[#a0a0a0]">当前正在第一步。后续收益指标在验收完成后显示。</p>
      </section>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <section className="border border-[#333] p-5"><h2 className="text-lg">实际扫描发现的问题</h2>
          <p className="mt-3 text-sm">重复代码日期：{report.duplicate_keys} 组</p>
          <p className="mt-3 text-sm text-[#ffc107]">待核对的价格与成交均价差异：{report.price_basis_issues.reduce((n, x) => n + x.rows, 0).toLocaleString()} 行</p>
          <p className="mt-1 text-xs text-[#888]">仅当成交量额与日内高低价覆盖同一类成交时，成交额 ÷ 成交量才应处于高低价之间。当前来源的口径尚未核实，1%容差检查只记录待核对差异，不据此判定价格错误，也不自动改价。</p>
          <div className="mt-3 overflow-x-auto"><table className="w-full text-left text-sm"><thead className="text-[#888]"><tr><th>代码</th><th>异常行</th><th>期间</th></tr></thead><tbody>{report.price_basis_issues.map(x => <tr key={x.code} className="border-t border-[#292929]"><td className="py-2">{x.code}</td><td>{x.rows.toLocaleString()}</td><td>{x.first_date}—{x.last_date}</td></tr>)}</tbody></table></div>
          <p className="mt-4 text-sm">交易状态冲突：停牌有成交 {report.trade_state_issues.suspended_with_turnover} 行；交易日缺成交 {report.trade_state_issues.trading_without_turnover} 行；无成交且状态未知 {report.trade_state_issues.unknown_zero_turnover} 行。</p>
          <p className="mt-4 text-sm">代码变更的复权基准待验证：</p>
          {report.unverified_code_changes.map(x => <p key={`${x.old_code}-${x.new_code}`} className="mt-1 text-sm text-[#ffc107]">{x.old_code} → {x.new_code} · {x.effective_date}</p>)}
        </section>
        <section className="border border-[#333] p-5"><h2 className="text-lg">你确定的策略规则</h2>
          <p className="mt-3 text-sm leading-7">每天收盘计算市场广度：在20日日均成交额≥5000万元的股票中，计算收盘价高于MA120的比例，再取5日平均。</p>
          <p className="mt-3 text-sm leading-7">广度≥60%允许开仓；从收盘价高于MA120的合格股票中，选20日日均成交额最低的10%，按20日收益从低到高选8只，等权分配。</p>
          <p className="mt-3 text-sm leading-7">广度&lt;45%全部卖出；每月末重选，退出旧名单并买入新名单。收盘生成信号，下一交易日开盘执行。</p>
          <p className="mt-4 text-xs text-[#888]">这是用户规则说明。代码与规则的一致性及完整交易闭环尚待验收。</p>
        </section>
      </div>
      <details className="mt-6 border border-[#333] p-4"><summary className="cursor-pointer">查看源文件 SHA-256（{Object.keys(report.partition_sha256).length} 个行情分区）</summary><div className="mt-3 overflow-x-auto text-xs">{Object.entries(report.partition_sha256).map(([file, hash]) => <p key={file} className="whitespace-nowrap py-1"><span className="inline-block w-28">{file}</span>{hash}</p>)}</div></details>
    </>}
    </details>
  </div>;
}

function Card({title, value, detail}: {title: string; value: string; detail: string}) {
  return <div className="border border-[#333] p-4"><p className="text-xs text-[#888]">{title}</p><p className="mt-2 text-2xl">{value}</p><p className="mt-2 text-xs leading-5 text-[#a0a0a0]">{detail}</p></div>;
}
