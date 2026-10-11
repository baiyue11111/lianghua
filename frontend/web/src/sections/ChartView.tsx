import {maSeries} from "@display/lib/ma-series";
import { useMemo, useRef } from "react";
import ReactECharts from "echarts-for-react";
import type { ECharts } from "echarts";
import type {KlineResp,StockItem} from "@display/lib/lake";
import {useValue,useOperation} from "../interaction/model";

const FREQS = ["分时", "日", "周", "月", "年"];
const ADJS = ["不复权", "前复权", "后复权"];
const RANGES: { label: string; days: number | null }[] = [
  { label: "近1月", days: 31 },
  { label: "近3月", days: 93 },
  { label: "近1年", days: 366 },
  { label: "近3年", days: 1096 },
  { label: "全部", days: null },
];
const MAS = [5, 10, 20, 60];
const MA_COLORS = ["#475569", "#0878d1", "#c58b12", "#8154a8"];

function quoteColor(v?: number | null) {
  if (v == null || v === 0) return "kline-flat";
  return v > 0 ? "kline-up" : "kline-down";
}

interface OptionCtx {
  d: KlineResp;
  freq: string;
  maOn: number[];
}

function buildOption({ d, freq, maOn }: OptionCtx) {
  const closes = d.candles.map((c) => c[1]);
  const n = d.dates.length;
  const defaultStart = Math.max(0, n - 120); // 默认窗口：最近 120 根
  return {
    backgroundColor: "transparent",
    animation: false,
    grid: [
      { left: 56, right: 16, top: 16, height: "62%" },
      { left: 56, right: 16, top: "72%", height: "15%" },
    ],
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", lineStyle: { color: "#4a4a5a" } },
      backgroundColor: "#ffffff",
      borderColor: "#d0d5dd",
      textStyle: { color: "#1d2939", fontSize: 14, fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif" },
      formatter: (params: { seriesType?: string; dataIndex: number }[]) => {
        const p = params.find((x) => x.seriesType === "candlestick");
        if (!p) return "";
        const i = p.dataIndex;
        const [o, c, l, h] = d.candles[i] as number[];
        const prev = i > 0 ? (d.candles[i - 1][1] as number) : null;
        const chg = prev ? ((c - prev) / prev) * 100 : null;
        const color = c >= o ? "#d93025" : "#16875d";
        const rows = [
          [d.dates[i], ""],
          ["开", o?.toFixed(2)], ["高", h?.toFixed(2)],
          ["低", l?.toFixed(2)], ["收", c?.toFixed(2)],
          ["涨跌", chg != null ? `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%` : "—"],
          ["成交量", d.volumes[i] != null ? (Number(d.volumes[i]) / 1e4).toFixed(0) + " 万股" : "—"],
        ];
        return `<div style="color:${color};font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:14px">` +
          rows.map(([k, v]) => `<div>${k}&nbsp;&nbsp;${v}</div>`).join("") + "</div>";
      },
    },
    xAxis: [0, 1].map((g) => ({
      type: "category" as const, data: d.dates, gridIndex: g,
      axisLine: { lineStyle: { color: "#d0d5dd" } },
      axisLabel: g === 0
        ? { show: false }
        : { color: "#667085", fontSize: 13, fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif" },
      axisTick: { show: false },
    })),
    yAxis: [
      { scale: true, gridIndex: 0,
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { lineStyle: { color: "#eef0f3" } },
        axisLabel: { color: "#667085", fontSize: 13, fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif" } },
      { scale: true, gridIndex: 1,
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { show: false }, axisLabel: { show: false } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], startValue: defaultStart, endValue: n - 1 },
      { type: "slider", xAxisIndex: [0, 1], bottom: 6, height: 16,
        startValue: defaultStart, endValue: n - 1,
        borderColor: "#d0d5dd", backgroundColor: "#f2f4f7",
        fillerColor: "rgba(8,120,209,0.12)",
        handleStyle: { color: "#0878d1" },
        textStyle: { color: "#667085", fontSize: 12 },
        dataBackground: { lineStyle: { color: "#98a2b3" }, areaStyle: { color: "#e4e7ec" } } },
    ],
    series: [
      { name: "K线", type: "candlestick" as const, xAxisIndex: 0, yAxisIndex: 0,
        data: d.candles,
        itemStyle: {
          color: "#d93025", color0: "#16875d",
          borderColor: "#d93025", borderColor0: "#16875d",
        },
        barMaxWidth: freq === "分时" ? 4 : 14 },
      ...maOn.map((m, i) => ({
        name: `MA${m}`, type: "line" as const, xAxisIndex: 0, yAxisIndex: 0,
        data: maSeries(closes, m), smooth: false, symbol: "none",
        lineStyle: { width: 1, color: MA_COLORS[i % MA_COLORS.length] },
      })),
      { name: "成交量", type: "bar" as const, xAxisIndex: 1, yAxisIndex: 1,
        data: d.candles.map((c, i) => ({
          value: d.volumes[i],
          itemStyle: { color: c[1] != null && c[0] != null && c[1] >= c[0] ? "#d9302570" : "#16875d70" },
        })) },
    ],
  };
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="kline-group">
      <span className="kline-label">{label}</span>
      <div className="kline-segments">{children}</div>
    </div>
  );
}

export default function ChartView() {
  const [kw,setKw]=useValue('chart','keyword','');
  const [stock,setStock]=useValue<StockItem|null>('chart','stock',null);
  const [freq,setFreq]=useValue('chart','freq','日'),[adj,setAdj]=useValue('chart','adj','不复权');
  const [startDate,setStartDate]=useValue('chart','start',''),[endDate,setEndDate]=useValue('chart','end','');
  const [maOn,setMaOn]=useValue<number[]>('chart','ma',[5,20]);
  const search=useOperation<{items:StockItem[]}>('chart:search','lake.query',{q:kw.trim()},!!kw.trim());
  const series=useOperation<KlineResp>('chart:series','lake.kline',{code:stock?.代码??'',freq,adj,...(startDate?{start:startDate}:{}),...(endDate?{end:endDate}:{})},!!stock);
  const hits=search.value?.items??[],data=series.value??null,loading=series.status==='pending',error=search.error||series.error;
  const searchInput=useRef<HTMLInputElement>(null),chartRef=useRef<ReactECharts>(null);
  const load=()=>{if(stock)void series.invoke();};
  const option = useMemo(
    () => (data && data.dates.length ? buildOption({ d: data, freq, maOn }) : null),
    [data, freq, maOn],
  );
  const visibleHits = kw.trim() ? hits : [];

  // 快捷区间：直接驱动 dataZoom 窗口（成熟终端的交互方式）
  const zoomTo = (days: number | null) => {
    const inst: ECharts | undefined = chartRef.current?.getEchartsInstance();
    if (!inst || !data?.dates.length) return;
    const n = data.dates.length;
    if (days == null) {
      inst.dispatchAction({ type: "dataZoom", startValue: 0, endValue: n - 1 });
      return;
    }
    const cutoff = new Date(data.dates[n - 1]);
    cutoff.setDate(cutoff.getDate() - days);
    let i = n - 1;
    while (i > 0 && new Date(data.dates[i]) > cutoff) i--;
    inst.dispatchAction({ type: "dataZoom", startValue: i, endValue: n - 1 });
  };

  return (
    <div className="kline">
      <div className="kline-main">
        <div className="kline-toolbar">
          <div className="kline-search">
            <input
              ref={searchInput}
              value={kw}
              onChange={(e) => setKw(e.target.value)}
              placeholder="搜索股票代码或名称"
              aria-label="搜索股票代码或名称"
              className="kline-input"
            />
            {visibleHits.length > 0 && (
              <div className="kline-hits">
                {visibleHits.map((h) => (
                  <button key={h.代码}
                    onClick={() => { setStock(h); setKw(""); }}
                    className="kline-hit">
                    <span><span className="kline-code">{h.代码}</span> <span>{h.名称}</span></span>
                    <span className={quoteColor(h.涨跌幅)}>
                      {h.最新收盘 != null ? h.最新收盘.toFixed(2) : "—"}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
          {error && <div role="alert" className="kline-message">{error}</div>}

          <Group label="周期">
            {FREQS.map((f) => (
              <button key={f} onClick={() => setFreq(f)} aria-pressed={freq===f}
                className={`kline-choice ${freq===f?'active':''}`}>
                {f}
              </button>
            ))}
          </Group>

          <Group label="复权">
            {ADJS.map((a) => (
              <button key={a} onClick={() => setAdj(a)} aria-pressed={adj===a}
                className={`kline-choice ${adj===a?'active':''}`}>
                {a}
              </button>
            ))}
          </Group>

          <Group label="区间">
            {RANGES.map((r) => (
              <button key={r.label} onClick={() => zoomTo(r.days)}
                className="kline-choice">
                {r.label}
              </button>
            ))}
          </Group>

          <Group label="均线">
            {MAS.map((m) => (
              <button key={m}
                onClick={() => setMaOn((s) => s.includes(m) ? s.filter((x) => x !== m) : [...s, m])}
                aria-pressed={maOn.includes(m)}
                className={`kline-choice ${maOn.includes(m)?'active':''}`}>
                MA{m}
              </button>
            ))}
          </Group>

          <div className="kline-dates">
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)}
              aria-label="开始日期" className="kline-input" />
            <span>—</span>
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)}
              aria-label="结束日期" className="kline-input" />
            {(startDate || endDate) && (
              <button onClick={() => { setStartDate(""); setEndDate(""); }}
                aria-label="清除日期范围" className="kline-choice">清除</button>
            )}
          </div>

          <button onClick={load} className="kline-refresh">
            刷新行情
          </button>
          {loading && <span role="status" className="pill">读取中</span>}
        </div>

        <div className="kline-plot">
          {option ? (
            <ReactECharts ref={chartRef} option={option}
              style={{ height: "100%", width: "100%" }} notMerge />
          ) : (
            <div className="kline-empty">
              {stock ? "暂无行情" : <button onClick={()=>searchInput.current?.focus()}>选择股票</button>}
            </div>
          )}
        </div>
      </div>

      {stock&&<aside className="kline-inspector">
        <h2>{stock.名称}</h2>
        <div className="kline-code">{stock.代码} · {stock.交易所} · {stock.状态}</div>
        {stock.最新收盘!=null&&<>
          <div className={`kline-price ${quoteColor(stock.涨跌幅)}`}>{stock.最新收盘.toFixed(2)}</div>
          <div className={`kline-change ${quoteColor(stock.涨跌幅)}`}>{stock.涨跌幅!=null&&stock.涨跌幅>0?'+':''}{stock.涨跌幅?.toFixed(2)}%</div>
        </>}
        <div className="kline-facts">
          <div><span>行情日期</span><span>{stock.行情日期??'—'}</span></div>
          <div><span>成交额</span><span>{stock.成交额亿!=null?`${stock.成交额亿.toFixed(2)} 亿`:'—'}</span></div>
          <div><span>首日</span><span>{stock.首日??'—'}</span></div>
          <div><span>末日</span><span>{stock.末日??'—'}</span></div>
          {data?.区间&&<div><span>图上区间</span><span>{data.区间}</span></div>}
          <div><span>周期 · 复权</span><span>{freq} · {adj}</span></div>
          <div><span>K 线根数</span><span>{data?.dates.length??0}</span></div>
        </div>
      </aside>}
    </div>
  );
}
