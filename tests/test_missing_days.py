"""The missing set is a difference, not a watermark.

Every case here is something a ``max(date)`` per security cannot express: a
hole in the middle of a series, a session before a security listed, a session
after it delisted, and a row that exists but carries no value for a field the
caller asked for.
"""
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from market import build_data

from collect.core import Collect
from collect.methods import Methods

SESSIONS = ['2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08']


def bar(code, day, close=10.0):
    return {'code': code, 'date': day, 'open': close, 'high': close, 'low': close,
            'close': close, 'volume': 100.0, 'amount': 1000.0,
            'close_hfq': close, 'amount_hfq': 1000.0}


class MissingDaysTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.data = build_data(Path(self._tmp.name))
        self.methods = Methods(self.data)
        self.data.put_rows(
            'fx_bars',
            [bar('sh.600000', '2025-01-02'), bar('sh.600000', '2025-01-06'),
             {**bar('sh.600000', '2025-01-07'), 'close': None},
             *[bar('sh.600001', day) for day in SESSIONS]],
            keys=('code', 'date'),
            types={'code': 'VARCHAR', 'date': 'VARCHAR', 'open': 'DOUBLE', 'high': 'DOUBLE',
                   'low': 'DOUBLE', 'close': 'DOUBLE', 'volume': 'DOUBLE', 'amount': 'DOUBLE',
                   'close_hfq': 'DOUBLE', 'amount_hfq': 'DOUBLE'},
            storage='parquet')
        self.data.bind('fx', 'bars', 'lake:fx_bars')
        self.data.save_calendar('fx', {'sessions': SESSIONS,
                                       'coverage': [{'start': SESSIONS[0], 'end': SESSIONS[-1]}]})
        self.data.save_manifest('fx', {'schema': 3, 'calendar': 'calendar:fx',
                                       'inputs': {'bars': 'lake:fx_bars'}, 'fields': {},
                                       'scope': {'symbols': ['sh.600000', 'sh.600001',
                                                             'sz.000001', 'sz.000002'],
                                                 'universe': 'TEST'}})
        for code, listed, delisted in (('sh.600000', '2010-01-04', None),
                                       ('sh.600001', '2025-01-02', None),
                                       ('sz.000001', '2025-01-07', None),
                                       ('sz.000002', '2010-01-04', '2025-01-06')):
            self.data.save_fact({
                'schema': 1, 'kind': 'security', 'code': code,
                'start': None, 'end': None, 'year': None, 'source_hash': 'a' * 64,
                'records': [{'listing_date': listed, 'delisting_date': delisted,
                             'type': '1', 'status': '1' if delisted is None else '0'}]})
        self.source = {'id': 'daily', 'storage_id': 'fx', 'dataset_id': 'fx',
                       'method': 'sina_daily',
                       'fields': ['open', 'high', 'low', 'close', 'volume', 'amount']}

    def tearDown(self):
        self.data._store.close()
        self._tmp.cleanup()

    def missing(self, codes, **kwargs):
        return self.methods.missing_days(self.source, codes, **kwargs)

    def test_a_hole_in_the_middle_is_reported(self):
        self.assertEqual(self.missing(['sh.600000'], start=SESSIONS[0], end=SESSIONS[-1]),
                         {'sh.600000': ['2025-01-03', '2025-01-07', '2025-01-08']})

    def test_a_row_without_a_requested_value_counts_as_absent(self):
        # 2025-01-07 exists for sh.600000 but carries no close.
        self.assertIn('2025-01-07',
                      self.missing(['sh.600000'], start=SESSIONS[0], end=SESSIONS[-1])['sh.600000'])

    def test_a_complete_series_is_left_out_of_the_answer(self):
        self.assertEqual(self.missing(['sh.600001'], start=SESSIONS[0], end=SESSIONS[-1]), {})

    def test_sessions_before_listing_are_not_missing(self):
        self.assertEqual(self.missing(['sz.000001'], start=SESSIONS[0], end=SESSIONS[-1]),
                         {'sz.000001': ['2025-01-07', '2025-01-08']})

    def test_sessions_after_delisting_are_not_missing(self):
        self.assertEqual(self.missing(['sz.000002'], start=SESSIONS[0], end=SESSIONS[-1]),
                         {'sz.000002': ['2025-01-02', '2025-01-03']})

    def test_resume_starts_where_the_security_stopped(self):
        self.assertEqual(self.missing(['sh.600000'], end=SESSIONS[-1]),
                         {'sh.600000': ['2025-01-07', '2025-01-08']})

    def test_an_unknown_code_is_reported_for_every_session_it_could_trade(self):
        self.assertEqual(sorted(self.missing(['sz.000003'], start=SESSIONS[0],
                                             end=SESSIONS[-1])['sz.000003']), SESSIONS)

    def test_a_series_keyed_by_something_other_than_a_security_still_diffs(self):
        self.assertEqual(self.missing(['fred.nasdaqcom'], start=SESSIONS[0], end=SESSIONS[-1]),
                         {'fred.nasdaqcom': SESSIONS})

    def test_both_spellings_of_one_code_get_the_same_answer(self):
        self.assertEqual(
            self.missing(['sh.600000', '600000'], start=SESSIONS[0], end=SESSIONS[-1]),
            {'sh.600000': ['2025-01-03', '2025-01-07', '2025-01-08'],
             '600000': ['2025-01-03', '2025-01-07', '2025-01-08']})

    def test_without_a_calendar_there_is_no_difference_to_take(self):
        empty = build_data(Path(self._tmp.name) / 'bare')
        try:
            self.assertIsNone(Methods(empty).missing_days(self.source, ['sh.600000'], end=SESSIONS[-1]))
        finally:
            empty._store.close()

    def test_coverage_reads_the_same_difference(self):
        coverage = self.methods.daily_coverage(
            'fx', ('sh.600000',), start=date.fromisoformat(SESSIONS[0]),
            end=date.fromisoformat(SESSIONS[-1]), fields=('close',), identity='fx')
        self.assertEqual(coverage['missing'], {'sh.600000': ['2025-01-03', '2025-01-07', '2025-01-08']})
        self.assertFalse(coverage['complete'])
        self.assertEqual(coverage['sessions'], tuple(SESSIONS))


class LatestRequestTests(unittest.TestCase):
    """What ``latest`` asks the provider for, given a difference set."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.data = build_data(Path(self._tmp.name))
        self.fetched = []
        outer = self

        class Stub:
            data = self.data

            def fetch(self, source, request):
                outer.fetched.append((list(request['codes']), request['start'], request['end']))
                return {'records': [], 'raw': [], 'scope': {}, 'request': dict(request)}

            def validate(self, packet):
                pass

            def publish(self, source, packet):
                return {'ref': None, 'records': 0, 'failures': []}

            def records(self, source, offset=0, limit=0):
                return {'snapshot_id': 'fixed'}

        self.collect = Collect(SimpleNamespace(get=lambda name: {'id': 'daily'}), Stub())

    def tearDown(self):
        self.data._store.close()
        self._tmp.cleanup()

    def test_a_security_with_nothing_absent_is_not_requested(self):
        job = self.collect._run({'id': 'daily'}, {'codes': ['sh.600000', 'sh.600001']},
                                missing={'sh.600000': ['2025-01-07', '2025-01-08']})
        self.assertEqual(self.fetched, [(['sh.600000'], '2025-01-07', '2025-01-08')])
        self.assertEqual(job['status'], 'succeeded')

    def test_the_request_covers_the_absent_span_only(self):
        self.collect._run({'id': 'daily'}, {'codes': ['sh.600000']},
                          missing={'sh.600000': ['2025-01-03', '2025-01-07']})
        self.assertEqual(self.fetched, [(['sh.600000'], '2025-01-03', '2025-01-07')])

    def test_an_empty_difference_makes_no_request_at_all(self):
        job = self.collect._run({'id': 'daily'}, {'codes': ['sh.600000']}, missing={})
        self.assertEqual(self.fetched, [])
        self.assertEqual(job['records'], 0)


if __name__ == '__main__':
    unittest.main()