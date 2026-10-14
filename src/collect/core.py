"""Collection inputs, task state and scheduling; no provider or file I/O."""
from __future__ import annotations
import threading
import time
import uuid
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
class Collect:
    """One execution engine for manual and scheduled source collection."""

    def __init__(self, sources, methods):
        self.sources = sources
        self.methods = methods
        self.data = methods.data
        self._lock = threading.RLock()
        self._jobs = {}
        self._active = set()
        self._cancelled = set()
        self._packets = {}
        self._current_job_id = None
        self._fetch_thread = None
        self._merge_thread = None
        self._merge_state = {"running": False, "output": "", "error": None,
                             "started_at": None, "finished_at": None}
        self._recover_tasks()
        self._closed = False
        self._stop = threading.Event()
        self._thread = None

    def _save(self, job):
        self.data.write(
            "state", "collect", f"jobs/{job['job_id']}.json",
            json.dumps(job, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode(),
        )

    def run(self, request):
        if self._closed:
            raise RuntimeError("collection is closed")
        source = self._source(request)
        source_id = source["id"]
        request = self.methods.prepare_request(source, request)
        request["source_id"] = source_id
        with self._lock:
            if source_id in self._active:
                raise RuntimeError("source collection already running")
            self._active.add(source_id)
        job = {"job_id": uuid.uuid4().hex, "source_id": source_id,
               "request": dict(request), "status": "running", "error": None,
               "started_at": datetime.now().isoformat(), "finished_at": None,
               "snapshot_id": None, "records": 0}
        self._jobs[job["job_id"]] = job
        saved = False
        try:
            self._save(job)
            saved = True
            coverage = self.methods.coverage(source, request)
            if coverage is not None and coverage.get("complete") and not request.get("force"):
                job.update(status="succeeded", records=0)
            else:
                if coverage is not None and not request.get("force"):
                    request = self.methods.missing_request(request, coverage)
                    job["request"] = dict(request)
                packet = self.methods.fetch(source, request)
                self.methods.validate(packet)
                self._packets[job["job_id"]] = packet
                self._current_job_id = job["job_id"]
                if job["job_id"] in self._cancelled:
                    raise RuntimeError("collection cancelled before publish")
                published = self.methods.publish(packet)
                if source.get("data_type") == "daily":
                    view = self.data.daily_collection_view(source_id, source["dataset_id"])
                    job["snapshot_id"] = view["snapshot_id"]
                    job["records"] = view["records_count"]
                else:
                    snapshot = self.data.latest_collection(source_id)
                    if snapshot is None:
                        raise RuntimeError("published collection has no current snapshot")
                    job["snapshot_id"] = snapshot["snapshot_id"]
                    job["records"] = len(packet["records"])
                job["status"] = "succeeded"
        except Exception as error:
            job.update(status="failed", error=f"{type(error).__name__}: {error}")
        finally:
            try:
                job["finished_at"] = datetime.now().isoformat()
                try:
                    self.methods.log({"job_id": job["job_id"], "source_id": source_id,
                                      "status": job["status"], "error": job["error"],
                                      "records": job["records"]})
                except Exception as error:
                    job["log_error"] = f"{type(error).__name__}: {error}"
                if saved:
                    try:
                        self._save(job)
                    except Exception as error:
                        job["save_error"] = f"{type(error).__name__}: {error}"
            finally:
                with self._lock:
                    self._active.discard(source_id)
                    self._cancelled.discard(job["job_id"])
        return dict(job)

    def _source(self, request):
        source_id = request.get("source_id")
        if source_id is not None:
            return self.sources.get(source_id)
        mode = request.get("mode", "auto")
        data_type = request.get("data_type")
        if data_type is None:
            matches = [source for source in self.sources.list()
                       if source.get("default") is True and
                       mode in source.get("modes", ())]
            if len(matches) != 1:
                raise ValueError(f"expected one default source for mode {mode}, found {len(matches)}")
            return matches[0]
        return self.sources.select(mode=mode, data_type=data_type)

    def _fetch_only(self, request):
        source = self._source(request)
        source_id = source["id"]
        request = self.methods.prepare_request(source, request)
        request["source_id"] = source_id
        with self._lock:
            if source_id in self._active:
                raise RuntimeError("source collection already running")
            self._active.add(source_id)
        job = {"job_id": uuid.uuid4().hex, "source_id": source_id,
               "request": dict(request), "status": "running", "phase": "fetch",
               "error": None, "started_at": datetime.now().isoformat(),
               "finished_at": None, "snapshot_id": None, "records": 0}
        self._jobs[job["job_id"]] = job
        self._current_job_id = job["job_id"]
        self._save(job)
        self._fetch_thread = threading.Thread(
            target=self._fetch_task, args=(source, request, job), daemon=True)
        self._fetch_thread.start()
        return {"ok": True}

    def _fetch_task(self, source, request, job):
        source_id = source["id"]
        try:
            packet = self.methods.fetch(source, request)
            self.methods.validate(packet)
            if job["job_id"] in self._cancelled:
                job.update(status="stopped", phase="fetch", error="collection stopped")
            else:
                self._packets[job["job_id"]] = packet
                job.update(status="fetched", phase="validate", records=len(packet["records"]))
        except Exception as error:
            job.update(status="failed", phase="fetch", error=f"{type(error).__name__}: {error}")
        finally:
            job["finished_at"] = datetime.now().isoformat()
            self._save(job)
            with self._lock:
                self._active.discard(source_id)

    def _recover_tasks(self):
        for ref in self.data.list("state", "collect"):
            if not ref.name.startswith("jobs/"):
                continue
            try:
                job = json.loads(self.data.read("state", "collect", ref.name))
            except Exception:
                continue
            if job.get("status") not in {"running", "fetched"}:
                self._jobs[job.get("job_id", ref.name)] = job
                continue
            job.update(status="failed", error="collection interrupted before publish",
                       finished_at=datetime.now().isoformat())
            self._save(job)
            self._jobs[job.get("job_id", ref.name)] = job

    @staticmethod
    def _fetch_view(job):
        return {"ok": job["status"] == "fetched", "status": job["status"],
                "job_id": job["job_id"], "done": 1 if job["status"] == "fetched" else 0,
                "total": 1, "rows": job.get("records", 0), "empty": 0,
                "failed": [] if job["status"] == "fetched" else [job["source_id"]],
                "current": job.get("source_id", ""), "finished_at": job.get("finished_at"),
                "error": job.get("error"), "log": []}

    def _merge_task(self, job_id, dry_run):
        try:
            packet = self._packets[job_id]
            stats = self.methods.prepare_publish(packet)
            job = self.status(job_id)
            if dry_run:
                job.update(status="validated", phase="validate", merge=stats)
            else:
                published = self.methods.publish(packet)
                job.update(status="succeeded", phase="publish", merge=stats)
                if packet.get("data_type") == "daily":
                    view = self.data.daily_collection_view(job["source_id"], packet["dataset_id"])
                    job["snapshot_id"] = view["snapshot_id"]
                    job["records"] = view["records_count"]
                else:
                    snapshot = self.data.latest_collection(job["source_id"])
                    if snapshot is None:
                        raise RuntimeError("published collection has no current snapshot")
                    job["snapshot_id"] = snapshot["snapshot_id"]
                self._packets.pop(job_id, None)
            job["finished_at"] = datetime.now().isoformat()
            self._save(job)
            self._merge_state.update(running=False, output=json.dumps(stats, ensure_ascii=False),
                                     error=None, finished_at=job["finished_at"])
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            job = self.status(job_id)
            job.update(status="failed", phase="publish", error=message,
                       finished_at=datetime.now().isoformat())
            self._save(job)
            self._merge_state.update(running=False, output="", error=message,
                                     finished_at=job["finished_at"])

    def _merge_current(self, dry_run):
        job_id = self._current_job_id
        if not job_id or job_id not in self._packets:
            raise RuntimeError("no fetched collection task")
        if self._merge_state["running"]:
            raise RuntimeError("merge already running")
        self._merge_state = {"running": True, "output": "", "error": None,
                             "started_at": datetime.now().isoformat(), "finished_at": None}
        self._merge_thread = threading.Thread(target=self._merge_task,
                                              args=(job_id, bool(dry_run)), daemon=True)
        self._merge_thread.start()
        return {"ok": True, "status": "running", "job_id": job_id,
                "dryRun": bool(dry_run), "error": None}

    def schedule(self, source_id, settings):
        source = self.sources.get(source_id)
        value = dict(settings)
        request = value.pop("input", value.pop("request", {}))
        if not isinstance(request, dict):
            raise TypeError("schedule request must be an object")
        enabled = bool(value.get("enabled", False))
        interval = value.get("interval_seconds", 60)
        if type(interval) is not int or interval < 30:
            raise ValueError("interval must be at least 30 seconds")
        if "start" in request and "end" in request:
            try:
                window_days = max(0, (date.fromisoformat(request["end"]) -
                                      date.fromisoformat(request["start"])).days)
            except (TypeError, ValueError) as error:
                raise ValueError("schedule request must contain valid start and end") from error
        else:
            window_days = value.get("window_days", 0)
            if type(window_days) is not int or window_days < 0:
                raise ValueError("window_days must be non-negative")
        now = time.time()
        value = {
            "source_id": source["id"], "enabled": enabled,
            "interval_seconds": interval, "request": deepcopy(request),
            "window_days": window_days,
            "next_at": now + interval if enabled else None,
            "error": None,
        }
        self.data.write(
            "state", "collect", f"schedules/{source_id}.json",
            json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode(),
        )
        return value

    def cancel(self, job_id):
        job = self.status(job_id)
        if job["status"] == "running":
            self._cancelled.add(job_id)
        return dict(job)

    def status(self, job_id):
        if job_id in self._jobs:
            return dict(self._jobs[job_id])
        try:
            value = json.loads(self.data.read("state", "collect", f"jobs/{job_id}.json"))
        except Exception as error:
            raise KeyError(job_id) from error
        self._jobs[job_id] = value
        return dict(value)

    def close(self):
        self._closed = True
        self._stop.set()
        self._cancelled.update(self._jobs)
        if self._fetch_thread is not None:
            self._fetch_thread.join(timeout=5)
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("collection scheduler did not stop")
        deadline = time.monotonic() + 5
        while True:
            with self._lock:
                if not self._active:
                    return
            if time.monotonic() >= deadline:
                raise RuntimeError("collection jobs did not stop")
            time.sleep(0.01)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("collection scheduler already started")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(1):
            self.tick()

    def tick(self, at=None):
        now = time.time() if at is None else at
        for ref in self.data.list("state", "collect"):
            if not ref.name.startswith("schedules/"):
                continue
            schedule = json.loads(self.data.read("state", "collect", ref.name))
            next_at = schedule.get("next_at")
            if (not schedule.get("enabled") or next_at is None or
                    next_at > now):
                continue
            request = dict(schedule.get("request", {}))
            if schedule.get("window_days", 0) and "start" in request and "end" in request:
                today = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date()
                request["start"] = (today - timedelta(days=schedule["window_days"])).isoformat()
                request["end"] = today.isoformat()
            request["source_id"] = schedule["source_id"]
            result = self.run(request)
            schedule["last_job_id"] = result["job_id"]
            schedule["next_at"] = now + int(schedule.get("interval_seconds", 60))
            self.data.write("state", "collect", ref.name,
                            json.dumps(schedule, sort_keys=True).encode())

    def handle(self, operation, value):
        value = value if isinstance(value, dict) else {}
        if operation == "workbench.sources":
            return [self._source_view(source) for source in self.sources.list()]
        if operation == "workbench.sync":
            request = dict(value.get("input", {}))
            request["source_id"] = value.get("id")
            return self.run(request)
        if operation == "workbench.schedule":
            source_id = value["id"]
            self.sources.get(source_id)
            return self.schedule(source_id, value)
        if operation == "workbench.job":
            return self.status(value["id"])
        if operation == "workbench.snapshot":
            source_id = value["id"]
            source = self.sources.get(source_id)
            if source.get("data_type") == "daily":
                snapshot = self.data.daily_collection_page(
                    source_id, source["dataset_id"], offset=value.get("offset", 0),
                    limit=value.get("limit", 50), start=value.get("start"),
                    end=value.get("end"), symbols=value.get("symbols", value.get("codes")))
            else:
                snapshot = self.data.latest_collection(source_id)
            if snapshot is None:
                raise KeyError(source_id)
            offset = value.get("offset", 0); limit = value.get("limit", 50)
            if (source.get("data_type") != "daily" and
                    (type(offset) is not int or type(limit) is not int or
                     offset < 0 or not 1 <= limit <= 1000)):
                raise ValueError("invalid snapshot page")
            if source.get("data_type") == "daily":
                return snapshot
            return {
                **{key: item for key, item in snapshot.items() if key != "raw"},
                "records": snapshot.get("records", [])[offset:offset + limit],
                "offset": offset, "limit": limit,
                "total": len(snapshot.get("records", [])),
            }
        if operation == "workbench.evidence":
            source_id = value["id"]
            source = self.sources.get(source_id)
            if source.get("data_type") == "daily":
                index = value.get("index")
                if type(index) is not int or index < 0:
                    raise ValueError("invalid evidence index")
                snapshot = self.data.daily_collection_page(
                    source_id, source["dataset_id"], offset=index, limit=1,
                    start=value.get("start"), end=value.get("end"),
                    symbols=value.get("symbols", value.get("codes")))
            else:
                snapshot = self.data.latest_collection(source_id)
            if snapshot is None:
                raise KeyError(source_id)
            index = value.get("index")
            record_index = 0 if source.get("data_type") == "daily" else index
            if (type(index) is not int or index < 0 or
                    record_index >= len(snapshot.get("records", []))):
                raise ValueError("invalid evidence index")
            return {
                "snapshot_id": snapshot["snapshot_id"],
                "collected_at": snapshot.get("collected_at"),
                "request": snapshot.get("request", {}),
                "record_index": index,
                "record": snapshot["records"][record_index],
                "raw": snapshot.get("raw", []),
                "scope": snapshot.get("scope", {}),
                "reviews": snapshot.get("reviews", []),
            }
        if operation == "lake.researchInputs":
            return self._research_inputs()
        if operation == "lake.status":
            return self._profile()
        if operation == "lake.query":
            return self._lake_search(value["q"])
        if operation == "lake.kline":
            return self._lake_kline(value)
        if operation == "lake.startFetch":
            return self._fetch_only(value)
        if operation == "lake.fetchJob":
            if self._current_job_id is None:
                return {"running": False, "status": "idle", "done": 0, "total": 0,
                        "rows": 0, "empty": 0, "failed": [], "current": "",
                        "finished_at": None, "error": None, "log": []}
            return self._fetch_view(self.status(self._current_job_id))
        if operation == "lake.stopFetch":
            if self._current_job_id is None:
                return {"ok": False, "message": "no active collection task"}
            job = self.status(self._current_job_id)
            if job.get("status") != "running":
                return {"ok": False, "message": "collection task is not running", **self._fetch_view(job)}
            return {"ok": True, **self._fetch_view(self.cancel(self._current_job_id))}
        if operation == "lake.continuousFetch":
            return self._continuous_fetch()
        if operation == "lake.setContinuousFetch":
            source = self._source({"mode": "auto"})
            self.schedule(source["id"], {"enabled": value["enabled"],
                                          "interval_seconds": value.get("interval_seconds", 86400),
                                          "input": source.get("defaults", {})})
            return self._continuous_fetch()
        if operation == "lake.startMerge":
            return self._merge_current(value["dryRun"])
        if operation == "lake.mergeJob":
            return dict(self._merge_state)
        if operation == "lake.doctor":
            return self._lake_doctor()
        if operation == "lake.doctorFix":
            return self._doctor_fix(value["id"], value.get("max_tasks", 0))
        if operation == "lake.doctorJob":
            return self._doctor_job()
        raise ValueError(f"unsupported collection operation: {operation}")

    def _lake_query(self, value):
        from protocal import DataQuery
        dataset = value.get("dataset_id", value.get("dataset"))
        fields = tuple(value.get("fields") or ("open", "high", "low", "close", "volume", "amount"))
        symbols = value.get("symbols", value.get("codes"))
        query = DataQuery(dataset, date.fromisoformat(value["start"]),
                          date.fromisoformat(value["end"]), fields,
                          int(value.get("history", 1)), value.get("price_basis", "raw"),
                          symbols=None if symbols is None else tuple(symbols),
                          universe=value.get("universe", "dataset"))
        result = self.data.query(query)
        rows = []
        for record in result.records:
            for symbol, values in record.frame.fields.items():
                rows.append({"code": symbol, "date": record.trade_date.isoformat(), **values})
        return {"items": rows}

    def _lake_search(self, query):
        return self.data.search(query)

    def _doctor_fix(self, identity, max_tasks):
        plan = self._doctor_plan()
        item = next((row for row in plan if row.get("id") == identity), None)
        if item is None:
            raise KeyError(identity)
        if not item.get("fix"):
            raise ValueError("doctor item is not repairable")
        request = dict(item["fix"] if isinstance(item["fix"], dict) else {})
        if max_tasks:
            request["max_tasks"] = max_tasks
        return self._fetch_only(request)

    def _doctor_plan(self):
        return self._lake_doctor()["items"]

    def _profile(self):
        return self.data.profile_report()

    def _research_inputs(self):
        return self.data.research_report()

    def _continuous_fetch(self):
        schedules = self._schedule_list()["items"]
        if len(schedules) > 1:
            raise ValueError("continuous fetch requires one explicit default source")
        schedule = schedules[0] if schedules else {
            "enabled": False, "interval_seconds": 86400, "next_at": None,
            "error": None,
        }
        return {
            "enabled": schedule.get("enabled", False),
            "interval_seconds": schedule.get("interval_seconds", 86400),
            "last_run_at": schedule.get("last_run_at"),
            "next_run_at": schedule.get("next_at"),
            "latest_data_date": None, "latest_data_stock_count": None,
            "tracked_stock_count": None, "median_stock_date": None,
            "schedule_error": schedule.get("error"), "latest_data_error": None,
        }

    def _doctor_job(self):
        if self._current_job_id is None:
            return {"running": False, "issue": "", "stage": "", "verdict": None,
                    "verdicts": {}, "merge_output": "", "log": [], "finished_at": None,
                    "fetch": self._fetch_view({"status": "idle", "job_id": "", "source_id": "",
                                               "finished_at": None, "records": 0, "error": None})}
        return {"running": False, "issue": "", "stage": "publish",
                "verdict": self.status(self._current_job_id)["status"],
                "verdicts": {}, "merge_output": "", "log": [],
                "finished_at": self.status(self._current_job_id).get("finished_at"),
                "fetch": self._fetch_view(self.status(self._current_job_id))}

    def _lake_kline(self, value):
        frequencies = {"分时", "日", "周", "月", "年"}
        adjustments = {"不复权", "前复权", "后复权"}
        freq, adj = value.get("freq", "日"), value.get("adj", "不复权")
        if freq not in frequencies or adj not in adjustments:
            raise ValueError("非法参数")
        if freq == "分时":
            source = self._source({"mode": "kline", "data_type": "minute"})
            if source.get("frequency") not in {"minute", "intraday"}:
                raise ValueError("分时源频率无效")
            raise ValueError("分时源尚未提供标准查询字段")
        source = self._source({"mode": "kline", "data_type": "daily"})
        dataset = source.get("dataset_id")
        calendar_name = source.get("calendar")
        if not isinstance(dataset, str) or not isinstance(calendar_name, str):
            raise ValueError("daily kline source must declare dataset_id and calendar")
        calendar = json.loads(self.data.read("sources", dataset, calendar_name))
        sessions = [date.fromisoformat(str(item)) for item in calendar.get("sessions", ())]
        if len(sessions) < 2:
            raise ValueError("daily kline calendar is missing")
        value = {**value, "dataset": dataset, "symbols": [value["code"]],
                 "start": value.get("start", sessions[0].isoformat()),
                 "end": value.get("end", sessions[-2].isoformat())}
        value["price_basis"] = "raw" if adj == "不复权" else "adjusted"
        result = self._lake_query(value)
        rows = result["items"]
        rows.sort(key=lambda row: row["date"])
        if freq != "日":
            groups = {}
            for row in rows:
                day = date.fromisoformat(row["date"])
                if freq == "周":
                    key = day.isocalendar()[:2]
                elif freq == "月":
                    key = (day.year, day.month)
                else:
                    key = (day.year,)
                groups.setdefault(key, []).append(row)
            rows = []
            for values in groups.values():
                values.sort(key=lambda item: item["date"])
                first, last = values[0], values[-1]
                rows.append({"date": last["date"],
                             "open": first.get("open"),
                             "high": max(item["high"] for item in values),
                             "low": min(item["low"] for item in values),
                             "close": last.get("close"),
                             "volume": sum(item.get("volume") or 0 for item in values),
                             "amount": sum(item.get("amount") or 0 for item in values)})
        return {
            "dates": [row["date"] for row in rows],
            "candles": [[row.get("open"), row.get("close"), row.get("low"), row.get("high")]
                        for row in rows],
            "volumes": [row.get("volume") for row in rows],
        }

    def _schedule_list(self):
        return {"items": [json.loads(self.data.read("state", "collect", ref.name))
                           for ref in self.data.list("state", "collect")
                           if ref.name.startswith("schedules/")]}

    def _lake_doctor(self):
        return self.data.doctor_report()

    def _source_view(self, source):
        source_id = source["id"]
        job = None
        for ref in self.data.list("state", "collect"):
            if not ref.name.startswith("jobs/"):
                continue
            item = json.loads(self.data.read("state", "collect", ref.name))
            if item.get("source_id") == source_id:
                if job is None or item.get("finished_at", "") > job.get("finished_at", ""):
                    job = item
        try:
            schedule = json.loads(self.data.read(
                "state", "collect", f"schedules/{source_id}.json"))
        except Exception:
            schedule = None
        try:
            if source.get("data_type") == "daily":
                snapshot = self.data.daily_collection_summary(source_id, source["dataset_id"])
            else:
                snapshot = self.data.latest_collection(source_id)
            summary = None if snapshot is None else {
                key: snapshot.get(key) for key in
                ("snapshot_id", "collected_at", "data_as_of", "scope")
            }
            records = 0 if snapshot is None else snapshot.get("records_count", len(snapshot.get("records", [])))
            integrity_error = None
        except Exception as error:
            summary, records, integrity_error = None, 0, str(error)
        return {
            "id": source_id, "name": source["name"], "frequency": source["frequency"],
            "defaults": deepcopy(source["defaults"]), "job": job,
            "schedule": schedule, "integrity_error": integrity_error,
            "snapshot": summary, "records": records,
        }
