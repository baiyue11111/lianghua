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
        self._job_sources = {}
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
        self._job_sources[job['job_id']] = deepcopy(source)
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
                self.methods.publish(source, packet)
                view = self.methods.records(source, 0, 0)
                if view is None:
                    raise RuntimeError("published source has no current records")
                job["snapshot_id"] = view["snapshot_id"]
                job["records"] = view["records_count"]
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
        self._job_sources[job['job_id']] = deepcopy(source)
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
        for ref in self.data.list("state", "collect", "jobs"):
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
            job = self.status(job_id)
            source = self._job_sources[job_id]
            stats = self.methods.prepare(source, packet)
            if dry_run:
                job.update(status="validated", phase="validate", merge=stats)
            else:
                self.methods.publish(source, packet)
                job.update(status="succeeded", phase="publish", merge=stats)
                view = self.methods.records(source, 0, 0)
                if view is None:
                    raise RuntimeError("published source has no current records")
                job["snapshot_id"] = view["snapshot_id"]
                job["records"] = view['records_count']
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
        for ref in self.data.list("state", "collect", "schedules"):
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
        if operation == "collection.sources":
            return [self._source_view(source) for source in self.sources.list()]
        if operation == "collection.sync":
            request = dict(value.get("input", {}))
            request["source_id"] = value.get("id")
            return self.run(request)
        if operation == "collection.schedule":
            source_id = value["id"]
            self.sources.get(source_id)
            return self.schedule(source_id, value)
        if operation == "collection.job":
            return self.status(value["id"])
        if operation in {"collection.snapshot", "collection.evidence"}:
            source = self.sources.get(value["id"])
            evidence = operation == "collection.evidence"
            offset = value.get("index") if evidence else value.get("offset", 0)
            limit = 1 if evidence else value.get("limit", 50)
            scope = {name: value[name] for name in ("start", "end", "symbols", "keyword", "fields") if name in value}
            page = self.methods.records(source, offset, limit, **scope)
            if page is None:
                raise KeyError(source["id"])
            if not evidence:
                return {key: item for key, item in page.items() if key != "raw"}
            if not page["records"]:
                raise ValueError("invalid evidence index")
            return {"snapshot_id": page["snapshot_id"], "collected_at": page.get("collected_at"),
                    "request": page.get("request", {}), "record_index": offset,
                    "record": page["records"][0], "raw": page.get("raw", []),
                    "scope": page.get("scope", {}), "reviews": page.get("reviews", [])}
        if operation == "collection.start":
            return self._fetch_only(value)
        if operation == "collection.current":
            if self._current_job_id is None:
                return {"running": False, "status": "idle", "done": 0, "total": 0,
                        "rows": 0, "empty": 0, "failed": [], "current": "",
                        "finished_at": None, "error": None, "log": []}
            return self._fetch_view(self.status(self._current_job_id))
        if operation == "collection.stop":
            if self._current_job_id is None:
                return {"ok": False, "message": "no active collection task"}
            job = self.status(self._current_job_id)
            if job.get("status") != "running":
                return {"ok": False, "message": "collection task is not running", **self._fetch_view(job)}
            return {"ok": True, **self._fetch_view(self.cancel(self._current_job_id))}
        if operation == "collection.automatic":
            return self._continuous_fetch()
        if operation == "collection.set_automatic":
            source = self._source({"mode": "auto"})
            self.schedule(source["id"], {"enabled": value["enabled"],
                                          "interval_seconds": value.get("interval_seconds", 86400),
                                          "input": source.get("defaults", {})})
            return self._continuous_fetch()
        if operation == "collection.merge":
            return self._merge_current(value["dryRun"])
        if operation == "collection.merge_job":
            return dict(self._merge_state)
        if operation == "collection.repair":
            return self._doctor_fix(value["id"], value.get("max_tasks", 0))
        if operation == "collection.repair_job":
            return self._doctor_job()
        raise ValueError(f"unsupported collection operation: {operation}")



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
        return self.data.doctor_report()["items"]



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


    def _schedule_list(self):
        return {"items": [json.loads(self.data.read("state", "collect", ref.name))
                           for ref in self.data.list("state", "collect", "schedules")]}


    def _source_view(self, source):
        source_id = source["id"]
        job = None
        for ref in self.data.list("state", "collect", "jobs"):
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
            snapshot = self.methods.records(source, 0, 0)
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
