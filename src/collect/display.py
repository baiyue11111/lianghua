"""Collection interaction: manual requests, schedules, progress, evidence and reviews."""
from __future__ import annotations
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .core import SyncRequest, ReviewRequest, ScheduleRequest, SyncService, Scheduler

class CollectPort:
    def __init__(self, sources, store, providers):
        self.store = store
        self.sources = {item['id']: item for item in sources}
        if len(self.sources) != len(sources): raise ValueError('duplicate source identity')
        if any(not name.replace('_', '').replace('-', '').isalnum() for name in self.sources): raise ValueError('invalid source identity')
        self.service = SyncService(store, providers)
        self.scheduler = Scheduler(self.service, self._dispatch)
        self._threads = []
        self._lock = threading.Lock()
        self._closed = False

    def start(self): self.scheduler.start()

    def wait(self):
        with self._lock: threads = tuple(self._threads)
        for thread in threads:
            thread.join(timeout=30)
            if thread.is_alive(): raise TimeoutError('collection has not stopped')

    def close(self):
        self._closed = True
        self.scheduler.close()
        self.wait()

    def handle(self, operation, data):
        if self._closed: raise RuntimeError('collection port is closed')
        if not isinstance(data, dict): raise TypeError('operation data must be an object')
        handlers = {
            'workbench.sources': self._sources, 'workbench.sync': self._sync,
            'workbench.schedule': self._schedule, 'workbench.job': self._job,
            'workbench.snapshot': self._snapshot, 'workbench.evidence': self._evidence,
            'workbench.failedEvidence': self._failed_evidence,
            'workbench.reviews': self._reviews, 'workbench.review': self._review,
        }
        if operation not in handlers: raise ValueError(f'unsupported collection operation: {operation}')
        return handlers[operation](data)

    def _sources(self, _):
        result = []
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        for key, source in self.sources.items():
            pointer = self.store.read("source_jobs", key)
            job = self.store.read("jobs", pointer["job_id"]) if pointer else None
            error = None
            snapshot = None
            try:
                snapshot = self.store.latest(key)
            except (ValueError, KeyError) as exc:
                error = str(exc)
            defaults = dict(source["defaults"])
            days = defaults.pop("lookback_days", 1)
            defaults.update(
                start=(today - timedelta(days=days)).isoformat(), end=today.isoformat()
            )
            result.append({
                "id": key, "name": source["name"], "frequency": source["frequency"],
                "defaults": defaults, "job": job,
                "schedule": self.store.read("schedules", key), "integrity_error": error,
                "snapshot": None if snapshot is None else {
                    name: snapshot[name]
                    for name in ("snapshot_id", "collected_at", "data_as_of", "scope")
                },
                "records": len(snapshot["records"]) if snapshot else 0,
            })
        return {"items": result}

    def _sync(self, value):
        request = SyncRequest.model_validate(value["input"]).model_dump(mode="json", exclude_unset=True)
        job = self.service.begin(value["id"], request)
        self._dispatch(job)
        return job

    def _dispatch(self, job):
        thread = threading.Thread(target=self.service.execute, args=(job.copy(),), daemon=True)
        with self._lock:
            self._threads = [item for item in self._threads if item.is_alive()]
            self._threads.append(thread)
            thread.start()

    def _schedule(self, value):
        request = ScheduleRequest.model_validate({
            "enabled": value["enabled"],
            "interval_seconds": value["interval_seconds"],
            "request": value["input"],
        })
        return self.scheduler.configure(
            value["id"], request.enabled, request.interval_seconds,
            request.request.model_dump(mode="json", exclude_unset=True),
        )

    def _job(self, value):
        identity = value["id"]
        if not identity.isalnum():
            raise KeyError("job not found")
        result = self.store.read("jobs", identity)
        if result is None:
            raise KeyError("job not found")
        return result

    def _snapshot(self, value):
        snapshot = self.store.get_snapshot(value["id"])
        offset = value.get("offset", 0)
        limit = value.get("limit", 50)
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 1000:
            raise ValueError('invalid snapshot page')
        return {
            **{key: item for key, item in snapshot.items() if key != "raw"},
            "records": snapshot["records"][offset:offset + limit],
            "offset": offset, "limit": limit, "total": len(snapshot["records"]),
        }

    def _evidence(self, value):
        snapshot = self.store.get_snapshot(value["id"])
        index = value["index"]
        if not 0 <= index < len(snapshot["records"]):
            raise KeyError("record not found")
        return {
            "snapshot_id": value["id"], "collected_at": snapshot["collected_at"],
            "request": snapshot["request"], "record_index": index,
            "record": snapshot["records"][index], "raw": snapshot["raw"],
            "scope": snapshot["scope"],
            "reviews": [
                row for row in self.store.reviews()
                if row["snapshot_id"] == value["id"] and row["record_index"] == index
            ],
        }

    def _failed_evidence(self, value):
        result = self.store.read("failed_evidence", value["id"])
        if result is None:
            raise KeyError("failed raw evidence not present")
        return result

    def _reviews(self, _):
        return self.store.reviews()

    def _review(self, value):
        request = ReviewRequest.model_validate(value)
        return self.store.review(**request.model_dump())
