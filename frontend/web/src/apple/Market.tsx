import {useMemo, useState} from 'react';
import ReactECharts from '../shared/chart';
import {maSeries} from '@display/lib/ma-series';
import type {KlineResp, StockItem} from '@display/lib/market';
import type {BenchmarkSpec, BenchmarkSeries} from '@display/research/port';
import {useValue, useOperation} from '../interaction/model';
import {amount, day, errorMessage, number, percent, security, text} from '../shared/format';
import {Empty, KV, Notice, PageHead, Segmented, Sparkline} from './ui';
import {firstError} from './summary';

const FREQS = ['日', '周', '月', '年'] as const;
const ADJS = ['不复权', '前复权'] as const;
const MAS = [5, 10, 20, 60];
const MA_COLORS = ['#6e6e73', '#0071e3', '#c58b12', '#8154a8'];
const dateBack = (days: number) => new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
// Presentation order for the index picker: broad benchmarks first, then style
// indices, then the total-return mirror. Anything unlisted follows in registry
// order, so a newly registered index still appears.
const INDEX_ORDER = ['hs300', 'sse_composite', 'szse_component', 'chinext',
  'csi500', 'csi1000', 'sse50', 'csi_dividend', 'csi_all_share_total'];
// Three fixed slots, Apple Stocks style: a preset that covers the common read,
// overridable per slot. Hick's law — 14 selectable cards on the first screen
// made every choice slower and pushed the chart off the fold.
const SLOT_COUNT = 3;
const DEFAULT_SLOTS = ['nasdaq_composite', 'hs300', 'chinext'];
const DEFAULT_INDEX = 'hs300';

function klineOption(data: KlineResp, freq: string, maOn: number[]) {
  const closes = data.candles.map(candle => candle[1] as number);
  const n = data.dates.length;
  const from = Math.max(0, n - 120);
  return {
    backgroundColor: 'transparent', animation: false,
    grid: [{left: 58, right: 14, top: 12, height: '62%'}, {left: 58, right: 14, top: '75%', height: '14%'}],
    axisPointer: {link: [{xAxisIndex: 'all'}]},
    tooltip: {trigger: 'axis', backgroundColor: '#fff', borderColor: '#e8e8ed', textStyle: {color: '#1d1d1f', fontSize: 12}},
    xAxis: [0, 1].map(g => ({type: 'category' as const, data: data.dates.map(day), gridIndex: g,
      axisLine: {lineStyle: {color: '#e8e8ed'}}, axisTick: {show: false},
      axisLabel: g === 0 ? {show: false} : {color: '#a1a1a6', fontSize: 10}})),
    yAxis: [
      {scale: true, gridIndex: 0, axisLine: {show: false}, axisTick: {show: false},
        splitLine: {lineStyle: {color: '#f2f2f5'}}, axisLabel: {color: '#a1a1a6', fontSize: 10}},
      {scale: true, gridIndex: 1, axisLine: {show: false}, axisTick: {show: false},
        splitLine: {show: false}, axisLabel: {show: false}},
    ],
    dataZoom: [{type: 'inside' as const, xAxisIndex: [0, 1], startValue: from, endValue: n - 1},
      {type: 'slider' as const, xAxisIndex: [0, 1], bottom: 2, height: 12, startValue: from, endValue: n - 1,
        borderColor: 'transparent', backgroundColor: '#f5f5f7', fillerColor: 'rgba(0,113,227,.12)',
        handleStyle: {color: '#0071e3'}, textStyle: {color: '#a1a1a6', fontSize: 10}}],
    series: [
      {name: 'K 线', type: 'candlestick' as const, xAxisIndex: 0, yAxisIndex: 0, data: data.candles,
        itemStyle: {color: '#d93025', color0: '#16875d', borderColor: '#d93025', borderColor0: '#16875d'},
        barMaxWidth: freq === '分时' ? 4 : 12},
      ...maOn.map((period, index) => ({name: `MA${period}`, type: 'line' as const, xAxisIndex: 0, yAxisIndex: 0,
        data: maSeries(closes, period), smooth: false, symbol: 'none',
        lineStyle: {width: 1, color: MA_COLORS[index % MA_COLORS.length]}})),
      {name: '成交量', type: 'bar' as const, xAxisIndex: 1, yAxisIndex: 1,
        data: data.candles.map((candle, index) => ({value: data.volumes[index],
          itemStyle: {color: (candle[1] ?? 0) >= (candle[0] ?? 0) ? '#d9302566' : '#16875d66'}}))},
    ],
  };
}

function levelOption(series: BenchmarkSeries) {
  return {
    animation: false, backgroundColor: 'transparent',
    grid: {left: 58, right: 16, top: 16, bottom: 30},
    tooltip: {trigger: 'axis', textStyle: {fontSize: 12}},
    xAxis: {type: 'category' as const, data: series.points.map(point => day(point.at)),
      axisLine: {lineStyle: {color: '#e8e8ed'}}, axisTick: {show: false}, axisLabel: {color: '#a1a1a6', fontSize: 10}},
    yAxis: {type: 'value' as const, scale: true, axisLine: {show: false}, axisTick: {show: false},
      splitLine: {lineStyle: {color: '#f2f2f5'}}, axisLabel: {color: '#a1a1a6', fontSize: 10}},
    series: [{type: 'line' as const, showSymbol: false, data: series.points.map(point => point.close),
      lineStyle: {color: '#0071e3', width: 1.8}, areaStyle: {color: 'rgba(0,113,227,.08)'}}],
  };
}

function SlotCard({id, spec, series, pending, failed, active, options, onSelect, onOpen}: {
  id: string;
  spec?: BenchmarkSpec;
  series?: BenchmarkSeries;
  pending: boolean;
  failed: boolean;
  active: boolean;
  options: BenchmarkSpec[];
  onSelect: (id: string) => void;
  onOpen: () => void;
}) {
  const points = (series?.points ?? []).map(point => point.close).filter(value => Number.isFinite(value));
  const last = points.length ? points[points.length - 1] : null;
  const first = points.length ? points[0] : null;
  const change = first && last != null && first !== 0 ? (last - first) / first : null;
  const tone = change == null || change === 0 ? '' : change > 0 ? ' up' : ' down';
  const stroke = tone === ' up' ? '#d93025' : tone === ' down' ? '#16875d' : '#0071e3';
  return <div className="index-card" data-active={active ? 'true' : undefined}>
    <div className="index-head">
      <select className="index-select" aria-label="选择指数" value={id}
        onChange={event => onSelect(event.target.value)}>
        {options.length
          ? options.map(item => <option key={item.id} value={item.id}>{text(item.name) ?? item.id}</option>)
          : <option value={id}>{id}</option>}
      </select>
      <span className="index-tag">{spec?.return_type === 'total' ? '全收益' : text(spec?.currency) ?? ''}</span>
    </div>
    <button type="button" className="index-body" aria-pressed={active}
      title="查看该指数详情" onClick={onOpen}>
      <span className="index-code">{security(spec?.code) ?? id} · {text(spec?.provider) ?? '来源未提供'}</span>
      {pending && last == null
        ? <span className="index-skel" aria-hidden="true" />
        : <span className="index-val num">{last != null ? amount(last) : '—'}</span>}
      <span className={`index-delta num${tone}`}>
        {change != null ? `${change > 0 ? '+' : ''}${percent(change)}`
          : failed ? '读取失败' : pending ? '读取中' : '区间无数据'}
      </span>
      {points.length > 1 && <Sparkline points={points.slice(-40)} color={stroke} className="index-spark" />}
    </button>
  </div>;
}

export default function Market() {
  const [kw, setKw] = useValue('apple:market', 'keyword', '');
  const [typing, setTyping] = useState(false);
  const [picked, setPicked] = useValue<StockItem | null>('apple:market', 'stock', null);
  const [watch, setWatch] = useValue<StockItem[]>('apple:market', 'watch', []);
  const [freq, setFreq] = useValue<string>('apple:market', 'freq', '日');
  const [adj, setAdj] = useValue<string>('apple:market', 'adj', '不复权');
  const [maOn, setMaOn] = useValue<number[]>('apple:market', 'ma', [5, 20]);
  const [slots, setSlots] = useValue<string[]>('apple:market', 'slots', DEFAULT_SLOTS);
  // The detail pane is a single subject at a time: an index, or a security.
  // Picking a security clears the index and vice versa, so the pane never has
  // two candidates competing for the same heading.
  const [indexId, setIndexId] = useValue('apple:market', 'index', DEFAULT_INDEX);
  const [start, setStart] = useValue('apple:market', 'benchStart', dateBack(365));
  const [end, setEnd] = useValue('apple:market', 'benchEnd', dateBack(0));

  const slotIds = Array.from({length: SLOT_COUNT}, (_unused, index) =>
    (Array.isArray(slots) && typeof slots[index] === 'string' && slots[index]) || DEFAULT_SLOTS[index]);
  const rangeReady = !!start && !!end && start <= end;

  const catalog = useOperation<BenchmarkSpec[]>('apple:benchmarks', 'research.benchmarks', {}, true, true, {fresh: 60_000});
  // Every visible slot fetches its own series, so no card can sit on an empty
  // dash while its neighbours are populated.
  const slotA = useOperation<BenchmarkSeries>('apple:market:slotA', 'research.benchmark',
    {id: slotIds[0], start, end}, rangeReady);
  const slotB = useOperation<BenchmarkSeries>('apple:market:slotB', 'research.benchmark',
    {id: slotIds[1], start, end}, rangeReady);
  const slotC = useOperation<BenchmarkSeries>('apple:market:slotC', 'research.benchmark',
    {id: slotIds[2], start, end}, rangeReady);
  const slotQueries = [slotA, slotB, slotC];

  const search = useOperation<{items: StockItem[]}>('apple:market:search', 'data.search', {q: kw.trim()}, !!kw.trim());
  const benchmarks = catalog.value ?? [];
  const ordered = [
    ...INDEX_ORDER.map(id => benchmarks.find(item => item.id === id)).filter((item): item is BenchmarkSpec => !!item),
    ...benchmarks.filter(item => !INDEX_ORDER.includes(item.id)),
  ];

  const subjectIndex = picked ? null : (benchmarks.find(item => item.id === indexId) ?? null);
  // An index level is a point series: no adjustment, and its turnover is the
  // aggregate of the constituents, so the K-line stays on raw.
  const isLevel = !!subjectIndex && /^(sh|sz)\./.test(subjectIndex.code ?? '');
  const activeCode = picked?.代码 ?? (isLevel ? subjectIndex!.code : '');
  const series = useOperation<KlineResp>('apple:market:bars', 'data.bars',
    {code: activeCode, freq, adj: isLevel ? '不复权' : adj}, !!activeCode);
  // A benchmark outside the A-share dataset (Nasdaq, FX) has no bars; its
  // detail view is the close series it does have, not an error panel.
  const slotForIndex = slotIds.includes(indexId) ? slotQueries[slotIds.indexOf(indexId)] : null;
  const detailLevel = useOperation<BenchmarkSeries>('apple:market:level', 'research.benchmark',
    {id: indexId, start, end}, !!subjectIndex && !isLevel && !slotForIndex && rangeReady);
  const levelSeries = slotForIndex?.value ?? detailLevel.value;

  const kline = series.value;
  const option = useMemo(() => kline?.dates.length ? klineOption(kline, freq, maOn) : null, [kline, freq, maOn]);
  const levelOpt = useMemo(() => {
    const points = levelSeries?.points ?? [];
    return points.length ? levelOption({...levelSeries!, points}) : null;
  }, [levelSeries]);

  const hits = kw.trim() ? (search.value?.items ?? []) : [];
  const closes = (kline?.candles ?? []).map(candle => candle[1] as number).filter(value => Number.isFinite(value));
  const lastClose = closes.length ? closes[closes.length - 1] : null;
  const firstClose = closes.length ? closes[0] : null;
  const spanChange = firstClose && lastClose != null && firstClose !== 0 ? (lastClose - firstClose) / firstClose : null;
  const levelPoints = (levelSeries?.points ?? []).map(point => point.close).filter(value => Number.isFinite(value));
  const levelLast = levelPoints.length ? levelPoints[levelPoints.length - 1] : null;
  const levelChange = levelPoints.length > 1 && levelPoints[0] !== 0
    ? (levelPoints[levelPoints.length - 1] - levelPoints[0]) / levelPoints[0] : null;
  const tone = (value: number | null) => value == null || value === 0 ? '' : value > 0 ? ' up' : ' down';

  const errors = firstError(catalog.error, search.error, series.error, detailLevel.error);
  const hasSubject = !!picked || !!subjectIndex;
  const chartError = firstError(search.error, series.error, detailLevel.error);
  // The window actually rendered, stated next to the change it produced.
  const windowText = isLevel
    ? (kline?.dates.length ? `${day(kline.dates[0])} – ${day(kline.dates[kline.dates.length - 1])}` : '未提供')
    : (levelSeries?.points?.length
      ? `${day(levelSeries.points[0].at)} – ${day(levelSeries.points[levelSeries.points.length - 1].at)}`
      : '未提供');

  function setSlot(index: number, id: string) {
    setSlots(() => {
      const next = [...slotIds];
      next[index] = id;
      return next;
    });
  }

  function openIndex(id: string) {
    setPicked(null);
    setIndexId(id);
  }

  function pick(item: StockItem) {
    setPicked(item);
    setKw('');
    const others = watch.filter(entry => entry.代码 !== item.代码);
    setWatch([item, ...others].slice(0, 8));
  }

  const title = subjectIndex
    ? text(subjectIndex.name) ?? '指数'
    : text(picked?.名称) ?? '证券';
  const subtitle = subjectIndex
    ? [security(subjectIndex.code), text(subjectIndex.provider),
        subjectIndex.return_type === 'total' ? '全收益指数' : '价格指数'].filter(Boolean).join(' · ')
    : [security(picked?.代码), text(picked?.交易所)].filter(Boolean).join(' · ');

  return <>
    <PageHead eyebrow="Market" title="行情"
      subtitle="指数、自选与 K 线 — 涨红跌绿，点位与成交口径来自交互核心" />

    <div className="strip-head">
      <span className="strip-title">指数</span>
      <div className="strip-range">
        <span>涨跌区间</span>
        <input type="date" aria-label="指数区间起始" value={start} onChange={event => setStart(event.target.value)} />
        <span>—</span>
        <input type="date" aria-label="指数区间截止" value={end} onChange={event => setEnd(event.target.value)} />
      </div>
    </div>
    <div className="index-strip">
      {slotIds.map((id, index) => {
        const spec = benchmarks.find(item => item.id === id);
        return <SlotCard key={`${index}-${id}`} id={id} spec={spec}
          series={slotQueries[index].value}
          pending={slotQueries[index].status === 'pending'}
          failed={!!slotQueries[index].error}
          active={!picked && indexId === id}
          options={ordered}
          onSelect={next => setSlot(index, next)}
          onOpen={() => openIndex(id)} />;
      })}
      {!benchmarks.length && <div className="card muted" style={{gridColumn: '1 / -1'}}>
        {catalog.status === 'pending' ? '读取基准指数目录…' : '暂无已登记的基准指数'}
      </div>}
    </div>

    <div className="market-layout">
      <aside className="card rail">
        <div className="search-wrap">
          <input className="input" aria-label="搜索股票代码或名称" placeholder="搜索代码或名称"
            value={kw} onChange={event => setKw(event.target.value)}
            onFocus={() => setTyping(true)} onBlur={() => setTyping(false)} />
          {/* Only offer results while the field is in use: a default keyword
              must not open a popover that covers the list below. */}
          {typing && !!hits.length && <div className="hits" onMouseDown={event => event.preventDefault()}>
            {hits.map(item => <button key={item.代码} type="button" onClick={() => pick(item)}>
              <span>{text(item.名称) ?? '证券名称未提供'} <span className="faint">{security(item.代码)}</span></span>
              <span className="num">{item.最新收盘 != null ? amount(item.最新收盘) : '—'}</span>
            </button>)}</div>}
        </div>

        <div className="rail-group"><span>自选</span><span className="faint">{watch.length}</span></div>
        {!watch.length
          ? <div className="rail-empty">搜索并选中一只证券，它会留在这里。</div>
          : watch.map(item => <button key={item.代码} type="button" className="rail-item"
              data-active={picked?.代码 === item.代码 ? 'true' : undefined}
              onClick={() => pick(item)}>
              <span><span className="nm">{text(item.名称) ?? '证券名称未提供'}</span>
                <div className="cd">{security(item.代码)}</div></span>
              <span className="px num">{item.最新收盘 != null ? amount(item.最新收盘) : '—'}</span>
            </button>)}

        <div className="rail-group"><span>指数</span><span className="faint">{ordered.length}</span></div>
        {!ordered.length && <div className="rail-empty">读取指数目录…</div>}
        {ordered.map(spec => <button key={spec.id} type="button" className="rail-item"
          data-active={!picked && indexId === spec.id ? 'true' : undefined}
          onClick={() => openIndex(spec.id)}>
          <span><span className="nm">{text(spec.name) ?? spec.id}</span>
            <div className="cd">{security(spec.code) ?? ''}{spec.return_type === 'total' ? ' · 全收益' : ''}</div></span>
        </button>)}
      </aside>

      <section className="card detail" data-od-id="market-detail">
        {!hasSubject
          ? <Empty>从左侧选择一个指数，或搜索一只证券以查看走势</Empty>
          : <>
            <div className="detail-head">
              <div>
                <div className="detail-name">{title}</div>
                <div className="detail-meta">{subtitle || '标的信息未提供'}</div>
              </div>
              <div className="detail-quote">
                <div className="detail-price num">
                  {isLevel || picked
                    ? (lastClose != null ? amount(lastClose) : '—')
                    : (levelLast != null ? amount(levelLast) : '—')}
                </div>
                {(() => {
                  const change = isLevel || picked ? spanChange : levelChange;
                  if (change != null) return <div className={`detail-delta num${tone(change)}`}>
                    {`${change > 0 ? '+' : ''}${percent(change)} 区间`}</div>;
                  const busy = series.status === 'pending' || detailLevel.status === 'pending';
                  return <div className="detail-delta faint">{busy ? '读取中…' : '区间无数据'}</div>;
                })()}
              </div>
            </div>

            {(isLevel || picked) && <div className="toolbar" style={{marginTop: 16}}>
              <Segmented<string> options={FREQS.map(item => ({value: item, label: item}))} value={freq} onChange={setFreq} label="周期" />
              <Segmented<string> options={ADJS.map(item => ({value: item, label: item}))} value={isLevel ? '不复权' : adj}
                onChange={setAdj} label="复权" disabled={!!isLevel} />
              <div className="chip-list">
                {MAS.map(period => <button key={period} type="button" className="chip"
                  aria-pressed={maOn.includes(period)}
                  onClick={() => setMaOn(list => list.includes(period) ? list.filter(item => item !== period) : [...list, period])}>
                  MA{period}
                </button>)}
              </div>
              <span className="grow" />
              <button className="btn sm" type="button" disabled={!activeCode || series.status === 'pending'}
                onClick={() => void series.invoke()}>刷新</button>
            </div>}

            {chartError && <Notice tone="error">{errorMessage(chartError)}</Notice>}

            {(isLevel || picked)
              ? (option
                ? <ReactECharts option={option} className="chart tall" style={{height: 360, width: '100%'}} notMerge />
                : <Empty busy={!!activeCode && series.status === 'pending'}>正在读取 K 线</Empty>)
              : (levelOpt
                ? <ReactECharts option={levelOpt} className="chart" style={{height: 300, width: '100%'}} notMerge />
                : <Empty busy={detailLevel.status === 'pending' || !!slotForIndex && slotForIndex.status === 'pending'}>
                    该指数不在 A 股数据集内，仅提供收盘序列</Empty>)}

            <div className="divider" />
            {subjectIndex ? <>
              <div className="grid g2">
                <KV items={[
                  {name: '代码', value: security(subjectIndex.code) ?? '未提供'},
                  {name: '类型', value: subjectIndex.return_type === 'total' ? '全收益（含股息再投资）' : '价格（不含股息）'},
                  {name: '币种', value: text(subjectIndex.currency) ?? '未提供'},
                ]} />
                <KV items={[
                  {name: '数据源', value: text(subjectIndex.provider) ?? '未提供'},
                  {name: '起止', value: windowText},
                  {name: '观测数', value: isLevel
                    ? (kline?.dates.length ? `${kline.dates.length} 根 K 线` : '未提供')
                    : (levelPoints.length ? `${levelPoints.length} 个收盘点` : '未提供')},
                ]} />
              </div>
              <div className="faint" style={{fontSize: 11, lineHeight: 1.7, marginTop: 4}}>
                指数为点位序列；成交量与成交额汇总其覆盖市场，不构成该标的自身的换手。
              </div>
            </> : picked && <>
              <div className="grid g2">
                <KV items={[
                  {name: '状态', value: text(picked.状态) ?? '未提供'},
                  {name: '行情日期', value: day(picked.行情日期)},
                  {name: '成交额', value: picked.成交额亿 != null ? `${number(picked.成交额亿)} 亿` : '未提供'},
                ]} />
                <KV items={[
                  {name: '首日', value: day(picked.首日)},
                  {name: '末日', value: day(picked.末日)},
                  {name: '周期 · 复权', value: `${freq} · ${adj}`},
                ]} />
              </div>
              {kline?.区间 && <div className="faint" style={{fontSize: 11, marginTop: 4}}>
                图上区间：{text(kline.区间) ?? '未提供'} · {kline.dates.length} 根
              </div>}
            </>}
          </>}
      </section>
    </div>
    {errors && <Notice tone="error">{errorMessage(errors)}</Notice>}
  </>;
}
