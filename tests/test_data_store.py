import os
import math
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import duckdb
from protocol import DataError, Filter, TableDef, TableQuery, TableWrite, SQLQuery
from storage import DataStore


class DataStoreTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = DataStore(Path(self.folder.name))
        self.table = TableDef('prices', {'code': 'VARCHAR', 'day': 'DATE', 'price': 'DECIMAL(20,6)',
                                        'shares': 'BIGINT'}, ('code', 'day'), ('price',))
        self.store.define(self.table)
        self.row = {'code': 'sh.600000', 'day': date(2025, 1, 2), 'price': Decimal('12.123456'),
                    'shares': 9007199254740993}

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_native_types_and_parquet_persist_after_reopen(self):
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
        self.store.close()
        self.store = DataStore(Path(self.folder.name))
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist(), [self.row])
        self.assertEqual(self.store.scan(TableQuery('prices')).table.column_names, list(self.table.columns))
        self.assertTrue(list((Path(self.folder.name) / 'data/parquet').rglob('*.parquet')))
        self.assertFalse(hasattr(self.store, 'path'))

    def test_idempotency_and_batch_conflict_are_atomic(self):
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row, self.row])))
        self.assertEqual(self.store.scan(TableQuery('prices')).table.num_rows, 1)
        different = {**self.row, 'price': Decimal('13')}
        with self.assertRaises(DataError):
            self.store.write(TableWrite('prices', pa.Table.from_pylist([different, self.row]), True))
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist(), [self.row])

    def test_refresh_updates_only_registered_columns(self):
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
        with self.assertRaises(DataError):
            self.store.write(TableWrite('prices', pa.Table.from_pylist([{**self.row, 'price': Decimal('13')}] )))
        self.store.write(TableWrite('prices', pa.Table.from_pylist([{**self.row, 'price': Decimal('13')}] ), True))
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist()[0]['price'], Decimal('13'))
        with self.assertRaises(DataError):
            self.store.write(TableWrite('prices', pa.Table.from_pylist([{**self.row, 'shares': 1}] ), True))

    def test_nested_failure_cannot_commit_other_tables_or_metadata(self):
        with self.assertRaises(DataError):
            with self.store.transaction():
                self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
                try:
                    with self.store.transaction():
                        self.store.define(TableDef('second', {'id': 'BIGINT'}, ('id',)))
                        raise ValueError('stop')
                except ValueError:
                    pass
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist(), [])
        with self.assertRaises(DataError):
            self.store.definition('second')

    def test_shared_writer_serializes_threads(self):
        other = DataStore(Path(self.folder.name))
        self.assertIs(self.store, other)
        failures = []
        def insert():
            try:
                other.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=insert) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(other.scan(TableQuery('prices')).table.num_rows, 1)

    def test_lossy_decimal_and_integer_casts_are_rejected(self):
        for row in ({**self.row, 'price': Decimal('12.1234567')},
                    {**self.row, 'shares': 1.5}):
            with self.assertRaisesRegex(DataError, 'lossy'):
                self.store.write(TableWrite('prices', pa.Table.from_pylist([row])))
        self.assertEqual(self.store.scan(TableQuery('prices')).table.num_rows, 0)

    def test_second_process_fails_busy(self):
        script = "from storage import DataStore; from pathlib import Path; from protocol import DataError; import sys\ntry: DataStore(Path(sys.argv[1]))\nexcept DataError as e: print(e.code); sys.exit(5)"
        result = subprocess.run([sys.executable, '-c', script, self.folder.name], env=os.environ,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn('storage_busy', result.stdout)

    def test_structured_filters_delete_and_schema_conflict(self):
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
        query = TableQuery('prices', (Filter('shares', 'gt', 9007199254740992),))
        self.assertEqual(self.store.scan(query).table.to_pylist(), [self.row])
        self.store.delete(query)
        self.assertEqual(self.store.scan(query).table.to_pylist(), [])
        with self.assertRaises(DataError):
            self.store.define(TableDef('prices', {'id': 'BIGINT'}, ('id',)))
        with self.assertRaises(DataError):
            self.store.scan(TableQuery('prices', (Filter('x;drop table', 'eq', 1),)))

    def test_native_select_and_unicode_columns_use_the_same_port(self):
        self.store.define(TableDef('公告 行情', {'证券': 'VARCHAR', '金额': 'DECIMAL(20,6)'}, ('证券',)))
        self.store.write(TableWrite('公告 行情', pa.Table.from_pylist([
            {'证券': '600000', '金额': Decimal('12.123456')}])) )
        result = self.store.scan(SQLQuery('SELECT sum("金额") AS total FROM "公告 行情" WHERE "证券"=?', ('600000',)))
        self.assertEqual(result.table.to_pylist(), [{'total': Decimal('12.123456')}])
        for sql in ('DELETE FROM prices', 'SELECT 1; DROP TABLE prices'):
            with self.assertRaises(DataError):
                self.store.scan(SQLQuery(sql))

    def test_numeric_cast_rejects_loss_in_values_and_keys(self):
        for sql in ('DOUBLE', 'FLOAT'):
            for key in (False, True):
                name = sql.lower() + ('_key' if key else '_value')
                self.store.define(TableDef(name, {'id': 'BIGINT', 'value': sql},
                                           ('value',) if key else ('id',)))
                for value in (Decimal('0.1'), Decimal('9007199254740993.123456')):
                    with self.subTest(sql=sql, key=key, value=value):
                        with self.assertRaises(DataError) as caught:
                            with self.store.transaction():
                                self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
                                self.store.write(TableWrite(name, pa.table({'id': [1], 'value': [value]})))
                        self.assertEqual(caught.exception.code, 'invalid_record')
                        self.assertEqual(self.store.scan(TableQuery(name)).table.num_rows, 0)
                        self.assertEqual(self.store.scan(TableQuery('prices')).table.num_rows, 0)

    def test_float_keys_do_not_collapse_distinct_decimal_keys(self):
        self.store.define(TableDef('keys', {'value': 'DOUBLE'}, ('value',)))
        batch = pa.table({'value': [Decimal('9007199254740992'), Decimal('9007199254740993')]})
        with self.assertRaises(DataError):
            self.store.write(TableWrite('keys', batch))
        self.assertEqual(self.store.scan(TableQuery('keys')).table.num_rows, 0)

    def test_exact_numeric_cast_preserves_null_nan_and_infinity(self):
        for sql in ('DOUBLE', 'FLOAT'):
            name = sql.lower()
            self.store.define(TableDef(name, {'id': 'BIGINT', 'value': sql}, ('id',)))
            self.store.write(TableWrite(name, pa.table({'id': [1, 2],
                                                       'value': [Decimal('0.5'), None]})))
            self.store.write(TableWrite(name, pa.table({'id': [3, 4, 5, 6],
                                                       'value': [float('nan'), float('inf'), -float('inf'), 1.5]})))
            values = self.store.scan(TableQuery(name)).table.column('value').to_pylist()
            self.assertEqual(values[:2], [0.5, None])
            self.assertTrue(math.isnan(values[2]))
            self.assertEqual(values[3:], [float('inf'), -float('inf'), 1.5])
        for value in (1.1, 16777217):
            with self.subTest(value=value), self.assertRaises(DataError):
                self.store.write(TableWrite('float', pa.table({'id': [7], 'value': [value]})))

    def test_sql_error_preserves_data_error_and_rolls_back_all_tables(self):
        self.store.define(TableDef('jsons', {'id': 'BIGINT', 'doc': 'JSON'}, ('id',)))
        for caught_inside in (False, True):
            with self.subTest(caught_inside=caught_inside):
                with self.assertRaises(DataError) as outer:
                    with self.store.transaction():
                        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
                        self.store.define(TableDef('second', {'id': 'BIGINT'}, ('id',)))
                        self.store.write(TableWrite('second', pa.table({'id': [1]})))
                        bad = TableWrite('jsons', pa.table({'id': [1], 'doc': ['{broken']}))
                        if caught_inside:
                            with self.assertRaises(DataError) as inner:
                                self.store.write(bad)
                            self.assertEqual(inner.exception.code, 'invalid_record')
                            self.assertIn('Malformed JSON', str(inner.exception))
                        else:
                            self.store.write(bad)
                self.assertEqual(outer.exception.code, 'transaction_failed' if caught_inside else 'invalid_record')
                if not caught_inside:
                    self.assertIn('Malformed JSON', str(outer.exception))
                self.assertEqual(self.store.scan(TableQuery('prices')).table.num_rows, 0)
                self.assertEqual(self.store.scan(TableQuery('jsons')).table.num_rows, 0)
                with self.assertRaises(DataError):
                    self.store.definition('second')
        self.store.write(TableWrite('jsons', pa.table({'id': [2], 'doc': ['{"ok":true}']})))
        self.assertEqual(self.store.scan(TableQuery('jsons')).table.num_rows, 1)

    def test_sql_scan_rejects_ducklake_import_that_writes(self):
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
        schema = self.store.scan(TableQuery('prices')).table.schema
        path = Path(self.folder.name) / 'incoming.parquet'
        pq.write_table(pa.Table.from_pylist([{**self.row, 'price': Decimal('99')}], schema=schema), path)
        for sql in ("SELECT * FROM ducklake_add_data_files('lake', 'prices', ?)",
                    "WITH x AS (SELECT * FROM ducklake_add_data_files('lake', 'prices', ?)) SELECT * FROM x"):
            with self.subTest(sql=sql), self.assertRaises(DataError) as caught:
                self.store.scan(SQLQuery(sql, (str(path),)))
            self.assertEqual(caught.exception.code, 'invalid_query')
            self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist(), [self.row])
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
        result = self.store.scan(SQLQuery('SELECT code FROM prices WHERE code IN (SELECT UNNEST(?))',
                                          ([self.row['code']],)))
        self.assertEqual(result.table.to_pylist(), [{'code': self.row['code']}])

    def test_sql_scan_cannot_nest_in_write_transaction(self):
        with self.assertRaises(DataError) as outer:
            with self.store.transaction():
                self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
                with self.assertRaises(DataError) as inner:
                    self.store.scan(SQLQuery('SELECT 1'))
                self.assertEqual(inner.exception.code, 'transaction_mode')
        self.assertEqual(outer.exception.code, 'transaction_failed')
        self.assertEqual(self.store.scan(TableQuery('prices')).table.num_rows, 0)
        with self.store.transaction(readonly=True):
            self.assertEqual(self.store.scan(SQLQuery('SELECT 1 AS value')).table.to_pylist(), [{'value': 1}])

    def test_readonly_transaction_rejects_native_insert(self):
        with self.assertRaises(duckdb.Error):
            with self.store.transaction(readonly=True):
                self.store._connection.execute("INSERT INTO lake.main.prices (code, day) VALUES ('sh.600000', DATE '2025-01-02')")
        self.assertEqual(self.store.scan(TableQuery('prices')).table.num_rows, 0)

    def test_partial_write_preserves_absent_columns_and_can_set_null(self):
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
        self.store.write(TableWrite('prices', pa.Table.from_pylist([
            {'price': Decimal('13.5'), 'day': self.row['day'], 'code': self.row['code']}]), True))
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist(), [{**self.row, 'price': Decimal('13.5')}])
        self.store.write(TableWrite('prices', pa.Table.from_pylist([
            {'code': self.row['code'], 'day': self.row['day'], 'price': None}]), True))
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist(), [{**self.row, 'price': None}])
        self.store.write(TableWrite('prices', pa.record_batch({'code': ['sh.600001'], 'day': [self.row['day']]})))
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist()[1],
                         {'code': 'sh.600001', 'day': self.row['day'], 'price': None, 'shares': None})
        self.store.write(TableWrite('prices', pa.table({'code': [self.row['code']], 'day': [self.row['day']]}), True))
        with self.assertRaises(DataError) as caught:
            self.store.write(TableWrite('prices', pa.table({'code': [self.row['code']],
                                                            'day': [self.row['day']], 'shares': [1]}), True))
        self.assertEqual(caught.exception.code, 'data_conflict')

    def test_partial_write_rejects_missing_keys_and_extra_columns(self):
        for batch in (pa.table({'code': ['sh.600000']}),
                      pa.table({'code': ['sh.600000'], 'day': [self.row['day']], 'extra': [1]}),
                      pa.table({})):
            with self.subTest(columns=batch.column_names), self.assertRaises(DataError) as caught:
                self.store.write(TableWrite('prices', batch))
            self.assertEqual(caught.exception.code, 'invalid_record')
        self.assertEqual(self.store.scan(TableQuery('prices')).table.num_rows, 0)

    def test_additive_schema_preserves_columns_values_and_reopen(self):
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
        columns = {**self.table.columns, '净"利润': 'DOUBLE', '来源': 'VARCHAR'}
        extended = TableDef('prices', columns, self.table.keys, (*self.table.updates, '净"利润'))
        self.store.define(extended)
        self.store.define(extended)
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist(),
                         [{**self.row, '净"利润': None, '来源': None}])
        self.store.write(TableWrite('prices', pa.table({'净"利润': [Decimal('0.5')],
                                                        'day': [self.row['day']], 'code': [self.row['code']]}), True))
        self.store.close()
        self.store = DataStore(Path(self.folder.name))
        result = self.store.scan(TableQuery('prices')).table
        self.assertEqual(result.column_names, list(columns))
        self.assertEqual(result.to_pylist(), [{**self.row, '净"利润': 0.5, '来源': None}])
        self.assertEqual(self.store.definition('prices'), extended)

    def test_additive_schema_rejects_changes_to_old_contract(self):
        columns = dict(self.table.columns)
        definitions = [
            TableDef('prices', {**columns, 'price': 'DOUBLE'}, self.table.keys, self.table.updates),
            TableDef('prices', dict(reversed(list(columns.items()))), self.table.keys, self.table.updates),
            TableDef('prices', columns, ('code',), self.table.updates),
            TableDef('prices', columns, self.table.keys, self.table.updates, True),
            TableDef('prices', {**columns, 'new': 'BIGINT'}, self.table.keys),
            TableDef('prices', {**columns, 'new': 'BIGINT'}, self.table.keys, ('price', 'shares')),
        ]
        for definition in definitions:
            with self.subTest(definition=definition), self.assertRaises(DataError) as caught:
                self.store.define(definition)
            self.assertEqual(caught.exception.code, 'schema_conflict')
            self.assertEqual(self.store.definition('prices'), self.table)

    def test_additive_schema_and_metadata_roll_back_on_sql_error(self):
        self.store.write(TableWrite('prices', pa.Table.from_pylist([self.row])))
        self.store.define(TableDef('jsons', {'id': 'BIGINT', 'doc': 'JSON'}, ('id',)))
        extended = TableDef('prices', {**self.table.columns, '附加': 'BIGINT'}, self.table.keys, self.table.updates)
        with self.assertRaises(DataError) as caught:
            with self.store.transaction():
                self.store.define(extended)
                self.store.write(TableWrite('jsons', pa.table({'id': [1], 'doc': ['{broken']})))
        self.assertEqual(caught.exception.code, 'invalid_record')
        self.assertEqual(self.store.definition('prices'), self.table)
        self.assertEqual(self.store.scan(TableQuery('prices')).table.to_pylist(), [self.row])
        self.store.close()
        self.store = DataStore(Path(self.folder.name))
        self.assertEqual(self.store.definition('prices'), self.table)
        self.assertEqual(self.store.scan(TableQuery('prices')).table.column_names, list(self.table.columns))

    def test_fingerprint_hashes_typed_rows_and_nulls_in_key_order(self):
        definition = TableDef('摘要', {'证券': 'VARCHAR', '金额': 'DECIMAL(20,6)', '注释': 'VARCHAR'},
                              ('证券',), ('金额', '注释'))
        self.store.define(definition)
        self.store.write(TableWrite('摘要', pa.Table.from_pylist([
            {'证券': 'B', '金额': Decimal('1.5'), '注释': '换\n行'},
            {'证券': 'A', '金额': Decimal('0.5'), '注释': None}])))
        text = ('[["证券","VARCHAR"],["金额","DECIMAL(20,6)"],["注释","VARCHAR"]]\n'
                '{"证券":"A","金额":0.500000,"注释":null}\n'
                '{"证券":"B","金额":1.500000,"注释":"换\\n行"}')
        before = self.store.fingerprint('摘要')
        self.assertEqual(before, sha256(text.encode()).hexdigest())
        with self.store.transaction(readonly=True):
            self.assertEqual(self.store.fingerprint('摘要'), before)
        with self.store.transaction():
            self.store.write(TableWrite('摘要', pa.table({'证券': ['A'], '注释': ['']}), True))
            self.assertNotEqual(self.store.fingerprint('摘要'), before)
        changed = self.store.fingerprint('摘要')
        self.store.close()
        self.store = DataStore(Path(self.folder.name))
        self.assertEqual(self.store.fingerprint('摘要'), changed)

    def test_fingerprint_includes_schema_and_ignores_insert_order(self):
        for name, kind in (('a', 'DOUBLE'), ('b', 'DOUBLE'), ('c', 'DECIMAL(20,6)')):
            self.store.define(TableDef(name, {'id': 'BIGINT', 'value': kind}, ('id',)))
        a = self.store.fingerprint('a')
        self.assertEqual(a, self.store.fingerprint('b'))
        self.assertNotEqual(a, self.store.fingerprint('c'))
        for name, ids in (('a', [2, 1]), ('b', [1, 2])):
            self.store.write(TableWrite(name, pa.table({'id': ids, 'value': [0.5, 0.5]})))
        self.assertNotEqual(self.store.fingerprint('a'), a)
        self.assertEqual(self.store.fingerprint('a'), self.store.fingerprint('b'))
        with self.assertRaises(DataError):
            self.store.fingerprint('missing')

    def test_fingerprint_covers_all_fields_when_a_column_is_named_row(self):
        self.store.define(TableDef('items', {'id': 'BIGINT', 'row': 'VARCHAR', 'value': 'BIGINT'},
                                   ('id',), ('value',)))
        self.store.write(TableWrite('items', pa.table({'id': [1], 'row': ['same'], 'value': [1]})))
        before = self.store.fingerprint('items')
        self.store.write(TableWrite('items', pa.table({'id': [1], 'value': [2]}), True))
        self.assertNotEqual(self.store.fingerprint('items'), before)

    def test_sql_reads_only_registered_relations_and_scoped_ctes(self):
        import pyarrow.parquet as pq
        path = Path(self.folder.name) / 'outside.parquet'
        pq.write_table(pa.table({'id': [9]}), path)
        with self.assertRaisesRegex(DataError, 'registered relation'):
            self.store.scan(SQLQuery(f'SELECT * FROM "{path}"'))
        rows = self.store.scan(SQLQuery(
            'WITH selected AS (SELECT * FROM prices) SELECT count(*) AS n FROM selected')).table.to_pylist()
        self.assertEqual(rows, [{'n': 0}])
        with self.assertRaisesRegex(DataError, 'registered relation'):
            self.store.scan(SQLQuery('SELECT * FROM (WITH hidden AS (SELECT 1) SELECT * FROM hidden) t '
                                     'JOIN hidden USING (x)'))


if __name__ == '__main__':
    unittest.main()
