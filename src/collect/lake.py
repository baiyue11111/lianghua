#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""lake.py —— 统一 data 根目录内的数据湖管理工具

子命令：
  status   数据体检报告：覆盖年份 / 股票数 / 字段覆盖率 / 新鲜度 / 异常检查
  fetch    数据抓取：自动增量、指定区间修复、扩展字段、因子补抓；断点续传
  merge    把抓取缓存合并进数据湖（行情 upsert + 扩展字段回填），校验后原子替换
  query    按代码或名称查股票：主档信息 + 最新行情
  chart    K 线可视化：分时 / 日 / 周 / 月 / 年，不复权 / 前复权 / 后复权，输出 HTML

设计原则：
  确定性 —— 任务清单由数据湖现状唯一推导并缓存，重复运行结果一致
  可续传 —— 每个任务一个缓存文件，写完才算完成；中断后重跑自动跳过
  可视化 —— 终端下 rich 实时进度面板；非终端退化为定期状态行
  安全   —— merge 先写临时文件再原子替换，替换前自动备份，校验通过才收尾

用法示例：
  .venv/bin/python tools/lake.py status
  .venv/bin/python tools/lake.py fetch                          # 自动增量更新
  .venv/bin/python tools/lake.py fetch --start 2014-01-01 --end 2025-12-31 --fields st
  .venv/bin/python tools/lake.py fetch --codes 600036,000001 --start 2026-01-01
  .venv/bin/python tools/lake.py fetch --factors                # 补抓估值因子
  .venv/bin/python tools/lake.py merge --dry-run
  .venv/bin/python tools/lake.py query 平安
  .venv/bin/python tools/lake.py chart 600036 --freq 日 --adj 前复权
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from pathlib import Path
import pandas as pd
from collect.data import saved, encoded
from collect.quotes import daily_frame, factor_frame
from collect.securities import listed_names

# ================================================================ 路径与常量

DATA = None
LAKE = CACHE = CHARTS = DAILY = DAILY_GLOB = None
STOCK_INFO = VALUATION = FUNDAMENTALS = LATEST = None
README = PROFILE_JSON = TASKS_JSON = NEW_STOCKS_JSON = ADJ_CACHE = PROBED_JSON = None


def bind(data):
    global DATA, LAKE, CACHE, CHARTS, DAILY, DAILY_GLOB, STOCK_INFO, VALUATION
    global FUNDAMENTALS, LATEST, README, PROFILE_JSON, TASKS_JSON, NEW_STOCKS_JSON, ADJ_CACHE, PROBED_JSON
    DATA = data
    LAKE = str(data.path('sources', 'lake', 'stock_info.parquet').parent)
    CACHE = str(data.path('sources', 'lake', '_incr_cache/tasks_v2.json').parent)
    CHARTS = str(data.path('runs', 'charts', 'index.html').parent)
    DAILY = str(data.path('sources', 'lake', 'daily/_.parquet').parent)
    DAILY_GLOB = str(data.path('sources', 'lake', 'daily/*.parquet'))
    STOCK_INFO = str(data.path('sources', 'lake', 'stock_info.parquet'))
    VALUATION = str(data.path('sources', 'lake', 'valuation_factors.parquet'))
    FUNDAMENTALS = str(data.path('sources', 'lake', 'fundamentals.parquet'))
    LATEST = str(data.path('sources', 'lake', 'latest_snapshot.parquet'))
    README = str(data.path('sources', 'lake', 'README.md'))
    PROFILE_JSON = str(data.path('state', 'lake', 'profile.json'))
    TASKS_JSON = str(data.path('state', 'lake', 'tasks.json'))
    NEW_STOCKS_JSON = str(data.path('state', 'lake', 'new_stocks.json'))
    ADJ_CACHE = str(data.path('sources', 'lake', '_adj_cache/_.parquet').parent)
    PROBED_JSON = str(data.path('state', 'lake', 'probed_empty.json'))
    _LATEST_COVERAGE_CACHE.update(mtime=None, profile=None)

ADJ_TTL = 7 * 86400                            # 因子缓存最长寿命（秒）

ROW_GROUP_SIZE = 250_000       # 分区文件行组大小（排序+zone map 谓词剪枝的粒度）
ZSTD_LEVEL = 3

PROBED_RETRY_DAYS = 30                       # 确认无数据后间隔多少天复探

_LATEST_COVERAGE_CACHE = {"mtime": None, "profile": None}


def _load_probed() -> dict:
    """已探测确认无数据股票：{code: 探测日期}。"""
    raw = saved(DATA, 'state', 'lake', 'probed_empty.json')
    return {} if raw is None else json.loads(raw)


def _fresh_probed() -> set[str]:
    """最近 30 天内探测过且为空的股票（短期内不重复探测）。"""
    cutoff = (pd.Timestamp(today()) - pd.Timedelta(days=PROBED_RETRY_DAYS)
              ).strftime("%Y-%m-%d")
    return {c for c, d in _load_probed().items() if d >= cutoff}

def today() -> str:
    """Return the local date at call time for long-lived service imports."""
    return time.strftime("%Y-%m-%d")


def _today() -> str:
    return today()


# Legacy public alias; internal code must call today() instead.
TODAY = today()

# baostock 字段预设：preset -> (k-data 字段串, 缓存文件后缀)
FIELD_PRESETS = {
    "basic": ("date,open,high,low,close,volume,amount,turn,pctChg,tradestatus,isST", ""),
    "st": ("date,close,turn,pctChg,tradestatus,isST", "_st"),
    "valuation": ("date,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST", "_val"),
}
BASIC_NUMERIC = FIELD_PRESETS["basic"][0].split(",")[1:]





# ================================================================ 数据湖读取

def daily_mtime() -> float:
    """分区目录的最新文件 mtime：缓存失效与因子重抓的唯一依据。"""
    mt = 0.0
    for fn in os.listdir(DAILY):
        if fn.endswith(".parquet"):
            mt = max(mt, os.path.getmtime(os.path.join(DAILY, fn)))
    return mt


def lake_mtime() -> float:
    """全湖 mtime（体检快照失效依据）。"""
    mt = daily_mtime()
    for f in (STOCK_INFO, VALUATION, FUNDAMENTALS, LATEST):
        if os.path.exists(f):
            mt = max(mt, os.path.getmtime(f))
    return mt


def latest_data_coverage() -> dict:
    """Report both the newest bar and how many symbols actually reach it.

    ``median_stock_date`` is the lower median of each symbol's last bar: at
    least half the tracked symbols have bars on or after that date. The cache
    is invalidated by the same daily-partition mtime used by the API.
    """
    mt = daily_mtime()
    if _LATEST_COVERAGE_CACHE["mtime"] == mt:
        return dict(_LATEST_COVERAGE_CACHE["profile"])
    files = [fn for fn in os.listdir(DAILY) if fn.endswith(".parquet")]
    if not files:
        profile = {"latest_data_date": None, "latest_data_stock_count": 0,
                   "tracked_stock_count": 0, "median_stock_date": None}
    else:
        import duckdb
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT code, max(date) AS last_date FROM read_parquet(?) GROUP BY code",
                [DAILY_GLOB],
            ).fetchall()
        finally:
            con.close()
        dates = sorted(str(last)[:10] for _, last in rows)
        latest = dates[-1] if dates else None
        profile = {"latest_data_date": latest,
                   "latest_data_stock_count": dates.count(latest) if latest else 0,
                   "tracked_stock_count": len(dates),
                   "median_stock_date": dates[(len(dates) - 1) // 2] if dates else None}
    _LATEST_COVERAGE_CACHE.update(mtime=mt, profile=profile)
    return dict(profile)


def read_daily(columns=None, filter=None) -> pd.DataFrame:
    """读分区数据集。filter 为 pyarrow compute 表达式（如 ds.field("code")=="600036"），
    带过滤时靠 排序+zone map 剪枝，单股读取约 10~100ms；不带过滤为全表扫描。"""
    import pyarrow.dataset as ds
    dset = ds.dataset(DAILY, format="parquet")
    return dset.to_table(columns=columns, filter=filter).to_pandas()


def read_daily_one(code: str, columns=None) -> pd.DataFrame:
    """A：谓词下推单股读取。"""
    import pyarrow.dataset as ds
    return read_daily(columns=columns, filter=(ds.field("code") == code))


def read_latest() -> pd.DataFrame:
    """C：物化最新快照（merge/迁移时重建）。缺失时现算并落盘。"""
    if not os.path.exists(LATEST):
        rebuild_latest()
    return pd.read_parquet(LATEST)


def rebuild_latest() -> pd.DataFrame:
    """用 duckdb 一趟聚合重建全市场最新快照（约 0.1s）。"""
    import duckdb
    df = duckdb.sql(f"""
        SELECT code, max(date) AS date,
               arg_max(close, date) AS close,
               arg_max(pctChg, date) AS pctChg,
               arg_max(amount, date) AS amount
        FROM '{DAILY_GLOB}' GROUP BY code""").df()
    DATA.write('sources', 'lake', 'latest_snapshot.parquet',
               df.to_parquet(index=False, compression='zstd', compression_level=ZSTD_LEVEL))
    return df


def read_stock_info() -> pd.DataFrame:
    return pd.read_parquet(STOCK_INFO)


def resolve_code(keyword: str) -> list[pd.Series]:
    """按代码或名称模糊匹配主档，返回命中行列表。"""
    si = read_stock_info()
    if keyword.isdigit() and len(keyword) == 6:
        hit = si[si["code"] == keyword]
    else:
        hit = si[si["name"].str.contains(keyword, na=False)]
    return [row for _, row in hit.iterrows()]


# ================================================================ 抓取任务

@dataclass(frozen=True)
class Task:
    """一只股票的一个待抓区间。tag 是缺口语义标签，preset 决定字段集与缓存文件名。"""
    code: str
    tag: str                 # auto: 自动增量 | fix: 指定区间修复 | factors: 因子补抓
    start: str
    end: str
    preset: str = "basic"

    @property
    def cache_file(self) -> str:
        suffix = FIELD_PRESETS[self.preset][1]
        return str(DATA.path('sources', 'lake', f'_incr_cache/{self.code}_{self.tag}_{self.start}_{self.end}{suffix}.parquet'))


def _target_quarter() -> str:
    """上一自然季度（季报应已披露完毕）。"""
    y, m = time.localtime().tm_year, time.localtime().tm_mon
    tq = (m - 1) // 3
    if tq == 0:
        y, tq = y - 1, 4
    return f"{y}Q{tq}"


def _quarters_between(last_quarter: str, target_quarter: str) -> list[str]:
    """('2025Q4', '2026Q2') -> ['2026Q1', '2026Q2']"""
    def parse(q):
        return int(q[:4]), int(q[-1])
    y, q = parse(last_quarter)
    ty, tq = parse(target_quarter)
    out = []
    while (y, q) < (ty, tq):
        q += 1
        if q == 5:
            y, q = y + 1, 1
        out.append(f"{y}Q{q}")
    return out


def build_auto_tasks() -> list[Task]:
    """自动增量：由数据湖现状推导缺口（上市前回补 + 最新增量 + 新股探测）。"""
    import duckdb
    si = read_stock_info()
    valid = duckdb.sql(f"""
        SELECT code, min(date) AS first, max(date) AS last
        FROM '{DAILY_GLOB}'
        WHERE pool = 'current' AND close IS NOT NULL GROUP BY code
        """).df().set_index("code")
    known_years = set(duckdb.sql(
        f"SELECT DISTINCT substr(date, 1, 4) FROM '{DAILY_GLOB}'").df().iloc[:, 0])

    tasks: list[Task] = []
    probed = _fresh_probed()
    for code in si.loc[si["status"] == "listed", "code"]:
        if code not in valid.index:                       # 无行情股：全量探测
            if code in probed:                            # 近期已确认无数据，跳过
                continue
            tasks.append(Task(code, "auto", "2013-01-01", today()))
            continue
        first, last = valid.loc[code, "first"], valid.loc[code, "last"]
        list_year = first[:4]
        if list_year > min(known_years):                  # 上市早于数据起点 → 回补
            tasks.append(Task(code, "auto", f"{int(list_year)-1}-01-01",
                              f"{list_year}-12-31"))
        if last < today():                                # 尾部增量
            tasks.append(Task(code, "auto", last, today()))

    # 主档外新上市股
    new_names: dict[str, str] = {}
    names = listed_names()
    known = set(si["code"])
    for _, row in names.iterrows():
        c = str(row["code"])
        if c not in known and c[0] in "603":
            new_names[c] = str(row["name"])
            tasks.append(Task(c, "auto", "2013-01-01", today()))
    if new_names:
        DATA.write('state', 'lake', 'new_stocks.json', encoded(new_names))
    return tasks


def build_factor_tasks() -> list[Task]:
    """估值因子补抓：valuation_factors 缺失季度（PE/PB 来自 k 线，ROE 来自业绩快报）。"""
    v = pd.read_parquet(VALUATION, columns=["code", "quarter"])
    last_q = v["quarter"].max()
    missing = _quarters_between(last_q, _target_quarter())
    if not missing:
        return []
    start = f"{missing[0][:4]}-01-01"
    si = read_stock_info()
    return [Task(c, "factors", start, today(), "valuation")
            for c in si.loc[si["status"] == "listed", "code"]]


def _st_blind_codes(start: str, end: str) -> list[str]:
    """st 盲区股票清单：区间内 turn 整段为空的现存股（逐股判缺，已补过的不重抓）。"""
    import duckdb
    have = set(duckdb.sql(f"""
        SELECT DISTINCT code FROM '{DAILY_GLOB}'
        WHERE date >= '{start}' AND date <= '{end}' AND turn IS NOT NULL
        """).df()["code"])
    si = read_stock_info()
    return [c for c in si.loc[si["status"] == "listed", "code"] if c not in have]


def build_tasks(args) -> list[Task]:
    """根据命令行参数确定任务清单（手动参数优先，否则自动增量）。"""
    if args.factors:
        return build_factor_tasks()
    if args.start or args.codes:
        si = read_stock_info()
        codes = args.codes.split(",") if args.codes else \
            list(si.loc[si["status"] == "listed", "code"])
        if args.fields == "st" and not args.codes:
            codes = _st_blind_codes(args.start or "2014-01-01",
                                    args.end or "2025-12-31")
        return [Task(c.strip(), "fix", args.start or "2013-01-01",
                     args.end or today(), args.fields) for c in codes]
    # 每次按当前数据缺口和日期规划；快照仅供审计，不作为下一次输入。
    tasks = build_auto_tasks()
    DATA.write('state', 'lake', 'tasks.json', encoded([t.__dict__ for t in tasks]))
    return tasks


def pending_tasks(tasks: list[Task], force: bool = False) -> list[Task]:
    if force:
        return tasks
    return [t for t in sorted(tasks, key=lambda t: (t.code, t.tag))
            if not os.path.exists(t.cache_file)]


# ================================================================ baostock 查询

def bs_query(code: str, task: Task) -> pd.DataFrame:
    fields = FIELD_PRESETS[task.preset][0]
    return daily_frame(code, fields, task.start, task.end)


def save_cache(task: Task, df: pd.DataFrame) -> int:
    """写缓存（空结果也写，作为"已确认无数据"的断点标记）。返回行数。"""
    if len(df):
        for c in df.columns:
            if c != "date":
                df[c] = pd.to_numeric(df[c], errors="coerce")
    DATA.write('sources', 'lake', '_incr_cache/' + Path(task.cache_file).name,
               df.to_parquet(index=False))
    return len(df)


def _adjust_cache_is_fresh(fp: str) -> bool:
    """Factor cache is valid only for today's query endpoint and current lake."""
    mt = os.path.getmtime(fp)
    cache_day = time.strftime("%Y-%m-%d", time.localtime(mt))
    return (cache_day == _today() and mt >= daily_mtime()
            and time.time() - mt < ADJ_TTL)


# ------------------------------------------------ 复权因子（本地前/后复权）

def adjust_factors(code: str) -> pd.DataFrame:
    """该股全部除权因子：DataFrame(dt, fore, back)，按 dt 升序。

    磁盘缓存每股一份；日线数据更新（mtime 更晚）或缓存超过 7 天时重抓。
    已验证：本地 不复权价 × 因子 与 baostock 前/后复权结果误差 < 0.1%。
    """
    fp = str(DATA.path('sources', 'lake', f'_adj_cache/{code}.parquet'))
    if os.path.exists(fp):
        if _adjust_cache_is_fresh(fp):
            return pd.read_parquet(fp)

    raw = factor_frame(code, '1990-01-01', today())
    if len(raw):
        fac = raw[["dividOperateDate", "foreAdjustFactor",
                   "backAdjustFactor"]].copy()
        fac.columns = ["dt", "fore", "back"]
        fac["dt"] = pd.to_datetime(fac["dt"])
        fac["fore"] = pd.to_numeric(fac["fore"], errors="coerce")
        fac["back"] = pd.to_numeric(fac["back"], errors="coerce")
        fac = fac.dropna().sort_values("dt", ignore_index=True)
    else:   # 从未除权：空表，调用方按因子 1 处理
        fac = pd.DataFrame({"dt": pd.Series(dtype="datetime64[ns]"),
                            "fore": pd.Series(dtype="float64"),
                            "back": pd.Series(dtype="float64")})
    DATA.write('sources', 'lake', f'_adj_cache/{code}.parquet', fac.to_parquet(index=False))
    return fac


# ================================================================ fetch：抓取

@dataclass
class FetchStats:
    total: int = 0
    done: int = 0
    rows: int = 0
    empty: int = 0
    failed: list = field(default_factory=list)
    started: float = field(default_factory=time.time)
    current: str = ""

    @property
    def eta(self) -> str:
        if not self.done:
            return "--:--"
        left = (self.total - self.done) * (time.time() - self.started) / self.done
        return time.strftime("%H:%M:%S", time.gmtime(left))


def _fetch_panel(stats: FetchStats, bar):
    from rich.table import Table
    from rich.columns import Columns
    summary = Table.grid(padding=(0, 3))
    summary.add_row(
        f"[bold]{stats.done}/{stats.total}[/] 任务",
        f"新增 [cyan]{stats.rows:,}[/] 行",
        f"空 {stats.empty}",
        f"失败 [red]{len(stats.failed)}[/]",
        f"预计剩余 {stats.eta}",
    )
    summary.add_row(f"当前: {stats.current}")
    return Columns([bar, summary])


def cmd_fetch(args) -> int:
    tasks = pending_tasks(build_tasks(args), force=args.force)
    if args.max_tasks:
        tasks = tasks[:args.max_tasks]
    if not tasks:
        print("没有待办任务，数据已是最新。")
        return 0
    print(f"待抓 {len(tasks)} 个任务（字段预设: {FIELD_PRESETS.keys()} 可用）")

    stats = FetchStats(total=len(tasks))
    tty = sys.stdout.isatty()

    def work(report):
        for task in tasks:
            stats.current = f"{task.code} [{task.tag}/{task.preset}] {task.start}~{task.end}"
            df = bs_query(task.code, task)
            if df is None:
                stats.failed.append(task.code)
            else:
                stats.rows += save_cache(task, df)
                stats.empty += (len(df) == 0)
            stats.done += 1
            report()
            if args.sleep:
                time.sleep(args.sleep)

    if tty:
        from rich.live import Live
        from rich.progress import (Progress, BarColumn, TextColumn,
                                   TimeElapsedColumn)
        bar = Progress(TextColumn("{task.percentage:5.1f}%"), BarColumn(),
                       TimeElapsedColumn())
        bar_task = bar.add_task("抓取", total=len(tasks))
        with Live(_fetch_panel(stats, bar), refresh_per_second=4) as live:
            def report():
                bar.advance(bar_task)
                live.update(_fetch_panel(stats, bar))
            work(report)
    else:
        def report():
            if stats.done % 50 == 0 or stats.done == stats.total:
                print(f"[{time.strftime('%H:%M:%S')}] {stats.done}/{stats.total} "
                      f"新增={stats.rows:,} 空={stats.empty} "
                      f"失败={len(stats.failed)} 预计剩余={stats.eta}", flush=True)
        work(report)

    print(f"完成 {stats.done}，新增 {stats.rows:,} 行，空 {stats.empty}，"
          f"失败 {len(stats.failed)} {stats.failed[:10]}")
    if stats.done and not args.force:
        if not pending_tasks(build_tasks(args)) and saved(DATA, 'state', 'lake', 'tasks.json') is not None:
            DATA.delete('state', 'lake', 'tasks.json')
    return 1 if stats.failed else 0


# ================================================================ merge：合并

def _validate_merge_inputs(partitions: dict[str, dict[str, bool]]) -> None:
    """Reject backfill caches that have no partition to update."""
    for year, state in partitions.items():
        if state.get("fill") and not state.get("exists"):
            raise ValueError(f"missing target partition for fill: {year}")


def _file_signature(path: str) -> tuple[int, int, int]:
    """Identity used to avoid deleting a cache replaced during merge."""
    st = os.stat(path)
    return st.st_ino, st.st_size, st.st_mtime_ns


def _remove_unchanged_files(files: dict[str, tuple[int, int, int]]) -> int:
    """Remove only files read by this merge and unchanged since that read."""
    removed = 0
    for path, signature in files.items():
        try:
            if _file_signature(path) != signature:
                continue
            DATA.delete('sources', 'lake', '_incr_cache/' + Path(path).name)
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def _parquet_relation(paths: list[str]) -> str:
    """Return a DuckDB relation over an explicit parquet file set."""
    if not paths:
        raise ValueError("no daily parquet partitions available for merge validation")
    quoted = ", ".join("'" + path.replace("'", "''") + "'" for path in paths)
    return f"read_parquet([{quoted}])"


def cmd_merge(args) -> int:
    """Own the candidate workspace, including all failure paths."""
    with tempfile.TemporaryDirectory(prefix=".merge-", dir=LAKE) as candidate_dir:
        return _merge_candidates(args, candidate_dir)


def _merge_candidates(args, candidate_dir: str) -> int:
    """Merge caches through temporary candidates and a full-lake validation.

    Production files are replaced one by one only after validation. This is
    not a multi-file transaction: a commit interruption can leave partial
    production changes.
    """
    import duckdb

    print("1/6 读取抓取缓存 ...")
    si = read_stock_info()
    names = dict(zip(si["code"], si["name"]))
    consumed_cache: dict[str, tuple[int, int, int]] = {}

    def remember(path: str) -> None:
        try:
            consumed_cache[path] = _file_signature(path)
        except FileNotFoundError:
            pass

    raw_names = saved(DATA, 'state', 'lake', 'new_stocks.json')
    if raw_names is not None:
        names.update(json.loads(raw_names))
    basic_frames, fill_frames, factor_frames = [], [], []
    probed = _load_probed()
    cache_files = [
        os.path.join(CACHE, fn) for fn in sorted(os.listdir(CACHE))
        if fn.endswith(".parquet")
    ]
    for cache_path in cache_files:
        fn = os.path.basename(cache_path)
        remember(cache_path)
        if not os.path.exists(cache_path):
            continue
        code = fn.split("_")[0]
        df = pd.read_parquet(cache_path)
        if not len(df):
            # 空结果 = 已探测确认无数据（仅行情类缓存记入，30 天内不再重复探测）
            if not fn.endswith(("_st.parquet", "_val.parquet")):
                probed[code] = today()
            continue
        df["code"] = code
        if fn.endswith("_val.parquet"):
            factor_frames.append(df)
        elif fn.endswith("_st.parquet"):
            fill_frames.append(df)
        else:
            basic_frames.append(df)
    incr = pd.concat(basic_frames, ignore_index=True) if basic_frames else pd.DataFrame()
    fill = pd.concat(fill_frames, ignore_index=True) if fill_frames else pd.DataFrame()
    if not len(incr) and not len(fill) and not factor_frames:
        print("缓存为空，无新数据可合并。")
        return 0

    before = int(duckdb.sql(f"SELECT count(*) FROM '{DAILY_GLOB}'").fetchone()[0])

    print(f"2/6 行情增量 {len(incr):,} 行 / 回填 {len(fill):,} 行，按年分组 ...")
    for c in ("float_mv", "total_mv", "float_shares", "close_hfq"):
        if len(incr) and c not in incr.columns:
            incr[c] = float("nan")
    if len(incr):
        incr["name"] = incr["code"].map(names)
        incr["pool"] = "current"
    touched = sorted(set(incr["date"].str[:4] if len(incr) else [])
                     | set(fill["date"].str[:4] if len(fill) else []))
    _validate_merge_inputs({
        year: {
            "fill": bool(len(fill) and (fill["date"].str[:4] == year).any()),
            "exists": os.path.exists(os.path.join(DAILY, f"{year}.parquet")),
        }
        for year in touched
    })

    print(f"3/6 生成临时候选（触动 {len(touched)} 个年份：{'、'.join(touched)}）...")
    written = []
    candidate_daily: dict[str, str] = {}
    candidate_valuation: str | None = None
    for year in touched:
        fp = os.path.join(DAILY, f"{year}.parquet")
        part = pd.read_parquet(fp) if os.path.exists(fp) else None
        if part is not None:
            part = part[part["close"].notna()]      # 剔除历史占位空行
        if len(incr):
            y = incr[incr["date"].str[:4] == year]
            if len(y):
                y = y.reindex(columns=part.columns if part is not None else None)
                part = y if part is None else pd.concat(
                    [part, y.astype(part.dtypes.to_dict())], ignore_index=True)
        if len(fill) and part is not None:
            yf = fill[fill["date"].str[:4] == year]
            cols = [c for c in ("turn", "pctChg", "tradestatus", "isST")
                    if c in yf.columns and c in part.columns]
            if len(yf) and cols:
                part = part.set_index(["code", "date"])
                part.update(yf.set_index(["code", "date"])[cols])
                part = part.reset_index()
        if part is None:
            continue
        part = (part.drop_duplicates(["code", "date"], keep="last")
                    .sort_values(["code", "date"], ignore_index=True))
        assert not part.duplicated(["code", "date"]).any(), f"{year} 存在重复行！"
        assert part["close"].notna().all(), f"{year} 仍有空行！"
        candidate_path = os.path.join(candidate_dir, f"{year}.parquet")
        part.to_parquet(candidate_path, index=False, compression="zstd",
                        compression_level=ZSTD_LEVEL,
                        row_group_size=ROW_GROUP_SIZE)
        candidate_daily[year] = candidate_path
        written.append((year, len(part)))
        print(f"  {year}: {len(part):,} 行 {'(预演)' if args.dry_run else '候选'}")

    print("4/6 合并估值因子 ...")
    if factor_frames:
        fac = pd.concat(factor_frames, ignore_index=True)
        fac["date"] = fac["date"].astype(str)
        fac["quarter"] = (fac["date"].str[:4] + "Q" +
                          ((fac["date"].str[5:7].astype(int) - 1) // 3 + 1).astype(str))
        snap = fac.sort_values("date").groupby(["code", "quarter"]).tail(1)
        v = pd.read_parquet(VALUATION)
        new_rows = pd.DataFrame({
            "code": snap["code"], "name": snap["code"].map(names),
            "quarter": snap["quarter"], "pe_ttm": snap["peTTM"], "pb": snap["pbMRQ"],
            "roe": float("nan"), "div_yield": float("nan"),
        })
        v = pd.concat([v, new_rows], ignore_index=True)
        v = v.drop_duplicates(["code", "quarter"], keep="last")
        candidate_valuation = os.path.join(candidate_dir, "valuation.parquet")
        v.to_parquet(candidate_valuation, index=False, compression="zstd",
                     compression_level=ZSTD_LEVEL)
        print(f"     valuation_factors -> {len(v):,} 行，最新季度 {v['quarter'].max()}")

    production_daily = [
        os.path.join(DAILY, fn) for fn in sorted(os.listdir(DAILY))
        if fn.endswith(".parquet") and fn[:-8] not in candidate_daily
    ]
    validation_relation = _parquet_relation(
        list(candidate_daily.values()) + production_daily
    )

    print("5/6 重建主档统计与最新快照候选（duckdb 一趟聚合）...")
    g = duckdb.sql(f"""SELECT code, min(date) AS first, max(date) AS last,
                       count(*) AS n FROM {validation_relation} GROUP BY code"""
                   ).df().set_index("code")
    si = si.set_index("code")
    si["first_date"] = g["first"]
    si["last_date"] = g["last"]
    si["n_trading_days"] = g["n"].fillna(0).astype(int)
    si["has_daily"] = g["n"].notna().reindex(si.index).fillna(False).astype(bool)
    si = si.reset_index()
    new_codes = [c for c in names if c not in set(si["code"])]
    if new_codes:
        si = pd.concat([si, pd.DataFrame({
            "code": new_codes, "name": [names[c] for c in new_codes],
            "exchange": ["SH" if c[0] == "6" else "SZ" for c in new_codes],
            "status": "listed",
            "first_date": g["first"].reindex(new_codes),
            "last_date": g["last"].reindex(new_codes),
            "n_trading_days": g["n"].reindex(new_codes).fillna(0).astype(int),
            "has_daily": True,
        })], ignore_index=True)

    candidate_stock_info = os.path.join(candidate_dir, "stock_info.parquet")
    si.to_parquet(candidate_stock_info, index=False, compression="zstd",
                  compression_level=ZSTD_LEVEL)
    latest_df = duckdb.sql(f"""
        SELECT code, max(date) AS date,
               arg_max(close, date) AS close,
               arg_max(pctChg, date) AS pctChg,
               arg_max(amount, date) AS amount
        FROM {validation_relation} GROUP BY code""").df()
    candidate_latest = os.path.join(candidate_dir, "latest.parquet")
    latest_df.to_parquet(candidate_latest, index=False, compression="zstd",
                         compression_level=ZSTD_LEVEL)

    print("6/6 全湖校验 ...")
    after = int(duckdb.sql(f"SELECT count(*) FROM {validation_relation}").fetchone()[0])
    dup = int(duckdb.sql(f"SELECT count(*) - count(DISTINCT (code, date)) "
                         f"FROM {validation_relation}").fetchone()[0])
    nulls = int(duckdb.sql(f"SELECT count(*) FROM {validation_relation} "
                           f"WHERE close IS NULL").fetchone()[0])
    span = duckdb.sql(f"SELECT min(date), max(date), count(DISTINCT code) "
                      f"FROM {validation_relation}").fetchone()
    report = {"合并前行数": before, "合并后行数": after,
              "股票数": span[2], "日期范围": f"{span[0]} ~ {span[1]}",
              "重复行": dup, "close 空值": nulls, "新增股票": new_codes,
              "重写分区": written}
    for k, v in report.items():
        print(f"  {k}: {v}")
    assert dup == 0, "存在重复 (code,date)！"
    assert nulls == 0, "仍有空行！"

    if args.dry_run:
        print("预演模式，不写盘。")
        return 0

    # Validation passed. Replacements remain a visible per-file sequence, not
    # a multi-file transaction; an interruption can leave partial changes.
    for year, candidate_path in candidate_daily.items():
        DATA.write('sources', 'lake', f'daily/{year}.parquet', Path(candidate_path).read_bytes())
    if candidate_valuation:
        DATA.write('sources', 'lake', 'valuation_factors.parquet', Path(candidate_valuation).read_bytes())
    DATA.write('sources', 'lake', 'stock_info.parquet', Path(candidate_stock_info).read_bytes())
    DATA.write('sources', 'lake', 'latest_snapshot.parquet', Path(candidate_latest).read_bytes())
    DATA.write('state', 'lake', 'probed_empty.json', encoded(probed))

    # 数据已入湖，清理本次读取且未改变的缓存（保留 fetch.log 备查）。
    merged = _remove_unchanged_files(consumed_cache)
    print(f"缓存已清理：{merged} 个文件")
    if os.path.exists(PROFILE_JSON):          # 体检快照失效，下次 status 自动重建
        DATA.delete('state', 'lake', 'profile.json')
    previous = saved(DATA, 'sources', 'lake', 'README.md')
    entry = (f"\n## {today()} 增量合并（collect/lake.py，分区感知）\n\n"
                f"- 行情行数 {before:,} -> {after:,}，股票 {span[2]} 只\n"
                f"- 重写分区：{'、'.join(y for y, _ in written)}\n"
                f"- 来源 baostock（不复权）；增量行 float_mv/total_mv/float_shares 为空\n")
    DATA.write('sources', 'lake', 'README.md', (b'' if previous is None else previous) + entry.encode())
    print(f"写盘完成：{DAILY}/（{after:,} 行，重写 {len(written)} 个分区）")
    return 0



# ================================================================ status：体检报告

# 日线字段展示分组：(显示名, 列, 盲区说明)；有值率取组内最低
DAILY_FIELD_GROUPS = [
    ("开/高/低/收", ["open", "high", "low", "close"], "—"),
    ("成交量/成交额", ["volume", "amount"], "停牌日为 0，属正常"),
    ("流通市值/总市值", ["float_mv", "total_mv"], "2013 与 2026 段为空（baostock 不提供）"),
    ("流通股本", ["float_shares"], "同上"),
    ("换手率/涨跌幅/交易状态/ST标记", ["turn", "pctChg", "tradestatus", "isST"],
     "现存股 2014~2025 主体段为空（宽表不含）"),
    ("后复权收盘", ["close_hfq"], "仅退市股有值"),
]

# 基本面 37 项指标分组（体检默认只显示每组有值率最低项，--full 展开）
FUNDAMENTAL_GROUPS = {
    "盈利": ["归母净利润(TTM)", "净利润(TTM)", "扣非净利润(TTM)", "毛利率", "销售净利率",
             "ROE", "ROA", "盈利含金量"],
    "成长": ["营收增速", "净利润增速"],
    "偿债": ["资产负债率(摘要)", "流动比率", "速动比率", "现金比率", "现金短债比", "产权比率"],
    "营运": ["应收账款周转率", "存货周转率", "总资产周转率"],
    "现金流": ["经营现金流净额(TTM)", "资本开支(TTM)", "自由现金流(TTM)"],
    "资产结构": ["净资产", "商誉", "货币资金", "短期借款", "长期借款", "一年内到期非流动负债",
                 "应付债券", "资产总计", "负债合计", "应收账款", "归属于母公司股东权益合计",
                 "期间费用率", "权益乘数", "营业总收入(TTM)", "营业成本(TTM)"],
}


def _level(ok: float, warn: float) -> str:
    """有值率 -> 级别：✅ >= ok，⚠️ >= warn，否则 ❌"""
    return "✅" if ok >= 90 else ("⚠️" if ok >= 50 else "❌")


def build_profile() -> dict:
    """duckdb 单趟聚合全量体检（约 1~2 秒），输出 schema 与原 pandas 版一致。"""
    import duckdb
    si = read_stock_info()
    v = pd.read_parquet(VALUATION)
    f = pd.read_parquet(FUNDAMENTALS)

    p: dict = {"生成时间": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ---- 总览（一趟）
    n_rows, n_codes, d_min, last_date = duckdb.sql(
        f"SELECT count(*), count(DISTINCT code), min(date), max(date) "
        f"FROM '{DAILY_GLOB}'").fetchone()
    lag = len(pd.bdate_range(last_date, today())) - 1
    p["总览"] = {
        "日线行情": {"行数": n_rows, "股票数": n_codes,
                    "区间": f"{d_min} ~ {last_date}", "落后交易日": lag},
        "股票主档": {"行数": len(si), "现存": int((si["status"] == "listed").sum()),
                    "退市": int((si["status"] == "delisted").sum())},
        "估值因子": {"行数": len(v), "股票数": int(v["code"].nunique()),
                    "区间": f'{v["quarter"].min()} ~ {v["quarter"].max()}'},
        "基本面因子": {"行数": len(f), "股票数": int(f["code"].nunique()),
                     "区间": f'{f["quarter"].min()} ~ {f["quarter"].max()}'},
    }

    # ---- 年度覆盖（一趟）
    year_rows = duckdb.sql(f"""
        SELECT substr(date, 1, 4) AS yr, pool, count(*) AS n,
               count(DISTINCT code) AS stocks, count(DISTINCT date) AS days
        FROM '{DAILY_GLOB}' GROUP BY 1, 2 ORDER BY 1""").df()
    years: dict[str, dict] = {}
    for r in year_rows.itertuples():
        y = years.setdefault(r.yr, {"交易日": int(r.days)})
        key = "现存股日均" if r.pool == "current" else "退市股日均"
        y[key] = round(int(r.n) / max(int(r.days), 1))
    p["年度覆盖"] = years

    # ---- 日线字段覆盖（一趟：有值率 + 覆盖股数）
    cols = ["open", "high", "low", "close", "volume", "amount", "float_mv",
            "total_mv", "float_shares", "turn", "pctChg", "tradestatus",
            "isST", "close_hfq"]
    selects = []
    for c in cols:
        cond = f"({c} IS NOT NULL AND {c} > 0)" if c == "close" \
            else f"({c} IS NOT NULL)"
        selects.append(f"100 * avg(CASE WHEN {cond} THEN 1.0 ELSE 0.0 END) "
                       f"AS {c}__rate")
        selects.append(f"count(DISTINCT code) FILTER (WHERE {cond}) "
                       f"AS {c}__stocks")
    row = duckdb.sql(f"SELECT {', '.join(selects)} FROM '{DAILY_GLOB}'"
                     ).fetchone()
    p["日线字段"] = {c: {"有值率": round(float(row[i * 2]), 1),
                        "覆盖股数": int(row[i * 2 + 1])}
                     for i, c in enumerate(cols)}

    # ---- 因子字段覆盖（小表，pandas 即可）
    p["估值因子字段"] = {c: round(v[c].notna().mean() * 100, 1)
                       for c in ["pe_ttm", "pb", "roe", "div_yield"]}
    fund_fields = {}
    for group, fcols in FUNDAMENTAL_GROUPS.items():
        fund_fields[group] = {c: round(f[c].notna().mean() * 100, 1)
                              for c in fcols if c in f.columns}
    p["基本面因子字段"] = fund_fields

    # ---- 异常检查（一趟）
    dup, bad_price, zero_vol = duckdb.sql(f"""
        SELECT count(*) - count(DISTINCT (code, date)),
               sum(CASE WHEN close <= 0 OR high < low THEN 1 ELSE 0 END),
               100 * avg(CASE WHEN volume <= 0 THEN 1.0 ELSE 0.0 END)
        FROM '{DAILY_GLOB}'""").fetchone()
    anomalies = []
    if dup:
        anomalies.append(f"❌ 存在 {int(dup)} 条重复 (代码,日期) 行")
    if bad_price:
        anomalies.append(f"❌ {int(bad_price)} 条非法价格")
    zero_vol = round(float(zero_vol), 2)
    anomalies.append(f"{'⚠️' if zero_vol > 1 else '✅'} 成交量为 0 的行占 {zero_vol}%（停牌残留）")
    no_daily = int((si["has_daily"] == False).sum())  # noqa: E712
    if no_daily:
        anomalies.append(f"⚠️ {no_daily} 只主档股票无任何日线（上市未满或从未交易）")
    p["异常检查"] = anomalies

    # ---- 回测可用性（一趟；策略区间 2014-06 ~ 2025-12）
    b_rows, b_codes, b_min, b_max = duckdb.sql(f"""
        SELECT count(*), count(DISTINCT code), min(date), max(date)
        FROM '{DAILY_GLOB}'
        WHERE pool = 'current' AND date >= '2014-06-01' AND date <= '2025-12-31'
        """).fetchone()
    p["回测区间"] = {"区间": "2014-06 ~ 2025-12", "行数": b_rows,
                   "股票数": b_codes,
                   "完整": bool(b_min <= "2014-06-05" and b_max >= "2025-12-30")}

    # ---- 结论：取全表最差级别，列出需要注意的点
    issues: list[str] = []
    if lag > 5:
        issues.append(f"行情落后 {lag} 个交易日（lake.py fetch 可更新）")
    target_q = _target_quarter()
    v_lag = len(_quarters_between(v["quarter"].max(), target_q))
    if v_lag >= 1:
        issues.append(f"估值因子落后 {v_lag} 个季度（lake.py fetch --factors 可补）")
    f_lag = len(_quarters_between(f["quarter"].max(), target_q))
    if f_lag >= 1:
        issues.append(f"基本面因子落后 {f_lag} 个季度（来源为宽表，暂不支持自动补抓）")
    blind = [name for name, cols, _ in DAILY_FIELD_GROUPS
             if min(p["日线字段"][c]["有值率"] for c in cols) < 90]
    if blind:
        issues.append("字段盲区：" + "、".join(blind))
    severe = any(a.startswith("❌") for a in anomalies)
    p["结论"] = {"级别": "❌" if severe else ("⚠️" if issues else "✅"),
                "要点": issues, "回测可用": p["回测区间"]["完整"]}
    return p


def load_profile(rescan: bool = False) -> dict:
    """读快照；parquet 比快照新（或不存在）时自动重建。"""
    if not rescan and os.path.exists(PROFILE_JSON):
        if lake_mtime() <= os.path.getmtime(PROFILE_JSON):
            return json.loads(DATA.read('state', 'lake', 'profile.json'))
    print("正在全量扫描数据湖（duckdb 单趟聚合，约 1~2 秒）...", flush=True)
    p = build_profile()
    DATA.write('state', 'lake', 'profile.json', encoded(p))
    return p


def _render_plain(p: dict, full: bool):
    """统一的中文文本渲染（终端与日志通用）。"""
    o = p["总览"]
    d = o["日线行情"]
    c = p["结论"]
    print("═" * 55)
    print(f' data/sources/lake 体检报告 · {p["生成时间"]}')
    print("═" * 55)

    # 结论先行：一句话回答"现在数据能不能用"
    verdict = "✅ 数据可用" if c["级别"] == "✅" else \
        (f'⚠️ 数据可用但有 {len(c["要点"])} 项注意' if c["级别"] == "⚠️" else "❌ 数据存在严重问题")
    backtest = "回测区间完整" if c["回测可用"] else "回测区间不完整"
    print(f'\n 结论：{verdict}（{backtest}）')
    for issue in c["要点"]:
        print(f"       · {issue}")

    lag = d["落后交易日"]
    lag_mark = "✅" if lag <= 5 else ("⚠️" if lag <= 20 else "❌")
    print("\n【数据总览】")
    print(f'  日线行情      {d["行数"]:>12,} 行  {d["股票数"]} 只  {d["区间"]}  {lag_mark} 落后 {lag} 个交易日')
    print(f'  股票主档      {o["股票主档"]["行数"]:>12,} 只  现存 {o["股票主档"]["现存"]} / 退市 {o["股票主档"]["退市"]}')
    print(f'  估值因子      {o["估值因子"]["行数"]:>12,} 行  {o["估值因子"]["股票数"]} 只  {o["估值因子"]["区间"]}')
    print(f'  基本面因子    {o["基本面因子"]["行数"]:>12,} 行  {o["基本面因子"]["股票数"]} 只  {o["基本面因子"]["区间"]}')

    print("\n【年度覆盖】")
    print(f'  {"年份":<6}{"交易日":>6}{"现存股/日":>10}{"退市股/日":>10}')
    for yr, y in p["年度覆盖"].items():
        print(f'  {yr:<6}{y["交易日"]:>6}{y.get("现存股日均", 0):>10}{y.get("退市股日均", 0):>10}')

    print("\n【字段覆盖】")
    df = p["日线字段"]
    for name, cols, note in DAILY_FIELD_GROUPS:
        rate = min(df[c]["有值率"] for c in cols)
        stocks = min(df[c]["覆盖股数"] for c in cols)
        print(f'  {_level(rate, 50)} {name:<22} 有值率 {rate:>5}%  覆盖 {stocks} 只  {note}')
    print("  估值因子: " + "  ".join(
        f'{k} {r}%' for k, r in p["估值因子字段"].items()))
    for group, cols in p["基本面因子字段"].items():
        items = sorted(cols.items(), key=lambda kv: kv[1])
        show = items if full else items[:2]
        detail = "  ".join(f"{k} {r}%" for k, r in show)
        suffix = "" if full else f"（共 {len(items)} 项，--full 展开）"
        print(f"  基本面·{group}: {detail}{suffix}")

    print("\n【异常检查】")
    for a in p["异常检查"]:
        print(f"  {a}")

    b = p["回测区间"]
    mark = "✅" if b["完整"] else "❌"
    print(f'\n【回测可用性】{mark} 策略区间 {b["区间"]}：{b["行数"]:,} 行 / {b["股票数"]} 只')


def cmd_status(args) -> int:
    p = load_profile(rescan=args.rescan)
    if args.json:
        print(json.dumps(p, ensure_ascii=False, indent=2))
    else:
        _render_plain(p, args.full)
    return 0


# ================================================================ query：股票查询

def cmd_query(args) -> int:
    hits = resolve_code(args.keyword)
    if not hits:
        print(f"未找到「{args.keyword}」匹配的股票。")
        return 1
    if len(hits) > 20:
        print(f"命中 {len(hits)} 只，仅显示前 20 只：")
        hits = hits[:20]

    latest = read_latest().set_index("code")

    print(f'{"代码":<8}{"名称":<12}{"交易所":<6}{"状态":<6}'
          f'{"首日":<12}{"末日":<12}{"最新收盘":>9}{"涨跌幅%":>9}{"成交额(亿)":>11}')
    for row in hits:
        code = row["code"]
        lp = latest.loc[code] if code in latest.index else None
        if lp is not None:
            amt = lp["amount"] / 1e8 if pd.notna(lp["amount"]) else float("nan")
            print(f'{code:<8}{row["name"]:<12}{row["exchange"]:<6}'
                  f'{"现存" if row["status"] == "listed" else "退市":<6}'
                  f'{str(row["first_date"]):<12}{str(row["last_date"]):<12}'
                  f'{lp["close"]:>9.2f}{lp["pctChg"] if pd.notna(lp["pctChg"]) else float("nan"):>9.2f}'
                  f'{amt:>11.2f}  [{lp["date"]}]')
        else:
            print(f'{code:<8}{row["name"]:<12}{row["exchange"]:<6}'
                  f'{"现存" if row["status"] == "listed" else "退市":<6}'
                  f'{"—":<12}{"—":<12}{"无日线数据":>9}')
    return 0


# ================================================================ chart：K 线可视化

# 周期 -> (说明, 默认显示窗口, 重采样规则)
# 取数始终返回该股的完整历史，显示窗口由前端 dataZoom 决定
FREQS = {
    "分时": ("5 分钟", None, None),
    "日": ("日线", None, None),
    "周": ("周线", None, "W-FRI"),
    "月": ("月线", None, "ME"),
    "年": ("年线", None, "YE"),
}
ADJUSTS = {"不复权": "3", "前复权": "2", "后复权": "1"}


def daily_one(code: str) -> pd.DataFrame:
    """单股不复权日线（谓词下推，约 10~100ms），dt 索引升序，含 OHLC + 量额。"""
    df = read_daily_one(code, columns=["code", "date", "open", "high", "low",
                                       "close", "volume", "amount"])
    df["dt"] = pd.to_datetime(df["date"])
    return df.set_index("dt").sort_index()


def _apply_adjust(df: pd.DataFrame, code: str, adj: str) -> pd.DataFrame:
    """前/后复权 = 不复权 OHLC × 缓存复权因子（量额按口径不调整）。"""
    if adj == "不复权" or not len(df):
        return df
    fac = adjust_factors(code)
    if not len(fac):
        return df
    col = "fore" if adj == "前复权" else "back"
    out = df.reset_index()
    out = pd.merge_asof(out.sort_values("dt"), fac[["dt", col]], on="dt")
    # 首个除权日之前：前复权沿用最早事件因子，后复权因子为 1
    out[col] = out[col].ffill().fillna(fac[col].iloc[0] if adj == "前复权" else 1.0)
    for c in ("open", "high", "low", "close"):
        out[c] = out[c] * out[col]
    return out.set_index("dt")


def shape_chart(raw: pd.DataFrame, code: str, freq: str, adj: str,
                start: str | None, end: str | None) -> pd.DataFrame:
    """纯变换：复权 -> 区间过滤 -> 重采样。不读盘、不联网（因子走磁盘缓存）。"""
    end = end or today()
    df = _apply_adjust(raw, code, adj)
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    df = df[df.index <= pd.Timestamp(end)]
    rule = FREQS[freq][2]
    if rule:  # 周/月/年：OHLC 聚合 + 量额求和（bin 标签为周期末日）
        df = df.resample(rule).agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
            "volume": "sum", "amount": "sum"}).dropna(subset=["close"])
    return df


def _minute_data(code: str, adj: str, start: str | None, end: str) -> pd.DataFrame:
    """分时（5 分钟）：本地无存储，走 baostock 常驻会话。"""
    start = start or (pd.Timestamp(end) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")

    df = daily_frame(code, "date,time,open,high,low,close,volume,amount",
                     start, end, frequency="5", adjustment=ADJUSTS[adj])
    if not len(df):
        return df
    df["dt"] = pd.to_datetime(df["time"].str[:14], format="%Y%m%d%H%M%S")
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).set_index("dt")


def _chart_data(code: str, freq: str, adj: str,
                start: str | None, end: str | None) -> pd.DataFrame:
    """取数：日/周/月/年全部本地（复权本地算）；分时走 baostock 实时。"""
    end = end or today()
    if freq == "分时":
        return _minute_data(code, adj, start, end)
    return shape_chart(daily_one(code), code, freq, adj, start, end)


def cmd_chart(args) -> int:
    hits = resolve_code(args.keyword)
    if not hits:
        print(f"未找到「{args.keyword}」。")
        return 1
    if len(hits) > 1:
        print(f"命中 {len(hits)} 只，请用 6 位代码精确指定：")
        for row in hits[:10]:
            print(f'  {row["code"]}  {row["name"]}')
        return 1
    code, name = hits[0]["code"], hits[0]["name"]

    print(f"取数中：{code} {name} · {args.freq} · {args.adj} ...")
    df = _chart_data(code, args.freq, args.adj, args.start, args.end)
    if not len(df):
        print("该区间无数据（分时数据 baostock 仅保留近期）。")
        return 1

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.75, 0.25], vertical_spacing=0.02)
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name="K线",
        increasing_line_color="#d43f3f", decreasing_line_color="#2e8b57"),
        row=1, col=1)
    colors = ["#d43f3f" if c >= o else "#2e8b57"
              for o, c in zip(df["open"], df["close"])]
    fig.add_trace(go.Bar(x=df.index, y=df["volume"], name="成交量",
                         marker_color=colors, opacity=0.6), row=2, col=1)
    span = f"{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}"
    fig.update_layout(
        title=f"{code} {name} · {FREQS[args.freq][0]} · {args.adj} · {span}",
        xaxis_rangeslider_visible=False, height=640,
        font=dict(family="PingFang SC, Microsoft YaHei, sans-serif"),
        margin=dict(l=40, r=20, t=50, b=30))
    name = f'{code}_{args.freq}_{args.adj}_{time.strftime("%Y%m%d_%H%M%S")}.html'
    out = DATA.write('runs', 'charts', name, fig.to_html(include_plotlyjs='cdn').encode()).file
    print(f"图表已保存：{out}")
    return 0


# ================================================================ doctor：诊断 + 一键修复

# 修复动作 -> fetch 参数（cmd_fetch 的 args 命名空间）
def _fix_args(fix: str, max_tasks: int = 0):
    """各修复动作对应的抓取参数。全部走增量/缺口语义，已有正常数据不重复抓。"""
    from types import SimpleNamespace
    base = dict(factors=False, start=None, end=None, codes=None,
                fields="basic", force=False, max_tasks=max_tasks, sleep=0.0)
    if fix == "auto":       # 行情增量 + 无日线股探测 + 新股探测
        return SimpleNamespace(**base)
    if fix == "factors":    # 估值因子缺失季度
        return SimpleNamespace(**base, factors=True)
    if fix == "st":         # 换手率/涨跌幅/交易状态/ST 盲区补抓（仅现存股空白段）
        return SimpleNamespace(**{**base, "start": "2014-01-01",
                                  "end": "2025-12-31", "fields": "st"})
    raise ValueError(f"未知修复动作: {fix}")


def doctor_plan() -> list[dict]:
    """把体检结论转成结构化修复计划（CLI 与 Web 共用，口径一致）。

    每项：id / 级别 / 问题 / 可修复 / 动作 / 范围 / 说明。
    可修复项的「范围」只覆盖缺失内容，已有正常数据一律不重复抓。
    """
    p = load_profile()
    o = p["总览"]
    df = p["日线字段"]
    items: list[dict] = []

    # 已抓未合并的缓存：最优先（不抓新数据，直接合并）
    n_cache = len([f for f in os.listdir(CACHE) if f.endswith(".parquet")])
    if n_cache:
        items.append({"id": "cache_pending", "级别": "⚠️",
                      "问题": f"{n_cache} 个已抓取缓存尚未合并入湖",
                      "可修复": True, "fix": "merge_only",
                      "动作": "直接合并入湖（校验后原子写盘）",
                      "范围": "不抓取任何新数据", "说明": ""})

    lag = o["日线行情"]["落后交易日"]
    if lag > 5:
        items.append({"id": "daily_lag", "级别": "⚠️",
                      "问题": f"行情落后 {lag} 个交易日",
                      "可修复": True, "fix": "auto",
                      "动作": "增量抓取缺失交易日",
                      "范围": "只抓各股缺口区间，已有行情不重复抓",
                      "说明": ""})

    v_lag = len(_quarters_between(o["估值因子"]["区间"].split(" ~ ")[-1],
                                  _target_quarter()))
    if v_lag:
        items.append({"id": "factor_lag", "级别": "⚠️",
                      "问题": f"估值因子落后 {v_lag} 个季度",
                      "可修复": True, "fix": "factors",
                      "动作": "补抓估值因子缺失季度（PE/PB 等）",
                      "范围": "只补缺失季度，已有季度不重复抓",
                      "说明": ""})

    f_lag = len(_quarters_between(o["基本面因子"]["区间"].split(" ~ ")[-1],
                                  _target_quarter()))
    if f_lag:
        items.append({"id": "fund_lag", "级别": "⚠️",
                      "问题": f"基本面因子落后 {f_lag} 个季度",
                      "可修复": False, "fix": None, "动作": "", "范围": "",
                      "说明": "来源为宽表，暂不支持自动补抓"})

    if min(df["turn"]["有值率"], df["pctChg"]["有值率"]) < 90:
        n = len(_st_blind_codes("2014-01-01", "2025-12-31"))
        items.append({"id": "st_blind", "级别": "⚠️",
                      "问题": "换手率/涨跌幅/交易状态/ST 字段存在盲区",
                      "可修复": True, "fix": "st",
                      "动作": "补抓扩展字段空白段",
                      "范围": f"{n} 只盲区股 × 2014~2025（已补过的自动跳过）",
                      "说明": "任务量大（每股一次查询），可随时停止、续传"})

    if min(df["float_mv"]["有值率"], df["total_mv"]["有值率"]) < 90:
        items.append({"id": "mv_blind", "级别": "⚠️",
                      "问题": "流通市值/总市值/流通股本存在盲区（2013 与 2026 段）",
                      "可修复": False, "fix": None, "动作": "", "范围": "",
                      "说明": "baostock 不提供市值字段；来源宽表仅覆盖 2014~2025，暂无自动来源"})

    if df["close_hfq"]["有值率"] < 90:
        items.append({"id": "hfq_blind", "级别": "ℹ️",
                      "问题": "后复权收盘字段仅退市股有值",
                      "可修复": False, "fix": None, "动作": "", "范围": "",
                      "说明": "现存股后复权价已由复权因子实时换算（K线支持），暂不回填"})

    si = read_stock_info()
    no_daily_codes = list(si.loc[si["has_daily"] == False, "code"])  # noqa: E712
    probed = _fresh_probed()
    unprobed = [c for c in no_daily_codes if c not in probed]
    confirmed = len(no_daily_codes) - len(unprobed)
    if unprobed:
        items.append({"id": "no_daily", "级别": "⚠️",
                      "问题": f"{len(unprobed)} 只主档股票从未探测过日线",
                      "可修复": True, "fix": "auto",
                      "动作": "全量探测这些股票（含新股探测）",
                      "范围": f"仅 {len(unprobed)} 只未探测股，其余不碰",
                      "说明": "多为上市未满或长期停牌，探测后无数据属正常"})
    if confirmed:
        items.append({"id": "no_data_confirmed", "级别": "ℹ️",
                      "问题": f"{confirmed} 只股票已探测确认无数据（停牌/未上市）",
                      "可修复": False, "fix": None, "动作": "", "范围": "",
                      "说明": f"属正常现象，系统每 {PROBED_RETRY_DAYS} 天自动复探"})

    for a in p["异常检查"]:
        if a.startswith("❌"):
            items.append({"id": "anomaly", "级别": "❌", "问题": a[2:].strip(),
                          "可修复": False, "fix": None, "动作": "", "范围": "",
                          "说明": "数据损坏需人工核查；merge 校验会拦截脏数据写入"})
    return items


def cmd_doctor(args) -> int:
    plan = doctor_plan()
    if not plan:
        print("✅ 体检未发现需要处理的问题。")
        return 0
    print(f"发现 {len(plan)} 项：")
    for it in plan:
        mark = "🔧" if it["可修复"] else "📌"
        print(f'  {it["级别"]} {mark} [{it["id"]}] {it["问题"]}')
        if it["可修复"]:
            print(f'        修复：{it["动作"]}｜范围：{it["范围"]}')
        else:
            print(f'        说明：{it["说明"]}')
    if not args.fix:
        print('\n执行修复：lake.py doctor --fix <id>（修复 = 抓取 → 合并 → 复检）')
        return 0

    target = next((i for i in plan if i["id"] == args.fix), None)
    if not target:
        print(f"计划中没有 id={args.fix}")
        return 1
    if not target["可修复"]:
        print(f'[{args.fix}] 不可自动修复：{target["说明"]}')
        return 1

    code = 0
    if target["fix"] == "merge_only":
        print('\n=== 1/3 抓取：跳过（仅合并已有缓存） ===')
    else:
        print(f'\n=== 1/3 抓取：{target["动作"]} ===')
        code = cmd_fetch(_fix_args(target["fix"], max_tasks=args.max_tasks))
    print('\n=== 2/3 合并入湖 ===')
    mcode = cmd_merge(SimpleNamespace(dry_run=False))
    print('\n=== 3/3 复检 ===')
    lake_profile = load_profile(rescan=True)
    still = any(i["id"] == args.fix for i in doctor_plan())
    print(f'复检结论：{"⚠️ 问题仍存在" if still else "✅ 已修复"}'
          f'（整体级别 {lake_profile["结论"]["级别"]}）')
    return 1 if (code or mcode) else 0

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="lake.py",
        description="统一 data 根目录的数据湖管理工具：体检 / 抓取 / 合并 / 查询 / K线可视化 / 一键修复",
        epilog="示例：\n"
               "  lake.py status                 数据体检报告\n"
               "  lake.py doctor                 诊断并列出可修复项\n"
               "  lake.py doctor --fix st_blind  一键修复：抓取 → 合并 → 复检\n"
               "  lake.py fetch                  自动增量抓取\n"
               "  lake.py fetch --start 2014-01-01 --end 2025-12-31 --fields st\n"
               "  lake.py merge --dry-run        预演合并\n"
               "  lake.py query 平安             按名称查股票\n"
               "  lake.py chart 600036 --freq 日 --adj 前复权",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="子命令")

    p = sub.add_parser("status", help="数据体检报告（年份/股数/字段覆盖/新鲜度/异常）")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--full", action="store_true", help="展开基本面全部 37 项指标")
    p.add_argument("--rescan", action="store_true", help="忽略缓存快照，强制重新全量扫描")

    p = sub.add_parser("fetch", help="抓取数据（断点续传；可修复、可更新、可扩展）")
    p.add_argument("--start", help="起始日期 YYYY-MM-DD（配合 --end 做区间修复）")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--codes", help="只抓指定股票，逗号分隔 6 位代码")
    p.add_argument("--fields", choices=list(FIELD_PRESETS), default="basic",
                   help="字段预设：basic=行情基础 / st=换手涨跌幅ST / valuation=PE/PB等")
    p.add_argument("--factors", action="store_true", help="补抓估值因子缺失季度")
    p.add_argument("--force", action="store_true", help="忽略缓存重抓（修复错误数据）")
    p.add_argument("--max-tasks", type=int, default=0, help="只跑前 N 个任务（调试用）")
    p.add_argument("--sleep", type=float, default=0.0, help="每次查询间隔秒数（防限流）")

    p = sub.add_parser("merge", help="把抓取缓存合并进数据湖（校验后原子替换）")
    p.add_argument("--dry-run", action="store_true", help="只校验不写盘")

    p = sub.add_parser("query", help="按代码或名称查询股票主档与最新行情")
    p.add_argument("keyword", help="6 位代码或名称片段，如 600036 / 平安")

    p = sub.add_parser("chart", help="K线可视化，输出 HTML 到 charts/ 目录")
    p.add_argument("keyword", help="6 位代码或唯一名称")
    p.add_argument("--freq", choices=list(FREQS), default="日",
                   help="周期：分时=5分钟 / 日 / 周 / 月 / 年")
    p.add_argument("--adj", choices=list(ADJUSTS), default="不复权",
                   help="复权方式（前/后复权由本地缓存因子计算）")
    p.add_argument("--start", help="起始日期 YYYY-MM-DD")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD")

    p = sub.add_parser("doctor", help="诊断并一键修复（抓取 → 合并 → 复检）")
    p.add_argument("--fix", metavar="问题ID", help="执行对应修复，如 st_blind")
    p.add_argument("--max-tasks", type=int, default=0, help="只跑前 N 个抓取任务（调试用）")

    args = ap.parse_args()
    from protocal import build_data
    bind(build_data(Path(__file__).resolve().parents[2]))
    return {"status": cmd_status, "fetch": cmd_fetch, "merge": cmd_merge,
            "query": cmd_query, "chart": cmd_chart, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
