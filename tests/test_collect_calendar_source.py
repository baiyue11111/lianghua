"""Calendar renewal uses the normal registered source and collection job."""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from collect.core import Collect
from collect.methods import BaostockDaily, Methods
from collect.sources import Sources
from market import build_data
from protocol import Filter, TableQuery


SOURCE = {'id': 'calendar_source', 'name': 'Calendar', 'method': 'baostock_calendar',
          'frequency': 'daily', 'defaults': {}, 'data_type': 'calendar', 'dataset_id': 'test'}
MARKET = {'scope': 'TEST', 'calendar': 'calendar:test', 'calendar_source': SOURCE['id']}


class Provider:
    def __init__(self, *, omit=None, error=False):
        self.omit, self.error, self.logged_out = omit, error, False

    def login(self):
        return SimpleNamespace(error_code='0')

    def logout(self):
        self.logged_out = True

    def query_trade_dates(self, *, start_date, end_date):
        if self.error:
            raise OSError('calendar network unavailable')
        first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
        rows = []
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            if day.isoformat() == self.omit:
                continue
            # Provider says Nov 18 is a holiday; do not infer weekdays.
            open_ = day.weekday() < 5 and day.isoformat() != '2026-11-18'
            rows.append([day.isoformat(), '1' if open_ else '0'])
        class Result:
            error_code = '0'
            fields = ['calendar_date', 'is_trading_day']
            def __init__(self, values): self.rows = iter(values)
            def next(self):
                try: self.row = next(self.rows); return True
                except StopIteration: return False
            def get_row_data(self): return self.row
        return Result(rows)


class CalendarSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bus = build_data(Path(self.temp.name))
        self.addCleanup(self.bus.close)
        self.bus.save_calendar('test', {'sessions': ['2026-11-12', '2026-11-13'],
            'coverage': [{'start': '2026-11-12', 'end': '2026-11-13'}]})
        self.sources = Sources(self.bus)
        self.sources.register(SOURCE)
        self.collect = Collect(self.sources, Methods(self.bus))
        self.addCleanup(self.collect.close)

    def run_calendar(self, provider):
        with patch.dict(sys.modules, {'baostock': provider}), \
             patch.object(BaostockDaily, '_call', lambda self, fn, *args, **kwargs: fn(*args, **kwargs)):
            return self.collect.run({'source_id': SOURCE['id'],
                                     'start': '2026-11-14', 'end': '2026-11-20'})

    def test_refresh_uses_collection_job_and_preserves_holidays_and_history(self):
        provider = Provider()
        job = self.run_calendar(provider)
        self.assertEqual((job['status'], job['outcome']), ('finished', 'succeeded'))
        self.assertEqual(job['records'], 7)
        self.assertTrue(provider.logged_out)
        calendar = self.bus.calendar('test')
        self.assertEqual(calendar['coverage'], [{'start': '2026-11-12', 'end': '2026-11-20'}])
        self.assertEqual(calendar['sessions'], ['2026-11-12', '2026-11-13', '2026-11-16',
                                                '2026-11-17', '2026-11-19', '2026-11-20'])
        audits = self.bus.scan(TableQuery('collection_audit')).table.to_pylist()
        events = [json.loads(row['settings']) for row in audits]
        self.assertTrue(any(event.get('snapshot_id') == job['snapshot_id']
                            and 'calendar_date' in json.dumps(event.get('raw')) for event in events))

    def test_incomplete_or_failed_provider_never_changes_calendar(self):
        for provider in (Provider(omit='2026-11-18'), Provider(error=True)):
            with self.subTest(provider=provider):
                before = self.bus.calendar('test')
                job = self.run_calendar(provider)
                self.assertEqual((job['status'], job['outcome']), ('finished', 'failed'))
                self.assertEqual(self.bus.calendar('test'), before)
                self.assertTrue(provider.logged_out)

    def test_failed_calendar_write_rolls_back_the_whole_update(self):
        before = self.bus.calendar('test')
        save = self.bus.save_calendar
        def fail_after_write(*args):
            save(*args)
            raise OSError('write failed')
        with patch.object(self.bus, 'save_calendar', side_effect=fail_after_write):
            job = self.run_calendar(Provider())
        self.assertEqual(job['outcome'], 'failed')
        self.assertEqual(self.bus.calendar('test'), before)

    def test_other_registered_calendar_provider_can_open_a_weekend(self):
        source = {**SOURCE, 'id': 'other_calendar', 'method': 'other_calendar'}
        self.sources.register(source)
        self.collect.methods.handlers['other_calendar'] = lambda descriptor, request: {
            'records': [
                {'date': '2026-11-14', 'is_trading_day': True, 'raw_page': 0},
                {'date': '2026-11-15', 'is_trading_day': False, 'raw_page': 0},
                {'date': '2026-11-16', 'is_trading_day': True, 'raw_page': 0}],
            'raw': [{'provider': 'other'}], 'scope': {},
        }
        job = self.collect.run({'source_id': 'other_calendar',
                                'start': '2026-11-14', 'end': '2026-11-16'})
        self.assertEqual(job['outcome'], 'succeeded')
        self.assertEqual(self.bus.calendar('test')['sessions'],
                         ['2026-11-12', '2026-11-13', '2026-11-14', '2026-11-16'])

    def test_other_provider_must_explicitly_cover_every_calendar_day(self):
        source = {**SOURCE, 'id': 'other_calendar', 'method': 'other_calendar'}
        self.sources.register(source)
        self.collect.methods.handlers['other_calendar'] = lambda descriptor, request: {
            'records': [
                {'date': '2026-11-14', 'is_trading_day': True, 'raw_page': 0},
                {'date': '2026-11-16', 'is_trading_day': True, 'raw_page': 0}],
            'raw': [{'provider': 'other'}], 'scope': {},
        }
        before = self.bus.calendar('test')
        job = self.collect.run({'source_id': 'other_calendar',
                                'start': '2026-11-14', 'end': '2026-11-16'})
        self.assertEqual(job['outcome'], 'failed')
        self.assertIn('cover every requested date', job['error'])
        self.assertEqual(self.bus.calendar('test'), before)

    def test_source_cannot_write_another_calendar_dataset(self):
        source = {**SOURCE, 'id': 'other_calendar', 'method': 'other_calendar'}
        self.sources.register(source)
        self.collect.methods.handlers['other_calendar'] = lambda descriptor, request: {
            'records': [{'date': '2026-11-14', 'is_trading_day': True, 'raw_page': 0}],
            'raw': [{'provider': 'other'}], 'scope': {}, 'dataset_id': 'other',
        }
        before = self.bus.calendar('test')
        job = self.collect.run({'source_id': 'other_calendar',
                                'start': '2026-11-14', 'end': '2026-11-14'})
        self.assertEqual(job['outcome'], 'failed')
        self.assertIn('dataset', job['error'])
        self.assertEqual(self.bus.calendar('test'), before)

    def test_first_fetch_initializes_an_empty_calendar(self):
        with tempfile.TemporaryDirectory() as folder:
            bus = build_data(Path(folder))
            try:
                sources = Sources(bus)
                sources.register(SOURCE)
                collector = Collect(sources, Methods(bus))
                try:
                    provider = Provider()
                    with patch.dict(sys.modules, {'baostock': provider}), \
                         patch.object(BaostockDaily, '_call', lambda self, fn, *args, **kwargs: fn(*args, **kwargs)):
                        job = collector.run({'source_id': SOURCE['id'],
                                             'start': '2026-11-14', 'end': '2026-11-20'})
                    self.assertEqual((job['status'], job['outcome']), ('finished', 'succeeded'), job['error'])
                    self.assertEqual(bus.calendar('test'), {
                        'sessions': ['2026-11-16', '2026-11-17', '2026-11-19', '2026-11-20'],
                        'coverage': [{'start': '2026-11-14', 'end': '2026-11-20'}],
                    })
                finally:
                    collector.close()
            finally:
                bus.close()

    def test_sessions_without_coverage_are_not_treated_as_a_new_calendar(self):
        query = TableQuery('calendar_coverage', (Filter('dataset', 'eq', 'test'),))
        self.bus.delete(query)
        sessions = self.bus.scan(TableQuery('calendar_sessions', (Filter('dataset', 'eq', 'test'),))).table.to_pylist()
        job = self.run_calendar(Provider())
        self.assertEqual(job['outcome'], 'failed')
        self.assertIn('without coverage', job['error'])
        self.assertEqual(self.bus.scan(query).table.num_rows, 0)
        self.assertEqual(self.bus.scan(TableQuery('calendar_sessions', (Filter('dataset', 'eq', 'test'),))).table.to_pylist(), sessions)

    def test_gap_remains_unknown_until_it_is_collected(self):
        def collect(start, end):
            with patch.dict(sys.modules, {'baostock': Provider()}), \
                 patch.object(BaostockDaily, '_call', lambda self, fn, *args, **kwargs: fn(*args, **kwargs)):
                return self.collect.run({'source_id': SOURCE['id'], 'start': start, 'end': end})

        job = collect('2026-11-19', '2026-11-20')
        self.assertEqual(job['outcome'], 'succeeded', job['error'])
        self.assertEqual(self.bus.calendar('test')['coverage'], [
            {'start': '2026-11-12', 'end': '2026-11-13'},
            {'start': '2026-11-19', 'end': '2026-11-20'},
        ])
        rows = self.collect.methods.records(SOURCE)['records']
        self.assertEqual([row['date'] for row in rows],
                         ['2026-11-12', '2026-11-13', '2026-11-19', '2026-11-20'])

        job = collect('2026-11-14', '2026-11-18')
        self.assertEqual(job['outcome'], 'succeeded', job['error'])
        self.assertEqual(self.bus.calendar('test')['coverage'], [
            {'start': '2026-11-12', 'end': '2026-11-20'},
        ])
        rows = self.collect.methods.records(SOURCE)['records']
        self.assertEqual(len(rows), 9)
        self.assertFalse(next(row for row in rows if row['date'] == '2026-11-18')['is_trading_day'])

    def test_conflicting_overlap_does_not_replace_recorded_sessions(self):
        before = self.bus.calendar('test')
        # Previously recorded Friday is now reported as a holiday.
        class Revised(Provider):
            def query_trade_dates(self, **kwargs):
                result = super().query_trade_dates(**kwargs)
                rows = list(result.rows)
                for row in rows:
                    if row[0] == '2026-11-13': row[1] = '0'
                return type(result)(rows)
        with patch.dict(sys.modules, {'baostock': Revised()}), \
             patch.object(BaostockDaily, '_call', lambda self, fn, *args, **kwargs: fn(*args, **kwargs)):
            job = self.collect.run({'source_id': SOURCE['id'],
                                    'start': '2026-11-12', 'end': '2026-11-20'})
        self.assertEqual(job['outcome'], 'failed')
        self.assertIn('conflict', job['error'])
        self.assertEqual(self.bus.calendar('test'), before)

    def test_registry_explicitly_binds_shared_calendar_source(self):
        path = Path(__file__).resolve().parents[1] / 'data/source/registry/sources.json'
        registry = json.loads(path.read_text())
        markets = [item for item in registry['markets'] if item['scope'] in {'CN_A', 'CN_A_SH_SZ'}]
        self.assertEqual(len(markets), 2)
        bindings = {item['calendar_source'] for item in markets}
        self.assertEqual(bindings, {'cn_a_calendar'})
        sources = [item for item in registry['sources'] if item['id'] in bindings]
        self.assertEqual(len(sources), 1)
        self.assertEqual((sources[0]['method'], sources[0]['data_type'], sources[0]['dataset_id']),
                         ('baostock_calendar', 'calendar', 'cn_a'))


if __name__ == '__main__':
    unittest.main()
