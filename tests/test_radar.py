"""Radar comparisons use independent sessions and actual observation times."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market import build_data
from radar import Radar, turnover

NOW = datetime(2026, 11, 20, 2, 30, tzinfo=timezone.utc)
SESSIONS = ['2026-11-13', '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20']


def quotes():
    return [{'code': 'sh.510999', 'data_at': day + 'T02:30:00+00:00',
             'price': 2, 'amount': 100, 'volume': 50} for day in SESSIONS[:-1]]


class RadarTests(unittest.TestCase):
    def score(self, rows, now=NOW):
        return turnover(rows, now, sessions=SESSIONS, timezone_name='Asia/Shanghai')

    def test_same_time_prior_sessions_detect_threefold_turnover(self):
        result = self.score([*quotes(), {'data_at': NOW.isoformat(), 'amount': 300}])
        self.assertEqual((result['status'], result['ratio'], result['baseline'], result['samples']),
                         ('surge', 3, 100, 5))

    def test_many_quotes_from_one_session_are_only_one_sample(self):
        rows = [{**quotes()[0], 'data_at': (NOW - timedelta(days=1, minutes=n)).isoformat()}
                for n in range(5)]
        result = self.score([*rows, {'data_at': NOW.isoformat(), 'amount': 300}])
        self.assertEqual((result['status'], result['samples'], result['ratio']),
                         ('insufficient', 1, None))

    def test_later_intraday_observations_and_other_times_are_excluded(self):
        rows = [{**row, 'data_at': (datetime.fromisoformat(row['data_at']) + timedelta(minutes=1)).isoformat()}
                for row in quotes()]
        result = self.score([*rows, {'data_at': NOW.isoformat(), 'amount': 300}])
        self.assertEqual((result['samples'], result['ratio']), (0, None))

    def test_weekends_and_samples_outside_session_window_are_excluded(self):
        rows = [*quotes(), {'data_at': '2026-11-15T02:30:00+00:00', 'amount': 1},
                {'data_at': NOW.isoformat(), 'amount': 300}]
        result = turnover(rows, NOW, sessions=SESSIONS, timezone_name='Asia/Shanghai',
                          window_days=3, min_samples=3)
        self.assertEqual((result['samples'], result['baseline']), (3, 100))

    def test_zero_baseline_missing_values_and_old_values_have_explicit_states(self):
        self.assertEqual(self.score([])['status'], 'unavailable')
        zero = [{**row, 'amount': 0} for row in quotes()]
        result = self.score([*zero, {'data_at': NOW.isoformat(), 'amount': 300}])
        self.assertEqual((result['status'], result['ratio']), ('insufficient', None))
        result = self.score([*quotes(), {'data_at': NOW.isoformat(), 'amount': 300}],
                            NOW + timedelta(minutes=10))
        self.assertEqual(result['status'], 'stale')

    def test_timezone_equivalence_and_invalid_observations(self):
        rows = [*quotes(), {'data_at': '2026-11-20T10:30:00+08:00', 'amount': 300}]
        self.assertEqual(self.score(rows)['ratio'], 3)
        for item in ({'data_at': '2026-11-20T10:30:00', 'amount': 100},
                     {'data_at': (NOW + timedelta(minutes=1)).isoformat(), 'amount': 100},
                     {'data_at': NOW.isoformat(), 'amount': float('nan')}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.score([item])

    def test_module_reads_configured_standard_sources_and_queries_names(self):
        with TemporaryDirectory() as folder, closing(build_data(Path(folder))) as data:
            rule = {'id': 'turnover', 'source': 'snapshots', 'catalogue': 'securities',
                    'name_field': 'name', 'timezone': 'Asia/Shanghai', 'window_days': 20,
                    'min_samples': 5, 'threshold': 3, 'tolerance_seconds': 300,
                    'calendar': 'calendar:test'}
            data.update_registry(lambda _: {'sources': [{'id': 'snapshots'}, {'id': 'securities'}],
                                             'radars': [rule]})
            data.save_calendar('test', {'sessions': SESSIONS,
                'coverage': [{'start': SESSIONS[0], 'end': SESSIONS[-1]}]})
            data.put_rows('catalogue', [{'code': 'sh.510999', 'name': 'Example ETF'},
                                       {'code': 'sz.159888', 'name': 'Other fund'}], keys=('code',))
            data.bind('securities', 'records', 'lake:catalogue')
            history = quotes()
            history[0]['data_at'] = SESSIONS[0] + 'T02:27:00+00:00'
            history.append({**history[0], 'data_at': SESSIONS[0] + 'T02:29:00+00:00',
                            'amount': None})
            data.put_rows('observations', [*history, {'code': 'sh.510999',
                'data_at': NOW.isoformat(), 'price': 2, 'amount': 300, 'volume': 150}],
                keys=('code', 'data_at'))
            data.put_rows('observations', [
                {'code': 'sh.510999', 'data_at': (NOW + timedelta(minutes=1)).isoformat(),
                 'price': 2, 'amount': 999, 'volume': 150},
                {'code': 'sh.510999', 'data_at': '2026-11-15T01:00:00+00:00',
                 'price': 2, 'amount': 999, 'volume': 150},
            ], keys=('code', 'data_at'))
            data.bind('snapshots', 'records', 'lake:observations')
            with patch('radar.datetime') as clock:
                clock.now.return_value = NOW
                clock.fromisoformat.side_effect = datetime.fromisoformat
                clock.combine.side_effect = datetime.combine
                result = Radar(data).handle('radar.scan', {'q': 'EXAMPLE'})
            self.assertEqual(len(result['items']), 1)
            self.assertEqual(result['items'][0]['ratio'], 3)
            with self.assertRaises(ValueError):
                Radar(data).handle('radar.scan', {'q': 3})

    def test_protocol_routes_radar_without_touching_market_or_collector(self):
        from backend import Modules, Protocol
        import importlib
        ui = importlib.import_module('frontend.ui-protocol')
        class Port:
            def __init__(self): self.calls = []
            def handle(self, operation, data):
                self.calls.append(operation)
                return {'items': []}
        ports = [Port() for _ in range(6)]
        protocol = Protocol(Modules(*ports))
        request = ui.UiRequest('radar-test', 'radar.scan',
                               'urn:lianghua:ui:input:radar.scan:v1', {})
        result = asyncio.run(protocol.send(request))
        self.assertNotEqual(result[0].kind, 'problem')
        self.assertEqual(ports[-1].calls, ['radar.scan'])
        self.assertTrue(all(not port.calls for port in ports[:-1]))
