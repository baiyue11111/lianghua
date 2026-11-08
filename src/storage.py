"""Typed relational persistence in DuckLake; Parquet is owned by DuckLake."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import threading
import weakref

import duckdb
import pyarrow as pa
from protocol import DataError, Filter, TableDef, TableQuery, TableResult, TableWrite, SQLQuery


def canonical(value):
    def encode(item):
        if isinstance(item, (date, datetime, Decimal)):
            return {'type': type(item).__name__, 'value': str(item)}
        if isinstance(item, bytes):
            return {'type': 'bytes', 'value': item.hex()}
        raise TypeError(type(item).__name__)
    return json.dumps(value, default=encode, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


class DataStore:
    _owners = weakref.WeakValueDictionary()
    _guard = threading.RLock()

    def __new__(cls, project, *, extension_directory=None):
        key = (os.getpid(), str(Path(project).resolve()))
        with cls._guard:
            owner = cls._owners.get(key)
            if owner is None:
                owner = super().__new__(cls)
                owner._ready = False
                cls._owners[key] = owner
            return owner

    def __init__(self, project, *, extension_directory=None):
        with self._guard:
            if self._ready:
                return
            self.project = Path(project).resolve()
            self.root = self.project / 'data'
            self.root.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()
            self._local = threading.local()
            self._pid = os.getpid()
            config = {'autoinstall_known_extensions': 'false',
                      'autoload_known_extensions': 'false'}
            directory = extension_directory or os.environ.get('LIANGHUA_EXTENSION_DIRECTORY')
            if directory:
                config['extension_directory'] = str(directory)
            connection = duckdb.connect(':memory:', config=config)
            try:
                connection.execute('LOAD ducklake')
            except duckdb.Error as error:
                connection.close()
                raise DataError('ducklake_unavailable', 'LOAD ducklake failed; install the official extension offline or set LIANGHUA_EXTENSION_DIRECTORY: ' + str(error)) from error
            try:
                connection.execute("SET ducklake_default_data_inlining_row_limit = 0")
                catalog = str(self.root / 'catalog.ducklake').replace("'", "''")
                parquet = str(self.root / 'parquet').replace("'", "''")
                connection.execute(f"ATTACH 'ducklake:{catalog}' AS lake (DATA_PATH '{parquet}', DATA_INLINING_ROW_LIMIT 0)")
                connection.execute('CREATE TABLE IF NOT EXISTS lake.main._tables (name VARCHAR, definition JSON)')
                connection.execute("CALL lake.set_option('data_inlining_row_limit', 0)")
                connection.execute('USE lake.main')
            except duckdb.Error as error:
                connection.close()
                raise DataError('storage_busy' if 'lock' in str(error).lower() else 'storage_failed', str(error)) from error
            self._connection = connection
            self._ready = True

    def _check(self):
        if self._pid != os.getpid():
            raise DataError('storage_busy', 'catalog connection belongs to another process; open a new writer')
        if not self._ready:
            raise DataError('storage_closed', 'data store is closed')

    def close(self):
        with self._lock:
            self._check()
            self._connection.close()
            self._ready = False
            self._owners.pop((self._pid, str(self.project)), None)

    @contextmanager
    def transaction(self, *, readonly=False):
        with self._lock:
            self._check()
            depth = getattr(self._local, 'depth', 0)
            if depth and readonly != self._local.readonly:
                self._local.failed = True
                raise DataError('transaction_mode', 'nested transaction must use the same read/write mode')
            if depth == 0:
                self._connection.execute('BEGIN TRANSACTION READ ONLY' if readonly else 'BEGIN TRANSACTION')
                self._local.readonly = readonly
                self._local.failed = False
            self._local.depth = depth + 1
            try:
                yield self
                if depth == 0 and self._local.failed:
                    raise DataError('transaction_failed', 'nested transaction failed; outer transaction cannot commit')
            except BaseException:
                self._local.failed = True
                if depth == 0:
                    self._connection.execute('ROLLBACK')
                raise
            else:
                if depth == 0:
                    try:
                        self._connection.execute('COMMIT')
                    except BaseException:
                        self._connection.execute('ROLLBACK')
                        raise
            finally:
                self._local.depth = depth

    def define(self, table: TableDef):
        if not isinstance(table, TableDef):
            raise TypeError('table definition required')
        definition = canonical({'name': table.name, 'columns': list(table.columns.items()),
                                'keys': table.keys, 'updates': table.updates, 'audit': table.audit})
        with self.transaction():
            rows = self._connection.execute('SELECT definition FROM lake.main._tables WHERE name=?', [table.name]).fetchall()
            if rows:
                if len(rows) != 1:
                    raise DataError('schema_conflict', 'table definition differs: ' + table.name)
                old, new = json.loads(rows[0][0]), json.loads(definition)
                if old == new:
                    return
                count = len(old['columns'])
                old_names = {n for n, _ in old['columns']}
                if (len(new['columns']) <= count or new['columns'][:count] != old['columns']
                        or new['keys'] != old['keys'] or new['audit'] != old['audit']
                        or [n for n in new['updates'] if n in old_names] != old['updates']):
                    raise DataError('schema_conflict', 'table definition differs: ' + table.name)
                for name, kind in new['columns'][count:]:
                    self._connection.execute(f'ALTER TABLE lake.main.{quote(table.name)} ADD COLUMN {quote(name)} {kind}')
                self._connection.execute('UPDATE lake.main._tables SET definition=? WHERE name=?', [definition, table.name])
                return
            columns = ', '.join(quote(n) + ' ' + t for n, t in table.columns.items())
            self._connection.execute(f'CREATE TABLE lake.main.{quote(table.name)} ({columns})')
            self._connection.execute('INSERT INTO lake.main._tables VALUES (?, ?)', [table.name, definition])

    def definition(self, name):
        with self._lock:
            self._check()
            rows = self._connection.execute('SELECT definition FROM lake.main._tables WHERE name=?', [name]).fetchall()
            if len(rows) != 1:
                raise DataError('missing_table', 'table is not registered: ' + str(name))
            definition = json.loads(rows[0][0])
            definition['columns'] = dict(definition['columns'])
            return TableDef(**definition)

    def _where(self, definition, filters):
        expressions, values = [], []
        operators = {'eq': '=', 'ne': '<>', 'lt': '<', 'le': '<=', 'gt': '>', 'ge': '>='}
        for item in filters:
            if not isinstance(item, Filter) or item.column not in definition.columns:
                raise DataError('invalid_query', 'filter column is not registered')
            column = quote(item.column)
            if item.op == 'is_null':
                expressions.append(column + (' IS NULL' if item.value else ' IS NOT NULL'))
            elif item.op == 'in':
                expressions.append(column + ' IN (SELECT * FROM UNNEST(?))')
                values.append(list(item.value))
            elif item.value is None:
                if item.op not in ('eq', 'ne'):
                    raise DataError('invalid_query', 'null requires eq or ne')
                expressions.append(column + (' IS NULL' if item.op == 'eq' else ' IS NOT NULL'))
            else:
                expressions.append(column + ' ' + operators[item.op] + ' ?')
                values.append(item.value)
        return (' WHERE ' + ' AND '.join(expressions) if expressions else ''), values

    def scan(self, query: TableQuery):
        if isinstance(query, SQLQuery):
            try:
                with self.transaction(readonly=True):
                    statements = self._connection.extract_statements(query.sql)
                    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                        raise DataError('invalid_query', 'scan accepts one read-only SELECT statement')
                    tree = json.loads(self._connection.execute('SELECT json_serialize_sql(?)', [query.sql]).fetchone()[0])
                    if tree['error']:
                        raise DataError('invalid_query', tree['error_message'])
                    registered = {name.lower() for name, in self._connection.execute(
                        'SELECT name FROM lake.main._tables').fetchall()}
                    def check(node, scope=frozenset()):
                        if isinstance(node, dict):
                            scope = scope | {item['key'].lower() for item in node.get('cte_map', {}).get('map', [])}
                            if node.get('type') == 'TABLE_FUNCTION':
                                raise DataError('invalid_query', 'SQL scan does not execute table functions')
                            if node.get('type') == 'BASE_TABLE':
                                if (node.get('catalog_name', '').lower() not in ('', 'lake')
                                        or node.get('schema_name', '').lower() not in ('', 'main')
                                        or node['table_name'].lower() not in registered | scope):
                                    raise DataError('invalid_query', 'SQL scan requires a registered relation')
                            for child in node.values():
                                check(child, scope)
                        elif isinstance(node, list):
                            for child in node:
                                check(child, scope)
                    check(tree)
                    return TableResult(self._connection.execute(query.sql, list(query.values)).to_arrow_table(), 'lake:query')
            except duckdb.Error as error:
                raise DataError('invalid_query', str(error)) from error
        if not isinstance(query, TableQuery):
            raise TypeError('TableQuery required')
        with self._lock:
            definition = self.definition(query.table)
            columns = query.columns or tuple(definition.columns)
            order = query.order or definition.keys
            if not set((*columns, *order)) <= definition.columns.keys():
                raise DataError('invalid_query', 'query columns are not registered')
            where, values = self._where(definition, query.filters)
            sql = f'SELECT {", ".join(map(quote, columns))} FROM lake.main.{quote(query.table)}' + where
            sql += ' ORDER BY ' + ', '.join(map(quote, order))
            if query.limit is not None:
                sql += ' LIMIT ?'
                values.append(query.limit)
            sql += ' OFFSET ?'
            values.append(query.offset)
            table = self._connection.execute(sql, values).to_arrow_table()
            return TableResult(table, 'lake:' + query.table)

    def fingerprint(self, name):
        with self._lock:
            definition = self.definition(name)
            columns = ', '.join(map(quote, definition.columns))
            fields = ', '.join(quote(column) + ' := row.' + quote(column) for column in definition.columns)
            keys = ', '.join('row.' + quote(key) for key in definition.keys)
            schema = canonical(list(definition.columns.items()))
            sql = ("SELECT sha256(? || '\n' || coalesce("
                   f"string_agg(to_json(struct_pack({fields})), '\n' ORDER BY {keys}), '')) "
                   f'FROM (SELECT {columns} FROM lake.main.{quote(name)}) AS row')
            return self._connection.execute(sql, [schema]).fetchone()[0]

    def write(self, value: TableWrite):
        if not isinstance(value, TableWrite):
            raise TypeError('TableWrite required')
        with self.transaction():
            table = self.definition(value.name)
            if not isinstance(value.table, (pa.Table, pa.RecordBatch)):
                raise TypeError('TableWrite requires an Arrow table or record batch')
            batch = pa.Table.from_batches([value.table]) if isinstance(value.table, pa.RecordBatch) else value.table
            provided = set(batch.column_names)
            if (len(provided) != len(batch.column_names) or not provided <= table.columns.keys()
                    or not set(table.keys) <= provided):
                raise DataError('invalid_record', 'records require all logical keys and only registered columns')
            columns = tuple(n for n in table.columns if n in provided)
            projection = ', '.join(map(quote, columns))
            target_schema = self._connection.execute(f'SELECT {projection} FROM lake.main.{quote(table.name)} LIMIT 0').to_arrow_table().schema
            try:
                source = batch.select(columns)
                batch = source.cast(target_schema, safe=True)
                for name in columns:
                    before, after = source[name], batch[name]
                    numeric = lambda kind: (pa.types.is_integer(kind) or pa.types.is_floating(kind)
                                            or pa.types.is_decimal(kind))
                    if before.type == after.type or not (numeric(before.type) and numeric(after.type)):
                        continue
                    # Python numeric equality compares Decimal and float exactly; a
                    # cast back to Decimal can round again and hide the first loss.
                    for left, right in zip(before.to_pylist(), after.to_pylist()):
                        if left == right or (isinstance(left, float) and isinstance(right, float)
                                             and math.isnan(left) and math.isnan(right)):
                            continue
                        raise DataError('invalid_record', 'lossy numeric column cast: ' + name)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as error:
                raise DataError('invalid_record', 'lossy or invalid column cast: ' + str(error)) from error
            if any(batch[k].null_count for k in table.keys):
                raise DataError('invalid_record', 'logical keys cannot be null')
            if not batch.num_rows:
                return TableResult(batch, 'lake:' + value.name)
            connection = self._connection
            connection.register('_batch', batch)
            try:
                connection.execute(f'CREATE TEMP TABLE incoming AS SELECT {projection} FROM lake.main.{quote(table.name)} LIMIT 0')
                connection.execute('INSERT INTO incoming BY NAME SELECT * FROM _batch')
                keys = ','.join(map(quote, table.keys))
                conflict = connection.execute(f'SELECT 1 FROM (SELECT DISTINCT * FROM incoming) GROUP BY {keys} HAVING count(*)>1 LIMIT 1').fetchone()
                if conflict:
                    raise DataError('data_conflict', 'different values for one logical key in batch')
                match = ' AND '.join('old.' + quote(k) + ' IS NOT DISTINCT FROM new.' + quote(k) for k in table.keys)
                if connection.execute(f'SELECT 1 FROM {"lake.main." + quote(table.name)} old WHERE EXISTS (SELECT 1 FROM incoming new WHERE {match}) GROUP BY ' + ','.join('old.' + quote(k) for k in table.keys) + ' HAVING count(*)>1 LIMIT 1').fetchone():
                    raise DataError('data_corrupt', 'target contains duplicate logical keys')
                immutable = [n for n in columns if n not in table.keys and (n not in table.updates or not value.refresh)]
                differing = ' OR '.join('old.' + quote(n) + ' IS DISTINCT FROM new.' + quote(n) for n in immutable)
                target = 'lake.main.' + quote(table.name)
                if differing and connection.execute(f'SELECT 1 FROM {target} old JOIN incoming new ON {match} WHERE {differing} LIMIT 1').fetchone():
                    raise DataError('data_conflict', 'stored logical key has conflicting immutable values')
                updates = [n for n in table.updates if n in provided]
                if value.refresh and updates:
                    assignments = ','.join(quote(n) + '=new.' + quote(n) for n in updates)
                    connection.execute(f'UPDATE {target} old SET {assignments} FROM (SELECT DISTINCT * FROM incoming) new WHERE {match}')
                connection.execute(f'INSERT INTO {target} BY NAME SELECT DISTINCT new.* FROM incoming new WHERE NOT EXISTS (SELECT 1 FROM {target} old WHERE {match})')
                connection.execute('DROP TABLE incoming')
            except duckdb.Error as error:
                raise DataError('invalid_record', str(error)) from error
            finally:
                connection.unregister('_batch')
            return TableResult(batch, 'lake:' + value.name)

    def delete(self, query: TableQuery):
        if query.columns or query.order or query.limit is not None or query.offset:
            raise DataError('invalid_query', 'delete accepts table and filters only')
        with self.transaction():
            table = self.definition(query.table)
            where, values = self._where(table, query.filters)
            self._connection.execute('DELETE FROM lake.main.' + quote(table.name) + where, values)
