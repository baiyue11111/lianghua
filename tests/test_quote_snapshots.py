"""Quote adapter uses standard identities, share units and bounded batches."""
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from collect.methods import EastmoneyQuotes, CollectionError


class QuoteSnapshotTests(unittest.TestCase):
    def test_collection_does_not_export_quote_history_to_finish_a_poll(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources
        from market import build_data
        with TemporaryDirectory() as folder, closing(build_data(Path(folder))) as data:
            sources = Sources(data)
            sources.register({'id': 'universe', 'name': 'Universe', 'method': 'fixture',
                              'frequency': 'snapshot', 'defaults': {}})
            data.put_rows('universe', [{'code': 'sh.510300'}], keys=('code',))
            data.bind('universe', 'records', 'lake:universe')
            source = sources.register({'id': 'snapshots', 'name': 'Snapshots',
                'method': 'quotes', 'data_type': 'quotes', 'frequency': 'snapshot',
                'defaults': {}, 'params': {'catalogue': 'universe'}})
            provider = EastmoneyQuotes({}, self.transport([]))
            methods = Methods(data, {'quotes': provider})
            with closing(Collect(sources, methods)) as collect, \
                 patch.object(methods, 'records', side_effect=AssertionError('full history export')):
                for _ in range(2):
                    job = collect.run({'source_id': source['id']})
                    self.assertEqual(job['outcome'], 'succeeded', job['error'])
                    self.assertRegex(job['snapshot_id'], r'^[a-f0-9]{64}$')
                old = {'source_id': 'snapshots', 'started_at': '2026-11-19T10:00:00',
                       'finished_at': '2026-11-19T10:00:01'}
                running = {'source_id': 'snapshots', 'started_at': '2026-11-20T10:00:00',
                           'finished_at': None}
                collect._records_cache[source['id']] = ({}, 0, None)
                self.assertEqual(collect._source_view(source, [old, running])['job'], running)
            self.assertEqual(data.table(data.source('snapshots')).num_rows, 1)

    def transport(self, urls):
        def get(url):
            urls.append(url)
            identities = parse_qs(urlparse(url).query)['secids'][0].split(',')
            return {'data': {'diff': [
                {'f13': int(identity.split('.')[0]), 'f12': identity.split('.')[1],
                 'f14': 'ETF', 'f2': 2, 'f3': 1, 'f5': 100, 'f6': 20000,
                 'f124': 1795228200} for identity in identities]}}
        return get

    def test_quotes_convert_exchange_ids_and_lots_at_the_provider_boundary(self):
        urls = []
        packet = EastmoneyQuotes({}, self.transport(urls)).collect({
            'codes': ['sh.510300', 'sz.159919']})
        self.assertEqual([row['code'] for row in packet['records']], ['sh.510300', 'sz.159919'])
        self.assertEqual(packet['records'][0]['volume'], 10000)
        self.assertEqual(packet['records'][0]['amount'], 20000)
        self.assertEqual(parse_qs(urlparse(urls[0]).query)['secids'], ['1.510300,0.159919'])

    def test_batches_preserve_valid_observations_if_a_later_request_fails(self):
        urls = []
        get = self.transport(urls)
        def partial(url):
            if len(urls) == 1:
                raise OSError('provider disconnected')
            return get(url)
        codes = [f'sh.{510000 + n}' for n in range(26)]
        packet = EastmoneyQuotes({}, partial).collect({'codes': codes})
        self.assertEqual(len(packet['records']), 25)
        self.assertEqual(packet['failures'][0]['code'], codes[-1])
        self.assertEqual({row['raw_page'] for row in packet['records']}, {0})

    def test_valid_quote_is_preserved_when_another_security_is_missing(self):
        get = self.transport([])

        def partial(url):
            response = get(url)
            response['data']['diff'].pop()
            return response

        packet = EastmoneyQuotes({}, partial).collect({'codes': ['sh.510300', 'sz.159919']})
        self.assertEqual([row['code'] for row in packet['records']], ['sh.510300'])
        self.assertEqual([item['code'] for item in packet['failures']], ['sz.159919'])
        self.assertIn('missing', packet['failures'][0]['error'])

    def test_valid_quote_is_preserved_when_another_security_has_invalid_data(self):
        get = self.transport([])

        def partial(url):
            response = get(url)
            response['data']['diff'][0]['f5'] = '-'
            return response

        packet = EastmoneyQuotes({}, partial).collect({'codes': ['sh.510300', 'sz.159919']})
        self.assertEqual([row['code'] for row in packet['records']], ['sz.159919'])
        self.assertEqual([item['code'] for item in packet['failures']], ['sh.510300'])
        self.assertIn('invalid quote value', packet['failures'][0]['error'])

    def test_a_response_with_an_outside_security_rejects_the_batch(self):
        get = self.transport([])

        def outside(url):
            response = get(url)
            response['data']['diff'][0]['f12'] = '510301'
            return response

        with self.assertRaisesRegex(CollectionError, 'identities differ from request'):
            EastmoneyQuotes({}, outside).collect({'codes': ['sh.510300', 'sz.159919']})

    def test_vendor_ids_and_failed_first_request_are_reported(self):
        with self.assertRaises(CollectionError):
            EastmoneyQuotes({}).collect({'codes': ['1.510300']})
        with self.assertRaises(CollectionError):
            EastmoneyQuotes({}, lambda _: {'data': {'diff': []}}).collect({'codes': ['sh.510300']})
