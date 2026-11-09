"""The relational data bus. No runtime access to legacy directories."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import quote

import pyarrow as pa
from protocol import (DataError, DataQuery, Dataset, Filter, TableDef, TableQuery,
                      TableSelect, TableWrite, validate_facts, _calendar_facts)


def table_name(value):
    return 'source_' + sha256(value.encode()).hexdigest()[:24]


# Fact payload columns that stay numeric in the lake instead of being stored as text.
FACT_NUMERIC = {'adjust': ('forward', 'backward'),
                'dividend': ('cash_per_share', 'stock_dividend_per_share',
                             'reserve_to_stock_per_share'),
                'security': ()}

# Source factor and dividend values carry up to 20 fractional digits. At scale
# 18 pyarrow refuses to rescale them ("Rescaling Decimal128 value would cause
# data loss"); 24 keeps every source digit and still leaves 14 integer digits.
FACT_DECIMAL = 'DECIMAL(38,24)'


def fact_ref(kind, code, start=None, end=None, year=None):
    return 'fact:' + ':'.join(quote(str(v), safe='.') if v is not None else ''
                             for v in (kind, code, start, end, year))


def digest(value):
    from storage import canonical
    if isinstance(value, pa.Table):
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, value.schema) as writer:
            writer.write_table(value.combine_chunks())
        return sha256(sink.getvalue()).hexdigest()
    return sha256(canonical(value).encode()).hexdigest()


class DataBus:
    def __init__(self, store):
        self._store = store
        self.define(TableDef('registry', {'section': 'VARCHAR', 'position': 'BIGINT',
                                         'settings': 'JSON'}, ('section', 'position'), ('settings',)))
        self.define(TableDef('datasets', {'id': 'VARCHAR', 'settings': 'JSON'}, ('id',), ('settings',)))
        self.define(TableDef('configurations', {'id': 'VARCHAR', 'settings': 'JSON'}, ('id',), ('settings',)))
        self.define(TableDef('calendar_sessions', {'dataset': 'VARCHAR', 'session': 'VARCHAR'}, ('dataset', 'session')))
        self.define(TableDef('calendar_coverage', {'dataset': 'VARCHAR', 'start': 'VARCHAR', 'end': 'VARCHAR'}, ('dataset', 'start', 'end')))
        self.define(TableDef('fact_coverage', {'reference': 'VARCHAR', 'kind': 'VARCHAR', 'code': 'VARCHAR',
                                            'start': 'VARCHAR', 'end': 'VARCHAR', 'year': 'VARCHAR',
                                            'source_hash': 'VARCHAR', 'record_count': 'BIGINT'}, ('reference',), ('source_hash', 'record_count')))
        self.define(TableDef('attachments', {'id': 'VARCHAR', 'content_type': 'VARCHAR', 'sha256': 'VARCHAR',
                                          'content': 'BLOB'}, ('id',)))
        self.define(TableDef('source_tables', {'identity': 'VARCHAR', 'kind': 'VARCHAR',
                                              'reference': 'VARCHAR'}, ('identity', 'kind'), ('reference',)))

    def define(self, table):
        return self._store.define(table)

    def scan(self, query):
        return self._store.scan(query)

    def write(self, value):
        return self._store.write(value)

    def delete(self, query):
        return self._store.delete(query)

    def transaction(self, *, readonly=False):
        return self._store.transaction(readonly=readonly)

    def close(self):
        return self._store.close()

    def definition(self, name):
        return self._store.definition(name)

    def bind(self, identity, kind, reference):
        if not isinstance(reference, str) or not reference.startswith('lake:'):
            raise DataError('invalid_ref', 'native source table required')
        self.definition(reference[5:])
        self.put_rows('source_tables', [{'identity': identity, 'kind': kind, 'reference': reference}],
                      keys=('identity', 'kind'), refresh=True)

    def source(self, identity, kind='records'):
        rows = self.scan(TableQuery('source_tables', (Filter('identity', 'eq', identity),
                                                      Filter('kind', 'eq', kind)))).table.to_pylist()
        if len(rows) != 1:
            raise DataError('missing', f'source is not registered: {identity}/{kind}')
        return rows[0]['reference']

    def put_rows(self, name, rows, *, keys, updates=None, types=None, refresh=False, storage=None):
        """Write only provided fields; SQL owns casts, merge and atomicity.

        ``rows`` is a record sequence, an Arrow table, or a ``TableSelect``: a
        query whose result the database hands straight to the merge.
        """
        keys = tuple(keys)
        updates = None if updates is None else tuple(updates)
        with self.transaction():
            try:
                existing = self.definition(name)
            except DataError as error:
                if error.code != 'missing_table':
                    raise
                existing = None
            layers = existing.storage if existing is not None else (storage or 'table')
            columns = dict(existing.columns) if existing is not None else {}
            declared = dict(types or {})
            known_types = {**columns, **declared}
            if isinstance(rows, TableSelect):
                # The rows already live in the database, so the shape comes from
                # the query and the rows never pass through this process.
                table = pa.Table.from_batches([], schema=self._store.select_schema(rows))
                groups = [rows]
            elif isinstance(rows, (pa.Table, pa.RecordBatch)):
                table = pa.Table.from_batches([rows]) if isinstance(rows, pa.RecordBatch) else rows
                groups = [table]
            else:
                records = tuple(dict(row) for row in rows)
                names = tuple(dict.fromkeys(column for row in records for column in row))
                def array(column, values):
                    if known_types.get(column) == 'JSON':
                        values = [value if isinstance(value, str) or value is None
                                  else json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
                                  for value in values]
                        return pa.array(values, type=pa.string())
                    return pa.array(values)
                table = pa.Table.from_arrays([array(column, [row.get(column) for row in records])
                                              for column in names], names=names)
                grouped = {}
                for row in records:
                    grouped.setdefault(tuple(column for column in names if column in row), []).append(row)
                groups = [pa.Table.from_arrays([array(column, [row[column] for row in group])
                                                for column in provided], names=provided)
                          for provided, group in grouped.items()]
            if existing is not None:
                if keys != existing.keys:
                    raise DataError('schema_conflict', 'logical keys differ from registered keys')
                for column, sql in declared.items():
                    if column in columns and columns[column] != sql:
                        raise DataError('schema_conflict', 'column type differs: ' + column)
                if updates is not None and (set(updates) & columns.keys()) != set(existing.updates):
                    raise DataError('schema_conflict', 'existing update policy differs')
            for field in table.schema:
                if field.name in columns:
                    continue
                kind = field.type
                if field.name in declared: sql = declared[field.name]
                elif pa.types.is_null(kind): raise DataError('invalid_schema', 'explicit type required for null column: ' + field.name)
                elif pa.types.is_string(kind) or pa.types.is_large_string(kind): sql = 'VARCHAR'
                elif pa.types.is_boolean(kind): sql = 'BOOLEAN'
                elif pa.types.is_integer(kind): sql = 'BIGINT'
                elif pa.types.is_float32(kind): sql = 'FLOAT'
                elif pa.types.is_floating(kind): sql = 'DOUBLE'
                elif pa.types.is_decimal(kind): sql = f'DECIMAL({kind.precision},{kind.scale})'
                elif pa.types.is_date(kind): sql = 'DATE'
                elif pa.types.is_timestamp(kind): sql = 'TIMESTAMPTZ' if kind.tz else 'TIMESTAMP'
                elif pa.types.is_binary(kind) or pa.types.is_large_binary(kind): sql = 'BLOB'
                else: raise DataError('invalid_schema', f'explicit relational schema required for {field.name}: {kind}')
                columns[field.name] = sql
            for column, sql in declared.items():
                columns.setdefault(column, sql)
            policy = existing.updates if updates is None and existing is not None else tuple(updates or ())
            self.define(TableDef(name, columns, keys, policy, storage=layers))
            result = None
            for group in groups:
                result = self.write(TableWrite(name, group, refresh))
            return result

    @staticmethod
    def _settings(row):
        value = row['settings']
        return json.loads(value) if isinstance(value, str) else value

    def registry(self):
        result = {'sources': [], 'datasets': [], 'benchmarks': [], 'markets': []}
        for row in self.scan(TableQuery('registry', order=('section', 'position'))).table.to_pylist():
            result.setdefault(row['section'], []).append(self._settings(row))
        return result

    def update_registry(self, change):
        with self.transaction():
            value = change(deepcopy(self.registry()))
            if not isinstance(value, Mapping) or any(not isinstance(v, list) for v in value.values()):
                raise DataError('invalid_registry', 'registry sections must be lists')
            rows = [{'section': section, 'position': i, 'settings': json.dumps(item, ensure_ascii=False, allow_nan=False)}
                    for section, items in value.items() for i, item in enumerate(items)]
            self.delete(TableQuery('registry'))
            if rows:
                self.put_rows('registry', rows, keys=('section', 'position'), refresh=True)
            return deepcopy(dict(value))

    def manifest(self, dataset):
        rows = self.scan(TableQuery('datasets', (Filter('id', 'eq', dataset),))).table.to_pylist()
        if len(rows) != 1:
            raise DataError('missing', 'dataset is not registered: ' + str(dataset))
        value = self._settings(rows[0])
        self._validate_manifest(dataset, value)
        return value

    def save_manifest(self, dataset, manifest):
        self._validate_manifest(dataset, manifest)
        return self.put_rows('datasets', [{'id': dataset, 'settings': json.dumps(manifest, ensure_ascii=False, allow_nan=False)}],
                             keys=('id',), refresh=True)

    def _validate_manifest(self, dataset, manifest):
        if not isinstance(dataset, str) or not dataset or '/' in dataset or dataset.startswith('.'):
            raise DataError('invalid_identity', 'invalid dataset identity')
        if not isinstance(manifest, Mapping) or manifest.get('schema') != 3:
            raise DataError('invalid_manifest', 'relational dataset schema 3 required; run explicit offline migration')
        scope = manifest.get('scope')
        symbols = scope.get('symbols') if isinstance(scope, Mapping) else None
        if (not isinstance(scope, Mapping) or not isinstance(scope.get('universe'), str)
                or not scope['universe'].strip() or not isinstance(symbols, (list, tuple))
                or not symbols or list(symbols) != sorted(set(symbols))):
            raise DataError('invalid_manifest', 'ordered unique symbols and universe are required')
        if not isinstance(manifest.get('fields'), Mapping):
            raise DataError('invalid_manifest', 'field bindings are required')
        bars = manifest.get('inputs', {}).get('bars')
        if not isinstance(bars, str) or not bars.startswith('lake:'):
            raise DataError('invalid_manifest', 'native bars table binding required')
        self.definition(bars[5:])

    def calendar(self, dataset):
        filters = (Filter('dataset', 'eq', dataset),)
        days = self.scan(TableQuery('calendar_sessions', filters, ('session',), ('session',))).table.column('session').to_pylist()
        spans = self.scan(TableQuery('calendar_coverage', filters, ('start', 'end'), ('start',))).table.to_pylist()
        value = {'sessions': days, 'coverage': spans}
        _calendar_facts(value)
        return value

    def save_calendar(self, dataset, value):
        _calendar_facts(value)
        with self.transaction():
            for table in ('calendar_sessions', 'calendar_coverage'):
                self.delete(TableQuery(table, (Filter('dataset', 'eq', dataset),)))
            self.put_rows('calendar_sessions', [{'dataset': dataset, 'session': d} for d in value['sessions']], keys=('dataset', 'session'))
            self.put_rows('calendar_coverage', [{'dataset': dataset, **v} for v in value['coverage']], keys=('dataset', 'start', 'end'))

    def save_config(self, name, value):
        self.put_rows('configurations', [{'id': name, 'settings': json.dumps(value, ensure_ascii=False, allow_nan=False)}], keys=('id',), refresh=True)
        return 'config:' + name

    def config(self, name):
        rows = self.scan(TableQuery('configurations', (Filter('id', 'eq', name),))).table.to_pylist()
        if len(rows) != 1:
            raise DataError('missing', 'configuration not found: ' + name)
        return self._settings(rows[0])

    def save_fact(self, payload):
        return self.save_facts((payload,))[0]

    def save_facts(self, payloads, *, batch=2000):
        """Write many fact payloads in one transaction, one insert per kind.

        A full-market import produces tens of thousands of small payloads. One
        transaction and one parquet file per payload is what made the legacy
        import take hours, so payloads are grouped by kind and written in
        batches inside a single commit.
        """
        items = []
        for payload in payloads:
            kind, code, start, end, year = (payload[k] for k in ('kind', 'code', 'start', 'end', 'year'))
            validate_facts(payload, kind=kind, code=code, start=start, end=end, year=year)
            items.append((fact_ref(kind, code, start, end, year), payload))
        if not items:
            return ()
        grouped = {}
        for reference, payload in items:
            grouped.setdefault(payload['kind'], []).append((reference, payload['records']))
        with self.transaction():
            self.put_rows('fact_coverage',
                          [{k: payload[k] for k in ('kind', 'code', 'start', 'end', 'year', 'source_hash')}
                           | {'reference': reference, 'record_count': len(payload['records'])}
                           for reference, payload in items],
                          keys=('reference',), refresh=True)
            for kind, entries in grouped.items():
                name = 'fact_' + kind
                columns = FACT_NUMERIC.get(kind, ())
                try:
                    self.definition(name)
                    exists = True
                except DataError as error:
                    if error.code != 'missing_table':
                        raise
                    exists = False
                for start in range(0, len(entries), batch):
                    chunk = entries[start:start + batch]
                    if exists:
                        self.delete(TableQuery(name, (Filter('reference', 'in',
                                                             [reference for reference, _ in chunk]),)))
                    rows, types = self._fact_rows(chunk, columns)
                    if rows:
                        self.put_rows(name, rows, keys=('reference', 'position'), types=types,
                                      storage='parquet')
        return tuple(reference for reference, _ in items)

    @staticmethod
    def _fact_rows(entries, numeric):
        """Build the fact rows and their declared column types for one batch."""
        rows = []
        for reference, records in entries:
            for position, record in enumerate(records):
                rows.append({'reference': reference, 'position': position, **record})
        types = {column: 'VARCHAR' for row in rows for column in row
                 if column not in {'position', *numeric}}
        types['position'] = 'BIGINT'
        if numeric:
            types['_numeric_types'] = 'JSON'
            types.update({column: FACT_DECIMAL for column in numeric})
            for row in rows:
                row['_numeric_types'] = {column: type(row[column]).__name__ for column in numeric
                                         if row[column] is not None}
                for column in numeric:
                    if row[column] is not None:
                        row[column] = Decimal(str(row[column]))
        return rows, types

    def fact(self, reference):
        payloads = self.facts((reference,), strict=True)
        return payloads[reference]

    def facts(self, references, *, strict=False):
        """Read fact payloads with one query per fact table.

        ``strict`` keeps the single-reference contract: a missing reference or
        an unreadable payload raises instead of being omitted.
        """
        wanted = tuple(dict.fromkeys(references))
        if not wanted:
            return {}
        identities = self.scan(TableQuery('fact_coverage', (Filter('reference', 'in', wanted),))).table.to_pylist()
        found = {identity['reference'] for identity in identities}
        if strict:
            absent = [reference for reference in wanted if reference not in found]
            if absent:
                raise DataError('missing', 'fact coverage not found: ' + absent[0])
        grouped = {}
        for identity in identities:
            grouped.setdefault(identity['kind'], []).append(identity)
        payloads = {}
        for kind, rows in grouped.items():
            keys = tuple(row['reference'] for row in rows)
            records = []
            if any(row['record_count'] for row in rows):
                records = self.scan(TableQuery('fact_' + kind, (Filter('reference', 'in', keys),),
                                               order=('reference', 'position'))).table.to_pylist()
            by_reference = {}
            for record in records:
                by_reference.setdefault(record['reference'], []).append(record)
            for identity in rows:
                items = by_reference.get(identity['reference'], [])
                if len(items) != identity['record_count']:
                    raise DataError('data_corrupt', 'fact row count differs from committed coverage')
                try:
                    for row in items:
                        numeric_types = json.loads(row.pop('_numeric_types')) if '_numeric_types' in row else {}
                        for column, name in numeric_types.items():
                            row[column] = {'float': float, 'int': int, 'Decimal': Decimal}[name](row[column])
                except (TypeError, ValueError, ArithmeticError, KeyError):
                    if strict:
                        raise DataError('data_corrupt', 'fact payload is not readable: ' + identity['reference'])
                    continue
                payload = {'schema': 1, **{k: identity[k] for k in ('kind', 'code', 'start', 'end', 'year', 'source_hash')},
                           'records': [{k: v for k, v in row.items() if k not in ('reference', 'position')}
                                       for row in items]}
                if strict:
                    validate_facts(payload, **{k: identity[k] for k in ('kind', 'code', 'start', 'end', 'year')})
                payloads[identity['reference']] = payload
        return payloads

    def object(self, reference):
        if reference.startswith('fact:'):
            return self.fact(reference)
        if reference.startswith('config:'):
            return self.config(reference[7:])
        if reference.startswith('calendar:'):
            return self.calendar(reference[9:])
        if reference.startswith('attachment:'):
            rows = self.scan(TableQuery('attachments', (Filter('id', 'eq', reference[11:]),))).table.to_pylist()
            if len(rows) != 1:
                raise DataError('missing', 'attachment not found: ' + reference)
            return rows[0]['content']
        if reference.startswith('changes:'):
            name = reference[8:]
            return {'schema': 1, 'events': self.scan(TableQuery(name)).table.to_pylist()}
        raise DataError('invalid_ref', 'unsupported logical object reference: ' + str(reference))

    def table(self, reference, *, filters=(), columns=(), order=(), limit=None, offset=0):
        if not isinstance(reference, str) or not reference.startswith('lake:'):
            raise DataError('invalid_ref', 'native table reference required')
        return self.scan(TableQuery(reference[5:], tuple(filters), tuple(columns), tuple(order), limit, offset)).table

    def evidence(self, reference):
        if reference.startswith('attachment:'):
            return sha256(self.object(reference)).hexdigest()
        if reference.startswith('lake:'):
            return self._store.fingerprint(reference[5:])
        return digest(self.object(reference))

    def manifest_digest(self, reference):
        """Generation of a bulk table by its file manifest, not by every row."""
        if not isinstance(reference, str) or not reference.startswith('lake:'):
            raise DataError('invalid_ref', 'native table reference required')
        return self._store.manifest_digest(reference[5:])

    def query(self, query):
        if not isinstance(query, DataQuery):
            raise DataError('invalid_query', 'query must be DataQuery')
        with self.transaction(readonly=True):
            return self._query_locked(query)
    def _query_locked(self, query: DataQuery) -> Dataset:
        from .reader import _prepare_dataset
        if not isinstance(query, DataQuery):
            raise DataError("invalid_query", "query must be DataQuery")
        try:
            source_query = query
            if query.market is not None:
                from dataclasses import replace
                from plugins.data import Adapter
                candidates = Adapter.candidates(self, query.market, query.dataset,
                                                query.start, query.end)
                if not candidates:
                    raise DataError("market_data_missing", "market selector has no source candidates")
                source_query = replace(query, symbols=candidates)
            result = _prepare_dataset(self, source_query)
            if query.market is not None:
                from plugins.data import Adapter
                result = Adapter.market(self, query, result)
        except DataError:
            raise
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise DataError("invalid_query", str(error)) from error
        if not isinstance(result, Dataset):
            raise DataError("invalid_dataset", "data query did not return Dataset")
        if result.dataset != query.dataset:
            raise DataError("dataset_mismatch", "query returned a different dataset")
        if result.scope.get("universe") is None:
            raise DataError("invalid_dataset", "dataset scope must declare universe")
        if query.universe != "dataset" and result.scope.get("universe") != query.universe:
            raise DataError("scope_mismatch", "dataset universe does not satisfy query")
        if query.symbols is not None and tuple(result.scope.get("symbols", ())) != query.symbols:
            raise DataError("scope_mismatch", "dataset symbols do not satisfy query")
        coverage_start = result.scope.get("calendar_start")
        if not isinstance(coverage_start, str) or date.fromisoformat(coverage_start) > query.start:
            raise DataError("date_coverage", "dataset calendar does not cover query start")
        if tuple(result.fields) != tuple(query.fields):
            raise DataError("field_mismatch", "dataset fields do not satisfy query")
        if result.history != query.history:
            raise DataError("history_mismatch", "dataset history does not satisfy query")
        if not result.records[-1].trade_date <= query.end < result.calendar[-1]:
            raise DataError("date_coverage", "dataset must cover the query end before its following-session sentinel")
        return result



def build_data(root: Path, *, extension_directory=None) -> DataBus:
    from storage import DataStore
    return DataBus(DataStore(root, extension_directory=extension_directory))
