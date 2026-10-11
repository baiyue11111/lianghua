import { useEffect, useRef } from "react";
import type {ContinuousFetch,FetchJob,MergeJob} from "@display/lib/lake";
import {useValue,useOperation} from "../interaction/model";

const MODES = [
  { key: "auto", label: "自动增量", desc: "由数据湖现状推导缺口：上市前回补 + 最新增量 + 新股探测" },
  { key: "fix", label: "区间修复", desc: "指定起止日期重抓，可限定股票与字段预设" },
  { key: "factors", label: "因子补抓", desc: "补齐估值因子（PE/PB 等）缺失的季度" },
];

const FIELD_PRESETS = [
  { key: "basic", label: "basic · 行情基础（OHLC/量额/换手/ST）" },
  { key: "st", label: "st · 换手/涨跌幅/交易状态/ST 标记" },
  { key: "valuation", label: "valuation · PE/PB/PS/PCF" },
];

function fetchStatus(status: FetchJob["status"], running: boolean) {
  if (running || status === "running") return { label: "抓取中", color: "text-[#00aaff]" };
  if (status === "stopping") return { label: "停止中", color: "text-[#ffc107]" };
  if (status === "stopped") return { label: "已停止，进度已保留", color: "text-[#ffc107]" };
  if (status === "failed") return { label: "抓取失败", color: "text-[#fb2c36]" };
  if (status === "succeeded") return { label: "抓取成功", color: "text-[#00bb7f]" };
  return { label: "空闲", color: "text-[#8f8f8f]" };
}

export default function FetchView() {
  const [mode,setMode]=useValue('fetch','mode','auto'),[start,setStart]=useValue('fetch','start',''),[end,setEnd]=useValue('fetch','end',''),[codes,setCodes]=useValue('fetch','codes',''),[fields,setFields]=useValue('fetch','fields','basic'),[force,setForce]=useValue('fetch','force',false);
  const task=useOperation<FetchJob>('fetch:job','lake.fetchJob');
  const merging=useOperation<MergeJob>('fetch:merge','lake.mergeJob');
  const schedule=useOperation<ContinuousFetch>('fetch:schedule','lake.continuousFetch');
  const beginning=useOperation('fetch:write','lake.startFetch',{mode,fields,force,start:start||null,end:end||null,codes:codes||null},true,false);
  const stopping=useOperation('fetch:write','lake.stopFetch',{},true,false);
  const continuous=schedule.value??null,job=task.value??null,merge=merging.value??null;
  const changing=useOperation('fetch:write','lake.setContinuousFetch',{enabled:!continuous?.enabled},true,false);
  const [dryRun,setDryRun]=useValue('fetch','dryRun',true);
  const mergeAction=useOperation('fetch:write','lake.startMerge',{dryRun},true,false);
  const continuousBusy=changing.status==='pending',stopRequested=stopping.status==='pending';
  const error=task.error||merging.error||schedule.error||beginning.error||stopping.error||changing.error||mergeAction.error;
  const logRef=useRef<HTMLDivElement>(null);
  useEffect(()=>{logRef.current?.scrollTo(0,logRef.current.scrollHeight);},[merge?.output]);
  const launch=()=>beginning.invoke(),stop=()=>stopping.invoke(),toggleContinuous=()=>changing.invoke();
  const pct = job && job.total > 0 ? Math.round((job.done / job.total) * 100) : 0;

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-4 py-6 sm:px-8">
      <div className="tag-label">FETCH · 数据抓取</div>
      {error && <div role="alert" className="mt-3 border border-[#fb2c36]/60 bg-[#fb2c36]/10 px-3 py-2 text-[12px] text-[#fb2c36]">{error}</div>}

      <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 border border-[#1e1e24] px-3 py-2.5 text-[12px]">
        <label className="flex items-center gap-2 text-[#c0c0c0]">
          <input type="checkbox" checked={continuous?.enabled ?? false} onChange={toggleContinuous}
            disabled={!continuous || continuousBusy} className="h-3.5 w-3.5 accent-[#00aaff]" />
          自动持续抓取
        </label>
        <span className={continuous?.enabled ? "text-[#00bb7f]" : "text-[#8f8f8f]"}>
          {continuous ? (continuous.enabled ? "已启用" : "已停用") : "读取中…"}
        </span>
        {continuous && <>
          <span className="text-[#8f8f8f]">上次运行 {continuous.last_run_at ?? "—"}</span>
          <span className="text-[#8f8f8f]">下次运行 {continuous.next_run_at ?? "—"}</span>
          <span className={continuous.latest_data_error ? "text-[#fb2c36]" : "text-[#8f8f8f]"}>
            最晚行情 {continuous.latest_data_error ? "读取失败" : (continuous.latest_data_date ?? "—")}
            {!continuous.latest_data_error && continuous.latest_data_date && continuous.tracked_stock_count != null &&
              `（${continuous.latest_data_stock_count ?? 0}/${continuous.tracked_stock_count}只）`}
          </span>
          {!continuous.latest_data_error && continuous.median_stock_date &&
            <span className="text-[#8f8f8f]">过半股票更新至 {continuous.median_stock_date}</span>}
          {continuous.schedule_error && <span role="alert" className="text-[#fb2c36]">调度错误：{continuous.schedule_error}</span>}
          {continuous.latest_data_error && <span role="alert" className="text-[#fb2c36]">最新数据错误：{continuous.latest_data_error}</span>}
        </>}
      </div>

      {/* 模式选择 */}
      <div className="mt-4 grid grid-cols-1 gap-px border border-[#1e1e24] bg-[#1e1e24] sm:grid-cols-3">
        {MODES.map((m) => (
          <button
            key={m.key}
            onClick={() => setMode(m.key)}
            className={`px-4 py-3 text-left transition-colors duration-200 ${
              mode === m.key ? "bg-[#101014] text-white" : "bg-black text-[#8f8f8f] hover:text-[#c0c0c0]"
            }`}
          >
            <div className="text-[13px]">{m.label}</div>
            <div className="mt-1 text-[11px] leading-relaxed opacity-70">{m.desc}</div>
            {mode === m.key && <div className="mt-2 h-[2px] w-8 bg-[#00aaff]" />}
          </button>
        ))}
      </div>

      {/* 参数 */}
      <div className="mt-4 flex flex-wrap items-end gap-4">
        {mode === "fix" && (
          <>
            <label className="block">
              <span className="tag-label">起始日期</span>
              <input value={start} onChange={(e) => setStart(e.target.value)} placeholder="2014-01-01"
                className="num mt-1 block w-36 max-w-full border border-[#2e2e2e] bg-black px-2 py-1.5 text-[12px] outline-none focus:border-[#00aaff]" />
            </label>
            <label className="block">
              <span className="tag-label">结束日期</span>
              <input value={end} onChange={(e) => setEnd(e.target.value)} placeholder="默认今天"
                className="num mt-1 block w-36 max-w-full border border-[#2e2e2e] bg-black px-2 py-1.5 text-[12px] outline-none focus:border-[#00aaff]" />
            </label>
            <label className="block">
              <span className="tag-label">股票（逗号分隔，留空=全部）</span>
              <input value={codes} onChange={(e) => setCodes(e.target.value)} placeholder="600036,000001"
                className="num mt-1 block w-56 max-w-full border border-[#2e2e2e] bg-black px-2 py-1.5 text-[12px] outline-none focus:border-[#00aaff]" />
            </label>
            <label className="block">
              <span className="tag-label">字段预设</span>
              <select value={fields} onChange={(e) => setFields(e.target.value)}
                className="num mt-1 block w-72 max-w-full border border-[#2e2e2e] bg-black px-2 py-1.5 text-[12px] outline-none focus:border-[#00aaff]">
                {FIELD_PRESETS.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-2 pb-1.5 text-[12px] text-[#9a9aab]">
              <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)}
                className="h-3.5 w-3.5 accent-[#00aaff]" />
              忽略缓存重抓
            </label>
          </>
        )}
        <button
          onClick={launch}
          disabled={job?.running}
          className="num bg-white px-5 py-2 text-[12px] tracking-[0.1em] text-black transition-colors duration-200 hover:bg-[#00aaff] hover:text-white disabled:opacity-40"
        >
          {job?.running ? "抓取中…" : "开始抓取"}
        </button>
        {job?.running && (
          <button onClick={stop} disabled={stopRequested}
            className="num border border-[#fb2c36] px-4 py-2 text-[12px] tracking-[0.1em] text-[#fb2c36] transition-colors duration-200 hover:bg-[#fb2c36] hover:text-white disabled:opacity-40">
            {stopRequested ? "停止请求已提交…" : "停止"}
          </button>
        )}
      </div>

      {/* 进度 */}
      {job && (job.total > 0 || job.running || job.error || job.status === "stopped" || job.status === "failed") && (
        <div className="mt-6 border border-[#1e1e24] p-4">
          <div className="num flex items-baseline justify-between text-[12px]">
            <span>
              <span className="text-xl text-white">{job.done}</span>
              <span className="text-[#8f8f8f]"> / {job.total} 任务</span>
            </span>
            <span className="text-right text-[#9a9aab]">
              {(() => { const state = fetchStatus(job.status, job.running); return <span className={`mr-3 ${state.color}`}>{state.label}</span>; })()}
              新增 <span className="text-[#00aaff]">{job.rows.toLocaleString()}</span> 行 ·
              空 {job.empty} · 失败 <span className={job.failed.length ? "text-[#fb2c36]" : ""}>{job.failed.length}</span>
              {job.finished_at && <span className="ml-3 text-[#8f8f8f]">结束于 {job.finished_at}</span>}
            </span>
          </div>
          {job.error && <div role="alert" className="mt-2 border-l-2 border-[#fb2c36] pl-2 text-[12px] text-[#fb2c36]">失败原因：{job.error}</div>}
          <div className="mt-2 h-1.5 bg-[#101014]">
            <div className="h-full bg-[#00aaff] transition-all duration-300" style={{ width: `${pct}%` }} />
          </div>
          <div className="num mt-2 truncate text-[11px] text-[#8f8f8f]">{job.current}</div>
          {job.log.map((l, i) => (
            <div key={i} className="num mt-1 text-[11px] text-[#9a9aab]">{l}</div>
          ))}
        </div>
      )}

      {/* 合并 */}
      <div className="tag-label mb-2 mt-8">MERGE · 合并入湖</div>
      <div className="flex items-center gap-3">
        <button onClick={() => {setDryRun(true);void mergeAction.invoke({dryRun:true});}} disabled={merge?.running}
          className="num border border-[#2e2e2e] px-4 py-2 text-[12px] tracking-[0.1em] transition-colors duration-200 hover:border-[#00aaff] hover:text-[#00aaff] disabled:opacity-40">
          预演合并
        </button>
        <button onClick={() => { if (confirm("确认正式合并？将改写数据湖文件并清理缓存。")) {setDryRun(false);void mergeAction.invoke({dryRun:false});} }}
          disabled={merge?.running}
          className="num bg-white px-4 py-2 text-[12px] tracking-[0.1em] text-black transition-colors duration-200 hover:bg-[#00aaff] hover:text-white disabled:opacity-40">
          正式合并
        </button>
        {merge?.running && <span className="num text-[12px] text-[#ffc107]">合并进行中（约 1~2 分钟）…</span>}
        {merge?.finished_at && !merge.running && (
          <span className="num text-[11px] text-[#8f8f8f]">结束于 {merge.finished_at}</span>
        )}
      </div>
      {merge?.output && (
        <div ref={logRef}
          className="num mt-3 max-h-64 overflow-y-auto border border-[#1e1e24] bg-[#050508] p-3 text-[11px] leading-relaxed text-[#9a9aab] whitespace-pre-wrap">
          {merge.output}
        </div>
      )}
    </div>
  );
}
