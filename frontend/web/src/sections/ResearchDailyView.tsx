import {useValue,useOperation} from "../interaction/model";
import type {ResearchDaily} from "@display/lib/lake";

const STATE: Record<string, string> = { active: "仿真启用", paused: "暂停", watch: "观察" };
const REASON: Record<string, string> = {
  historical_qualification_unavailable: "尚未取得历史验证资格",
  insufficient_shadow_history: "观察样本不足",
  entry_conditions_not_met: "近期表现未达恢复门槛",
  entry_confirmation: "等待持续满足启用条件",
  entry_conditions_confirmed: "持续满足启用条件",
  exit_confirmation: "近期走弱，等待确认",
  maintain_active: "持续满足运行条件",
  recent_performance_deteriorated: "近期表现恶化，已暂停",
  recent_performance_recovered: "近期表现恢复，重新启用",
  drawdown_limit: "近期回撤达到暂停门槛",
};
const percent = (x: number | null) => x === null ? "—" : `${(x * 100).toFixed(1)}%`;

export default function ResearchDailyView() {
  const [asOf,setAsOf]=useValue('daily','date','');
  const result=useOperation<ResearchDaily>('daily:report','lake.researchDaily',asOf?{asOf}:{});
  const daily=result.value??null,error=result.error,busy=result.status==='pending';
  const load=()=>result.invoke();
  return <section className="mb-8">
    <div className="flex flex-wrap items-end justify-between gap-4 border-b border-[#333] pb-5">
      <div><p className="tag-label">DAILY STRATEGY RESEARCH</p><h1 className="mt-2 text-2xl">每天测了多少，今天哪些能用</h1>
        <p className="mt-2 text-sm text-[#aaa]">{daily ? `${daily.as_of} · ` : ""}计数来自完整验证记录；启用名单每天更新。</p></div>
      <div className="flex flex-wrap gap-2"><input aria-label="研究日期" type="date" value={asOf} onChange={e => setAsOf(e.target.value)} className="border border-[#444] bg-black px-2 py-2 text-sm [color-scheme:dark]" />
        <button disabled={busy} onClick={() => void load()} className="border border-[#00aaff] px-3 py-2 text-sm disabled:opacity-40">{busy ? "读取中…" : "查看日报"}</button></div>
    </div>
    {error && <p role="alert" className="mt-4 text-[#fb2c36]">研究台账读取失败：{error}。计数不可用。</p>}
    {daily && !error && <>
      <div className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[ ["完成验证", daily.today_completed, `${daily.today_families}个家族 · ${daily.today_runs}次成功运行`],
          ["当前仿真启用", daily.active, "历史验证和动态启停规则均通过验收"],
          ["当日暂停", daily.today_paused, "持续走弱或达到回撤门槛"],
          ["当日恢复", daily.today_resumed, "暂停期间继续观察，满足恢复条件"] ].map(([label, value, hint]) =>
          <div key={String(label)} className="border border-[#333] p-4"><p className="text-xs text-[#aaa]">{label}</p><p className="mt-2 text-3xl">{value}</p><p className="mt-2 text-xs leading-5 text-[#888]">{hint}</p></div>)}
      </div>
      <p className="mt-3 text-xs text-[#888]">失败 {daily.today_failed} 次 · 无效 {daily.today_invalid} 次。同一配置重复运行只计一个策略配置。</p>
      {daily.strategies.length === 0 ? <div className="mt-5 border border-[#333] p-5"><h2 className="text-lg">还没有经过验收的动态策略名单</h2>
        <p className="mt-2 text-sm text-[#aaa]">当前有效记录为0。数据与资金账验收完成、策略实际跑完后，结果会进入这里；候选公式和测试样例不计入日报。</p></div> :
        <div className="mt-5 overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-sm"><thead className="text-[#888]"><tr><th>策略</th><th>状态</th><th>近期夏普</th><th>近期收益</th><th>近期回撤</th><th>判断日期</th><th>原因</th></tr></thead><tbody>{daily.strategies.map(row =>
          <tr key={`${row.strategy_id}-${row.config_hash}-${row.policy_hash}`} className="border-t border-[#333]"><td className="py-3 pr-4">{row.strategy_id}<span className="block text-xs text-[#888]">配置 {row.config_hash.slice(0, 8)} · 启停 {row.policy_hash.slice(0, 8)}</span></td><td className="pr-4">{row.assessment_current ? STATE[row.state] ?? row.state : "待更新判断"}</td><td>{row.sharpe?.toFixed(2) ?? "—"}</td><td>{percent(row.recent_return)}</td><td>{percent(row.drawdown)}</td><td className="px-3">{row.day}</td><td>{REASON[row.reason] ?? row.reason}</td></tr>)}</tbody></table></div>}
      <details className="mt-4 text-sm text-[#aaa]"><summary className="cursor-pointer">为什么策略可以暂停后恢复？</summary><p className="mt-2 max-w-3xl leading-7">历史资格与当前状态分开记录。暂停期间继续模拟原策略，使用当时已知的净收益判断是否恢复；收盘判断在下一交易日执行。启停参数也需要独立回测，目前研究参数尚未验收。</p></details>
    </>}
  </section>;
}
