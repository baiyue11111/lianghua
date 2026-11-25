"""Market collection schedules share the normal Collect execution path."""
import json
import tempfile
import sys
import unittest
from datetime import datetime
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import patch
from zoneinfo import ZoneInfo

from collect.core import Collect
from collect.methods import Methods
from collect.methods import BaostockDaily
from collect.sources import Sources
from market import build_data
from tests.test_collect_calendar_source import Provider, SOURCE


TZ = ZoneInfo('Asia/Shanghai')


def instant(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp()


class AutomaticMarketTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.bus = build_data(Path(self.folder.name))
        self.addCleanup(self.bus.close)
        self.bus.update_registry(lambda registry: {**registry, 'markets': [{
            'scope': 'TEST', 'name': '测试', 'timezone': 'Asia/Shanghai',
            'session_close': '15:00', 'calendar': 'calendar:test',
            'collect': {'default': ['daily'], 'data': {'daily': {'source': 'daily'}}},
        }]})
        self.bus.save_calendar('test', {
            'sessions': ['2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13', '2026-11-16'],
            'coverage': [{'start': '2026-11-09', 'end': '2026-11-16'}],
        })
        self.collect = Collect(Sources(self.bus), Methods(self.bus))
        self.addCleanup(self.collect.close)

    def enable(self, now, *, catchup=False):
        if not catchup:
            day = datetime.fromtimestamp(now, TZ).date().isoformat()
            sessions = self.bus.calendar('test')['sessions']
            prior = [item for item in sessions if item < day]
            self.collect._state('collection_schedules', 'market:TEST', {
                'kind': 'market', 'market': 'TEST', 'enabled': False,
                'last_session': prior[-1] if prior else None, 'last_run_at': None,
                'last_outcome': None, 'error': None, 'last_job_id': None,
                'retry_at': None, 'delay_seconds': 1800,
            })
        with patch('collect.core.time.time', return_value=now):
            return self.collect.handle('collection.set_automatic', {'market': '测试', 'enabled': True})

    def test_market_schedule_uses_calendar_and_runs_latest_after_close(self):
        state = self.enable(instant(11, 15, 0))
        self.assertEqual(state['market'], 'TEST')
        self.assertTrue(state['enabled'])
        self.assertEqual(state['next_run_at'], instant(11, 15, 30))
        with patch.object(self.collect, 'run', return_value={'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}) as run:
            self.collect.tick(instant(11, 15, 29))
            run.assert_not_called()
            self.collect.tick(instant(11, 15, 30))
            self.collect.tick(instant(11, 16, 0))
            run.assert_called_once_with({'market': 'TEST', 'command': 'latest', 'end': '2026-11-11'})
        state = self.collect.handle('collection.automatic', {'market': 'TEST'})
        self.assertEqual(state['last_outcome'], 'succeeded')
        self.assertEqual(state['next_run_at'], instant(12, 15, 30))

    def test_console_state_is_a_bounded_subset_of_the_diagnostic_view(self):
        now = instant(11, 15, 0)
        self.enable(now)
        diagnostic = self.collect.handle('collection.automatic', {'market': 'TEST'})
        console = self.collect.handle('collection.console', {'market': '测试'})
        self.assertTrue(console['enabled'])
        self.assertFalse(console['running'])
        # A saved plan does not start a thread by itself; the console must say
        # so instead of implying the scheduler is live.
        self.assertFalse(console['scheduler'])
        self.assertEqual(console['next_run_at'], diagnostic['next_run_at'])
        self.assertIsInstance(console['at'], float)
        # A one-second stream must not carry the whole failure history.
        self.assertLess(len(json.dumps(console, ensure_ascii=False)), 600)
        for key in ('jobs', 'failures', 'sessions', 'history'):
            self.assertNotIn(key, console)

    def test_linked_observation_market_shares_switch_but_uses_its_own_cutoff(self):
        now = instant(11, 16)
        self.enable(now)
        self.bus.update_registry(lambda registry: {**registry, 'markets': [*registry['markets'], {
            'scope': 'OBS', 'name': '观察', 'timezone': 'America/New_York', 'session_close': '16:00',
            'collect': {'default': ['price'], 'data': {'price': {'source': 'price'}},
                        'auto_with': 'TEST', 'poll_seconds': 3600},
        }]})
        self.collect.close()
        self.collect = Collect(Sources(self.bus), Methods(self.bus))
        self.addCleanup(self.collect.close)
        result = {'outcome': 'succeeded', 'jobs': [], 'error': None}
        with patch('collect.core.time.time', return_value=now), patch.object(self.collect, '_loop'), \
             patch.object(self.collect, 'run', return_value=result) as run:
            self.collect.start()
            self.assertTrue(self.collect.handle('collection.automatic', {'market': 'OBS'})['enabled'])
            self.collect.tick(now)
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                {'market': 'OBS', 'command': 'latest', 'end': '2026-11-10'},
                {'market': 'TEST', 'command': 'latest', 'end': '2026-11-11'},
            ])
        with patch('collect.core.time.time', return_value=now):
            self.collect.handle('collection.set_automatic', {'market': 'TEST', 'enabled': False})
        self.assertFalse(self.collect.handle('collection.automatic', {'market': 'OBS'})['enabled'])

    def test_observation_latest_requests_dates_beyond_its_stored_calendar(self):
        source = self.collect.sources.register({
            'id': 'observations', 'name': 'Observations', 'method': 'fixture',
            'data_type': 'daily', 'frequency': 'daily', 'dataset_id': 'observations',
            'fields': ['close'], 'defaults': {},
        })
        self.bus.put_rows('observed_prices', [
            {'code': 'fred.other', 'date': '2026-11-10', 'close': 100.0},
        ], keys=('code', 'date'))
        self.bus.bind('observations', 'bars', 'lake:observed_prices')
        self.bus.save_calendar('observations', {'sessions': ['2026-11-09', '2026-11-10'],
            'coverage': [{'start': '2026-11-09', 'end': '2026-11-10'}]})
        self.bus.update_registry(lambda registry: {**registry, 'markets': [*registry['markets'], {
            'scope': 'OBS', 'timezone': 'America/New_York', 'session_close': '16:00',
            'collect': {'default': ['price'], 'data': {
                'price': {'source': source['id'], 'codes': ['fred.other']},
            }, 'poll_seconds': 3600},
        }]})
        with patch.object(self.collect, '_run', return_value={'records': 1}) as run:
            result = self.collect.run({'market': 'OBS', 'command': 'latest', 'end': '2026-11-11'})
        self.assertEqual(result['outcome'], 'succeeded')
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1]['end'], '2026-11-11')
        self.assertEqual(run.call_args.kwargs['starts'], {'fred.other': '2026-11-10'})
        self.assertIsNone(run.call_args.kwargs['missing'])

    def test_start_preserves_observation_poll_retry(self):
        now = instant(11, 16)
        self.bus.update_registry(lambda registry: {**registry, 'markets': [*registry['markets'], {
            'scope': 'OBS', 'timezone': 'America/New_York', 'session_close': '16:00',
            'collect': {'default': ['price'], 'data': {'price': {'source': 'price'}},
                        'auto_with': 'TEST', 'poll_seconds': 3600},
        }]})
        self.enable(now)
        self.collect._state('collection_schedules', 'market:OBS', {
            'kind': 'market', 'market': 'OBS', 'enabled': True, 'poll_seconds': 3600,
            'next_at': now + 300, 'retry_at': now + 300, 'error': 'provider unavailable',
        })
        with patch('collect.core.time.time', return_value=now), patch.object(self.collect, '_loop'):
            self.collect.start()
        state = self.collect.handle('collection.automatic', {'market': 'OBS'})
        self.assertEqual(state['next_run_at'], now + 300)
        self.assertEqual(state['schedule_error'], 'provider unavailable')

    def test_registered_stock_markets_collect_index_prices_and_facts_by_default(self):
        registry = json.loads((Path(__file__).resolve().parents[1] /
                               'data/source/registry/sources.json').read_text())
        sources = {source['id']: source for source in registry['sources']}
        for market in (item for item in registry['markets'] if item['scope'] in ('CN_A', 'CN_A_SH_SZ')):
            collect = market['collect']
            selected = [sources[collect['data'][name]['source']] for name in collect['default']]
            index = collect['data']['cn_index']
            self.assertEqual(collect['default'][0], 'cn_index')
            self.assertIn('sh.000300', index['codes'])
            self.assertEqual(sources[index['source']]['data_type'], 'daily')
            kinds = {source.get('params', {}).get('kind') for source in selected}
            self.assertTrue({'security', 'adjust', 'dividend'} <= kinds)

    def test_market_status_reports_each_source_job(self):
        self.enable(instant(11, 15))
        jobs = [{'job_id': name, 'source_id': name, 'status': 'finished',
                 'outcome': 'succeeded', 'phase': 'publish', 'records': 1,
                 'failures': [], 'empty_codes': []} for name in ('prices', 'adjust')]
        for job in jobs:
            self.collect._save(job)
        result = {'market': 'TEST', 'outcome': 'succeeded', 'jobs': jobs,
                  'failures': [], 'error': None}
        with patch.object(self.collect, 'run', return_value=result):
            self.collect.tick(instant(11, 15, 30))
        state = self.collect.handle('collection.automatic', {'market': 'TEST'})
        self.assertEqual([job['source_id'] for job in state['jobs']], ['prices', 'adjust'])

    def test_market_scoped_source_runs_once_without_security_codes(self):
        self.collect.sources.register({'id': 'events', 'name': 'Events', 'method': 'fixture',
            'frequency': 'daily', 'defaults': {}, 'dataset_id': 'test',
            'params': {'key_fields': ['id']}})
        def bind(registry):
            market = registry['markets'][0]
            market['collect'] = {'default': ['events'],
                                 'data': {'events': {'source': 'events', 'scope': 'market'}}}
            return registry
        self.bus.update_registry(bind)
        calls = []
        def fetch(_source, request):
            calls.append(request)
            return {'records': [], 'raw': [], 'scope': {}}
        self.collect.methods.handlers['fixture'] = fetch
        result = self.collect.run({'market': 'TEST', 'command': 'latest', 'end': '2026-11-11'})
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('codes', calls[0])
        self.assertEqual((calls[0]['start'], calls[0]['end']), ('2026-11-09', '2026-11-11'))

    def test_market_scoped_source_resumes_from_first_uncovered_interval(self):
        self.collect.sources.register({'id': 'events', 'name': 'Events', 'method': 'fixture',
            'frequency': 'daily', 'defaults': {}, 'dataset_id': 'test',
            'params': {'key_fields': ['id']}})
        def bind(registry):
            registry['markets'][0]['collect'] = {'default': ['events'],
                'data': {'events': {'source': 'events', 'scope': 'market'}}}
            return registry
        self.bus.update_registry(bind)
        for identity, start, end in [('a', '2026-11-09', '2026-11-10'),
                                     ('b', '2026-11-12', '2026-11-13')]:
            self.collect._save({'job_id': identity, 'source_id': 'events', 'status': 'finished',
                                'outcome': 'succeeded', 'request': {'start': start, 'end': end}})
        calls = []
        self.collect.methods.handlers['fixture'] = lambda _source, request: (
            calls.append(request) or {'records': [], 'raw': [], 'scope': {}})
        self.collect.run({'market': 'TEST', 'command': 'latest', 'end': '2026-11-13'})
        self.assertEqual(calls[0]['start'], '2026-11-11')

    def test_check_market_scoped_source_reports_unknown_coverage(self):
        self.collect.sources.register({'id': 'events', 'name': 'Events', 'method': 'fixture',
            'frequency': 'daily', 'defaults': {}, 'dataset_id': 'test',
            'params': {'key_fields': ['id']}})
        def bind(registry):
            registry['markets'][0]['collect'] = {'default': ['events'],
                'data': {'events': {'source': 'events', 'scope': 'market'}}}
            return registry
        self.bus.update_registry(bind)
        result = self.collect.run({'market': 'TEST', 'command': 'check',
                                   'start': '2026-11-09', 'end': '2026-11-13'})
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['plans'][0]['coverage']['status'], 'unknown')

    def test_weekend_and_restart_catch_up_once(self):
        self.enable(instant(13, 16))
        with patch.object(self.collect, 'run', return_value={'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}) as run:
            self.collect.tick(instant(14, 16))
            self.collect.tick(instant(15, 16))
            run.assert_called_once()
        self.collect.close()
        self.collect = Collect(Sources(self.bus), Methods(self.bus))
        with patch.object(self.collect, 'run', return_value={'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}) as run:
            self.collect.tick(instant(16, 15, 29))
            run.assert_not_called()
            self.collect.tick(instant(16, 15, 30))
            run.assert_called_once()

    def test_missing_calendar_coverage_is_reported(self):
        self.enable(instant(11, 15))
        with patch.object(self.collect, 'run') as run:
            self.collect.tick(instant(17, 16))
            run.assert_not_called()
        state = self.collect.handle('collection.automatic', {'market': 'TEST'})
        self.assertIn('calendar', state['schedule_error'].lower())
        self.bus.save_calendar('test', {
            'sessions': ['2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13',
                         '2026-11-16', '2026-11-17', '2026-11-18'],
            'coverage': [{'start': '2026-11-09', 'end': '2026-11-18'}],
        })
        with patch.object(self.collect, 'run', return_value={'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}) as run:
            self.collect.tick(instant(17, 16, 5))
            run.assert_called_once_with({'market': 'TEST', 'command': 'latest', 'end': '2026-11-17'})
        self.assertIsNone(self.collect.handle('collection.automatic', {'market': 'TEST'})['schedule_error'])

    def test_failed_result_is_visible_and_retried(self):
        self.enable(instant(11, 15))
        failed = {'market': 'TEST', 'outcome': 'failed', 'jobs': [],
                  'failures': [{'code': 'X', 'error': 'unavailable'}], 'error': 'unavailable'}
        with patch.object(self.collect, 'run', return_value=failed) as run:
            self.collect.tick(instant(11, 15, 30))
            self.assertEqual(self.collect.handle('collection.automatic', {'market': 'TEST'})['last_outcome'], 'failed')
            self.collect.tick(instant(11, 15, 31))
            self.assertEqual(run.call_count, 1)
            self.collect.tick(instant(11, 15, 35))
            self.assertEqual(run.call_count, 2)

    def test_long_failed_run_waits_after_completion_before_retry(self):
        self.enable(instant(11, 15))
        failed = {'market': 'TEST', 'outcome': 'failed', 'jobs': [],
                  'failures': [{'code': 'X', 'error': 'unavailable'}], 'error': 'unavailable'}
        with patch.object(self.collect, 'run', return_value=failed) as run, \
             patch('collect.core.time.monotonic', side_effect=[0, 600]):
            self.collect.tick(instant(11, 15, 30))
        self.assertEqual(self.collect.handle('collection.automatic', {'market': 'TEST'})['next_run_at'],
                         instant(11, 15, 45))
        with patch.object(self.collect, 'run', return_value=failed) as retry:
            self.collect.tick(instant(11, 15, 40))
            retry.assert_not_called()

    def test_long_raised_error_waits_after_completion_before_retry(self):
        self.enable(instant(11, 15))
        with patch.object(self.collect, 'run', side_effect=ValueError('unavailable')), \
             patch('collect.core.time.monotonic', side_effect=[0, 600]):
            self.collect.tick(instant(11, 15, 30))
        self.assertEqual(self.collect.handle('collection.automatic', {'market': 'TEST'})['next_run_at'],
                         instant(11, 15, 45))

    def test_market_reports_partial_when_some_securities_were_saved(self):
        self.collect.sources.register({'id': 'daily', 'name': 'Daily', 'method': 'fixture',
                                       'frequency': 'daily', 'defaults': {}})
        job = {'outcome': 'partial', 'records': 1,
               'failures': [{'code': 'B', 'error': 'unavailable'}]}
        with patch.object(self.collect, '_run', return_value=job):
            result = self.collect.run({'market': 'TEST', 'command': 'range',
                                       'start': '2026-11-09', 'end': '2026-11-10',
                                       'codes': ['A', 'B']})
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(result['failures'], job['failures'])

    def test_thrown_collection_error_is_visible_and_retried(self):
        self.enable(instant(11, 15))
        with patch.object(self.collect, 'run', side_effect=ValueError('provider unavailable')) as run:
            self.collect.tick(instant(11, 15, 30))
            state = self.collect.handle('collection.automatic', {'market': 'TEST'})
            self.assertEqual(state['last_outcome'], 'failed')
            self.assertIn('provider unavailable', state['schedule_error'])
            self.collect.tick(instant(11, 15, 35))
            self.assertEqual(run.call_count, 2)

    def test_schedule_uses_registered_timezone_including_daylight_saving(self):
        zone = ZoneInfo('America/New_York')
        self.bus.update_registry(lambda registry: {**registry, 'markets': [{
            'scope': 'OTHER', 'timezone': 'America/New_York', 'session_close': '16:00',
            'calendar': 'calendar:other', 'collect': {'default': ['daily'], 'data': {'daily': {'source': 'daily'}}},
        }]})
        self.bus.save_calendar('other', {'sessions': ['2026-03-09', '2026-03-10'],
                                      'coverage': [{'start': '2026-03-09', 'end': '2026-03-10'}]})
        now = datetime(2026, 3, 9, 16, tzinfo=zone).timestamp()
        with patch('collect.core.time.time', return_value=now):
            state = self.collect.handle('collection.set_automatic', {'market': 'OTHER', 'enabled': True})
        self.assertEqual(state['next_run_at'], datetime(2026, 3, 9, 16, 30, tzinfo=zone).timestamp())

    def test_existing_source_schedule_uses_the_same_tick(self):
        self.collect.sources.register({'id': 'news', 'name': 'News', 'method': 'fixture',
                                       'frequency': 'daily', 'defaults': {},
                                       'params': {'key_fields': ['identity']}})
        self.collect.methods.handlers['fixture'] = lambda _source, _request: {
            'records': [], 'raw': [], 'scope': {}}
        with patch('collect.core.time.time', return_value=instant(11, 15)):
            self.collect.schedule('news', {'enabled': True, 'interval_seconds': 60, 'input': {}})
        self.collect.tick(instant(11, 15, 1))
        jobs = [job for job in self.collect._jobs.values() if job['source_id'] == 'news']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['outcome'], 'succeeded', jobs[0])

    def test_resident_scheduler_runs_due_source_during_market_collection(self):
        self.enable(instant(11, 15))
        source = self.collect.sources.register({
            'id': 'quotes_fixture', 'name': 'Quotes', 'method': 'fixture',
            'frequency': 'snapshot', 'defaults': {}, 'params': {'key_fields': ['id']},
        })
        market_entered, market_release, source_finished = Event(), Event(), Event()
        market_repeated = Event()
        market_calls = []
        now = instant(11, 15, 30)
        run = self.collect.run

        def execute(request):
            if 'market' in request:
                market_calls.append(current_thread())
                if len(market_calls) == 2:
                    market_repeated.set()
                market_entered.set()
                market_release.wait(8)
                return {'outcome': 'succeeded', 'jobs': []}
            result = run(request)
            source_finished.set()
            return result

        self.collect.methods.handlers['fixture'] = lambda *_: {
            'records': [{'id': 'one'}], 'raw': [], 'scope': {},
        }
        with patch('collect.core.time.time', side_effect=lambda: now), \
             patch.object(self.collect, 'run', side_effect=execute):
            self.collect.schedule(source['id'], {'enabled': True, 'interval_seconds': 60})
            self.collect.start()
            try:
                self.assertTrue(market_entered.wait(3))
                now = instant(11, 15, 31)
                self.assertTrue(source_finished.wait(3), 'due quote source waited for the market job')
                self.assertEqual(len(market_calls), 1)
                self.assertEqual(self.bus.table(self.bus.source(source['id'])).to_pylist(),
                                 [{'id': 'one'}])
                market_release.set()
                now = instant(12, 15, 30)
                self.assertTrue(market_repeated.wait(3))
                self.assertIs(market_calls[0], market_calls[1])
            finally:
                market_release.set()
                self.collect.close()

    def test_source_schedule_changes_survive_in_flight_job_completion(self):
        source = self.collect.sources.register({
            'id': 'quotes_fixture', 'name': 'Quotes', 'method': 'fixture',
            'frequency': 'snapshot', 'defaults': {}, 'params': {'key_fields': ['id']},
        })
        source_id = source['id']
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                with patch('collect.core.time.time', return_value=instant(11, 15)):
                    self.collect.schedule(source_id, {'enabled': True, 'interval_seconds': 60})
                entered, release = Event(), Event()

                def fetch(*_):
                    entered.set()
                    release.wait(3)
                    return {'records': [], 'raw': [], 'scope': {}}

                self.collect.methods.handlers['fixture'] = fetch
                worker = Thread(target=self.collect.tick, args=(instant(11, 15, 1),))
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    with patch('collect.core.time.time', return_value=instant(11, 15, 1)):
                        updated = self.collect.schedule(source_id, {
                            'enabled': enabled, 'interval_seconds': 120, 'input': {'keyword': 'new'},
                        })
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(self.collect._state('collection_schedules', source_id), updated)

    def test_failed_source_job_is_visible_and_retried(self):
        source = self.collect.sources.register({
            'id': 'quotes_fixture', 'name': 'Quotes', 'method': 'fixture',
            'frequency': 'snapshot', 'defaults': {}, 'params': {'key_fields': ['id']},
        })

        def fetch(*_):
            raise ValueError('provider unavailable')

        self.collect.methods.handlers['fixture'] = fetch
        with patch('collect.core.time.time', return_value=instant(11, 15)):
            self.collect.schedule(source['id'], {'enabled': True, 'interval_seconds': 600})
        self.collect.tick(instant(11, 15, 10))
        plan = self.collect._state('collection_schedules', source['id'])
        self.assertIsNotNone(plan['error'], 'source job failed but schedule error was cleared')
        self.assertIn('provider unavailable', plan['error'])
        self.assertEqual(plan['next_at'], instant(11, 15, 15))
        self.assertEqual(self.collect.status(plan['last_job_id'])['outcome'], 'failed')

    def test_disable_during_collection_remains_disabled(self):
        self.enable(instant(11, 15))
        entered, release = Event(), Event()
        def run(_request):
            entered.set()
            release.wait(3)
            return {'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}
        with patch.object(self.collect, 'run', side_effect=run):
            thread = Thread(target=self.collect.tick, args=(instant(11, 15, 30),))
            thread.start()
            self.assertTrue(entered.wait(3))
            self.collect.handle('collection.set_automatic', {'market': 'TEST', 'enabled': False})
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertFalse(self.collect.handle('collection.automatic', {'market': 'TEST'})['enabled'])

    def test_running_job_status_comes_from_its_state_machine(self):
        self.enable(instant(11, 15))
        entered, release = Event(), Event()
        def fetch(_source, _request):
            entered.set()
            release.wait(3)
            return {'records': [], 'raw': [], 'scope': {}, 'request': {'codes': ['X']}}
        def run(_request):
            job = self.collect._run({'id': 'source'}, {'codes': ['X']})
            return {'market': 'TEST', 'outcome': job['outcome'], 'jobs': [job],
                    'failures': job['failures'], 'error': job['error']}
        with patch.object(self.collect, 'run', side_effect=run), \
                patch.object(self.collect.methods, 'fetch', side_effect=fetch), \
                patch.object(self.collect.methods, 'validate'), \
                patch.object(self.collect.methods, 'publish', return_value={'records': 0, 'failures': [], 'ref': None}), \
                patch.object(self.collect.methods, 'log'):
            thread = Thread(target=self.collect.tick, args=(instant(11, 15, 30),))
            thread.start()
            self.assertTrue(entered.wait(3))
            status = self.collect.handle('collection.automatic', {'market': 'TEST'})
            self.assertTrue(status['running'])
            self.assertEqual(status['job']['status'], 'running')
            self.assertEqual(status['job']['source_id'], 'source')
            release.set()
            thread.join(3)
        status = self.collect.handle('collection.automatic', {'market': 'TEST'})
        self.assertEqual(status['job']['status'], 'finished')
        self.assertEqual(status['job']['outcome'], 'succeeded')
        self.assertEqual(status['job']['empty_codes'], ['X'])

    def test_calendar_end_after_success_does_not_repeat_the_completed_session(self):
        self.enable(instant(16, 15))
        with patch.object(self.collect, 'run', return_value={'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}) as run:
            self.collect.tick(instant(16, 15, 30))
            state = self.collect.handle('collection.automatic', {'market': 'TEST'})
            self.assertEqual(state['last_outcome'], 'succeeded')
            self.assertIn('calendar', state['schedule_error'].lower())
            self.collect.tick(instant(16, 15, 35))
            run.assert_called_once()

    def test_calendar_renewal_keeps_the_last_market_job(self):
        self.calendar_source()
        now = instant(17, 9)
        self.enable(now, catchup=True)
        job = {'job_id': 'market-job', 'source_id': 'daily', 'status': 'finished',
               'outcome': 'succeeded', 'phase': 'publish', 'records': 3,
               'failures': [], 'empty_codes': []}
        self.collect._save(job)
        plan = self.collect._state('collection_schedules', 'market:TEST')
        plan.update(last_session='2026-11-16', last_job_id='market-job', last_outcome='succeeded')
        self.collect._state('collection_schedules', 'market:TEST', plan)
        with patch.dict(sys.modules, {'baostock': Provider()}), \
             patch.object(BaostockDaily, '_call', lambda _self, fn, *args, **kwargs: fn(*args, **kwargs)):
            self.collect.tick(now)
        state = self.collect.handle('collection.automatic', {'market': 'TEST'})
        self.assertEqual(state['last_outcome'], 'succeeded')
        self.assertEqual(state['job']['job_id'], 'market-job')

    def test_removed_market_doctor_is_not_a_collection_command(self):
        self.collect.sources.register({'id': 'daily', 'name': 'Daily', 'method': 'fixture',
                                       'frequency': 'daily', 'defaults': {}})
        with self.assertRaisesRegex(ValueError, 'invalid collection command'):
            self.collect.run({'market': 'TEST', 'command': 'doctor', 'codes': ['A']})

    def test_preopen_restart_catches_up_only_the_last_completed_session(self):
        self.enable(instant(10, 16), catchup=True)
        self.collect.close()
        self.collect = Collect(Sources(self.bus), Methods(self.bus))
        with patch.object(self.collect, 'run', return_value={'market': 'TEST', 'outcome': 'succeeded',
                          'jobs': [], 'failures': [], 'error': None}) as run:
            self.collect.tick(instant(11, 9))
            run.assert_called_once_with({'market': 'TEST', 'command': 'latest', 'end': '2026-11-10'})
            self.collect.tick(instant(11, 9, 5))
            run.assert_called_once()

    def test_market_job_is_linked_before_work_and_survives_interruption(self):
        self.enable(instant(11, 16))
        def run(_request):
            return self.collect._run({'id': 'source'}, {'codes': ['X']})
        with patch.object(self.collect, 'run', side_effect=run), \
             patch.object(self.collect.methods, 'fetch', side_effect=SystemExit), \
             patch.object(self.collect.methods, 'log'):
            thread = Thread(target=self.collect.tick, args=(instant(11, 16),))
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        plan = self.collect._state('collection_schedules', 'market:TEST')
        self.assertIsNotNone(plan['last_job_id'])
        with patch.object(Collect, '_lease_dead', return_value=True):
            restored = Collect(Sources(self.bus), Methods(self.bus))
        try:
            state = restored.handle('collection.automatic', {'market': 'TEST'})
            self.assertEqual(state['job']['job_id'], plan['last_job_id'])
            self.assertEqual(state['job']['outcome'], 'interrupted')
        finally:
            restored.close()

    def test_explicit_latest_end_is_used_as_the_data_cutoff(self):
        self.collect.sources.register({'id': 'daily', 'name': 'Daily', 'method': 'fixture',
                                       'frequency': 'daily', 'defaults': {}})
        calls = []
        def run(_source, request, **_options):
            calls.append(request)
            return {'outcome': 'succeeded', 'records': 1, 'failures': []}
        with patch.object(self.collect.methods, 'last_dates', return_value={'A': '2026-11-09'}), \
             patch.object(self.collect, '_run', side_effect=run):
            self.collect.run({'market': 'TEST', 'command': 'latest', 'codes': ['A'], 'end': '2026-11-10'})
        self.assertEqual(calls[0]['end'], '2026-11-10')

    def test_latest_cutoff_does_not_recollect_when_security_is_already_ahead(self):
        self.collect.sources.register({'id': 'daily', 'name': 'Daily', 'method': 'fixture',
                                       'frequency': 'daily', 'defaults': {},
                                       'params': {'key_fields': ['code', 'date']}})
        with patch.object(self.collect.methods, 'last_dates', return_value={'A': '2026-11-11'}), \
             patch.object(self.collect.methods, 'fetch') as fetch, \
             patch.object(self.collect.methods, 'log'):
            result = self.collect.run({'market': 'TEST', 'command': 'latest',
                                       'codes': ['A'], 'end': '2026-11-10'})
        self.assertEqual(result['outcome'], 'succeeded')
        fetch.assert_not_called()

    def calendar_source(self, *, dataset='test'):
        source = {**SOURCE, 'dataset_id': dataset}
        self.collect.sources.register(source)
        def bind(registry):
            return {**registry, 'markets': [{**item, 'calendar_source': source['id']}
                                             for item in registry['markets']]}
        self.bus.update_registry(bind)

    def test_calendar_is_renewed_by_registered_collection_job(self):
        self.calendar_source()
        now = instant(17, 9)
        state = self.enable(now, catchup=True)
        self.assertEqual(state['next_run_at'], now)
        calls = []
        original = self.collect.run
        def run(request):
            calls.append(request)
            if 'source_id' in request:
                return original(request)
            return {'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}
        with patch.dict(sys.modules, {'baostock': Provider()}), \
             patch.object(BaostockDaily, '_call', lambda _self, fn, *args, **kwargs: fn(*args, **kwargs)), \
             patch.object(self.collect, 'run', side_effect=run):
            self.collect.tick(now)
        self.assertEqual(calls[0], {'source_id': 'calendar_source',
                                    'start': '2026-11-17', 'end': '2026-12-18'})
        self.assertEqual(calls[1], {'market': 'TEST', 'command': 'latest', 'end': '2026-11-16'})
        state = self.collect.handle('collection.automatic', {'market': 'TEST'})
        self.assertEqual(state['last_outcome'], 'succeeded')
        self.assertNotIn('2026-11-18', self.bus.calendar('test')['sessions'])
        self.assertIsNone(state['schedule_error'])

    def test_failed_calendar_job_is_reported_and_retried(self):
        self.calendar_source()
        now = instant(17, 9)
        self.enable(now, catchup=True)
        with patch.dict(sys.modules, {'baostock': Provider(error=True)}), \
             patch.object(BaostockDaily, '_call', lambda _self, fn, *args, **kwargs: fn(*args, **kwargs)):
            self.collect.tick(now)
        state = self.collect.handle('collection.automatic', {'market': 'TEST'})
        self.assertEqual(state['last_outcome'], 'failed')
        self.assertEqual(state['job']['source_id'], 'calendar_source')
        self.assertEqual(state['job']['outcome'], 'failed')
        self.assertIn('calendar', state['schedule_error'].lower())
        self.assertEqual(state['next_run_at'], now + 300)

    def test_calendar_refresh_fills_the_gap_before_later_coverage(self):
        self.bus.save_calendar('test', {
            'sessions': ['2026-11-09', '2026-11-10', '2026-11-16', '2026-11-17'],
            'coverage': [{'start': '2026-11-09', 'end': '2026-11-10'},
                         {'start': '2026-11-16', 'end': '2026-11-20'}],
        })
        self.calendar_source()
        now = instant(11, 9)
        self.enable(now, catchup=True)
        original, calls = self.collect.run, []
        def run(request):
            calls.append(request)
            if 'source_id' in request:
                return original(request)
            return {'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}
        with patch.dict(sys.modules, {'baostock': Provider()}), \
             patch.object(BaostockDaily, '_call', lambda _self, fn, *args, **kwargs: fn(*args, **kwargs)), \
             patch.object(self.collect, 'run', side_effect=run):
            self.collect.tick(now)
        self.assertEqual(calls[0], {'source_id': 'calendar_source',
                                    'start': '2026-11-11', 'end': '2026-11-15'})
        self.assertEqual(calls[1], {'market': 'TEST', 'command': 'latest', 'end': '2026-11-10'})
        self.assertIsNone(self.collect.handle('collection.automatic', {'market': 'TEST'})['schedule_error'])

    def test_catchup_fills_calendar_gap_before_later_covered_session(self):
        self.bus.save_calendar('test', {
            'sessions': ['2026-11-09', '2026-11-10', '2026-11-16', '2026-11-17'],
            'coverage': [{'start': '2026-11-09', 'end': '2026-11-10'},
                         {'start': '2026-11-16', 'end': '2026-11-18'}],
        })
        self.calendar_source()
        self.enable(instant(11, 9))
        now = instant(17, 16)
        original, calls = self.collect.run, []
        def run(request):
            calls.append(request)
            if 'source_id' in request:
                return original(request)
            return {'market': 'TEST', 'outcome': 'succeeded', 'jobs': [], 'failures': [], 'error': None}
        with patch.dict(sys.modules, {'baostock': Provider()}), \
             patch.object(BaostockDaily, '_call', lambda _self, fn, *args, **kwargs: fn(*args, **kwargs)), \
             patch.object(self.collect, 'run', side_effect=run):
            self.collect.tick(now)
        self.assertEqual(calls, [
            {'source_id': 'calendar_source', 'start': '2026-11-11', 'end': '2026-11-15'},
            {'market': 'TEST', 'command': 'latest', 'end': '2026-11-17'},
        ])
        self.assertEqual(self.collect._state('collection_schedules', 'market:TEST')['last_session'], '2026-11-17')

    def test_calendar_source_must_match_the_market_calendar(self):
        self.calendar_source(dataset='other')
        now = instant(17, 9)
        self.enable(now, catchup=True)
        with patch.object(self.collect.methods, 'fetch') as fetch:
            self.collect.tick(now)
        fetch.assert_not_called()
        self.assertIn('calendar', self.collect.handle('collection.automatic', {'market': 'TEST'})['schedule_error'].lower())


if __name__ == '__main__':
    unittest.main()
