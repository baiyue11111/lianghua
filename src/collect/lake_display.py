from __future__ import annotations
# -*- coding: utf-8 -*-
"""Data-lake module port behind the public backend protocol."""
import contextlib
import io
import threading
import time
import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

ROOT = str(Path(__file__).resolve().parents[2])
from . import lake
from tools.audit_research_inputs import audit as audit_research_inputs
from .fetch import FetchManager
from closed_loop_report import load_closed_loop_report
from research_registry import ResearchRegistry
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from protocal import DataPort

DASHBOARD_SOURCES = Path(ROOT) / "configs" / "dashboard_sources.json"


def _warmup():
    """启动后后台预热：最新行情快照 + 体检快照，首个请求不必干等。"""
    def _run():
        try:
            _latest_cached()
        except Exception:
            pass
        try:
            lake.load_profile()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------- 读缓存
# 数据湖 parquet 的 mtime 是唯一的失效依据：merge 落盘后缓存自动作废，
# 新数据不改任何代码即同步可见。

_CACHE_LOCK = threading.Lock()
_BAOSTOCK_LOCK = threading.Lock()
_DAILY_ONE = {"mtime": None, "data": {}}   # code -> 不复权日线 DataFrame
_LATEST = {"mtime": None, "df": None}      # 全市场最新一行快照
_DAILY_ONE_CAP = 200                        # 单股缓存上限（只）


def _daily_cached(code: str):
    """单股日线：谓词下推约 10~100ms，进程内再做一层缓存（毫秒级）。"""
    mt = lake.daily_mtime()
    with _CACHE_LOCK:
        if _DAILY_ONE["mtime"] != mt:
            _DAILY_ONE.update(mtime=mt, data={})
        d = _DAILY_ONE["data"]
        if code not in d:
            if len(d) >= _DAILY_ONE_CAP:
                d.clear()
            d[code] = lake.daily_one(code)
        return d[code]


def _latest_cached():
    """全市场最新行情快照（C：物化文件），按数据湖 mtime 失效。"""
    mt = lake.lake_mtime()
    with _CACHE_LOCK:
        if _LATEST["mtime"] != mt or _LATEST["df"] is None:
            df = lake.read_latest().set_index("code")
            _LATEST.update(mtime=mt, df=df)
        return _LATEST["df"]


def _latest_data_coverage() -> tuple[dict, str | None]:
    """Return the newest date together with its observed stock coverage."""
    try:
        return lake.latest_data_coverage(), None
    except Exception as exc:
        return {"latest_data_date": None, "latest_data_stock_count": None,
                "tracked_stock_count": None, "median_stock_date": None}, str(exc)


# ---------------------------------------------------------------- 体检

def get_status(rescan: bool = False):
    return lake.load_profile(rescan=rescan)


def research_inputs(data: DataPort):
    """Read-only evidence for research; lake health is not backtest acceptance."""
    report = audit_research_inputs(Path(lake.LAKE))
    changes = json.loads(data.read("sources", "research", "security_code_changes.json"))
    report["unverified_code_changes"] = [
        event for event in changes["events"]
        if not event["factor_basis_continuity_verified"]
    ]
    report["saved_factor_files"] = sum(ref.name.startswith('adjust_') and ref.name.endswith('.json')
                                      for ref in data.list('sources', 'research_cache_verified'))
    return report


def research_daily(data: DataPort, as_of: str | None = None):
    directory = data.path("state", "research_registry", "_").parent
    return ResearchRegistry(directory).dashboard(as_of or lake.today())


def closed_loop_report(data: DataPort):
    sources = json.loads(DASHBOARD_SOURCES.read_text(encoding="utf-8"))
    if sources.get("schema") != 1:
        raise ValueError("unsupported dashboard source schema")
    identity = sources["closed_loop_id"]
    manifest = data.path("validation", identity, "manifest.json")
    return load_closed_loop_report(manifest)


# ---------------------------------------------------------------- 查询

def query_stock(q: str):
    hits = lake.resolve_code(q)[:20]
    if not hits:
        return {"items": []}
    latest = _latest_cached()
    items = []
    for row in hits:
        code = row["code"]
        item = {"代码": code, "名称": row["name"], "交易所": row["exchange"],
                "状态": "现存" if row["status"] == "listed" else "退市",
                "首日": row["first_date"], "末日": row["last_date"]}
        if code in latest.index:
            lp = latest.loc[code]
            item.update({"最新收盘": _num(lp["close"]),
                         "涨跌幅": _num(lp["pctChg"]),
                         "成交额亿": _num(lp["amount"] / 1e8),
                         "行情日期": lp["date"]})
        items.append(item)
    return {"items": items}


def _num(x):
    return None if x != x else round(float(x), 4)  # NaN -> None


# ---------------------------------------------------------------- K 线

def kline(code: str, freq: str = "日", adj: str = "不复权",
          start: str | None = None, end: str | None = None):
    if freq not in lake.FREQS or adj not in lake.ADJUSTS:
        return {"error": "非法参数", "candles": []}
    with _BAOSTOCK_LOCK:
        if freq == "分时":
            df = lake._minute_data(code, adj, start, end or lake.today())
        else:
            df = lake.shape_chart(_daily_cached(code), code, freq, adj, start, end)
    if not len(df):
        return {"candles": [], "volumes": [], "dates": []}
    dates = [t.strftime("%Y-%m-%d %H:%M") if freq == "分时"
             else t.strftime("%Y-%m-%d") for t in df.index]
    candles = [[_num(r.open), _num(r.close), _num(r.low), _num(r.high)]
               for r in df.itertuples()]
    volumes = [_num(v) for v in df["volume"]]
    return {"dates": dates, "candles": candles, "volumes": volumes,
            "区间": f"{dates[0]} ~ {dates[-1]}"}


# ---------------------------------------------------------------- 抓取任务

_OPERATION_LOCK = threading.Lock()
_ACTIVE_OPERATION: str | None = None
_SCHEDULE_ATTEMPT_ERROR: str | None = None


def _reserve_operation(kind: str) -> bool:
    global _ACTIVE_OPERATION
    with _OPERATION_LOCK:
        if _ACTIVE_OPERATION is not None:
            return False
        _ACTIVE_OPERATION = kind
        return True


def _release_operation(kind: str) -> None:
    global _ACTIVE_OPERATION
    with _OPERATION_LOCK:
        if _ACTIVE_OPERATION == kind:
            _ACTIVE_OPERATION = None


FETCH_MANAGER = None


def _scheduled_fetch() -> bool:
    global _SCHEDULE_ATTEMPT_ERROR
    result = start_fetch(FetchRequest())
    if not result.get("ok"):
        _SCHEDULE_ATTEMPT_ERROR = f"调度未启动：{result.get('message', '未知原因')}"
        return False
    _SCHEDULE_ATTEMPT_ERROR = None
    return True


class FetchRequest(BaseModel):
    mode: str = "auto"            # auto | fix | factors
    start: str | None = None
    end: str | None = None
    codes: str | None = None
    fields: str = "basic"
    force: bool = False
    sleep: float = 0.0
    max_tasks: int = 0            # 调试用：只跑前 N 个任务


class FetchScheduleRequest(BaseModel):
    enabled: bool
    interval_seconds: int = 86400


def _fetch_worker(req: FetchRequest, stop_event: threading.Event):
    _BAOSTOCK_LOCK.acquire()
    try:
        today = lake.today()
        args = SimpleNamespace(factors=(req.mode == "factors"),
                               start=req.start if req.mode == "fix" else None,
                               end=req.end or today, codes=req.codes, fields=req.fields,
                               force=req.force)
        tasks = lake.pending_tasks(lake.build_tasks(args), force=req.force)
        if req.max_tasks:
            tasks = tasks[:req.max_tasks]
        FETCH_MANAGER.update(total=len(tasks))
        if not tasks:
            FETCH_MANAGER.add_log("没有待办任务，数据已是最新。")
            return
        for t in tasks:
            if stop_event.is_set():
                FETCH_MANAGER.add_log("已被手动停止，进度已保存，可随时续传。")
                return
            FETCH_MANAGER.update(current=f"{t.code} [{t.tag}/{t.preset}] {t.start}~{t.end}")
            df = lake.bs_query(t.code, t)
            if df is None:
                FETCH_MANAGER.update(failed=FETCH_MANAGER.snapshot()["failed"] + [t.code])
            else:
                snapshot = FETCH_MANAGER.snapshot()
                FETCH_MANAGER.update(rows=snapshot["rows"] + lake.save_cache(t, df),
                                     empty=snapshot["empty"] + (len(df) == 0))
            snapshot = FETCH_MANAGER.snapshot()
            FETCH_MANAGER.update(done=snapshot["done"] + 1)
            if req.sleep:
                time.sleep(req.sleep)
        snapshot = FETCH_MANAGER.snapshot()
        FETCH_MANAGER.add_log(
            f'完成 {snapshot["done"]}，新增 {snapshot["rows"]:,} 行，空 {snapshot["empty"]}，'
            f'失败 {len(snapshot["failed"])}')
    except Exception as e:
        FETCH_MANAGER.add_log(f"抓取异常中断：{e}")
        raise
    finally:
        _BAOSTOCK_LOCK.release()


def _complete_fetch(req: FetchRequest, stop: threading.Event, error: str | None) -> None:
    snapshot = FETCH_MANAGER.snapshot()
    if error is None and snapshot["failed"]:
        error = f"抓取失败 {len(snapshot['failed'])} 个任务"
    FETCH_MANAGER.finish(lambda: _release_operation("fetch"), error)
    if error is None and not stop.is_set() and req.mode == "auto" and FETCH_MANAGER.schedule()["enabled"]:
        start_merge(False)


def start_fetch(req: FetchRequest):
    ok, message, stop = FETCH_MANAGER.begin(lambda: _reserve_operation("fetch"))
    if not ok:
        return {"ok": False, "message": message}

    def run():
        error = None
        try:
            _fetch_worker(req, stop)
        except Exception as exc:
            error = str(exc)
        finally:
            _complete_fetch(req, stop, error)

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def fetch_progress():
    return FETCH_MANAGER.snapshot()


def stop_fetch():
    ok, message = FETCH_MANAGER.stop()
    return {"ok": ok, **({} if ok else {"message": message})}


def fetch_schedule():
    return FETCH_MANAGER.schedule()


def update_fetch_schedule(req: FetchScheduleRequest):
    try:
        return {"ok": True, **FETCH_MANAGER.set_schedule(req.enabled, req.interval_seconds)}
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}


def fetch_continuous():
    coverage, latest_data_error = _latest_data_coverage()
    schedule = FETCH_MANAGER.schedule()
    return {**schedule, **coverage,
            "latest_data_error": latest_data_error,
            "schedule_error": schedule.get("schedule_error") or _SCHEDULE_ATTEMPT_ERROR}


def update_fetch_continuous(req: FetchScheduleRequest):
    return update_fetch_schedule(req)


# ---------------------------------------------------------------- doctor：一键修复

DOCTOR = {"running": False, "issue": "", "stage": "", "verdict": None,
          "verdicts": {}, "merge_output": "", "log": [], "finished_at": None}


class DoctorFixRequest(BaseModel):
    id: str                   # 问题 id，或 "all" = 统一修复全部可修复项
    max_tasks: int = 0      # 调试用：只跑前 N 个抓取任务


def _doctor_worker(items: list[dict], max_tasks: int):
    """统一修复流水线：逐项抓取（复用 JOB 进度）→ 一次合并写盘 → 复检验证。"""
    DOCTOR.update(running=True, issue="", stage="", verdict=None, verdicts={},
                  merge_output="", log=[], finished_at=None)
    try:
        for it in items:
            if it["fix"] == "merge_only":
                continue                          # 不抓取，合并阶段统一处理
            DOCTOR.update(issue=it["id"], stage="抓取中")
            args = lake._fix_args(it["fix"], max_tasks=max_tasks)
            req = FetchRequest(mode=("factors" if args.factors
                                     else "fix" if (args.start or args.codes)
                                     else "auto"),
                               start=args.start, end=args.end, codes=args.codes,
                               fields=args.fields, force=args.force,
                               max_tasks=max_tasks)
            stop = FETCH_MANAGER.begin_reserved()
            try:
                _fetch_worker(req, stop)           # 同步执行，进度在 JOB 里
            except Exception:
                pass
            finally:
                FETCH_MANAGER.finish(lambda: None)
            fetch = FETCH_MANAGER.snapshot()
            if fetch["failed"]:
                DOCTOR["log"].append(f'[{it["id"]}] 抓取失败 '
                                     f"{len(fetch['failed'])} 只：{fetch['failed'][:10]}")
            if fetch["status"] == "stopped" or fetch["done"] < fetch["total"]:
                DOCTOR["log"].append(f'[{it["id"]}] 已被手动停止，'
                                     "已抓进度保留，直接进入合并")

        DOCTOR["stage"] = "合并中"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = lake.cmd_merge(SimpleNamespace(dry_run=False))
        DOCTOR["merge_output"] = buf.getvalue()
        if code != 0:
            raise RuntimeError("合并校验未通过，已放弃写盘")

        DOCTOR["stage"] = "复检中"
        lake.load_profile(rescan=True)          # 合并已删快照，这里强制重建
        remaining = {i["id"] for i in lake.doctor_plan()}
        DOCTOR["verdicts"] = {it["id"]: ("仍存在" if it["id"] in remaining
                                         else "已修复") for it in items}
        DOCTOR["verdict"] = ("已修复" if all(v == "已修复"
                                             for v in DOCTOR["verdicts"].values())
                             else "部分仍存在")
        DOCTOR["stage"] = "完成"
    except Exception as e:
        DOCTOR["stage"] = "失败"
        DOCTOR["log"].append(str(e))
    finally:
        DOCTOR["running"] = False
        DOCTOR["finished_at"] = time.strftime("%H:%M:%S")


def doctor_plan():
    return {"items": lake.doctor_plan()}


def doctor_fix(req: DoctorFixRequest):
    if DOCTOR["running"] or FETCH_MANAGER.snapshot()["running"] or MERGE["running"]:
        return {"ok": False, "message": "已有任务进行中"}
    plan = lake.doctor_plan()
    if req.id == "all":
        items = [i for i in plan if i["可修复"]]
        if not items:
            return {"ok": False, "message": "没有可修复的问题"}
    else:
        item = next((i for i in plan if i["id"] == req.id), None)
        if not item:
            return {"ok": False, "message": "计划中没有该问题"}
        if not item["可修复"]:
            return {"ok": False, "message": item["说明"]}
        items = [item]
    if not _reserve_operation("doctor"):
        return {"ok": False, "message": "已有任务进行中"}
    def run():
        try:
            _doctor_worker(items, req.max_tasks)
        finally:
            _release_operation("doctor")
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def doctor_job():
    return {**DOCTOR, "fetch": FETCH_MANAGER.snapshot()}  # 抓取阶段进度复用 JOB


# ---------------------------------------------------------------- 合并

MERGE = {"running": False, "output": "", "finished_at": None}


def _merge_worker(dry_run: bool):
    MERGE.update(running=True, output="", finished_at=None)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = lake.cmd_merge(SimpleNamespace(dry_run=dry_run))
        MERGE["output"] = buf.getvalue() + f"\n退出码 {code}"
    except Exception as e:
        MERGE["output"] = buf.getvalue() + f"\n合并失败：{e}"
    finally:
        MERGE["running"] = False
        MERGE["finished_at"] = time.strftime("%H:%M:%S")


def start_merge(dry_run: bool = True):
    if not _reserve_operation("merge"):
        return {"ok": False, "message": "已有任务进行中"}
    if MERGE["running"]:
        _release_operation("merge")
        return {"ok": False, "message": "合并正在进行中"}
    def run():
        try:
            _merge_worker(dry_run)
        finally:
            _release_operation("merge")
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def merge_progress():
    return MERGE


class LakePort:
    """All lake operations share this one standard module port."""

    def __init__(self, data: DataPort) -> None:
        global FETCH_MANAGER
        self.data = data
        lake.bind(data)
        FETCH_MANAGER = FetchManager(data)

    def start(self) -> None:
        _warmup()
        FETCH_MANAGER.start_scheduler(_scheduled_fetch)

    def close(self) -> None:
        FETCH_MANAGER.close()

    def handle(self, operation: str, data):
        if not isinstance(data, dict):
            raise TypeError("operation data must be an object")
        handlers = {
            "lake.researchDaily": lambda: research_daily(self.data, data.get("asOf")),
            "lake.researchInputs": lambda: research_inputs(self.data),
            "lake.closedLoop": lambda: closed_loop_report(self.data),
            "lake.status": lambda: get_status(bool(data.get("rescan", False))),
            "lake.query": lambda: query_stock(data["q"]),
            "lake.kline": lambda: kline(
                data["code"], data.get("freq", "日"), data.get("adj", "不复权"),
                data.get("start"), data.get("end"),
            ),
            "lake.startFetch": lambda: start_fetch(FetchRequest.model_validate(data)),
            "lake.fetchJob": fetch_progress,
            "lake.stopFetch": stop_fetch,
            "lake.continuousFetch": fetch_continuous,
            "lake.setContinuousFetch": lambda: update_fetch_continuous(
                FetchScheduleRequest(enabled=data["enabled"])
            ),
            "lake.startMerge": lambda: start_merge(bool(data["dryRun"])),
            "lake.mergeJob": merge_progress,
            "lake.doctor": doctor_plan,
            "lake.doctorFix": lambda: doctor_fix(DoctorFixRequest.model_validate(data)),
            "lake.doctorJob": doctor_job,
        }
        try:
            handler = handlers[operation]
        except KeyError as error:
            raise ValueError(f"unsupported lake operation: {operation}") from error
        return handler()


__all__ = ["LakePort"]
