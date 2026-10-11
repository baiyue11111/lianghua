import {useMemo} from "react";
import {useValue,useOperation} from "../interaction/model";
import type {ClosedLoopReport,ResearchInputs} from "@display/lib/lake";
import { formatMoney, formatPercent, formatPrice, sideLabel } from "@display/lib/closed-loop";

function day(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", timeZone: "UTC" })
    .format(new Date(value));
}

export default function ClosedLoopView({ inputs }: { inputs: ResearchInputs | null }) {
  const result=useOperation<ClosedLoopReport>('closed-loop:report','lake.closedLoop');
  const report=result.value??null,error=result.error;
  const [choice,setSelectedSequence]=useValue<number|null>('closed-loop','selected',null);
  const selectedSequence=choice??report?.timeline.at(-1)?.sequence??null;
  const selected = report?.timeline.find((point) => point.sequence === selectedSequence) ?? null;
  const selectedTrades = useMemo(
    () => report?.trades.filter((trade) => trade.sequence === selectedSequence) ?? [],
    [report, selectedSequence],
  );

  if (error) return <section className="border border-[#6b2424] bg-[#160b0b] p-5" role="alert">
    <h1 className="text-xl text-[#ff6b6b]">闭环审计读取失败</h1>
    <p className="mt-2 text-sm text-[#c9a3a3]">{error}</p>
  </section>;
  if (!report) return <section className="border-b border-[#2e2e2e] pb-6">
    <h1 className="text-2xl">策略验证</h1><p className="mt-2 text-sm text-[#999]">正在校验审计文件与哈希…</p>
  </section>;

  const final = report.timeline.at(-1)!;
  return <section className="mb-10">
    <header className="grid gap-5 border-b border-[#2e2e2e] pb-6 lg:grid-cols-[1fr_auto] lg:items-end">
      <div>
        <p className="text-xs text-[#7d8790]">策略验证</p>
        <h1 className="mt-2 text-3xl font-medium tracking-tight text-white">{report.strategy.name}</h1>
        <p className="mt-3 max-w-3xl text-sm leading-6 text-[#a4a8ad]">{report.validation.historical_reason}</p>
      </div>
      <div className="flex gap-5 border-l border-[#333] pl-5 text-sm">
        <Status label="工程闭环" value="通过" tone="good" />
        <Status label="历史有效性" value={report.validation.historical === "PASSED" ? "通过" : "未通过"} tone="blocked" />
      </div>
    </header>

    <div className="grid border-b border-[#2e2e2e] sm:grid-cols-2 lg:grid-cols-4">
      <Metric label="测试本金" value={formatMoney(report.run.initial_cash)} />
      <Metric label="闭环步骤" value={`${report.run.steps}天`} />
      <Metric label="完整成交" value={`${report.trades.length}笔`} />
      <Metric label="结束状态" value={`${final.position_count}只持仓 · ${formatMoney(final.cash)}`} />
    </div>

    <div className="grid gap-8 py-7 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.6fr)]">
      <section>
        <h2 className="text-lg text-white">买卖判断条件</h2>
        <ol className="mt-4 space-y-4 border-l border-[#34383d] pl-5 text-sm leading-6 text-[#b4b7bb]">
          {report.strategy.rules.map((rule, index) => <li key={rule} className="relative">
            <span className="absolute -left-[25px] top-1.5 h-2 w-2 bg-[#00aaff]" />
            <span className="mr-2 text-[#69717a]">{String(index + 1).padStart(2, "0")}</span>{rule}
          </li>)}
        </ol>
        <details className="mt-6 border-t border-[#292929] pt-4 text-xs text-[#8b9198]">
          <summary className="cursor-pointer text-[#b4b7bb]">查看冻结参数</summary>
          <dl className="mt-3 grid grid-cols-2 gap-x-5 gap-y-2">
            {Object.entries(report.strategy.parameters).map(([key, value]) => <div key={key} className="contents">
              <dt>{key}</dt><dd className="text-right font-mono text-[#d4d7da]">{value}</dd>
            </div>)}
          </dl>
        </details>
      </section>

      <section className="min-w-0">
        <div className="flex items-end justify-between gap-4">
          <div><h2 className="text-lg text-white">信号到资金的完整过程</h2>
            <p className="mt-1 text-xs text-[#858b92]">点击日期查看当日信号、订单、成交和持仓。</p></div>
          <p className="text-xs text-[#69717a]">T日收盘判断 · T+1开盘执行</p>
        </div>
        <div className="mt-4 grid gap-px bg-[#292d31] md:grid-cols-4">
          {report.timeline.map((point) => <button key={point.sequence} type="button"
            onClick={() => setSelectedSequence(point.sequence)}
            className={`min-h-32 bg-black p-4 text-left transition-colors ${selectedSequence === point.sequence ? "bg-[#0b1820] outline outline-1 outline-[#00aaff]" : "hover:bg-[#111417]"}`}>
            <span className="text-xs text-[#7d8790]">第{point.sequence}天 · {day(point.at)}</span>
            <strong className="mt-3 block text-sm font-medium text-white">{point.action}</strong>
            <span className="mt-3 block text-xs text-[#8f959b]">广度 {formatPercent(point.breadth_5d)}</span>
            <span className="mt-1 block text-xs text-[#8f959b]">现金 {formatMoney(point.cash)}</span>
          </button>)}
        </div>

        {selected && <div className="mt-5 border border-[#30343a] bg-[#080a0c]">
          <div className="grid gap-px bg-[#25292d] sm:grid-cols-3 lg:grid-cols-6">
            <Fact label="市场广度" value={formatPercent(selected.breadth_5d)} />
            <Fact label="入选股票" value={`${selected.signal_count}只`} />
            <Fact label="计划订单" value={`${selected.planned_count}笔`} />
            <Fact label="实际成交" value={`${selected.fill_count}笔`} />
            <Fact label="持仓" value={`${selected.position_count}只`} />
            <Fact label="账户权益" value={formatMoney(selected.equity)} />
          </div>
          <div className="p-4">
            <h3 className="text-sm text-white">当日成交</h3>
            {selectedTrades.length === 0 ? <p className="mt-3 text-sm text-[#7f858c]">当日没有成交。{selected.action}。</p> :
              <div className="mt-3 overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs">
                <thead className="text-[#767d84]"><tr><th>股票</th><th>方向</th><th>数量</th><th>成交价</th><th>佣金</th><th>印花税</th><th>总费用</th></tr></thead>
                <tbody>{selectedTrades.map((trade) => <tr key={trade.order_id} className="border-t border-[#25292d] text-[#c4c7ca]">
                  <td className="py-3 pr-5 font-mono">{trade.instrument}</td><td className={trade.side === "buy" ? "text-[#45d483]" : "text-[#ff7c73]"}>{sideLabel(trade.side)}</td>
                  <td>{Number(trade.quantity).toLocaleString("zh-CN")}</td><td>{formatPrice(trade.price)}</td><td>{formatMoney(trade.commission)}</td><td>{formatMoney(trade.stamp_duty)}</td><td>{formatMoney(trade.fee)}</td>
                </tr>)}</tbody>
              </table></div>}
            {selected.position_count > 0 && <p className="mt-3 text-xs text-[#858b92]">持仓：{Object.entries(selected.positions).map(([symbol, quantity]) => `${symbol} ${Number(quantity).toLocaleString("zh-CN")}股`).join(" · ")}</p>}
          </div>
        </div>}
      </section>
    </div>

    <div className="grid gap-4 border-y border-[#2e2e2e] py-5 text-sm md:grid-cols-[1fr_auto] md:items-center">
      <div><p className="text-[#c5c8cb]">真实历史回测仍被数据门禁阻断</p>
        <p className="mt-1 text-xs leading-5 text-[#858b92]">{inputs ? `${inputs.summary.rows.toLocaleString()}行行情中，带复权收盘价${inputs.summary.adjusted_close_rows.toLocaleString()}行，非法价格${inputs.summary.invalid_price_rows}行。` : "正在读取全量行情覆盖率。"}</p></div>
      <span className="w-fit border border-[#704b1f] px-3 py-2 text-xs text-[#f4b85f]">{inputs?.ranking_status ?? "读取中"}</span>
    </div>

    <details className="mt-4 text-xs text-[#7f858c]"><summary className="cursor-pointer text-[#aeb3b8]">审计来源与复现标识</summary>
      <dl className="mt-3 grid gap-2 font-mono"><div><dt className="inline text-[#626970]">run_id </dt><dd className="inline">{report.run.run_id}</dd></div>
        <div className="break-all"><dt className="inline text-[#626970]">SHA-256 </dt><dd className="inline">{report.run.events_sha256}</dd></div>
        <div className="break-all"><dt className="inline text-[#626970]">审计文件 </dt><dd className="inline">{report.run.events_file}</dd></div></dl>
    </details>
  </section>;
}

function Status({ label, value, tone }: { label: string; value: string; tone: "good" | "blocked" }) {
  return <div><p className="text-xs text-[#7d8790]">{label}</p><p className={`mt-1 text-base ${tone === "good" ? "text-[#45d483]" : "text-[#f4b85f]"}`}>{value}</p></div>;
}
function Metric({ label, value }: { label: string; value: string }) {
  return <div className="border-r border-[#2e2e2e] px-4 py-5 first:pl-0 last:border-r-0"><p className="text-xs text-[#7d8790]">{label}</p><p className="mt-2 text-xl text-white">{value}</p></div>;
}
function Fact({ label, value }: { label: string; value: string }) {
  return <div className="bg-[#080a0c] p-3"><p className="text-[11px] text-[#737a82]">{label}</p><p className="mt-1 text-sm text-[#d9dcdf]">{value}</p></div>;
}
