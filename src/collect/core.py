"""Collection inputs, task state and scheduling; no provider or file I/O."""
from __future__ import annotations
import threading
import time
import uuid
import json
import re
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
        self._jobs[job['job_id']] = job

    def run(self, request):
        if self._closed:
            raise RuntimeError("collection is closed")
        if 'market' in request:
            return self._market(request)
        source = self._source(request)
        return self._run(source, self.methods.prepare_request(source, request))

    @staticmethod
    def _join(packets):
        """Combine observed results; retain each record's original evidence page."""
        result = {key: value for key, value in packets[0].items() if key != 'no_trade'}
        result.update(records=[], raw=[])
        requests = [packet['request'] for packet in packets]
        request = dict(requests[0])
        codes = [code for item in requests for code in item.get('codes', ())]
        if codes:
            request['codes'] = codes
        for field, choose in (('start', min), ('end', max)):
            values = [item[field] for item in requests if field in item]
            if values:
                request[field] = choose(values)
        result['request'] = request
        result['scope'] = dict(result['scope'])
        for field in ('codes', 'start', 'end'):
            if field in request:
                result['scope'][field] = request[field]
        for packet in packets:
            offset = len(result['raw'])
            result['raw'].extend(packet['raw'])
            result['records'].extend({**row, **({'raw_page': row['raw_page'] + offset}
                if row.get('raw_page') is not None else {})} for row in packet['records'])
        calendars = [packet['calendar'] for packet in packets if 'calendar' in packet]
        if calendars:
            result['calendar'] = {
                'sessions': sorted({day for value in calendars for day in value['sessions']}),
                'coverage': [span for value in calendars for span in value['coverage']]}
        observed = [packet['data_as_of'] for packet in packets if packet.get('data_as_of')]
        if observed:
            result['data_as_of'] = max(observed)
        return result

    def _run(self, source, request, *, starts=None, size=25):
        source_id = source['id']
        request = {**request, 'source_id': source_id}
        if type(size) is not int or size < 1:
            raise ValueError('collection batch size must be positive')
        with self._lock:
            if source_id in self._active:
                raise RuntimeError("source collection already running")
            self._active.add(source_id)
        job = {'job_id': uuid.uuid4().hex, 'source_id': source_id,
               'request': dict(request), 'status': 'running', 'error': None,
               'started_at': datetime.now().isoformat(), 'finished_at': None,
               'snapshot_id': None, 'records': 0, 'failures': [], 'empty_codes': []}
        self._jobs[job['job_id']] = job
        self._job_sources[job['job_id']] = deepcopy(source)
        self._current_job_id = job['job_id']
        saved = False
        try:
            self._save(job)
            saved = True
            codes = request.get('codes') or [None]
            for offset in range(0, len(codes), size):
                packets = []
                for code in codes[offset:offset + size]:
                    if job['job_id'] in self._cancelled:
                        raise RuntimeError('collection cancelled before publish')
                    unit = {**request, **({'codes': [code]} if code is not None else {})}
                    packet = None
                    try:
                        if starts is not None:
                            if not starts.get(code):
                                raise ValueError('collection start is unknown; provide a range or a registered listing date')
                            unit['start'] = starts[code]
                        packet = self.methods.fetch(source, unit)
                        self.methods.validate(packet)
                        packets.append(packet)
                        if not packet['records'] and code is not None:
                            job['empty_codes'].append(code)
                    except Exception as error:
                        job['failures'].append({'code': code, 'error': f'{type(error).__name__}: {error}'})
                        evidence = packet['raw'] if packet is not None else getattr(error, 'evidence', ())
                        if evidence:
                            try:
                                self.methods.log({'source_id': source_id, 'request': unit,
                                                  'error': str(error), 'raw': evidence})
                            except Exception as log_error:
                                job['log_error'] = f'{type(log_error).__name__}: {log_error}'
                if packets:
                    packet = self._join(packets)
                    if job['job_id'] in self._cancelled:
                        raise RuntimeError('collection cancelled before publish')
                    try:
                        result = self.methods.publish(source, packet)
                        job['records'] += result['records']
                        job['failures'].extend(result['failures'])
                        if result['ref'] is not None:
                            job['snapshot_id'] = result['ref'].sha256
                    except Exception as error:
                        job['failures'].extend({'code': code, 'error': f'{type(error).__name__}: {error}'}
                            for item in packets for code in item['request'].get('codes', [None]))
                if saved:
                    self._save(job)
            if job['records']:
                job['snapshot_id'] = self.methods.records(source, 0, 0)['snapshot_id']
            job['status'] = 'failed' if job['failures'] else 'succeeded'
            if job['failures']:
                job['error'] = '; '.join(f"{item['code']}: {item['error']}" for item in job['failures'])
        except Exception as error:
            job.update(status='failed', error=f'{type(error).__name__}: {error}')
        finally:
            try:
                job['finished_at'] = datetime.now().isoformat()
                try:
                    self.methods.log({'job_id': job['job_id'], 'source_id': source_id,
                                      'status': job['status'], 'error': job['error'],
                                      'records': job['records'], 'failures': job['failures'],
                                      'empty_codes': job['empty_codes']})
                except Exception as error:
                    job['log_error'] = f'{type(error).__name__}: {error}'
                if saved:
                    try:
                        self._save(job)
                    except Exception as error:
                        job['save_error'] = f'{type(error).__name__}: {error}'
            finally:
                with self._lock:
                    self._active.discard(source_id)
                    self._cancelled.discard(job['job_id'])
        return dict(job)

    def _market(self, value):
        """Resolve registered market intentions inside the collection module."""
        registry = self.sources.describe()
        markets = [item for item in registry.get('markets', ())
                   if value['market'] in (item['scope'], item.get('name'), *item.get('aliases', ()))]
        if len(markets) != 1:
            raise ValueError('market must be registered uniquely: ' + str(value['market']))
        market = markets[0]
        command = value.get('command')
        if command not in {'range', 'latest', 'check', 'doctor', 'status'}:
            raise ValueError('invalid collection command')
        if command == 'status':
            return {'market': market['scope'], 'sources': self.handle('collection.sources', {})}
        today = datetime.now(ZoneInfo(market['timezone'])).date().isoformat()
        if ('start' in value) != ('end' in value):
            raise ValueError('start and end are required together')
        if command == 'range' and 'start' not in value:
            raise ValueError('range requires start and end')
        if command == 'latest' and 'start' in value:
            raise ValueError('latest determines dates from stored observations')
        if 'start' in value:
            if (date.fromisoformat(value['start']) > date.fromisoformat(value['end'])
                    or value['end'] > today):
                raise ValueError('dates must be ordered and not in the future')
        spec = market['collect']
        kinds = value.get('data') or spec.get('default', [])
        if kinds == ['all']:
            kinds = list(spec['data'])
        if not kinds or any(kind not in spec['data'] for kind in kinds):
            raise ValueError('collection data type is not registered')
        selected = value.get('codes') or spec.get('codes')
        jobs, plans, requests, catalogue = [], [], [], None
        for kind in kinds:
            binding = spec['data'][kind]
            source = self.sources.get(binding['source'])
            codes = binding.get('codes', selected)
            starts = (self.methods.last_dates(source, codes)
                      if command == 'latest' and codes is not None else None)
            if 'codes' not in binding and (codes is None or command == 'latest'
                    and 'securities' in spec and set(codes) - starts.keys()):
                if catalogue is None:
                    securities = spec['securities']
                    directory = self.sources.get(securities['source'])
                    if command != 'check':
                        update = self.run({'source_id': directory['id'], 'refresh': True,
                            **({'exchanges': securities['exchanges']} if 'exchanges' in securities else {})})
                        jobs.append(update)
                    catalogue, exchanges = {}, set()
                    if command == 'check' or update['status'] == 'succeeded' or update['records']:
                        rejected = {item['code'] for item in update['failures']} if command != 'check' else set()
                        for row in self.methods.record_rows(directory):
                            code = row[securities['code']]
                            if (any(row.get(field) not in allowed for field, allowed in securities.get('filters', {}).items())
                                    or market.get('code_pattern') and not re.fullmatch(market['code_pattern'], code)):
                                continue
                            exchanges.add(code.split('.')[0])
                            opened, closed = row.get(securities.get('from')), row.get(securities.get('to'))
                            if (code in rejected or opened and opened > value.get('end', today)
                                    or closed and (closed < value['start'] if 'start' in value else command != 'doctor')):
                                continue
                            catalogue[code] = opened
                        if set(securities.get('exchanges', ())) - exchanges:
                            raise ValueError('security catalogue is missing a registered exchange')
                if codes is None:
                    codes = sorted(catalogue)
            if not codes:
                if any(job.get('status') == 'failed' for job in jobs):
                    continue
                raise ValueError('no securities in the requested interval')
            request = {key: value[key] for key in ('start', 'end', 'fields', 'refresh') if key in value}
            request.update(source_id=source['id'], codes=sorted(set(codes)))
            if 'start' not in request and command != 'doctor':
                starts = self.methods.last_dates(source, codes) if starts is None else starts
                if catalogue is not None:
                    starts.update({code: catalogue[code] for code in codes
                                   if code not in starts and catalogue.get(code)})
                request['end'] = today
            if command == 'doctor':
                jobs.append(self.methods.doctor(source, request))
                continue
            if command == 'check':
                for code in codes:
                    unit = {**request, 'codes': [code]}
                    if starts is not None:
                        if not starts.get(code):
                            raise ValueError('collection start is unknown: ' + code)
                        unit['start'] = starts[code]
                    plans.append({'data': kind, 'request': unit,
                                  'coverage': self.methods.inspect(source, unit)['coverage']})
                continue
            if starts is not None:
                request['start'] = min(starts.values()) if starts else today
            requests.append((source, self.methods.prepare_request(source, request), starts))
        for source, request, starts in requests:
            jobs.append(self._run(source, request, starts=starts, size=spec.get('batch_size', 25)))
        if command == 'check':
            return {'market': market['scope'], 'status': 'complete' if all(
                plan['coverage']['complete'] for plan in plans) else 'incomplete', 'plans': plans}
        failures = [item for job in jobs for item in job.get('failures', ())]
        failures.extend({'code': None, 'error': job['error']} for job in jobs
                        if job.get('status') == 'failed' and not job.get('failures'))
        return {'market': market['scope'], 'status': 'failed' if failures else 'succeeded',
                'jobs': jobs, 'failures': failures,
                'error': '; '.join(f"{item['code']}: {item['error']}" for item in failures) or None}

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
                result = self.methods.publish(source, packet)
                failures = result['failures']
                job.update(status='failed' if failures else 'succeeded', phase='publish',
                           merge=stats, records=result['records'], failures=failures,
                           error='; '.join(f"{item['code']}: {item['error']}" for item in failures) or None)
                if result['ref'] is not None:
                    job['snapshot_id'] = self.methods.records(source, 0, 0)['snapshot_id']
                self._packets.pop(job_id, None)
            job["finished_at"] = datetime.now().isoformat()
            self._save(job)
            self._merge_state.update(running=False, output=json.dumps(stats, ensure_ascii=False),
                                     error=job.get('error'), finished_at=job["finished_at"])
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
