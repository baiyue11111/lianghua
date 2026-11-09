"""Relational persistence: Parquet holds bulk data, DuckDB is the DBMS.

Layout
------
``data/catalog.duckdb``
    The database. It holds the logical table definitions, the file manifest and
    every table that is small and mutable (state: registry, coverage, manifests,
    run headers). Those are real DuckDB tables, so ``PRIMARY KEY`` clauses are
    enforced by the engine instead of by this module.
``data/parquet/<table>/part-<id>.parquet``
    The body of a bulk table (bars, facts, run detail). Files are immutable:
    a commit appends one file, and only a row removal replaces the set.
``lake.main.<table>``
    For a bulk table, a view rendered from the manifest's file list. Readers use
    plain SQL; the view is re-rendered inside the same transaction that changes
    the file list, so the manifest and the view cannot disagree.

Writes to a bulk table are staged in a temporary table. ``_physical`` routes
every read to the staged rows, so a transaction sees its own writes without any
of them reaching disk; ``_flush`` copies the staged rows to one Parquet file and
registers it just before COMMIT. A rolled back or crashed write leaves the
Parquet files untouched, and ``_reconcile`` deletes files no manifest claims.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import threading
from uuid import uuid4
import weakref

import duckdb
import pyarrow as pa
from protocol import (DataError, Filter, TableDef, TableQuery, TableResult, TableSelect,
                      TableWrite, SQLQuery)

CHUNK = 1 << 20


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


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def record(table: TableDef):
    """The canonical definition stored in ``_tables``."""
    return canonical({'name': table.name, 'columns': list(table.columns.items()),
                      'keys': table.keys, 'updates': table.updates,
                      'audit': table.audit, 'storage': table.storage})


def file_digest(path):
    digest = sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


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
            self.parquet = self.root / 'parquet'
            self.parquet.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()
            self._local = threading.local()
            self._pid = os.getpid()
            config = {'autoinstall_known_extensions': 'false',
                      'autoload_known_extensions': 'false'}
            directory = extension_directory or os.environ.get('LIANGHUA_EXTENSION_DIRECTORY')
            if directory:
                config['extension_directory'] = str(directory)
            connection = duckdb.connect(':memory:', config=config)
            catalog = literal(self.root / 'catalog.duckdb')
            try:
                connection.execute(f'ATTACH {catalog} AS lake')
                connection.execute('USE lake.main')
                connection.execute('CREATE TABLE IF NOT EXISTS lake.main._tables '
                                   '(name VARCHAR PRIMARY KEY, definition JSON)')
                connection.execute('CREATE TABLE IF NOT EXISTS lake.main._files '
                                   '(table_name VARCHAR, path VARCHAR, row_count BIGINT,'
                                   ' size_bytes BIGINT, sha256 VARCHAR, created_at VARCHAR,'
                                   ' PRIMARY KEY (table_name, path))')
            except duckdb.Error as error:
                connection.close()
                raise DataError('storage_busy' if 'lock' in str(error).lower() else 'storage_failed', str(error)) from error
            self._connection = connection
            self._ready = True
            try:
                self._reconcile()
            except BaseException:
                self._ready = False
                connection.close()
                self._owners.pop((self._pid, str(self.project)), None)
                raise

    def close(self):
        with self._lock:
            self._check()
            self._connection.close()
            self._ready = False
            self._owners.pop((self._pid, str(self.project)), None)

    def _check(self):
        if self._pid != os.getpid():
            raise DataError('storage_busy', 'catalog connection belongs to another process; open a new writer')
        if not self._ready:
            raise DataError('storage_closed', 'data store is closed')

    # -- definitions -------------------------------------------------------

    @staticmethod
    def _decode(text):
        value = json.loads(text)
        value.setdefault('storage', 'table')
        return value

    def _schema(self, name):
        rows = self._connection.execute(
            'SELECT definition FROM lake.main._tables WHERE name=?', [name]).fetchall()
        if len(rows) != 1:
            raise DataError('missing_table', 'table is not registered: ' + str(name))
        return self._decode(rows[0][0])

    def definition(self, name):
        with self._lock:
            self._check()
            value = self._schema(name)
            value['columns'] = dict(value['columns'])
            return TableDef(**value)

    def define(self, table: TableDef):
        if not isinstance(table, TableDef):
            raise TypeError('table definition required')
        payload = record(table)
        with self.transaction():
            rows = self._connection.execute(
                'SELECT definition FROM lake.main._tables WHERE name=?', [table.name]).fetchall()
            if rows:
                old, new = self._decode(rows[0][0]), json.loads(payload)
                if old == new:
                    return
                count = len(old['columns'])
                old_names = {n for n, _ in old['columns']}
                if (new['storage'] != old['storage']
                        or len(new['columns']) <= count or new['columns'][:count] != old['columns']
                        or new['keys'] != old['keys'] or new['audit'] != old['audit']
                        or [n for n in new['updates'] if n in old_names] != old['updates']):
                    raise DataError('schema_conflict', 'table definition differs: ' + table.name)
                for name, kind in new['columns'][count:]:
                    self._add_column(table.name, old['storage'], name, kind)
                self._connection.execute('UPDATE lake.main._tables SET definition=? WHERE name=?',
                                         [payload, table.name])
                if old['storage'] == 'parquet':
                    self._render(table.name, dict(new['columns']))
                return
            columns = ', '.join(quote(n) + ' ' + t for n, t in table.columns.items())
            if table.storage == 'parquet':
                self._connection.execute(
                    f'CREATE VIEW lake.main.{quote(table.name)} AS '
                    + self._view(table.name, list(table.columns.items())))
            else:
                keys = ', '.join(map(quote, table.keys))
                self._connection.execute(
                    f'CREATE TABLE lake.main.{quote(table.name)} ({columns}, PRIMARY KEY ({keys}))')
            self._connection.execute('INSERT INTO lake.main._tables VALUES (?, ?)',
                                     [table.name, payload])

    def _add_column(self, name, storage, column, kind):
        if storage == 'parquet':
            state = self._buffers().get(name)
            if state is not None:
                self._connection.execute(
                    f'ALTER TABLE temp.main.{quote(state["table"])} ADD COLUMN {quote(column)} {kind}')
            return
        self._connection.execute(
            f'ALTER TABLE lake.main.{quote(name)} ADD COLUMN {quote(column)} {kind}')

    # -- parquet views -----------------------------------------------------

    def _view(self, name, columns):
        pairs = list(columns.items()) if hasattr(columns, 'items') else list(columns)
        paths = [row[0] for row in self._connection.execute(
            'SELECT path FROM lake.main._files WHERE table_name=? ORDER BY path', [name]).fetchall()]
        if not paths:
            empty = ', '.join(f'CAST(NULL AS {t}) AS {quote(c)}' for c, t in pairs)
            return f'SELECT {empty} WHERE FALSE'
        projection = ', '.join(f'CAST({quote(c)} AS {t}) AS {quote(c)}' for c, t in pairs)
        return (f'SELECT {projection} FROM read_parquet([{", ".join(map(literal, paths))}],'
                ' union_by_name=true)')

    def _render(self, name, columns):
        self._connection.execute(f'CREATE OR REPLACE VIEW lake.main.{quote(name)} AS '
                                 + self._view(name, columns))

    def _reconcile(self):
        """Trust the manifest, not the directory: verify and repair at open."""
        connection = self._connection
        for name, text in connection.execute(
                'SELECT name, definition FROM lake.main._tables').fetchall():
            definition = self._decode(text)
            if definition['storage'] != 'parquet':
                continue
            rows = connection.execute('SELECT path, row_count FROM lake.main._files '
                                      'WHERE table_name=? ORDER BY path', [name]).fetchall()
            for path, expected in rows:
                if not Path(path).exists():
                    raise DataError('data_corrupt', f'{name}: manifest file is missing: {path}')
                actual = connection.execute('SELECT count(*) FROM read_parquet(?)',
                                            [path]).fetchone()[0]
                if actual != expected:
                    raise DataError('data_corrupt',
                                    f'{name}: {path} holds {actual} rows, manifest says {expected}')
            claimed = {str(Path(path)) for path, _ in rows}
            folder = self.parquet / name
            if folder.exists():
                for stray in folder.glob('*.parquet'):
                    if str(stray) not in claimed:
                        stray.unlink()
            self._render(name, definition['columns'])
            keys = ', '.join(map(quote, definition['keys']))
            duplicate = connection.execute(
                f'SELECT 1 FROM {self._physical(name)} GROUP BY {keys} HAVING count(*)>1 LIMIT 1').fetchone()
            if duplicate:
                raise DataError('data_corrupt', f'{name}: duplicate logical keys on disk')

    # -- staged writes -----------------------------------------------------

    def _buffers(self):
        state = getattr(self._local, 'buffers', None)
        if state is None:
            state = self._local.buffers = {}
        return state

    def _stage(self, name):
        state = self._buffers().get(name)
        if state is not None:
            return state
        key = sha256(name.encode()).hexdigest()[:16]
        table = '_staged_' + key
        self._connection.execute(f'CREATE OR REPLACE TEMP TABLE temp.main.{quote(table)} AS '
                                 f'SELECT * FROM lake.main.{quote(name)} WHERE FALSE')
        state = self._buffers()[name] = {'key': key, 'table': table, 'view': None}
        return state

    def _replace(self, name):
        """Turn the staged rows into the whole table (a removal needs it)."""
        state = self._stage(name)
        if name in self._local.full:
            return state
        physical = self._physical(name)
        self._connection.execute(f'CREATE OR REPLACE TEMP TABLE temp.main.{quote(state["table"])} '
                                 f'AS SELECT * FROM {physical}')
        if state['view'] is not None:
            self._connection.execute(f'DROP VIEW IF EXISTS temp.main.{quote(state["view"])}')
            state['view'] = None
        self._local.full.add(name)
        return state

    def _physical(self, name):
        state = self._buffers().get(name)
        if state is None:
            return 'lake.main.' + quote(name)
        if name in getattr(self._local, 'full', ()):
            return 'temp.main.' + quote(state['table'])
        if state['view'] is None:
            view = '_pending_' + state['key']
            self._connection.execute(
                f'CREATE OR REPLACE TEMP VIEW temp.main.{quote(view)} AS '
                f'SELECT * FROM lake.main.{quote(name)} UNION ALL BY NAME '
                f'SELECT * FROM temp.main.{quote(state["table"])}')
            state['view'] = view
        return 'temp.main.' + quote(state['view'])

    def _sink(self, name):
        return 'temp.main.' + quote(self._stage(name)['table'])

    def _reset(self, files=()):
        connection = self._connection
        for state in self._buffers().values():
            if state['view'] is not None:
                connection.execute(f'DROP VIEW IF EXISTS temp.main.{quote(state["view"])}')
            connection.execute(f'DROP TABLE IF EXISTS temp.main.{quote(state["table"])}')
        self._local.buffers = {}
        self._local.full = set()
        for path in files:
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass

    def _flush(self):
        """Copy staged rows into one immutable Parquet file per table."""
        connection = self._connection
        for name, state in list(self._buffers().items()):
            definition = self._schema(name)
            if definition['storage'] != 'parquet':
                continue
            source = f'temp.main.{quote(state["table"])}'
            count = connection.execute(f'SELECT count(*) FROM {source}').fetchone()[0]
            if not count and name not in getattr(self._local, 'full', ()):
                continue
            folder = self.parquet / name
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / ('part-' + uuid4().hex + '.parquet')
            connection.execute(f'COPY (SELECT * FROM {source}) TO {literal(path)}'
                               ' (FORMAT PARQUET, COMPRESSION ZSTD)')
            self._local.written.append(path)
            if name in getattr(self._local, 'full', ()):
                stale = [row[0] for row in connection.execute(
                    'SELECT path FROM lake.main._files WHERE table_name=?', [name]).fetchall()]
                connection.execute('DELETE FROM lake.main._files WHERE table_name=?', [name])
                self._local.removed.extend(Path(item) for item in stale)
            connection.execute('INSERT INTO lake.main._files VALUES (?, ?, ?, ?, ?, ?)',
                               [name, str(path), count, path.stat().st_size, file_digest(path),
                                datetime.now(timezone.utc).isoformat()])
            self._render(name, definition['columns'])

    # -- transactions ------------------------------------------------------

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
                self._local.buffers = {}
                self._local.full = set()
                self._local.written = []
                self._local.removed = []
            self._local.depth = depth + 1
            try:
                yield self
                if depth == 0 and self._local.failed:
                    raise DataError('transaction_failed', 'nested transaction failed; outer transaction cannot commit')
            except BaseException:
                self._local.failed = True
                if depth == 0:
                    try:
                        self._connection.execute('ROLLBACK')
                    finally:
                        self._reset(self._local.written)
                raise
            else:
                if depth == 0:
                    try:
                        self._flush()
                        self._connection.execute('COMMIT')
                    except BaseException:
                        try:
                            self._connection.execute('ROLLBACK')
                        finally:
                            self._reset(self._local.written)
                        raise
                    self._reset(self._local.removed)
            finally:
                self._local.depth = depth

    # -- rows --------------------------------------------------------------

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
            sql = f'SELECT {", ".join(map(quote, columns))} FROM {self._physical(query.table)}' + where
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
                   f'FROM (SELECT {columns} FROM {self._physical(name)}) AS row')
            return self._connection.execute(sql, [schema]).fetchone()[0]

    def manifest_digest(self, name):
        """A generation id for a bulk table: its file set, not its rows.

        ``fingerprint`` serialises every row and aggregates the result into one
        string, which on a table of millions of rows means a full scan and a
        multi-gigabyte allocation. The manifest is the authority for what a bulk
        table is made of and already carries one digest per immutable file, so
        hashing it is O(parts) and changes exactly when the table changes.
        """
        definition = self.definition(name)
        if definition.storage != 'parquet':
            return self.fingerprint(name)
        with self._lock:
            self._check()
            rows = self._connection.execute(
                'SELECT path, row_count, size_bytes, sha256 FROM lake.main._files '
                'WHERE table_name=? ORDER BY path', [name]).fetchall()
        return sha256(json.dumps(rows, ensure_ascii=False, default=str).encode()).hexdigest()

    def select_schema(self, select: TableSelect):
        """Columns and Arrow types a stored ``SELECT`` returns, without running it."""
        with self._lock:
            self._check()
            return self._probe(select)

    def _probe(self, select):
        try:
            return self._connection.execute(
                f'SELECT * FROM ({select.sql}) AS _probe LIMIT 0',
                list(select.values)).to_arrow_table().schema
        except duckdb.Error as error:
            raise DataError('invalid_query', str(error)) from error

    @staticmethod
    def _lossy(source, declared):
        """Only the silent-rounding direction is refused.

        A float written into an exact column rounds without complaint, and rows
        arriving as a query never pass through Python, so the value-by-value
        check the Arrow path runs cannot run here.
        """
        if declared in ('DOUBLE', 'FLOAT'):
            return False
        if pa.types.is_floating(source):
            return True
        return pa.types.is_decimal(source) and not declared.startswith('DECIMAL')

    def write(self, value: TableWrite):
        if not isinstance(value, TableWrite):
            raise TypeError('TableWrite required')
        with self.transaction():
            table = self.definition(value.name)
            select = value.source if isinstance(value.source, TableSelect) else None
            if select is None and not isinstance(value.source, (pa.Table, pa.RecordBatch)):
                raise TypeError('TableWrite requires an Arrow table, record batch or TableSelect')
            stored = table.storage == 'parquet'
            if stored and value.refresh and table.updates:
                # An update has to rewrite rows that already live in Parquet.
                self._replace(table.name)
            source_relation = self._physical(table.name)
            target = self._sink(table.name) if stored else source_relation
            if select is not None:
                batch, schema = None, self._probe(select)
                provided = set(schema.names)
            else:
                batch = pa.Table.from_batches([value.source]) if isinstance(value.source, pa.RecordBatch) else value.source
                schema = None
                provided = set(batch.column_names)
                if len(provided) != len(batch.column_names):
                    raise DataError('invalid_record', 'records require all logical keys and only registered columns')
            if not provided <= table.columns.keys() or not set(table.keys) <= provided:
                raise DataError('invalid_record', 'records require all logical keys and only registered columns')
            columns = tuple(n for n in table.columns if n in provided)
            projection = ', '.join(map(quote, columns))
            # A query brings its own rows, so a result can only report its shape.
            emptied = batch if batch is not None else pa.Table.from_batches([], schema=schema)
            connection = self._connection
            if select is None:
                target_schema = connection.execute(f'SELECT {projection} FROM {source_relation} LIMIT 0').to_arrow_table().schema
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
                    return TableResult(batch, 'lake:' + value.name, 0)
            else:
                for name in columns:
                    if self._lossy(schema.field(name).type, table.columns[name]):
                        raise DataError('invalid_record', 'lossy numeric column cast: ' + name)
            if select is None:
                connection.register('_batch', batch)
            try:
                connection.execute(f'CREATE TEMP TABLE incoming AS SELECT {projection} FROM {source_relation} LIMIT 0')
                if select is None:
                    connection.execute('INSERT INTO incoming BY NAME SELECT * FROM _batch')
                else:
                    connection.execute(f'INSERT INTO incoming BY NAME {select.sql}', list(select.values))
                    null = ' OR '.join(quote(k) + ' IS NULL' for k in table.keys)
                    if connection.execute(f'SELECT 1 FROM incoming WHERE {null} LIMIT 1').fetchone():
                        raise DataError('invalid_record', 'logical keys cannot be null')
                keys = ','.join(map(quote, table.keys))
                conflict = connection.execute(f'SELECT 1 FROM (SELECT DISTINCT * FROM incoming) GROUP BY {keys} HAVING count(*)>1 LIMIT 1').fetchone()
                if conflict:
                    raise DataError('data_conflict', 'different values for one logical key in batch')
                match = ' AND '.join('old.' + quote(k) + ' IS NOT DISTINCT FROM new.' + quote(k) for k in table.keys)
                if not stored and connection.execute(f'SELECT 1 FROM {source_relation} old WHERE EXISTS (SELECT 1 FROM incoming new WHERE {match}) GROUP BY ' + ','.join('old.' + quote(k) for k in table.keys) + ' HAVING count(*)>1 LIMIT 1').fetchone():
                    # A native table has a primary key, so this only ever fires on
                    # a store written by an older version. Bulk tables are checked
                    # once at open by ``_reconcile`` instead of on every batch.
                    raise DataError('data_corrupt', 'target contains duplicate logical keys')
                immutable = [n for n in columns if n not in table.keys and (n not in table.updates or not value.refresh)]
                differing = ' OR '.join('old.' + quote(n) + ' IS DISTINCT FROM new.' + quote(n) for n in immutable)
                if differing and connection.execute(f'SELECT 1 FROM {source_relation} old JOIN incoming new ON {match} WHERE {differing} LIMIT 1').fetchone():
                    raise DataError('data_conflict', 'stored logical key has conflicting immutable values')
                rows = connection.execute('SELECT count(*) FROM incoming').fetchone()[0]
                if not rows:
                    connection.execute('DROP TABLE incoming')
                    return TableResult(emptied, 'lake:' + value.name, 0)
                updates = [n for n in table.updates if n in provided]
                if value.refresh and updates:
                    assignments = ','.join(quote(n) + '=new.' + quote(n) for n in updates)
                    connection.execute(f'UPDATE {target} old SET {assignments} FROM (SELECT DISTINCT * FROM incoming) new WHERE {match}')
                connection.execute(f'INSERT INTO {target} BY NAME SELECT DISTINCT new.* FROM incoming new WHERE NOT EXISTS (SELECT 1 FROM {source_relation} old WHERE {match})')
                connection.execute('DROP TABLE incoming')
            except duckdb.Error as error:
                raise DataError('invalid_record', str(error)) from error
            finally:
                if select is None:
                    connection.unregister('_batch')
            return TableResult(emptied, 'lake:' + value.name, rows)

    def delete(self, query: TableQuery):
        if query.columns or query.order or query.limit is not None or query.offset:
            raise DataError('invalid_query', 'delete accepts table and filters only')
        with self.transaction():
            table = self.definition(query.table)
            where, values = self._where(table, query.filters)
            if table.storage == 'parquet':
                target = 'temp.main.' + quote(self._replace(query.table)['table'])
            else:
                target = 'lake.main.' + quote(table.name)
            self._connection.execute('DELETE FROM ' + target + where, values)
