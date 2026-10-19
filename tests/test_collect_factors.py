import json
import tempfile
import unittest
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from market import build_data
from protocol import DataError, DataQuery
from collect import methods as factors


class FactorStorageTests(unittest.TestCase):
    def test_daily_publish_fills_missing_facts_and_advances_the_shared_range(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root, symbols=('600003', '600004'))
            bus = build_data(root)
            manifest = bus.manifest('fixture')
            query = DataQuery('fixture', date(2020, 1, 31), date(2020, 2, 4),
                              ('open', 'high', 'low', 'close', 'volume', 'amount', 'signal_close'),
                              20, 'adjusted', symbols=('600003',))
            baseline = bus.query(query)
            packet = {'records': [{'code': 'sh.600003', 'date': '2020-02-06',
                       'open': 12., 'high': 12., 'low': 12., 'close': 12.,
                       'volume': 100., 'amount': 1200.}], 'raw': [], 'scope': {},
                      'request': {'codes': ['sh.600003'], 'start': '2020-02-04', 'end': '2020-02-06'},
                      'calendar': {'sessions': ['2020-02-06', '2020-02-07'],
                                   'coverage': [{'start': '2020-02-06', 'end': '2020-02-07'}]}}
            bus.delete('sources', 'fixture', 'corp/dividend_sh.600003_2019.json')
            manifest['files'] = [r for r in manifest['files'] if r['file'] != 'corp/dividend_sh.600003_2019.json']
            bus.write('sources', 'fixture', 'manifest.json', json.dumps(manifest).encode())
            fetched = []
            def fetch(request, provider, timeout=factors.DEFAULT_TIMEOUT):
                fetched.append(request)
                return {'schema': 1, 'kind': request.kind, 'code': request.code,
                        'start': request.start, 'end': request.end, 'year': request.year,
                        'source_hash': 'a'*64, 'records': ([{'date': '1990-01-01',
                        'forward': 1., 'backward': 1.}] if request.kind == 'adjust' else [])}
            source = dict(id='prices', method='daily', dataset_id='fixture', storage_id='fixture')
            with patch('baostock.logout'), patch('collect.methods.login_with_timeout'), patch('collect.methods.fetch_one', side_effect=fetch), \
                 patch('collect.methods.normalize_facts', side_effect=lambda value: value):
                factors.Methods(bus).publish(source, packet)
                self.assertEqual(bus.manifest('fixture')['inputs']['adjustments']['end'], '2020-02-06')
                self.assertEqual({r.code for r in fetched if r.kind == 'adjust'}, {'sh.600003', 'sh.600004'})
                self.assertIn(factors.Request('dividend', 'sh.600003', None, None, '2019'), fetched)
                fetched.clear()
                factors.Methods(bus).publish(source, packet)
                self.assertEqual(fetched, [])
                updated = bus.manifest('fixture')
                with patch.object(bus, 'read', wraps=bus.read) as read:
                    self.assertTrue(factors.Methods(bus).complete_facts('fixture', updated, packet['request'], check=True))
                self.assertFalse(any('sh.600004' in str(call) for call in read.call_args_list))
            self.assertNotEqual(manifest['inputs']['adjustments']['end'], '2020-02-06')
            self.assertEqual([r.frame for r in baseline.records], [r.frame for r in bus.query(query).records])

    def test_fact_failure_does_not_publish_daily_or_leave_a_partial_transaction(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root, symbols=('600003', '600004'))
            bus = build_data(root)
            manifest = bus.read('sources', 'fixture', 'manifest.json')
            first = {'schema': 1, 'kind': 'adjust', 'code': 'sh.600003',
                     'start': '1990-01-01', 'end': '2020-02-06', 'year': None,
                     'source_hash': 'a'*64, 'records': [{'date': '1990-01-01', 'forward': 1., 'backward': 1.}]}
            with patch('baostock.logout'), patch('collect.methods.login_with_timeout'), \
                 patch('collect.methods.normalize_facts', side_effect=lambda value: value), \
                 patch('collect.methods.fetch_one', side_effect=[first, factors.ProviderError('factor unavailable')]):
                with self.assertRaisesRegex(factors.ProviderError, 'factor unavailable'):
                    with bus.transaction('sources'):
                        factors.Methods(bus).complete_facts('fixture', bus.manifest('fixture'),
                            {'codes': ['sh.600003'], 'start': '2020-02-04', 'end': '2020-02-06'})
            self.assertEqual(bus.read('sources', 'fixture', 'manifest.json'), manifest)
            self.assertFalse(list((root/'data/source/fixture/factors').glob('*2020-02-06.json')))

    def test_missing_fact_prevents_daily_coverage_from_skipping_collection(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root, symbols=('600003',))
            bus = build_data(root)
            source = dict(id='prices', method='daily', dataset_id='fixture')
            request = dict(codes=['sh.600003'], start='2020-01-31', end='2020-02-04')
            methods = factors.Methods(bus)
            with patch.object(methods, 'daily_coverage', return_value={'complete': True}):
                self.assertTrue(methods.coverage(source, request)['complete'])
                path = root/'data/source/fixture/manifest.json'
                value = json.loads(path.read_text())
                name = 'corp/dividend_sh.600003_2019.json'
                (root/'data/source/fixture'/name).unlink()
                value['files'] = [r for r in value['files'] if r['file'] != name]
                path.write_text(json.dumps(value))
                self.assertFalse(methods.coverage(source, request)['complete'])

    def test_latest_refreshes_the_current_year_but_preserves_older_dividends(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root, symbols=('600003',))
            bus = build_data(root)
            manifest = bus.manifest('fixture')
            collected = []
            def fetch(request, provider):
                collected.append(request)
                name = (f'factors/adjust_{request.code}_{request.start}_{request.end}.json'
                        if request.kind == 'adjust' else f'corp/dividend_{request.code}_{request.year}.json')
                return json.loads(bus.read('sources', 'fixture', name))
            basic = json.loads(bus.read('sources', 'fixture', 'basic/stock_basic_sh.600003.json'))
            with patch('baostock.logout'), patch('collect.methods.login_with_timeout'), \
                 patch('collect.methods.fetch_one', side_effect=fetch), \
                 patch('collect.methods.collect_stock_basic', return_value=basic), \
                 patch('collect.methods.normalize_facts', side_effect=lambda value: value):
                with bus.transaction('sources'):
                    factors.Methods(bus).complete_facts('fixture', manifest, {
                        'codes': ['sh.600003'], 'start': '2020-02-04', 'end': '2020-02-05', 'refresh': True})
            self.assertEqual([r.year for r in collected if r.kind == 'dividend'], ['2020'])

    def test_evidence_is_written_and_verified_through_protocol(self):
        request = factors.Request('adjust', '600000', '2020-01-01', '2020-12-31', None)
        response = SimpleNamespace(error_code='0', error_msg='', code='sh.600000',
                                   fields=['code', 'dividOperateDate', 'foreAdjustFactor', 'backAdjustFactor'],
                                   data=[['sh.600000', '2020-01-01', '1.2', '1.1']])
        response.next = Mock(side_effect=[True, False])
        response.get_row_data = Mock(return_value=response.data[0])
        provider = SimpleNamespace(query_adjust_factor=Mock(return_value=response))
        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            ref = factors.fetch_and_cache(request, provider, bus, timeout=0)
            self.assertEqual((ref.identity, ref.name), ('facts', f'{request.key}.json'))
            self.assertEqual(factors.pending_requests([request], bus), [])
            payload = json.loads(bus.read(ref.kind, ref.identity, ref.name))
            raw_log = json.loads(bus.read('state', 'collect', bus.list('state', 'collect')[0].name))
            self.assertEqual(payload['source_hash'], raw_log['raw_response_sha256'])
            payload['records'][0]['forward'] = -9
            bus.write(ref.kind, ref.identity, ref.name, json.dumps(payload).encode())
            with self.assertRaises(factors.CacheIntegrityError):
                factors.pending_requests([request], bus)

    def test_failed_provider_creates_no_cache(self):
        request = factors.Request('adjust', '600000', '2020-01-01', '2020-12-31', None)
        provider = SimpleNamespace(query_adjust_factor=Mock(return_value=SimpleNamespace(error_code='10002007', error_msg='failed')))
        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            with self.assertRaises(factors.ProviderError):
                factors.fetch_and_cache(request, provider, bus, timeout=0)
            self.assertEqual(bus.list('sources', 'facts'), ())
            provider.query_adjust_factor.assert_called_once()
