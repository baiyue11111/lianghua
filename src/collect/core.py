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
from queue import SimpleQueue
from zoneinfo import ZoneInfo
from protocol import DataError, Filter, TableDef, TableQuery, _calendar_facts, _calendar_covers

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


class CalendarCoverageError(ValueError):
    pass


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
        self._schedule_lock = threading.Lock()
        self._market_running = {}
        # A source's record total is a full-range count. It only changes when a
        # collection writes, so it is counted once and dropped on the next run
        # instead of rescanning every source on every page load.
        self._records_cache = {}
        self._recover_tasks()
        self._closed = False
        self._stop = threading.Event()
        self._thread = None
        self._market_thread = None
        self._market_tasks = SimpleQueue()
        self._market_pending = False
        # A console stream asks for the same market once a second; resolving a
        # name through the registry every time would scan it once a second too.
        self._console_scopes = {}

    def _console_view(self, name):
        """The few facts a control console needs, and nothing else.

        `collection.automatic` is a diagnostic view: it carries every job in the
        last run, including the full text of each provider failure, which came
        to over a megabyte. Pushing that to a browser that displays six numbers
        is not monitoring, it is a denial of service with extra steps. This view
        reads only the plan and the current job, both already in memory, and
        returns a bounded document suitable for a one-second stream.
        """
        scope = self._console_scopes.get(name)
        if scope is None:
            scope = self._market_config(name)['scope']
            self._console_scopes[name] = scope
        with self._lock:
            plan = dict(self._state('collection_schedules', 'market:' + scope) or {})
            job_id = self._current_job_id
            active = bool(self._active) or scope in self._market_running
            scheduler = self._thread is not None and self._thread.is_alive()
        job = None
        if job_id is not None:
            try:
                saved = self.status(job_id)
                job = {'job_id': job_id, 'status': saved.get('status'),
                       'phase': saved.get('phase'), 'source_id': saved.get('source_id'),
                       'records': saved.get('records', 0)}
            except KeyError:
                pass
        detail = (plan.get('error') or (job or {}).get('error'))
        return {
            'market': scope, 'at': time.time(),
            'enabled': bool(plan.get('enabled')), 'running': active,
            'scheduler': scheduler, 'phase': (job or {}).get('phase'),
            'source_id': (job or {}).get('source_id'),
            'records': (job or {}).get('records', 0),
            'next_run_at': plan.get('next_at'), 'last_run_at': plan.get('last_run_at'),
            'last_outcome': plan.get('last_outcome'),
            'error': detail[:200] if isinstance(detail, str) else None,
        }

    def _system_view(self):
        """A small status card per module, with a few recent events each.

        The console is an overview, not a diagnosis. Every read here is a small
        table or a value already in memory: the full data-quality report scans
        millions of rows and must never sit on a one-second stream. Diagnosis
        stays on the existing operations, reachable from each card.
        """
        markets = self.sources.describe().get('markets', ())
        market = self._console_view(markets[0]['scope']) if markets else None
        jobs = sorted(self._states('collection_jobs'),
                      key=lambda job: job.get('started_at') or '', reverse=True)
        events = []
        for job in jobs[:5]:
            events.append({
                'at': job.get('started_at'), 'label': job.get('source_id') or job.get('market') or '',
                'detail': job.get('outcome') or job.get('status') or '',
                'level': 'error' if job.get('outcome') in ('failed', 'interrupted') else
                         'warn' if job.get('outcome') in ('partial', 'cancelled') else 'ok',
            })
        sources = self.sources.describe().get('sources', [])
        running = [job for job in jobs if job.get('status') in (RUNNING, WAITING)]
        return {
            'at': time.time(),
            'modules': [
                {'key': 'service', 'label': '服务', 'level': 'ok',
                 'summary': '协议服务在线',
                 'facts': [], 'events': [],
                 'detail': 'service.info'},
                {'key': 'collection', 'label': '行情采集',
                 'level': 'error' if market and market.get('error') else
                          'ok' if market and market.get('enabled') else 'idle',
                 'summary': (('自动续采已开启' if market.get('enabled') else '自动续采未开启')
                             if market else '未登记沪深市场'),
                 'facts': ([
                     {'label': '状态', 'value': '运行中' if market.get('running') else '空闲'},
                     {'label': '调度线程', 'value': '正常' if market.get('scheduler') else '未运行'},
                     {'label': '下次续采', 'value': market.get('next_run_at')},
                     {'label': '最近结果', 'value': market.get('last_outcome') or '暂无'},
                 ] if market else []),
                 'events': events,
                 'detail': 'collection.automatic'},
                {'key': 'sources', 'label': '数据源',
                 'level': 'error' if any(item.get('integrity_error') for item in sources) else 'ok',
                 'summary': f'{len(sources)} 个已登记来源',
                 'facts': [{'label': '正在采集', 'value': f'{len(running)} 个任务'}],
                 'events': [],
                 'detail': 'collection.sources'},
            ],
        }

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
        # Counts taken before this write are stale by construction.
        self._records_cache.clear()
        if 'market' in request:
            return self._market(request)
        source = self._source(request)
        catalogue = source.get('params', {}).get('catalogue')
        if catalogue and not request.get('codes'):
            request = {**request, 'codes': [row['code'] for row in
                       self.methods.record_rows(self.sources.get(catalogue))]}
        return self._run(source, self.methods.prepare_request(source, request),
                         split=source.get('data_type') != 'quotes')

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

    def _run(self, source, request, *, starts=None, missing=None, size=25, split=True):
        source_id = source['id']
        request = {**request, 'source_id': source_id}
        if type(size) is not int or size < 1:
            raise ValueError('collection batch size must be positive')
        with self._lock:
            if self._closed:
                raise RuntimeError("collection is closed")
            if source_id in self._active:
                raise RuntimeError("source collection already running")
            self._active.add(source_id)
            job = {'job_id': uuid.uuid4().hex, 'source_id': source_id,
                   'request': dict(request), 'status': None, 'outcome': None, 'error': None,
                   'started_at': datetime.now().isoformat(), 'finished_at': None,
                   'snapshot_id': None, 'records': 0, 'failures': [], 'empty_codes': []}
            self._jobs[job['job_id']] = job
            self._job_sources[job['job_id']] = deepcopy(source)
        try:
            with self._lock:
                with self.data.transaction():
                    self._renew(job)
                    self._transition(job, 'submit', phase='fetch')
                    for market, running in self._market_running.items():
                        if running['thread'] == threading.get_ident():
                            key = 'market:' + market
                            plan = self._state('collection_schedules', key)
                            if plan is not None and running['market_job']:
                                plan['last_job_id'] = job['job_id']
                                plan.setdefault('last_job_ids', []).append(job['job_id'])
                                self._state('collection_schedules', key, plan)
                            running['job_id'] = job['job_id']
            codes = (request.get('codes') or [None]) if split else [None]
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
                            if unit['start'] > unit['end']:
                                continue
                        packet = self.methods.fetch(source, unit)
                        self.methods.validate(packet)
                        failures = packet.get('failures', [])
                        if not isinstance(failures, list) or any(not isinstance(item, dict)
                                or not item.get('error') for item in failures):
                            raise ValueError('packet failures must be a list of errors')
                        packets.append(packet)
                        job['failures'].extend(failures)
                        if not packet['records'] and not failures and code is not None:
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
                        if result.get('snapshot_id') is not None:
                            job['snapshot_id'] = result['snapshot_id']
                    except Exception as error:
                        job['failures'].extend({'code': code, 'error': f'{type(error).__name__}: {error}'}
                            for item in packets for code in item['request'].get('codes', [None]))
                self._renew(job)
                self._save(job)
            if job['records'] and job['snapshot_id'] is None:
                job['snapshot_id'] = self.methods.records(source, limit=0, include_audit=False)['snapshot_id']
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

    def _market_config(self, name):
        registry = self.sources.describe()
        markets = [item for item in registry.get('markets', ())
                   if name in (item['scope'], item.get('name'), *item.get('aliases', ()))]
        if len(markets) != 1:
            raise ValueError('market must be registered uniquely: ' + str(name))
        return markets[0]

    def _market(self, value):
        """Resolve registered market intentions inside the collection module."""
        market = self._market_config(value['market'])
        command = value.get('command')
        if command not in {'range', 'latest', 'check', 'status'}:
            raise ValueError('invalid collection command')
        if command == 'status':
            return {'market': market['scope'], 'sources': self.handle('collection.sources', {})}
        today = datetime.now(ZoneInfo(market['timezone'])).date().isoformat()
        if command != 'latest' and ('start' in value) != ('end' in value):
            raise ValueError('start and end are required together')
        if command == 'range' and 'start' not in value:
            raise ValueError('range requires start and end')
        if command == 'latest' and 'start' in value:
            raise ValueError('latest determines dates from stored observations')
        if 'end' in value:
            if date.fromisoformat(value['end']) > date.fromisoformat(today):
                raise ValueError('dates must not be in the future')
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
            market_scope = binding.get('scope') == 'market'
            codes = binding.get('codes', selected)
            # Listing dates come from the security relation inside the
            # difference query, so the catalogue is only needed to resolve an
            # unstated universe -- never to supply a starting date.
            starts = None
            if not market_scope and 'codes' not in binding and codes is None:
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
                                    or closed and ('start' not in value or closed < value['start'])):
                                continue
                            catalogue[code] = opened
                        if set(securities.get('exchanges', ())) - exchanges:
                            raise ValueError('security catalogue is missing a registered exchange')
                if codes is None:
                    codes = sorted(catalogue)
            if not codes and not market_scope:
                if any(job.get('outcome') in ('failed', 'interrupted') for job in jobs):
                    continue
                raise ValueError('no securities in the requested interval')
            request = {key: value[key] for key in ('start', 'end', 'fields', 'refresh') if key in value}
            request['source_id'] = source['id']
            if codes:
                request['codes'] = sorted(set(codes))
            missing = None
            if 'start' not in request:
                end = request.get('end', today)
                request['end'] = end
                if market_scope:
                    first = min(span['start'] for span in self.data.calendar(source['dataset_id'])['coverage'])
                    cursor = date.fromisoformat(first)
                    for job in sorted((job for job in self._states('collection_jobs')
                                       if job.get('source_id') == source['id']
                                       and job.get('outcome') == 'succeeded'
                                       and job.get('request', {}).get('start')
                                       and job.get('request', {}).get('end')),
                                      key=lambda job: job['request']['start']):
                        left = date.fromisoformat(job['request']['start'])
                        right = date.fromisoformat(job['request']['end'])
                        if left > cursor:
                            break
                        if right >= cursor:
                            cursor = right + timedelta(days=1)
                    request['start'] = min(cursor, date.fromisoformat(end)).isoformat()
                elif command != 'check' and not spec.get('poll_seconds') and (
                    source.get('data_type') == 'daily' or source.get('method') == 'daily'
                ):
                    # Ask the store what is absent instead of inferring it from
                    # the last stored day. The difference knows about sessions
                    # the watermark cannot see, and it is one query.
                    missing = self.methods.missing_days(source, request['codes'], end=end)
                if missing is None and not market_scope:
                    # Observed calendars end at the last published value.
                    # Resume from that value to discover new dates.
                    starts = self.methods.last_dates(source, codes) if starts is None else starts
                    if catalogue is not None:
                        starts.update({code: catalogue[code] for code in codes
                                       if code not in starts and catalogue.get(code)})
                    if command != 'check':
                        due_starts = [day for day in starts.values() if day <= end]
                        request['start'] = min(due_starts) if due_starts else end
                elif missing is not None:
                    absent = [day for days in missing.values() for day in days]
                    request['start'] = min(absent) if absent else end
            if command == 'check':
                for code in codes or [None]:
                    unit = {**request, **({'codes': [code]} if code is not None else {})}
                    if starts is not None:
                        if not starts.get(code):
                            raise ValueError('collection start is unknown: ' + code)
                        unit['start'] = starts[code]
                    if code is None:
                        plans.append({'data': kind, 'request': unit,
                                      'coverage': {'complete': False, 'status': 'unknown'}})
                        continue
                    plans.append({'data': kind, 'request': unit,
                                  'coverage': self.methods.inspect(source, unit)['coverage']})
                continue
            requests.append((source, self.methods.prepare_request(source, request), starts, missing, not market_scope))
        for source, request, starts, missing, split in requests:
            jobs.append(self._run(source, request, starts=starts, missing=missing,
                                  size=spec.get('batch_size', 25), split=split))
        if command == 'check':
            return {'market': market['scope'], 'status': 'complete' if all(
                plan['coverage']['complete'] for plan in plans) else 'incomplete', 'plans': plans}
        failures = [item for job in jobs for item in job.get('failures', ())]
        failures.extend({'code': None, 'error': job.get('error')} for job in jobs
                        if job.get('outcome') in ('failed', 'interrupted') and not job.get('failures'))
        outcome = ('partial' if any(job.get('records', 0) for job in jobs) else 'failed') if failures else 'succeeded'
        return {'market': market['scope'], 'outcome': outcome,
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
                job['snapshot_id'] = self.methods.records(source, limit=0, include_audit=False)['snapshot_id']
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
        with self._lock:
            self._state('collection_schedules', source_id, value)
        return value

    @staticmethod
    def _session_at(market, plan, day):
        zone = ZoneInfo(market['timezone'])
        close = datetime.strptime(market['session_close'], '%H:%M').time()
        return (datetime.combine(day, close, tzinfo=zone) + timedelta(seconds=plan['delay_seconds'])).timestamp()

    def _market_calendar(self, market):
        try:
            return _calendar_facts(self.data.object(market['calendar']))
        except (DataError, TypeError, ValueError) as error:
            raise CalendarCoverageError(f'calendar unavailable: {error}') from error

    def _market_next(self, market, plan, now):
        zone = ZoneInfo(market['timezone'])
        sessions, coverage = self._market_calendar(market)
        local = datetime.fromtimestamp(now, zone)
        today = local.date()
        last = date.fromisoformat(plan['last_session']) if plan.get('last_session') else None
        previous = [left for left, _ in coverage if left <= today]
        start = min(last + timedelta(days=1), today) if last else min(previous, default=today)
        if not _calendar_covers(coverage, start, today):
            raise CalendarCoverageError('calendar does not cover the collection period')
        due = [day for day in sessions if day <= today and self._session_at(market, plan, day) <= now and
               (last is None or day > last)]
        if due:
            retry = plan.get('retry_at')
            start = self._session_at(market, plan, due[-1])
            return max(start, retry) if retry is not None else start
        future = [day for day in sessions if self._session_at(market, plan, day) > now]
        if not future or not _calendar_covers(coverage, today, future[0]):
            raise CalendarCoverageError('calendar does not cover the next session')
        return self._session_at(market, plan, future[0])

    def _market_plan(self, name, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        market = self._market_config(name)
        owner = (market.get('collect') or {}).get('auto_with', market['scope'])
        now = time.time()
        with self._lock, self.data.transaction():
            for item in self.sources.describe().get('markets', ()):
                if (item.get('collect') or {}).get('auto_with', item['scope']) == owner:
                    self._save_market_plan(item, enabled, now)
        return self._market_status(market['scope'])

    def _save_market_plan(self, market, enabled, now):
        key = 'market:' + market['scope']
        plan = self._state('collection_schedules', key) or {
            'kind': 'market', 'market': market['scope'], 'last_session': None,
            'last_run_at': None, 'last_outcome': None, 'error': None,
            'last_job_id': None, 'retry_at': None, 'delay_seconds': 1800,
        }
        plan['enabled'] = enabled
        plan['poll_seconds'] = (market.get('collect') or {}).get('poll_seconds')
        interval = plan['poll_seconds']
        if interval is not None and (type(interval) is not int or interval < 30):
            raise ValueError('poll_seconds must be an integer of at least 30 seconds')
        if enabled:
            try:
                plan['next_at'] = now if interval else self._market_next(market, plan, now)
                plan['error'] = None
            except Exception as error:
                plan['error'] = f'{type(error).__name__}: {error}'
                plan['next_at'] = now if isinstance(error, CalendarCoverageError) and market.get('calendar_source') else now + 300
        else:
            plan['next_at'] = None
        self._state('collection_schedules', key, plan)

    def _market_status(self, name):
        market = self._market_config(name)
        key = 'market:' + market['scope']
        with self._lock:
            plan = self._state('collection_schedules', key) or {}
            active = self._market_running.get(market['scope'])
            running = active is not None
            job_id = active['job_id'] if active else plan.get('last_job_id')
            saved = self.status(job_id) if job_id else None
            fields = ('job_id', 'source_id', 'status', 'outcome', 'phase', 'records',
                      'empty_codes', 'failures', 'started_at', 'finished_at', 'error')
            job = ({key: saved.get(key) for key in fields} if saved is not None else None)
            jobs = [{key: saved.get(key) for key in fields}
                    for identity in plan.get('last_job_ids', [])
                    if (saved := self.status(identity)) is not None]
        return {'market': market['scope'], 'enabled': plan.get('enabled', False),
                'running': running, 'scheduler_running': self._thread is not None and self._thread.is_alive(),
                'next_run_at': plan.get('next_at'),
                'last_run_at': plan.get('last_run_at'), 'last_outcome': plan.get('last_outcome'),
                'schedule_error': plan.get('error'), 'scheduler_error': self._scheduler_error,
                'job': job, 'jobs': jobs}

    def _renew_calendar(self, market, plan, now):
        source_id = market.get('calendar_source')
        if not source_id:
            raise CalendarCoverageError('calendar source is not registered')
        source = self.sources.get(source_id)
        if (source.get('data_type') != 'calendar' or not source.get('dataset_id')
                or market.get('calendar') != 'calendar:' + source['dataset_id']):
            raise CalendarCoverageError('calendar source does not match the market calendar')
        today = datetime.fromtimestamp(now, ZoneInfo(market['timezone'])).date()
        try:
            _, coverage = self._market_calendar(market)
        except CalendarCoverageError:
            coverage = ()
        previous = [left for left, _ in coverage if left <= today]
        last = date.fromisoformat(plan['last_session']) if plan.get('last_session') else None
        start = min(last + timedelta(days=1), today) if last else min(previous, default=today - timedelta(days=31))
        for left, right in coverage:
            if left > start:
                break
            if right >= start:
                start = right + timedelta(days=1)
        end = max(start, today) + timedelta(days=31)
        following = [left for left, _ in coverage if left > start]
        if following:
            end = min(end, min(following) - timedelta(days=1))
        job = self.run({'source_id': source_id, 'start': start.isoformat(), 'end': end.isoformat()})
        if job.get('outcome') != 'succeeded':
            raise CalendarCoverageError('calendar collection failed: ' + str(job.get('error') or job.get('outcome')))

    def _tick_market(self, plan, now):
        key = 'market:' + plan['market']
        started = time.monotonic()
        attempted = False
        active = False
        try:
            market = self._market_config(plan['market'])
            with self._lock:
                current = self._state('collection_schedules', key)
                if self._closed or not current or not current.get('enabled'):
                    return
                self._market_running[market['scope']] = {
                    'thread': threading.get_ident(), 'job_id': None, 'market_job': False}
                active = True
            interval = plan.get('poll_seconds')
            next_at = plan['next_at']
            if not interval:
                try:
                    next_at = self._market_next(market, plan, now)
                except CalendarCoverageError:
                    attempted = True
                    self._renew_calendar(market, plan, now)
                    next_at = self._market_next(market, plan, now)
            if next_at is None or next_at > now:
                with self._lock:
                    current = self._state('collection_schedules', key)
                    if current and current.get('enabled'):
                        current.update(next_at=next_at, error=None)
                        self._state('collection_schedules', key, current)
                return
            local = datetime.fromtimestamp(now, ZoneInfo(market['timezone']))
            if interval:
                end = local.date()
                if self._session_at(market, plan, end) > now:
                    end -= timedelta(days=1)
            else:
                sessions, _ = self._market_calendar(market)
                due = [day for day in sessions if self._session_at(market, plan, day) <= now]
                if not due:
                    raise ValueError('calendar has no completed session')
                end = due[-1]
            with self._lock:
                current = self._state('collection_schedules', key)
                if self._closed or not current or not current.get('enabled'):
                    return
                attempted = True
                current['last_job_ids'] = []
                self._state('collection_schedules', key, current)
                self._market_running[market['scope']]['market_job'] = True
            result = self.run({'market': market['scope'], 'command': 'latest', 'end': end.isoformat()})
            outcome = result.get('outcome')
            if outcome not in {'succeeded', 'partial', 'failed', 'interrupted'}:
                raise ValueError('market collection returned an invalid outcome')
            error = result.get('error')
            jobs = result.get('jobs', [])
            job_id = jobs[-1].get('job_id') if jobs else None
            with self._lock:
                current = self._state('collection_schedules', key)
                if current is None:
                    return
                current.update(last_run_at=local.isoformat(), last_outcome=outcome, error=error)
                current['last_job_ids'] = [job['job_id'] for job in jobs if job.get('job_id')]
                if job_id is not None:
                    current['last_job_id'] = job_id
                if outcome == 'succeeded':
                    current['retry_at'] = None
                    if not interval:
                        current['last_session'] = end.isoformat()
                else:
                    current['retry_at'] = now + int(time.monotonic() - started) + 300
                if current.get('enabled'):
                    try:
                        if interval:
                            current['next_at'] = current['retry_at'] or now + (time.monotonic() - started) + interval
                        else:
                            current['next_at'] = self._market_next(market, current, now)
                    except Exception as error:
                        current.update(error=f'{type(error).__name__}: {error}',
                                       next_at=now + int(time.monotonic() - started) + 300)
                self._state('collection_schedules', key, current)
        except Exception as error:
            with self._lock:
                current = self._state('collection_schedules', key)
                if current:
                    current['error'] = f'{type(error).__name__}: {error}'
                    if attempted:
                        current.update(last_outcome='failed', last_run_at=datetime.fromtimestamp(
                            now, ZoneInfo(market['timezone'])).isoformat())
                        active_job = self._market_running.get(plan['market'], {}).get('job_id')
                        if active_job:
                            current['last_job_id'] = active_job
                    if current.get('enabled'):
                        current['next_at'] = now + int(time.monotonic() - started) + 300
                    self._state('collection_schedules', key, current)
        finally:
            if active:
                with self._lock:
                    self._market_running.pop(plan['market'], None)

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
        with self._lock:
            if not self._closed and self._market_thread is not None:
                self._market_tasks.put(None)
            self._closed = True
            self._stop.set()
            self._cancelled.update(self._jobs)
        if self._fetch_thread is not None:
            self._fetch_thread.join(timeout=5)
        for thread in (self._thread, self._market_thread):
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
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
        now = time.time()
        with self._lock, self.data.transaction():
            for market in self.sources.describe().get('markets', ()):
                collect = market.get('collect') or {}
                interval = collect.get('poll_seconds')
                if interval is None:
                    continue
                owner = collect.get('auto_with', market['scope'])
                plan = self._state('collection_schedules', 'market:' + owner)
                if plan is not None:
                    current = self._state('collection_schedules', 'market:' + market['scope'])
                    if (current and plan['enabled'] and current.get('enabled')
                            and current.get('poll_seconds') == interval and current.get('error')):
                        continue
                    self._save_market_plan(market, plan['enabled'], now)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(1):
            try:
                self._run_tick(time.time(), self._launch_market)
            except Exception as error:
                # One bad tick must not end the scheduler: before this, a single
                # rejected overlapping collection killed the thread for good.
                self._scheduler_error = f'{type(error).__name__}: {error}'
            else:
                self._scheduler_error = None

    def tick(self, at=None):
        self._run_tick(time.time() if at is None else at, self._tick_market)

    def _run_tick(self, now, market_runner):
        if not self._schedule_lock.acquire(blocking=False):
            return
        try:
            if not self._closed:
                self._tick(now, market_runner)
        finally:
            self._schedule_lock.release()

    def _launch_market(self, schedule, now):
        with self._lock:
            if self._closed:
                return
            if self._market_thread is None or not self._market_thread.is_alive():
                worker = threading.Thread(target=self._run_markets, daemon=True)
                worker.start()
                self._market_thread = worker
            self._market_pending = True
            self._market_tasks.put((schedule, now))

    def _run_markets(self):
        while (task := self._market_tasks.get()) is not None:
            try:
                self._tick_market(*task)
            except Exception as error:
                self._scheduler_error = f'{type(error).__name__}: {error}'
            finally:
                with self._lock:
                    self._market_pending = False

    def _tick(self, now, market_runner):
        schedules = self._states('collection_schedules')
        # Check bounded observation polls before starting a full market scan.
        for schedule in sorted(schedules, key=lambda item: not item.get('poll_seconds')):
            next_at = schedule.get("next_at")
            if (not schedule.get("enabled") or next_at is None or
                    next_at > now):
                continue
            if schedule.get('kind') == 'market':
                if not self._market_pending:
                    market_runner(schedule, now)
                continue
            self._tick_source(schedule, now)

    def _tick_source(self, schedule, now):
        source_id = schedule["source_id"]
        with self._lock:
            if self._closed or self._state('collection_schedules', source_id) != schedule:
                return
        request = dict(schedule.get("request", {}))
        if schedule.get("window_days", 0) and "start" in request and "end" in request:
            today = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date()
            request["start"] = (today - timedelta(days=schedule["window_days"])).isoformat()
            request["end"] = today.isoformat()
        request["source_id"] = source_id
        changes = {"last_run_at": datetime.fromtimestamp(now).isoformat()}
        try:
            result = self.run(request)
            changes["last_job_id"] = result.get("job_id")
            outcome = result["outcome"]
            error = None if outcome == 'succeeded' else (result.get("error") or outcome)
        except Exception as failure:
            error = f'{type(failure).__name__}: {failure}'
        interval = int(schedule.get("interval_seconds", 60))
        delay = min(300, interval) if error else interval
        changes.update(error=error, next_at=now + delay)
        with self._lock:
            if self._state('collection_schedules', source_id) == schedule:
                self._state('collection_schedules', source_id, {**schedule, **changes})

    def handle(self, operation, value):
        value = value if isinstance(value, dict) else {}
        if operation == "collection.sources":
            jobs = self._states('collection_jobs')
            return [self._source_view(source, jobs) for source in self.sources.list()]
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
            return self._market_status(value['market'])
        if operation == "collection.console":
            return self._console_view(value['market'])
        if operation == "system.console":
            return self._system_view()
        if operation == "collection.set_automatic":
            return self._market_plan(value['market'], value['enabled'])
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



    def _source_view(self, source, jobs=()):
        source_id = source["id"]
        job = None
        for item in jobs if jobs else self._states('collection_jobs'):
            if item.get("source_id") == source_id:
                if job is None or (item.get('started_at') or '') > (job.get('started_at') or ''):
                    job = item
        try:
            schedule = self._state('collection_schedules', source_id)
            if schedule is None:
                raise KeyError(source_id)
        except Exception:
            schedule = None
        if source_id in self._records_cache:
            summary, records, integrity_error = self._records_cache[source_id]
        else:
            try:
                snapshot = self.methods.records(source, 0, 0, include_audit=False)
                summary = None if snapshot is None else {
                    key: snapshot.get(key) for key in
                    ("snapshot_id", "collected_at", "data_as_of", "scope")
                }
                records = 0 if snapshot is None else snapshot.get("records_count", len(snapshot.get("records", [])))
                integrity_error = None
            except Exception as error:
                summary, records, integrity_error = None, 0, str(error)
            self._records_cache[source_id] = (summary, records, integrity_error)
        return {
            "id": source_id, "name": source["name"], "frequency": source["frequency"],
            "defaults": deepcopy(source["defaults"]), "job": job,
            "schedule": schedule, "integrity_error": integrity_error,
            "snapshot": summary, "records": records,
        }
