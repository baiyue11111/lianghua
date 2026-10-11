import {useValue,useOperation} from "../interaction/model";
import type {DoctorItem,DoctorJob,Profile} from "@display/lib/lake";

function levelColor(lv: string) {
  return lv === "✅" ? "text-[#00bb7f]" : lv === "⚠️" ? "text-[#ffc107]"
    : lv === "ℹ️" ? "text-[#00aaff]" : "text-[#fb2c36]";
}

function SectionTitle({ children }: { children: string }) {
  return <div className="tag-label mb-2 mt-6 first:mt-0">{children}</div>;
}

const STAGES = ["抓取中", "合并中", "复检中", "完成"];

function DoctorPanel({ job, items, onStop }: {
  job: DoctorJob; items: DoctorItem[]; onStop: () => void;
}) {
  const cur = STAGES.indexOf(job.stage);
  const f = job.fetch;
  const pct = f?.total ? Math.round((f.done / f.total) * 100) : 0;
  const issueLabel = (id: string) => items.find((i) => i.id === id)?.问题 ?? id;
  return (
    <div className="mt-3 border border-[#2e2e2e] bg-[#0a0a0e] p-3 text-[12px]">
      {/* 阶段进度条 */}
      <div className="flex items-center gap-2">
        {STAGES.map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <span className={`num border px-2 py-0.5 ${
              job.stage === "失败" && i === cur ? "border-[#fb2c36] text-[#fb2c36]"
              : i < cur || job.stage === "完成" && i <= cur ? "border-[#00bb7f] text-[#00bb7f]"
              : i === cur ? "border-[#00aaff] text-[#00aaff]"
              : "border-[#2e2e2e] text-[#555]"}`}>{s}</span>
            {i < STAGES.length - 1 && <span className="text-[#333]">→</span>}
          </div>
        ))}
        {job.running && job.stage === "抓取中" && (
          <button onClick={onStop}
            className="ml-auto border border-[#fb2c36]/50 px-2 py-0.5 text-[#fb2c36] transition-colors hover:bg-[#fb2c36]/10">
            停止（保留进度）
          </button>
        )}
      </div>

      {/* 抓取阶段细节 */}
      {(job.stage === "抓取中") && f && (
        <div className="num mt-3">
          {job.issue && (
            <div className="mb-1.5 text-[#c0c0c0]">正在修复：{issueLabel(job.issue)}</div>
          )}
          <div className="flex items-center gap-3">
            <div className="h-1.5 flex-1 bg-[#101014]">
              <div className="h-full bg-[#00aaff]" style={{ width: `${pct}%` }} />
            </div>
            <span>{f.done}/{f.total} · {pct}%</span>
          </div>
          <div className="mt-1.5 text-[#8f8f8f]">
            新增 {f.rows?.toLocaleString()} 行 · 空 {f.empty} · 失败 {f.failed?.length ?? 0}
          </div>
          <div className="mt-0.5 truncate text-[#666]">{f.current}</div>
        </div>
      )}
      {job.stage === "合并中" && <div className="num mt-3 text-[#8f8f8f]">正在合并写盘（只重写被触动的年份分区）…</div>}
      {job.stage === "复检中" && <div className="num mt-3 text-[#8f8f8f]">正在全量复检（约 1~2 秒）…</div>}
      {job.stage === "完成" && (
        <div className="num mt-3 space-y-1">
          <div className={job.verdict === "已修复" ? "text-[#00bb7f]" : "text-[#ffc107]"}>
            复检结论：{job.verdict === "已修复" ? "✅ 全部已修复" : "⚠️ 部分仍存在（可再次修复，已抓部分不会重复）"}
          </div>
          {Object.entries(job.verdicts ?? {}).map(([id, v]) => (
            <div key={id} className={v === "已修复" ? "text-[#00bb7f]" : "text-[#ffc107]"}>
              · {v === "已修复" ? "✅" : "⚠️"} {issueLabel(id)} —— {v}
            </div>
          ))}
        </div>
      )}
      {job.stage === "失败" && <div className="num mt-3 text-[#fb2c36]">修复失败</div>}
      {job.log.length > 0 && (
        <div className="num mt-2 space-y-0.5 text-[#666]">
          {job.log.map((l, i) => <div key={i}>· {l}</div>)}
        </div>
      )}
    </div>
  );
}

export default function StatusView() {
  const [rescan,setRescan]=useValue('status','rescan',false);
  const [issue,setIssue]=useValue('status','issue','');
  const report=useOperation<Profile>('status:profile','lake.status',{rescan});
  const issues=useOperation<{items:DoctorItem[]}>('status:issues','lake.doctor');
  const task=useOperation<DoctorJob>('status:job','lake.doctorJob');
  const repair=useOperation<{ok:boolean;message?:string}>('status:write','lake.doctorFix',{id:issue},true,false);
  const stop=useOperation('status:write','lake.stopFetch',{},true,false);
  const p=report.value??null,doc=issues.value?.items??null,job=task.value??null;
  const loading=report.status==='pending',err=report.error,fixErr=issues.error||task.error||repair.error||stop.error;
  const load=(value:boolean)=>{if(value===rescan)void report.invoke();else setRescan(value);};
  const runFix=(id:string)=>{setIssue(id);void repair.invoke({id});};
  if (loading && !p)
    return <div className="num flex flex-1 items-center justify-center text-[#8f8f8f]">正在全量扫描数据湖，约 1~2 秒…</div>;
  if (err)
    return <div className="flex flex-1 items-center justify-center text-[#fb2c36]">{err}</div>;
  if (!p) return null;

  const o = p.总览;
  const years = Object.entries(p.年度覆盖);
  const maxStocks = Math.max(...years.map(([, y]) => y.现存股日均 ?? 0));
  const lag = o.日线行情.落后交易日;

  return (
    <div className="flex-1 overflow-y-auto px-8 py-6">
      {/* 头部 + 结论 */}
      <div className="flex items-start justify-between border-b border-[#1e1e24] pb-4">
        <div>
          <div className="tag-label">DATA LAKE · 体检报告</div>
          <div className={`mt-2 text-xl ${levelColor(p.结论.级别)}`}>
            {p.结论.级别}{" "}
            {p.结论.级别 === "✅" ? "数据可用" : p.结论.级别 === "⚠️" ? "数据可用，有注意事项" : "数据存在严重问题"}
            <span className="ml-3 text-sm text-[#8f8f8f]">
              {p.结论.回测可用 ? "回测区间完整" : "回测区间不完整"}
            </span>
          </div>
          {/* 统一修复入口：一次点击修复全部可修复项 */}
          {(() => {
            const nFix = (doc ?? []).filter((i) => i.可修复).length;
            return nFix > 0 && (
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <button
                  onClick={() => runFix("all")}
                  disabled={!!job?.running}
                  className="num border border-[#00aaff]/60 bg-[#00aaff]/10 px-3 py-1 text-[12px] text-[#00aaff] transition-colors duration-200 hover:bg-[#00aaff]/20 disabled:opacity-40"
                >
                  一键修复（{nFix} 项）
                </button>
                <span className="text-[11px] text-[#666]">
                  逐项抓取缺失数据 → 一次合并入湖 → 复检验证；已补过的内容不会重复抓，可随时停止续传
                </span>
              </div>
            );
          })()}
          {/* 问题清单（只读展示，修复走上面的统一按钮） */}
          <div className="mt-3 space-y-1.5">
            {(doc ?? []).map((it) => (
              <div key={it.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                <span className={levelColor(it.级别)}>{it.级别}</span>
                <span className="text-[#c0c0c0]">{it.问题}</span>
                <span className="text-[#666]">{it.可修复 ? it.范围 : it.说明}</span>
              </div>
            ))}
            {doc !== null && doc.length === 0 && (
              <div className="text-xs text-[#00bb7f]">· 体检未发现需要处理的问题</div>
            )}
            {fixErr && <div className="text-xs text-[#fb2c36]">· {fixErr}</div>}
          </div>
          {job && (job.running || job.stage !== "") && (
            <DoctorPanel job={job} items={doc ?? []}
              onStop={() => stop.invoke()} />
          )}
        </div>
        <div className="flex flex-col items-end gap-2">
          <span className="num text-[11px] text-[#8f8f8f]">快照 {p.生成时间}</span>
          <button
            onClick={() => load(true)}
            disabled={loading}
            className="num border border-[#2e2e2e] px-3 py-1.5 text-[11px] tracking-[0.1em] text-[#e0e0e0] transition-colors duration-200 hover:border-[#00aaff] hover:text-[#00aaff] disabled:opacity-40"
          >
            {loading ? "扫描中…" : "重新扫描"}
          </button>
        </div>
      </div>

      {/* 总览 */}
      <SectionTitle>数据总览</SectionTitle>
      <table className="w-full text-[13px]">
        <thead>
          <tr className="tag-label border-b border-[#1e1e24] text-left">
            <th className="py-1.5 font-normal">表</th>
            <th className="py-1.5 text-right font-normal">行数</th>
            <th className="py-1.5 text-right font-normal">股票数</th>
            <th className="py-1.5 text-right font-normal">区间</th>
            <th className="py-1.5 text-right font-normal">新鲜度</th>
          </tr>
        </thead>
        <tbody className="num">
          <tr className="border-b border-[#15151a]">
            <td className="py-2 font-sans">日线行情</td>
            <td className="text-right">{o.日线行情.行数.toLocaleString()}</td>
            <td className="text-right">{o.日线行情.股票数.toLocaleString()}</td>
            <td className="text-right text-[#9a9aab]">{o.日线行情.区间}</td>
            <td className={`text-right ${lag <= 5 ? "text-[#00bb7f]" : lag <= 20 ? "text-[#ffc107]" : "text-[#fb2c36]"}`}>
              落后 {lag} 个交易日
            </td>
          </tr>
          <tr className="border-b border-[#15151a]">
            <td className="py-2 font-sans">股票主档</td>
            <td className="text-right">{o.股票主档.行数.toLocaleString()}</td>
            <td className="text-right text-[#9a9aab]">现存 {o.股票主档.现存} / 退市 {o.股票主档.退市}</td>
            <td /><td />
          </tr>
          <tr className="border-b border-[#15151a]">
            <td className="py-2 font-sans">估值因子（PE/PB 等）</td>
            <td className="text-right">{o.估值因子.行数.toLocaleString()}</td>
            <td className="text-right">{o.估值因子.股票数.toLocaleString()}</td>
            <td className="text-right text-[#9a9aab]">{o.估值因子.区间}</td>
            <td />
          </tr>
          <tr className="border-b border-[#15151a]">
            <td className="py-2 font-sans">基本面因子（37 项）</td>
            <td className="text-right">{o.基本面因子.行数.toLocaleString()}</td>
            <td className="text-right">{o.基本面因子.股票数.toLocaleString()}</td>
            <td className="text-right text-[#9a9aab]">{o.基本面因子.区间}</td>
            <td />
          </tr>
        </tbody>
      </table>

      {/* 年度覆盖：内联条形 */}
      <SectionTitle>年度覆盖</SectionTitle>
      <div className="space-y-1">
        {years.map(([yr, y]) => (
          <div key={yr} className="num flex items-center gap-3 text-[12px]">
            <span className="w-10 text-[#9a9aab]">{yr}</span>
            <div className="h-3.5 flex-1 bg-[#101014]">
              <div
                className="h-full bg-[#00aaff]/70"
                style={{ width: `${((y.现存股日均 ?? 0) / maxStocks) * 100}%` }}
              />
            </div>
            <span className="w-14 text-right">{y.现存股日均 ?? 0}</span>
            <span className="w-24 text-right text-[#8f8f8f]">退市 {y.退市股日均 ?? 0}</span>
            <span className="w-16 text-right text-[#8f8f8f]">{y.交易日} 天</span>
          </div>
        ))}
      </div>

      {/* 字段覆盖 */}
      <SectionTitle>字段覆盖</SectionTitle>
      <div className="grid grid-cols-2 gap-x-10 gap-y-1.5">
        {Object.entries(p.日线字段).map(([col, s]) => (
          <div key={col} className="num flex items-center gap-3 text-[12px]">
            <span className={`w-4 ${levelColor(s.有值率 >= 90 ? "✅" : s.有值率 >= 50 ? "⚠️" : "❌")}`}>
              {s.有值率 >= 90 ? "✅" : s.有值率 >= 50 ? "⚠️" : "❌"}
            </span>
            <span className="w-28 text-[#c0c0c0]">{col}</span>
            <div className="h-1.5 flex-1 bg-[#101014]">
              <div
                className={`h-full ${s.有值率 >= 90 ? "bg-[#00bb7f]" : s.有值率 >= 50 ? "bg-[#ffc107]" : "bg-[#fb2c36]"}`}
                style={{ width: `${s.有值率}%` }}
              />
            </div>
            <span className="w-14 text-right">{s.有值率}%</span>
            <span className="w-20 text-right text-[#8f8f8f]">{s.覆盖股数} 只</span>
          </div>
        ))}
      </div>
      <div className="num mt-3 text-[12px] text-[#9a9aab]">
        估值因子：
        {Object.entries(p.估值因子字段).map(([k, r]) => (
          <span key={k} className="mr-4">{k} <span className="text-[#e0e0e0]">{r}%</span></span>
        ))}
      </div>
      {Object.entries(p.基本面因子字段).map(([g, cols]) => (
        <div key={g} className="num mt-1.5 text-[12px] text-[#9a9aab]">
          基本面·{g}：
          {Object.entries(cols)
            .sort((a, b) => a[1] - b[1])
            .slice(0, 2)
            .map(([k, r]) => (
              <span key={k} className="mr-4">{k} <span className="text-[#e0e0e0]">{r}%</span></span>
            ))}
          <span className="text-[#666]">共 {Object.keys(cols).length} 项</span>
        </div>
      ))}

      {/* 异常 + 回测 */}
      <SectionTitle>异常检查</SectionTitle>
      <div className="space-y-1 text-[12px]">
        {p.异常检查.map((a) => (
          <div key={a} className={a.startsWith("❌") ? "text-[#fb2c36]" : a.startsWith("⚠️") ? "text-[#ffc107]" : "text-[#9a9aab]"}>{a}</div>
        ))}
      </div>

      <SectionTitle>回测可用性</SectionTitle>
      <div className="num text-[13px]">
        <span className={p.回测区间.完整 ? "text-[#00bb7f]" : "text-[#fb2c36]"}>
          {p.回测区间.完整 ? "✅" : "❌"} 策略区间 {p.回测区间.区间}
        </span>
        <span className="ml-4 text-[#9a9aab]">
          {p.回测区间.行数.toLocaleString()} 行 / {p.回测区间.股票数.toLocaleString()} 只
        </span>
      </div>
      <div className="h-8" />
    </div>
  );
}
