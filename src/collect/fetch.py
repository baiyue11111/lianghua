"""Thread-safe lifecycle and persistent scheduling for data-lake fetches."""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable


class FetchManager:
    def __init__(self, data):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._scheduler_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._scheduled_callback: Callable[[], bool] = lambda: False
        self.data = data
        self._schedule = self._load_schedule()
        self.state = self._new_state()

    @staticmethod
    def _new_state() -> dict:
        return {"running": False, "status": "idle", "done": 0, "total": 0,
                "rows": 0, "empty": 0, "failed": [], "current": "",
                "finished_at": None, "error": None, "log": []}

    def snapshot(self) -> dict:
        with self._lock:
            out = dict(self.state)
            out["failed"] = list(self.state["failed"])
            out["log"] = list(self.state["log"])
            return out

    def update(self, **values) -> None:
        with self._lock:
            self.state.update(values)

    def add_log(self, message: str) -> None:
        with self._lock:
            self.state["log"].append(message)

    def begin(self, reserve: Callable[[], bool]) -> tuple[bool, str | None, threading.Event | None]:
        with self._lock:
            if self.state["running"]:
                return False, "已有抓取任务进行中", None
            if not reserve():
                return False, "已有任务进行中", None
            self._stop = threading.Event()
            self.state = self._new_state()
            self.state.update(running=True, status="running")
            return True, None, self._stop

    def run_sync(self, worker: Callable[[threading.Event], None], release: Callable[[], None]) -> None:
        with self._lock:
            self._stop = threading.Event()
            self.state = self._new_state()
            self.state.update(running=True, status="running")
            stop = self._stop
        try:
            worker(stop)
        finally:
            self.finish(release)

    def begin_reserved(self) -> threading.Event:
        """Begin a fetch nested inside another reserved operation (doctor)."""
        with self._lock:
            self._stop = threading.Event()
            self.state = self._new_state()
            self.state.update(running=True, status="running")
            return self._stop

    def finish(self, release: Callable[[], None], error: str | None = None) -> None:
        with self._lock:
            stopped = self._stop.is_set()
            self.state.update(running=False,
                              status="stopped" if stopped else ("failed" if error else "succeeded"),
                              error=error, finished_at=time.strftime("%H:%M:%S"))
        release()

    def stop(self) -> tuple[bool, str | None]:
        with self._lock:
            if not self.state["running"]:
                return False, "没有正在运行的抓取任务"
            self._stop.set()
            self.state.update(status="stopping")
            return True, None

    def schedule(self) -> dict:
        with self._lock:
            return dict(self._schedule)

    def set_schedule(self, enabled: bool, interval_seconds: int) -> dict:
        if interval_seconds < 60:
            raise ValueError("interval_seconds 必须不小于 60")
        previous = self.schedule()
        value = {"enabled": bool(enabled), "interval_seconds": int(interval_seconds),
                 "last_run_at": previous.get("last_run_at"), "next_run_at": None,
                 "schedule_error": None}
        if enabled:
            value["next_run_at"] = (datetime.now() + timedelta(seconds=interval_seconds)).isoformat(timespec="seconds")
        with self._lock:
            self._schedule = value
            self._persist_schedule()
            self._scheduler_stop.set()
            if enabled:
                self._scheduler_stop = threading.Event()
                stop_event = self._scheduler_stop
                self._scheduler_thread = threading.Thread(
                    target=self._scheduler_loop, args=(stop_event,), daemon=True)
                self._scheduler_thread.start()
        return dict(value)

    def start_scheduler(self, callback: Callable[[], bool]) -> None:
        self._scheduled_callback = callback
        self._scheduler_stop.set()
        if self._schedule["enabled"]:
            self._scheduler_stop = threading.Event()
            stop_event = self._scheduler_stop
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop, args=(stop_event,), daemon=True)
            self._scheduler_thread.start()

    def close(self) -> None:
        """Stop process-local work without changing the saved schedule."""
        self._scheduler_stop.set()
        self._stop.set()
        for thread in (self._scheduler_thread, self._thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=30)
                if thread.is_alive():
                    raise TimeoutError('collection has not stopped')

    def _scheduler_loop(self, stop_event: threading.Event) -> None:
        while True:
            with self._lock:
                if not self._schedule["enabled"]:
                    return
                interval = self._schedule["interval_seconds"]
                next_run = self._schedule.get("next_run_at")
                delay = max(0.0, datetime.fromisoformat(next_run).timestamp() - time.time())
            if stop_event.wait(delay):
                return
            with self._lock:
                if not self._schedule["enabled"] or stop_event.is_set():
                    return
                callback = self._scheduled_callback
            try:
                accepted = callback()
            except Exception as exc:
                with self._lock:
                    if stop_event.is_set():
                        return
                    self._schedule["enabled"] = False
                    self._schedule["next_run_at"] = None
                    self._schedule["schedule_error"] = str(exc)
                    self._persist_schedule()
                return
            with self._lock:
                if not self._schedule["enabled"] or stop_event.is_set():
                    return
                now = datetime.now()
                self._schedule['schedule_error'] = None if accepted else 'collection not accepted'
                if accepted:
                    self._schedule["last_run_at"] = now.isoformat(timespec="seconds")
                self._schedule["next_run_at"] = (
                    now + timedelta(seconds=interval)).isoformat(timespec="seconds")
                self._persist_schedule()

    @staticmethod
    def today() -> str:
        return date.today().isoformat()

    def _load_schedule(self) -> dict:
        from protocal import DataError
        try:
            value = json.loads(self.data.read('state', 'schedules', 'lake.json'))
            interval = int(value["interval_seconds"])
            if interval < 60:
                raise ValueError
            if not isinstance(value.get("enabled"), bool):
                raise ValueError("enabled 必须是布尔值")
            next_run = value.get("next_run_at")
            if value['enabled'] and next_run is None:
                raise ValueError('enabled schedule requires next_run_at')
            if next_run is not None:
                datetime.fromisoformat(next_run)
            return {"enabled": bool(value["enabled"]), "interval_seconds": interval,
                    "last_run_at": value.get("last_run_at"),
                    "next_run_at": next_run,
                    "schedule_error": value.get("schedule_error")}
        except DataError as error:
            if error.code != 'missing': raise
            return {"enabled": False, "interval_seconds": 86400,
                    "last_run_at": None, "next_run_at": None, "schedule_error": None}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"调度配置损坏：{exc}") from exc

    def _persist_schedule(self) -> None:
        self.data.write('state', 'schedules', 'lake.json',
                        json.dumps(self._schedule, ensure_ascii=False).encode())
