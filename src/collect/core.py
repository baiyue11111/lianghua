"""Collection inputs, task state and scheduling; no provider or file I/O."""
from __future__ import annotations
import os
import socket
import threading
import time
import uuid
import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from protocol import Filter, TableDef, TableQuery

# A job has three states and one outcome. "How it ended" is not a state: nothing
# leaves ``finished``, so folding six outcomes into the state set would only add
# transitions that can never be legal. This is the same split GitHub Actions
# uses (status / conclusion) and it keeps the table small enough to read.
RUNNING, WAITING, FINISHED = 'running', 'awaiting_publish', 'finished'
OUTCOMES = ('succeeded', 'partial', 'failed', 'cancelled', 'interrupted')
LEGAL = {
    (None, 'submit'): RUNNING,
    (None, 'fail'): FINISHED,
    (RUNNING, 'phase'): RUNNING,
    (RUNNING, 'fetched'): WAITING,
    (WAITING, 'merge'): RUNNING,
    (RUNNING, 'publish'): FINISHED,
    (WAITING, 'publish'): FINISHED,
    (RUNNING, 'fail'): FINISHED,
    (WAITING, 'fail'): FINISHED,
    (RUNNING, 'cancel'): FINISHED,
    (WAITING, 'cancel'): FINISHED,
    (RUNNING, 'expire'): FINISHED,
    (WAITING, 'expire'): FINISHED,
}
# A running job holds a lease it must renew; an expired lease is the only proof
# that the process holding it is gone. Airflow calls the same thing a zombie.
LEASE_SECONDS = 300


class Collect:
    """One execution engine for manual and scheduled source collection."""

    def __init__(self, sources, methods):
        self.sources = sources
        self.methods = methods
        self.data = methods.data
        self.data.define(TableDef(
            'collection_jobs', {'job_id': 'VARCHAR', 'settings': 'JSON'}, ('job_id',), ('settings',)))
        self.data.define(TableDef(
            'collection_schedules', {'source_id': 'VARCHAR', 'settings': 'JSON'},
            ('source_id',), ('settings',)))
        self._lock = threading.RLock()
        self._jobs = {}
        self._active = set()
        self._cancelled = set()
        self._packets = {}
        self._job_sources = {}
        self._current_job_id = None
        self._fetch_thread = None
        self._merge_thread = None
        self._scheduler_error = None
        self._recover_tasks()
        self._closed = False
        self._stop = threading.Event()
        self._thread = None

    def _save(self, job):
        self.data.put_rows('collection_jobs', [{'job_id': job['job_id'],
            'settings': json.dumps(job, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False)}],
            keys=('job_id',), refresh=True)
        self._jobs[job['job_id']] = job

    def _transition(self, job, event, *, outcome=None, **changes):
        """Move a job along the table above and persist it; refuse anything else.

        This is the only place that writes ``status``. Keeping the table and the
        write together is what makes the states real: a caller cannot invent a
        move, and an illegal one fails here instead of surfacing later as a job
        stuck in a shape nothing handles.
        """
        current = job.get('status')
        try:
            following = LEGAL[(current, event)]
        except KeyError:
            raise ValueError(f'no transition from {current!r} on {event!r}') from None
        if following == FINISHED and outcome not in OUTCOMES:
            raise ValueError(f'finishing requires a known outcome, not {outcome!r}')
        now = datetime.now().isoformat()
        job.update(changes)
        job.update(status=following, updated_at=now)
        if following == FINISHED:
            job.update(outcome=outcome, finished_at=now)
            for name in ('lease_owner', 'lease_expires_at'):
                job.pop(name, None)
        self._save(job)
        return job

    @staticmethod
    def _renew(job):
        """Extend the lease; called between batches so a live job stays live."""
        job['lease_owner'] = f'{socket.gethostname()}:{os.getpid()}'
        job['lease_expires_at'] = (datetime.now() + timedelta(seconds=LEASE_SECONDS)).isoformat()

    @staticmethod
    def _lease_dead(job):
        """True when nothing is holding this job any more.

        An expired timestamp is enough across machines; on this machine a dead
        pid is proof right away, so a restart does not wait out the full lease.
        """
        host, _, rest = str(job.get('lease_owner') or '').partition(':')
        pid, _, _ = rest.partition(':')
        if host == socket.gethostname() and pid.isdigit():
            try:
                os.kill(int(pid), 0)
            except OSError:
                return True
            if int(pid) != os.getpid():
                return False
        expires = job.get('lease_expires_at')
        return not expires or expires < datetime.now().isoformat()

    @staticmethod
    def _settings(row):
        value = row['settings']
        return json.loads(value) if isinstance(value, str) else value

    def _state(self, table, key, value=None):
        column = 'job_id' if table == 'collection_jobs' else 'source_id'
        query = TableQuery(table, (Filter(column, 'eq', key),))
        if value is not None:
            return self.data.put_rows(table, [{column: key, 'settings': json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)}],
                keys=(column,), refresh=True)
        rows = self.data.scan(query).table.to_pylist()
        return self._settings(rows[0]) if rows else None

    def _states(self, table):
        return [self._settings(row) for row in self.data.scan(TableQuery(table)).table.to_pylist()]

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

    def _run(self, source, request, *, starts=None, missing=None, size=25):
        source_id = source['id']
        request = {**request, 'source_id': source_id}
        if type(size) is not int or size < 1:
            raise ValueError('collection batch size must be positive')
        with self._lock:
            if source_id in self._active:
                raise RuntimeError("source collection already running")
            self._active.add(source_id)
        job = {'job_id': uuid.uuid4().hex, 'source_id': source_id,
               'request': dict(request), 'status': None, 'outcome': None, 'error': None,
               'started_at': datetime.now().isoformat(), 'finished_at': None,
               'snapshot_id': None, 'records': 0, 'failures': [], 'empty_codes': []}
        self._jobs[job['job_id']] = job
        self._job_sources[job['job_id']] = deepcopy(source)
        self._current_job_id = job['job_id']
        try:
            self._renew(job)
            self._transition(job, 'submit', phase='fetch')
            codes = request.get('codes') or [None]
            for offset in range(0, len(codes), size):
                packets = []
                for code in codes[offset:offset + size]:
                    if job['job_id'] in self._cancelled:
                        raise RuntimeError('collection cancelled before publish')
                    unit = {**request, **({'codes': [code]} if code is not None else {})}
                    packet = None
                    try:
                        if missing is not None:
                            # A security with nothing absent needs no request.
                            days = missing.get(code) if code is not None else None
                            if not days:
                                continue
                            unit['start'], unit['end'] = days[0], days[-1]
                        elif starts is not None:
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
                self._renew(job)
                self._save(job)
            if job['records']:
                job['snapshot_id'] = self.methods.records(source, 0, 0)['snapshot_id']
            if job['failures']:
                job['error'] = '; '.join(f"{item['code']}: {item['error']}" for item in job['failures'])
                self._transition(job, 'publish', phase='publish',
                                 outcome='partial' if job['records'] else 'failed')
            else:
                self._transition(job, 'publish', phase='publish', outcome='succeeded')
        except Exception as error:
            message = f'{type(error).__name__}: {error}'
            job['error'] = message
            if job.get('status') != FINISHED:
                cancelled = job['job_id'] in self._cancelled
                try:
                    self._transition(job, 'cancel' if cancelled else 'fail',
                                     outcome='cancelled' if cancelled else 'failed', error=message)
                except Exception as store_error:
                    # The store itself is unavailable; keep the result honest in
                    # memory so the caller still learns what happened.
                    job.update(status=FINISHED, outcome='cancelled' if cancelled else 'failed',
                               finished_at=datetime.now().isoformat(),
                               save_error=f'{type(store_error).__name__}: {store_error}')
        finally:
            try:
                try:
                    self.methods.log({'job_id': job['job_id'], 'source_id': source_id,
                                      'status': job.get('status'), 'outcome': job.get('outcome'),
                                      'error': job['error'],
                                      'records': job['records'], 'failures': job['failures'],
                                      'empty_codes': job['empty_codes']})
                except Exception as error:
                    job['log_error'] = f'{type(error).__name__}: {error}'
                if job.get('status') == FINISHED:
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
            # Listing dates come from the security relation inside the
            # difference query, so the catalogue is only needed to resolve an
            # unstated universe -- never to supply a starting date.
            starts = None
            if 'codes' not in binding and codes is None:
                if catalogue is None:
                    securities = spec['securities']
                    directory = self.sources.get(securities['source'])
                    if command != 'check':
                        update = self.run({'source_id': directory['id'], 'refresh': True,
                            **({'exchanges': securities['exchanges']} if 'exchanges' in securities else {})})
                        jobs.append(update)
                    catalogue, exchanges = {}, set()
                    if command == 'check' or update.get('outcome') == 'succeeded' or update.get('records'):
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
                if any(job.get('outcome') in ('failed', 'interrupted') for job in jobs):
                    continue
                raise ValueError('no securities in the requested interval')
            request = {key: value[key] for key in ('start', 'end', 'fields', 'refresh') if key in value}
            request.update(source_id=source['id'], codes=sorted(set(codes)))
            missing = None
            if 'start' not in request and command != 'doctor':
                request['end'] = today
                if command != 'check' and (source.get('data_type') == 'daily'
                                           or source.get('method') == 'daily'):
                    # Ask the store what is absent instead of inferring it from
                    # the last stored day. The difference knows about sessions
                    # the watermark cannot see, and it is one query.
                    missing = self.methods.missing_days(source, request['codes'], end=today)
                if missing is None:
                    # No calendar to difference against: resume from the
                    # watermark, which is all that is left to go on.
                    starts = self.methods.last_dates(source, codes) if starts is None else starts
                    if catalogue is not None:
                        starts.update({code: catalogue[code] for code in codes
                                       if code not in starts and catalogue.get(code)})
                    if command != 'check':
                        request['start'] = min(starts.values()) if starts else today
                else:
                    absent = [day for days in missing.values() for day in days]
                    request['start'] = min(absent) if absent else today
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
            requests.append((source, self.methods.prepare_request(source, request), starts, missing))
        for source, request, starts, missing in requests:
            jobs.append(self._run(source, request, starts=starts, missing=missing,
                                  size=spec.get('batch_size', 25)))
        if command == 'check':
            return {'market': market['scope'], 'status': 'complete' if all(
                plan['coverage']['complete'] for plan in plans) else 'incomplete', 'plans': plans}
        failures = [item for job in jobs for item in job.get('failures', ())]
        failures.extend({'code': None, 'error': job.get('error')} for job in jobs
                        if job.get('outcome') in ('failed', 'interrupted') and not job.get('failures'))
        return {'market': market['scope'], 'outcome': 'failed' if failures else 'succeeded',
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
               "request": dict(request), "status": None, "outcome": None,
               "error": None, "started_at": datetime.now().isoformat(),
               "finished_at": None, "snapshot_id": None, "records": 0}
        self._jobs[job["job_id"]] = job
        self._job_sources[job['job_id']] = deepcopy(source)
        self._current_job_id = job["job_id"]
        self._renew(job)
        self._transition(job, 'submit', phase="fetch")
        self._fetch_thread = threading.Thread(
            target=self._fetch_task, args=(source, request, job), daemon=True)
        self._fetch_thread.start()
        return {"ok": True}

    def _fetch_task(self, source, request, job):
        source_id = source["id"]
        try:
            self._renew(job)
            self._save(job)
            packet = self.methods.fetch(source, request)
            self.methods.validate(packet)
            if job["job_id"] in self._cancelled:
                self._transition(job, 'cancel', outcome='cancelled',
                                 phase="fetch", error="collection stopped")
            else:
                self._packets[job["job_id"]] = packet
                self._transition(job, 'fetched', phase="validate", records=len(packet["records"]))
        except Exception as error:
            if job.get('status') != FINISHED:
                self._transition(job, 'fail', outcome='failed', phase="fetch",
                                 error=f"{type(error).__name__}: {error}")
        finally:
            with self._lock:
                self._active.discard(source_id)

    def _recover_tasks(self):
        """Resolve jobs whose process is gone; leave the ones still held.

        The old test was "status is running or fetched", which cannot tell a
        live worker from a dead one and would fail a job another process was
        still writing. The lease answers that, and a dead pid on this machine
        answers it without waiting the lease out.
        """
        for job in self._states('collection_jobs'):
            if job.get('status') in (RUNNING, WAITING) and self._lease_dead(job):
                try:
                    self._transition(job, 'expire', outcome='interrupted',
                                     error="collection interrupted before publish")
                except ValueError:
                    pass
            self._jobs[job['job_id']] = job

    @staticmethod
    def _fetch_view(job):
        ready = job.get("status") == WAITING
        return {"ok": ready, "status": job.get("status"), "outcome": job.get("outcome"),
                "job_id": job["job_id"], "done": 1 if ready else 0,
                "total": 1, "rows": job.get("records", 0), "empty": 0,
                "failed": [] if ready else [job["source_id"]],
                "current": job.get("source_id", ""), "finished_at": job.get("finished_at"),
                "error": job.get("error"), "log": []}

    def _merge_task(self, job_id, dry_run):
        try:
            packet = self._packets[job_id]
            job = self.status(job_id)
            source = self._job_sources[job_id]
            stats = self.methods.prepare(source, packet)
            if dry_run:
                # A dry run answers "what would this write" and consumes nothing,
                # so the job stays where it is and can still be published after.
                job.update(dry_run=stats)
                self._save(job)
                return
            result = self.methods.publish(source, packet)
            failures = result['failures']
            self._transition(job, 'publish', phase='publish', merge=stats,
                             records=result['records'], failures=failures,
                             outcome=('partial' if failures and result['records'] else
                                      'failed' if failures else 'succeeded'),
                             error='; '.join(f"{item['code']}: {item['error']}"
                                             for item in failures) or None)
            if result['ref'] is not None:
                job['snapshot_id'] = self.methods.records(source, 0, 0)['snapshot_id']
                self._save(job)
            self._packets.pop(job_id, None)
        except Exception as error:
            job = self.status(job_id)
            if job.get('status') != FINISHED:
                self._transition(job, 'fail', phase="publish", outcome='failed',
                                 error=f"{type(error).__name__}: {error}")

    def _merge_current(self, dry_run):
        job_id = self._current_job_id
        if not job_id or job_id not in self._packets:
            raise RuntimeError("no fetched collection task")
        job = self.status(job_id)
        if job.get('status') != WAITING:
            raise RuntimeError("merge already running")
        if not dry_run:
            self._renew(job)
            self._transition(job, 'merge', phase='publish')
        self._merge_thread = threading.Thread(target=self._merge_task,
                                              args=(job_id, bool(dry_run)), daemon=True)
        self._merge_thread.start()
        return {"ok": True, "status": RUNNING, "job_id": job_id,
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
        self._state('collection_schedules', source_id, value)
        return value

    def cancel(self, job_id):
        """A job waiting to publish stops at once; a running one stops at its
        next batch boundary, because that is the only safe place to break."""
        job = self.status(job_id)
        state = job.get('status')
        if state == WAITING:
            return dict(self._transition(job, 'cancel', outcome='cancelled',
                                         error='collection stopped'))
        if state == RUNNING:
            self._cancelled.add(job_id)
        return dict(job)

    def status(self, job_id):
        if job_id in self._jobs:
            return dict(self._jobs[job_id])
        try:
            value = self._state('collection_jobs', job_id)
            if value is None:
                raise KeyError(job_id)
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
            try:
                self.tick()
            except Exception as error:
                # One bad tick must not end the scheduler: before this, a single
                # rejected overlapping collection killed the thread for good.
                self._scheduler_error = f'{type(error).__name__}: {error}'
            else:
                self._scheduler_error = None

    def tick(self, at=None):
        now = time.time() if at is None else at
        for schedule in self._states('collection_schedules'):
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
            interval = int(schedule.get("interval_seconds", 60))
            try:
                result = self.run(request)
                schedule["last_job_id"] = result.get("job_id")
                schedule["error"] = None
                delay = interval
            except Exception as error:
                # Retry soon rather than a whole interval later, and keep the
                # other schedules in this tick moving.
                schedule["error"] = f'{type(error).__name__}: {error}'
                delay = min(300, interval)
            schedule["last_run_at"] = datetime.fromtimestamp(now).isoformat()
            schedule["next_at"] = now + delay
            self._state('collection_schedules', schedule['source_id'], schedule)

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
            if job.get("status") not in (RUNNING, WAITING):
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
            job = self.status(self._current_job_id) if self._current_job_id else {}
            return {"running": job.get('status') == RUNNING and job.get('phase') == 'publish',
                    "output": json.dumps(job.get('merge') or {}, ensure_ascii=False),
                    "error": job.get('error'), "started_at": job.get('started_at'),
                    "finished_at": job.get('finished_at')}
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
        job = self.status(self._current_job_id) if self._current_job_id else None
        if job is None:
            return {"running": False, "issue": "", "stage": "", "verdict": None,
                    "verdicts": {}, "merge_output": "", "log": [], "finished_at": None,
                    "fetch": self._fetch_view({"status": None, "outcome": None, "job_id": "",
                                               "source_id": "", "finished_at": None,
                                               "records": 0, "error": None})}
        return {"running": job.get('status') == RUNNING, "issue": "",
                "stage": job.get('phase') or "", "verdict": job.get('outcome'),
                "verdicts": {}, "merge_output": "", "log": [],
                "finished_at": job.get('finished_at'), "fetch": self._fetch_view(job)}


    def _schedule_list(self):
        return {"items": self._states('collection_schedules')}


    def _source_view(self, source):
        source_id = source["id"]
        job = None
        for item in self._states('collection_jobs'):
            if item.get("source_id") == source_id:
                if job is None or item.get("finished_at", "") > job.get("finished_at", ""):
                    job = item
        try:
            schedule = self._state('collection_schedules', source_id)
            if schedule is None:
                raise KeyError(source_id)
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
