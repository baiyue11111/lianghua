"""The public data port owns managed relations and atomic commits."""
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market import DataBus
from protocol import DataError, DataQuery, Filter, TableDef, TableQuery
from storage import DataStore


class DataProtocolTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.store = DataStore(Path(self.folder.name))
        self.data = DataBus(self.store)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def daily(self):
        self.data.put_rows('prices', [
            {'code': 'us.aapl', 'date': '2025-01-02', 'close': 100.0},
            {'code': 'us.aapl', 'date': '2025-01-03', 'close': 101.0},
        ], keys=('code', 'date'))
        self.data.bind('prices', 'bars', 'lake:prices')
        self.data.save_calendar('prices', {
            'sessions': ['2025-01-02', '2025-01-03', '2025-01-06'],
            'coverage': [{'start': '2025-01-02', 'end': '2025-01-06'}],
        })
        self.data.save_manifest('prices', {'schema': 3, 'calendar': 'calendar:prices',
            'inputs': {'bars': 'lake:prices'}, 'fields': {},
            'scope': {'symbols': ['us.aapl'], 'universe': 'TEST'}})

    def test_old_file_and_publication_channels_are_removed(self):
        for owner in (self.data, self.store):
            for name in ('path', 'read', 'read_ref', 'list', 'publish', 'namespace_lock'):
                self.assertFalse(hasattr(owner, name), name)

    def test_query_reads_registered_relation_and_preserves_values(self):
        self.daily()
        dataset = self.data.query(DataQuery('prices', date(2025, 1, 2), date(2025, 1, 3),
            ('close',), 1, 'raw', symbols=('us.aapl',), purpose='compare'))
        self.assertEqual([record.frame.fields['us.aapl']['close'] for record in dataset.records], [100.0, 101.0])
        self.assertEqual(dataset.scope['symbols'], ('us.aapl',))
        self.assertEqual(dataset.calendar[-1], date(2025, 1, 6))

    def test_missing_source_invalid_manifest_and_invalid_query_fail_explicitly(self):
        with self.assertRaises(DataError):
            self.data.source('missing')
        with self.assertRaises(DataError):
            self.data.manifest('missing')
        with self.assertRaises(DataError):
            self.data.query({})
        self.daily()
        with self.assertRaises(DataError):
            self.data.save_manifest('prices', {'schema': 2})
        with self.assertRaises(DataError):
            self.data.query(DataQuery('prices', date(2025, 1, 2), date(2025, 1, 3),
                ('close',), 1, 'raw', symbols=('MSFT',), purpose='compare'))

    def test_multi_table_failure_rolls_back_data_registry_and_schema(self):
        before = self.data.registry()
        with self.assertRaises(ValueError):
            with self.data.transaction():
                self.data.update_registry(lambda _: {'sources': [{'id': 'new'}]})
                self.data.put_rows('new_table', [{'id': 1}], keys=('id',))
                raise ValueError('stop')
        self.assertEqual(self.data.registry(), before)
        with self.assertRaises(DataError):
            self.data.definition('new_table')

    def test_nested_failure_cannot_be_hidden_by_outer_transaction(self):
        with self.assertRaises(DataError):
            with self.data.transaction():
                self.data.put_rows('new_table', [{'id': 1}], keys=('id',))
                try:
                    with self.data.transaction():
                        raise ValueError('stop')
                except ValueError:
                    pass
        with self.assertRaises(DataError):
            self.data.definition('new_table')

    def test_sparse_batch_keeps_later_fields_and_absence_does_not_erase(self):
        self.data.put_rows('records', [{'id': 1, 'v': 1}, {'id': 2, 'v': 2, 'evidence': 'keep'}],
                           keys=('id',), updates=('v', 'evidence'))
        self.data.put_rows('records', [{'id': 2, 'v': 3}], keys=('id',), refresh=True)
        self.assertEqual(self.data.table('lake:records').to_pylist(), [
            {'id': 1, 'v': 1, 'evidence': None}, {'id': 2, 'v': 3, 'evidence': 'keep'}])
        self.data.put_rows('records', [{'id': 2, 'evidence': None}], keys=('id',), refresh=True)
        self.assertIsNone(self.data.table('lake:records').to_pylist()[1]['evidence'])

    def test_registration_and_failed_first_write_are_atomic(self):
        with self.assertRaises(DataError):
            self.data.put_rows('bad', [{'id': None, 'v': Decimal('0.1')}],
                               keys=('id',), types={'id': 'BIGINT'})
        with self.assertRaises(DataError):
            self.data.definition('bad')
        self.data.put_rows('bad', [{'id': 1, 'v': Decimal('12.123456')}], keys=('id',))
        with self.assertRaises(DataError):
            self.data.put_rows('bad', [{'id': 2, 'v': Decimal('12.123456')}], keys=('v',))
        with self.assertRaises(DataError):
            self.data.put_rows('bad', [{'id': 2, 'v': Decimal('12.123456')}],
                               keys=('id',), types={'v': 'DOUBLE'})
        self.assertEqual(self.data.table('lake:bad').num_rows, 1)

    @staticmethod
    def dividend(value):
        return {'schema': 1, 'kind': 'dividend', 'code': 'sh.600000',
                'start': None, 'end': None, 'year': '2025', 'source_hash': '1' * 64,
                'records': [{'announce_date': '2025-01-02', 'registration_date': '2025-01-03',
                             'operation_date': '2025-01-06', 'payment_date': '2025-01-06',
                             'stock_market_date': None, 'cash_per_share': value,
                             'stock_dividend_per_share': None, 'reserve_to_stock_per_share': None,
                             'cash_after_tax_text': None}]}

    def test_fact_null_then_decimal_retains_type_and_missing_data_is_not_empty(self):
        first = self.dividend(None)
        reference = self.data.save_fact(first)
        self.assertEqual(self.data.fact(reference), first)
        second = self.dividend(Decimal('12.123456'))
        self.data.save_fact(second)
        self.assertEqual(self.data.fact(reference), second)
        self.data.delete(TableQuery('fact_dividend'))
        with self.assertRaises(DataError):
            self.data.fact(reference)

    def test_explicit_empty_fact_is_valid_and_writes_are_idempotent(self):
        empty = self.dividend(None)
        empty['records'] = []
        reference = self.data.save_fact(empty)
        self.assertEqual(self.data.fact(reference), empty)
        self.data.put_rows('records', [{'id': 1, 'v': 'same'}, {'id': 1, 'v': 'same'}], keys=('id',))
        self.data.put_rows('records', [{'id': 1, 'v': 'same'}], keys=('id',))
        self.assertEqual(self.data.table('lake:records').num_rows, 1)

    def test_query_rejects_wrong_adapter_result(self):
        self.daily()
        query = DataQuery('prices', date(2025, 1, 2), date(2025, 1, 3), ('close',), 1, 'raw', purpose='compare')
        with patch('market.reader._prepare_dataset', return_value={}):
            with self.assertRaises(DataError):
                self.data.query(query)


if __name__ == '__main__':
    unittest.main()
